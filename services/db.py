"""Cloud SQL (Postgres) — banco dos dados operacionais (vendas, devoluções,
endereçamento, credenciais das lojas).

As conexões vêm de um POOL. Antes cada função abria e fechava a sua, o que era
adequado ao volume — até deixar de ser: em 10/08/2026 tivemos 19 erros de conexão
em 24h (`ServerDisconnectedError`, `ConnectionDoesNotExistError`, `BrokenPipeError`)
e DUAS vendas pagas se perderam, porque o webhook do Mercado Livre falhou nas três
tentativas e ele desistiu. Abrir conexão por chamada não é só lento: sob
instabilidade, custa pedido.

O pool é criado sob demanda e, se por algum motivo não subir, o código cai para uma
conexão avulsa — o pior caso passa a ser o comportamento antigo, nunca uma tela fora
do ar.
"""
import asyncio
import json
import logging
import asyncpg
from google.cloud.sql.connector import Connector
from config.settings import get_settings

logger = logging.getLogger(__name__)

_connector: Connector | None = None
_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None


def _get_connector() -> Connector:
    """O Connector prende internamente ao event loop em que foi criado — se não
    passarmos o loop em execução explicitamente, ele cria um loop próprio (em
    outra thread) e `connect_async` falha com ConnectorLoopError assim que
    chamado de dentro do loop real do FastAPI/uvicorn."""
    global _connector
    if _connector is None:
        _connector = Connector(loop=asyncio.get_running_loop())
    return _connector


async def _abrir_conexao(*_args, **_kwargs) -> asyncpg.Connection:
    """Abre uma conexão nova via Cloud SQL Connector.

    Aceita e descarta argumentos porque o asyncpg chama esta função como sua
    `connect=` do pool, passando coisas dele (`loop`, `timeout`, `connection_class`).
    O Connector monta a conexão por conta própria a partir das settings, então nada
    disso se aplica aqui — mas recusar os kwargs derruba a criação do pool."""
    settings = get_settings()
    connector = _get_connector()
    return await connector.connect_async(
        settings.cloudsql_instance_connection_name,
        "asyncpg",
        user=settings.cloudsql_user,
        password=settings.cloudsql_password,
        db=settings.cloudsql_db,
    )


class _ConexaoDoPool:
    """Faz uma conexão emprestada do pool se comportar como as antigas.

    O projeto inteiro usa `conn = await _get_connection()` … `finally: await
    conn.close()` — são 56 lugares, quase todos mexendo com dado de produção.
    Reescrever todos para `async with pool.acquire()` seria uma varredura de risco
    desnecessário: este proxy delega tudo para a conexão real e só reinterpreta
    `close()`, que passa a DEVOLVER ao pool em vez de encerrar."""
    __slots__ = ("_conn", "_pool_ref", "_devolvida")

    def __init__(self, pool: asyncpg.Pool, conn: asyncpg.Connection):
        self._pool_ref = pool
        self._conn = conn
        self._devolvida = False

    def __getattr__(self, nome: str):
        # Só chega aqui quando o atributo não existe nos __slots__. Atributo
        # interno faltando significa proxy meio-construído — deixar cair no
        # getattr abaixo entraria em recursão.
        if nome.startswith("_"):
            raise AttributeError(nome)
        return getattr(self._conn, nome)

    async def close(self) -> None:
        if self._devolvida:
            return   # `finally` duplicado não pode devolver a mesma conexão 2x
        self._devolvida = True
        await self._pool_ref.release(self._conn)


async def _get_pool() -> asyncpg.Pool:
    global _pool, _pool_lock
    if _pool is not None:
        return _pool
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:   # outro request pode ter criado enquanto esperávamos
            _pool = await asyncpg.create_pool(
                connect=_abrir_conexao,
                min_size=1,
                max_size=8,
                # O Cloud SQL corta conexões ociosas por conta própria; reciclar
                # antes disso evita pegar do pool uma conexão já morta do outro lado.
                max_inactive_connection_lifetime=120.0,
                command_timeout=30.0,
            )
            logger.info("Pool de conexões criado (min=1, max=8).")
    return _pool


async def _get_connection():
    """Conexão pronta para uso. Devolve o proxy do pool; se o pool não estiver
    disponível, abre uma avulsa — degradar para o comportamento antigo é melhor
    que derrubar a requisição."""
    try:
        pool = await _get_pool()
        return _ConexaoDoPool(pool, await pool.acquire())
    except Exception as e:
        logger.warning("Pool indisponível (%s) — abrindo conexão avulsa.", e)
        return await _abrir_conexao()


async def close() -> None:
    """Fecha pool e connector (chamado no shutdown do app)."""
    global _connector, _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _connector is not None:
        await _connector.close_async()
        _connector = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS cancelamentos (
    id SERIAL PRIMARY KEY,
    venda_ml TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    empresa TEXT NOT NULL,
    status_ml TEXT NOT NULL,
    motivo TEXT,
    data_evento TIMESTAMPTZ NOT NULL,
    finalizado_por_gerente BOOLEAN NOT NULL DEFAULT FALSE,
    finalizado_em TIMESTAMPTZ,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS devolucoes (
    id SERIAL PRIMARY KEY,
    claim_id TEXT NOT NULL UNIQUE,
    venda_ml TEXT,
    order_id TEXT,
    empresa TEXT,
    tipo TEXT,
    stage TEXT,
    motivo TEXT,
    status TEXT NOT NULL DEFAULT 'Recebida',
    avaliacao TEXT,
    raw JSONB,
    data_evento TIMESTAMPTZ,
    prazo_devolucao TIMESTAMPTZ,
    avaliado_em TIMESTAMPTZ,
    finalizado_em TIMESTAMPTZ,
    -- `fase` é a etapa FÍSICA (a caminho / chegou / encerrada), gravada em vez de
    -- consultada ao vivo na renderização: o Mural tem poll, e perguntar ao ML uma
    -- vez por devolução a cada ciclo era uma chamada HTTP por card por atualização.
    fase TEXT,
    fase_em TIMESTAMPTZ,
    -- Quando o pacote chegou DE VERDADE (vem do histórico do envio), que não é o
    -- mesmo que `criado_em` — este é só o instante em que nós registramos.
    chegou_em TIMESTAMPTZ,
    -- Só no retorno sem entrega: é pelo envio original que se acompanha a volta,
    -- já que não existe claim no ML pra consultar (ver sync_service).
    shipping_id TEXT,
    -- Pra onde o pacote vai: 'seller_address' (nosso galpão) ou 'warehouse'
    -- (depósito do ML). Sem isto, devolução que nunca chega aqui aparecia no Mural
    -- pedindo confirmação de recebimento. NULL = retorno sem entrega (vem sempre
    -- pra cá) ou devolução registrada antes desta coluna existir.
    destino TEXT,
    -- `id` da devolução no ML. É ele, não o claim_id, que as ações de devolução
    -- exigem (revisão, anexo). Guardado pra não precisar consultar de novo.
    return_id TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fase 1 da migração Sheets → Postgres (Endereçamento). SKU é a chave única e
-- global de propósito: o endereço é do item físico (uma prateleira só), nunca
-- da loja que o vende — ver services/enderecos_db.py.
CREATE TABLE IF NOT EXISTS enderecos (
    sku TEXT PRIMARY KEY,
    corredor TEXT NOT NULL DEFAULT '',
    estante TEXT NOT NULL DEFAULT '',
    prateleira TEXT NOT NULL DEFAULT '',
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Estado (não histórico — 1 linha por loja, upsert) do botão "Vincular SKUs":
-- deixa o "buscando no ML" rodar desgrudado da requisição que clicou o botão
-- (BackgroundTasks), sobrevivendo mesmo se o usuário sair da tela.
CREATE TABLE IF NOT EXISTS enderecos_vinculo_status (
    loja TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'ocioso',
    novos INTEGER,
    removidos INTEGER,
    total INTEGER,
    erro TEXT,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migração das Credenciais (Firestore -> Postgres, 2026-07-28). user_id é o ID
-- do vendedor no Mercado Livre (mesma chave que era o nome do documento no
-- Firestore) — ver services/token_store.py.
CREATE TABLE IF NOT EXISTS lojas (
    user_id TEXT PRIMARY KEY,
    company_key TEXT NOT NULL,
    sheet_tab TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    cor TEXT,
    sku_prefixo TEXT,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quais SKUs cada loja ANUNCIA hoje. O prefixo do nome diz de quem é o produto
-- (a linha/cadastro); isto diz quem vende — e uma conta pode vender SKU de outra
-- linha. A chave é o PAR, então `ativo` desliga o SKU numa loja só, sem tocar na
-- outra e sem tocar no endereço (que é 1 por SKU, ver `enderecos` acima).
-- Preenchida pelo botão "Vincular SKUs", que já busca o catálogo real no ML.
-- Depende de `enderecos` e `lojas`, por isso vem depois das duas.
CREATE TABLE IF NOT EXISTS sku_lojas (
    sku           TEXT NOT NULL REFERENCES enderecos(sku) ON DELETE CASCADE,
    loja_user_id  TEXT NOT NULL REFERENCES lojas(user_id) ON DELETE CASCADE,
    ativo         BOOLEAN NOT NULL DEFAULT true,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sku, loja_user_id)
);
CREATE INDEX IF NOT EXISTS idx_sku_lojas_loja ON sku_lojas (loja_user_id);

-- Reserva atômica de order_id p/ dedup de webhooks concorrentes/reenviados —
-- equivalente ao antigo ref.create() do Firestore (só o 1º INSERT vence).
CREATE TABLE IF NOT EXISTS pedidos_processados (
    order_id TEXT PRIMARY KEY,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migração de Vendas (Sheets -> Postgres, 2026-07-29). `venda` = venda_ml
-- (pack_id) ou order_id (fallback) — mesma chave central usada em todo o app
-- (Mural/Gerente/Embalagem/Gaiolas). status/gaiola/id_exped/impresso_em
-- normalizados aqui (1x por venda, não 1x por SKU como era no Sheets) — ver
-- services/vendas_db.py.
CREATE TABLE IF NOT EXISTS vendas (
    venda TEXT PRIMARY KEY,
    empresa TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Separando',
    data_limite TEXT NOT NULL DEFAULT '',
    id_exped TEXT NOT NULL DEFAULT '',
    gaiola TEXT NOT NULL DEFAULT '',
    impresso_em TEXT NOT NULL DEFAULT '',
    -- Data/hora em que o status virou "Enviado" (bipagem de gaiola OU correção
    -- manual do Gerente) — em branco pros pedidos já enviados antes dessa
    -- coluna existir, não tem como recuperar isso retroativamente.
    enviado_em TEXT NOT NULL DEFAULT '',
    -- 'Expedição' (nosso galpão separa/embala/expede), 'Full' (Mercado Envios Full —
    -- o próprio ML separa/embala; nunca deve aparecer no Mural/Embalagem/Gaiolas, só
    -- na auditoria do Gerente) ou 'Em análise' (ainda não deu pra saber: envio não
    -- atribuído ou erro na consulta do shipment). 'Em análise' é estado temporário,
    -- NÃO um palpite — o webhook de shipments reavalia e resolve em minutos (ver
    -- MeliService.detectar_origem e sync_service.process_shipment_notification).
    origem TEXT NOT NULL DEFAULT 'Expedição',
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vendas_id_exped ON vendas (upper(id_exped));
CREATE INDEX IF NOT EXISTS idx_vendas_status ON vendas (lower(status));

-- 1 venda (pack) pode combinar >1 order_id do ML — dados fiscais são por ORDER.
CREATE TABLE IF NOT EXISTS venda_orders (
    order_id TEXT PRIMARY KEY,
    venda TEXT NOT NULL REFERENCES vendas(venda),
    empresa TEXT NOT NULL DEFAULT '',
    nf_numero_serie TEXT NOT NULL DEFAULT '',
    nf_valor TEXT NOT NULL DEFAULT '',
    nf_cfop TEXT NOT NULL DEFAULT '',
    nf_chave TEXT NOT NULL DEFAULT '',
    comprador TEXT NOT NULL DEFAULT '',
    documento TEXT NOT NULL DEFAULT '',
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_venda_orders_venda ON venda_orders (venda);

CREATE TABLE IF NOT EXISTS venda_itens (
    id SERIAL PRIMARY KEY,
    venda TEXT NOT NULL REFERENCES vendas(venda),
    order_id TEXT NOT NULL REFERENCES venda_orders(order_id),
    sku TEXT NOT NULL,
    nome TEXT NOT NULL DEFAULT '',
    qtd INTEGER NOT NULL DEFAULT 1,
    endereco TEXT NOT NULL DEFAULT '',
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_venda_itens_order_sku ON venda_itens (order_id, sku);
CREATE INDEX IF NOT EXISTS idx_venda_itens_venda ON venda_itens (venda);

-- Sequência atômica por prefixo (sigla+DDMMAA) do ID de expedição — substitui
-- o antigo "escanear a coluna Q e pegar o máximo" (não atômico).
CREATE TABLE IF NOT EXISTS expedicao_sequencias (
    prefixo TEXT PRIMARY KEY,
    ultimo_seq INTEGER NOT NULL DEFAULT 0
);

-- Guia de retirada de gaiola (2026-07-29) — registro permanente pra segurança
-- jurídica de quem levou o quê: motorista/CPF/placa + snapshot dos pacotes da
-- gaiola no momento da coleta. Nunca editado depois de criado (histórico).
CREATE TABLE IF NOT EXISTS guias_retirada (
    id SERIAL PRIMARY KEY,
    romaneio_id TEXT NOT NULL DEFAULT '',
    gaiola TEXT NOT NULL,
    motorista_nome TEXT NOT NULL,
    motorista_cpf TEXT NOT NULL,
    placa TEXT NOT NULL,
    transportadora TEXT NOT NULL DEFAULT '',
    copias INTEGER NOT NULL DEFAULT 1,
    pacotes JSONB NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Contador global (nunca reinicia) do nº do romaneio impresso no Termo de
-- Retirada — precisa existir ANTES da impressão (o nº vai no papel), então é
-- reservado em /retirada/preparar, antes de qualquer gravação em guias_retirada.
CREATE TABLE IF NOT EXISTS romaneio_sequencia (
    id INTEGER PRIMARY KEY DEFAULT 1,
    ultimo_seq INTEGER NOT NULL DEFAULT 0,
    CHECK (id = 1)
);
INSERT INTO romaneio_sequencia (id, ultimo_seq) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
"""

# Colunas adicionadas depois da 1ª versão da tabela — ALTER separado porque
# CREATE TABLE IF NOT EXISTS não altera uma tabela que já existe em produção.
MIGRATIONS = """
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS prazo_devolucao TIMESTAMPTZ;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS destino TEXT;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS return_id TEXT;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS status_ml TEXT;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS fase TEXT;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS fase_em TIMESTAMPTZ;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS chegou_em TIMESTAMPTZ;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS shipping_id TEXT;
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'Expedição';
ALTER TABLE vendas ADD COLUMN IF NOT EXISTS enviado_em TEXT NOT NULL DEFAULT '';
ALTER TABLE guias_retirada ADD COLUMN IF NOT EXISTS romaneio_id TEXT NOT NULL DEFAULT '';
ALTER TABLE guias_retirada ADD COLUMN IF NOT EXISTS transportadora TEXT NOT NULL DEFAULT '';
"""


async def ensure_schema() -> None:
    """Cria as tabelas se não existirem — chamado 1x no startup (main.py lifespan).

    Tolerante: se o Cloud SQL ainda não estiver configurado/acessível, só loga o
    aviso e segue — o resto do app (Mural/Embalagem/Gaiolas, tudo no Sheets) não
    depende disso pra funcionar."""
    try:
        conn = await _get_connection()
    except Exception:
        logger.exception("Cloud SQL indisponível no startup — recursos de cancelamento ficam desabilitados.")
        return
    try:
        await conn.execute(SCHEMA)
        await conn.execute(MIGRATIONS)
    finally:
        await conn.close()


async def registrar_cancelamento(venda_ml: str, order_id: str, empresa: str, status_ml: str,
                                  motivo: str | None, data_evento) -> None:
    """Idempotente — o ML pode reenviar o mesmo webhook mais de uma vez."""
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO cancelamentos (venda_ml, order_id, empresa, status_ml, motivo, data_evento)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (venda_ml) DO NOTHING""",
            venda_ml, order_id, empresa, status_ml, motivo, data_evento,
        )
    finally:
        await conn.close()


async def get_cancelamento(venda_ml: str) -> dict | None:
    conn = await _get_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM cancelamentos WHERE venda_ml = $1", venda_ml)
        return dict(row) if row else None
    finally:
        await conn.close()


async def listar_cancelados_arquivados() -> set[str]:
    """Vendas de cancelamentos já finalizados pelo Gerente — usado pelo Mural pra
    tirar o card (o Status na aba Vendas continua "Cancelado" pra sempre; só o
    Postgres sabe que já foi tratado)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT venda_ml FROM cancelamentos WHERE finalizado_por_gerente = TRUE")
        return {r["venda_ml"] for r in rows}
    finally:
        await conn.close()


async def finalizar_cancelamento(venda_ml: str) -> None:
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE cancelamentos SET finalizado_por_gerente = TRUE, finalizado_em = now() WHERE venda_ml = $1",
            venda_ml,
        )
    finally:
        await conn.close()


async def listar_vendas_com_devolucao() -> set[str]:
    """Vendas que já têm uma devolução (claim do ML) registrada — o ML às vezes
    marca o pedido como "cancelled" quando o reembolso da devolução é concluído,
    mesmo sendo fisicamente uma devolução (não um cancelamento antes do envio).
    Usado pelo Mural pra não duplicar o mesmo pedido como card de "Cancelado" em
    "Hoje" quando ele já está sendo tratado na aba Devoluções."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT DISTINCT venda_ml FROM devolucoes WHERE venda_ml IS NOT NULL")
        return {r["venda_ml"] for r in rows}
    finally:
        await conn.close()


async def definir_destino_devolucao(claim_id: str, destino: str) -> None:
    """Grava pra onde o pacote da devolução vai ('seller_address' | 'warehouse').

    Só preenche o que está vazio: o destino é um fato do envio de volta, e uma
    consulta posterior não deve reescrever o que já foi estabelecido."""
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE devolucoes SET destino = $2 WHERE claim_id = $1 AND destino IS NULL",
            str(claim_id), destino)
    finally:
        await conn.close()


async def registrar_devolucao(claim_id: str, venda_ml: str | None, order_id: str | None,
                               empresa: str | None, tipo: str | None, stage: str | None,
                               motivo: str | None, data_evento, raw: dict,
                               prazo_devolucao=None, status_ml: str | None = None,
                               shipping_id: str | None = None, destino: str | None = None,
                               return_id: str | None = None) -> None:
    """Grava ou atualiza (o claim do ML pode ser consultado/reenviado várias vezes ao
    longo da vida da devolução, ex.: "opened" → "closed") — os campos que vêm do ML
    (status_ml/tipo/stage/motivo/raw) sempre são atualizados pro valor mais recente;
    os campos do NOSSO workflow (status/avaliacao/finalizado_em) nunca são tocados aqui.

    `prazo_devolucao`: data limite pro item físico chegar de volta (quando o ML informar
    isso no claim); enquanto não tivermos isso, fica None e o card mostra "Aguardando
    devolução" sem prazo.

    `shipping_id`: só no retorno sem entrega, onde não existe claim e a volta do pacote
    é acompanhada pelo envio original. COALESCE porque um reenvio de webhook sem esse
    dado não pode apagar o que já foi guardado.

    `destino` ('seller_address' | 'warehouse') e `return_id` seguem a mesma regra do
    COALESCE: eles só existem depois que o ML cria o envio de volta, então as primeiras
    notificações do claim chegam sem eles e não podem apagar o que veio depois."""
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO devolucoes (claim_id, venda_ml, order_id, empresa, tipo, stage, motivo,
                                        data_evento, raw, prazo_devolucao, status_ml, shipping_id,
                                        destino, return_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14)
               ON CONFLICT (claim_id) DO UPDATE SET
                   tipo = EXCLUDED.tipo, stage = EXCLUDED.stage, motivo = EXCLUDED.motivo,
                   raw = EXCLUDED.raw, status_ml = EXCLUDED.status_ml,
                   prazo_devolucao = COALESCE(EXCLUDED.prazo_devolucao, devolucoes.prazo_devolucao),
                   shipping_id = COALESCE(EXCLUDED.shipping_id, devolucoes.shipping_id),
                   destino = COALESCE(EXCLUDED.destino, devolucoes.destino),
                   return_id = COALESCE(EXCLUDED.return_id, devolucoes.return_id)""",
            claim_id, venda_ml, order_id, empresa, tipo, stage, motivo, data_evento,
            json.dumps(raw), prazo_devolucao, status_ml, shipping_id, destino, return_id,
        )
    finally:
        await conn.close()


async def salvar_fase_devolucao(claim_id: str, fase: str, chegou_em=None) -> None:
    """Guarda a etapa física apurada no ML pra que a tela leia daqui, não da API.

    `chegou_em` só avança de NULL pra uma data (COALESCE): a data de chegada é um fato
    histórico, e uma consulta posterior que não a encontre não pode apagá-la."""
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE devolucoes SET fase = $2, fase_em = now(), "
            "chegou_em = COALESCE(devolucoes.chegou_em, $3) WHERE claim_id = $1",
            claim_id, fase, chegou_em,
        )
    finally:
        await conn.close()


async def listar_devolucoes(pendentes: bool = True) -> list[dict]:
    """`pendentes=True` (Mural, expedição): devoluções cujo PACOTE ainda é assunto do
    galpão. `pendentes=False` (Gerente): histórico completo — registro permanente.

    O que tira do Mural é a nossa baixa (`status = 'Finalizada'`) ou o pacote ter
    parado de vir (`fase = 'Encerrada'`: cancelada, expirada, devolvida ao comprador,
    perdida).

    Até 20/08/2026 o filtro era `status_ml IS DISTINCT FROM 'closed'`, e isso
    confundia duas coisas independentes: o ML fechar a RECLAMAÇÃO não faz o PACOTE
    chegar. Foi o que aconteceu com PLG100826001 — o ML encerrou a disputa a favor do
    comprador no dia 19, e a devolução sumiu do Mural enquanto o pacote ainda estava a
    caminho (chegada prevista entre 20 e 23/08). A expedição ia receber uma caixa sem
    card nenhum esperando por ela.

    `fase` NULL (recém-registrada, ainda não consultada) conta como pendente: na
    dúvida o card aparece, porque some-lo é o erro que custa caro."""
    conn = await _get_connection()
    try:
        query = "SELECT * FROM devolucoes"
        if pendentes:
            query += " WHERE status != 'Finalizada' AND fase IS DISTINCT FROM 'Encerrada'"
        query += " ORDER BY criado_em DESC"
        rows = await conn.fetch(query)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def avaliar_devolucao(claim_id: str, avaliacao: str) -> None:
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE devolucoes SET status = 'Avaliado', avaliacao = $2, avaliado_em = now() "
            "WHERE claim_id = $1",
            claim_id, avaliacao,
        )
    finally:
        await conn.close()


async def finalizar_devolucao(claim_id: str) -> None:
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE devolucoes SET status = 'Finalizada', finalizado_em = now() WHERE claim_id = $1",
            claim_id,
        )
    finally:
        await conn.close()


async def get_devolucao(claim_id: str) -> dict | None:
    conn = await _get_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM devolucoes WHERE claim_id = $1", claim_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def definir_shipping_devolucao(claim_id: str, shipping_id: str) -> None:
    """Amarra uma devolução ao envio, quando o registro é anterior a esse campo
    existir. Sem isso, um retorno gravado antes não teria como ser reconsultado."""
    conn = await _get_connection()
    try:
        await conn.execute(
            "UPDATE devolucoes SET shipping_id = $2 WHERE claim_id = $1 AND shipping_id IS NULL",
            claim_id, str(shipping_id),
        )
    finally:
        await conn.close()


async def get_devolucao_por_shipping(shipping_id: str) -> dict | None:
    """A devolução amarrada a um envio — usado pelo webhook de shipments, que só
    conhece o envio. Só o retorno sem entrega preenche `shipping_id`."""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM devolucoes WHERE shipping_id = $1 "
            "ORDER BY criado_em DESC LIMIT 1", str(shipping_id))
        return dict(row) if row else None
    finally:
        await conn.close()


async def contar_retiradas_do_dia() -> int:
    """Quantas gaiolas já saíram do galpão HOJE (guias efetivamente registradas).

    O corte é o dia de Brasília, não UTC: uma coleta das 22h viraria "amanhã" se
    contada em UTC, e o papel na mão do motorista diz outra data."""
    conn = await _get_connection()
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM guias_retirada "
            "WHERE (criado_em - interval '3 hours')::date = (now() - interval '3 hours')::date"
        ) or 0
    finally:
        await conn.close()


async def get_next_romaneio_id() -> str:
    """Reserva atomicamente o próximo nº do romaneio (Termo de Retirada) — chamado
    em /retirada/preparar, ANTES de imprimir (o nº precisa estar no papel). Nunca
    reinicia (numeração corrida, como nota fiscal) — se a impressão falhar e o
    operador tentar de novo, o número reservado fica pulado (igual formulário
    pré-numerado de papel: número "perdido" não é um problema, só não se repete)."""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            "UPDATE romaneio_sequencia SET ultimo_seq = ultimo_seq + 1 WHERE id = 1 RETURNING ultimo_seq")
        return f"ROM-{row['ultimo_seq']:06d}"
    finally:
        await conn.close()


async def registrar_guia_retirada(romaneio_id: str, gaiola: str, motorista_nome: str, motorista_cpf: str,
                                   placa: str, transportadora: str, copias: int, pacotes: list[dict]) -> int:
    """Registro permanente da retirada (segurança jurídica) — nunca editado depois
    de criado. `pacotes` = snapshot no momento da coleta ([{venda, id_exped,
    empresa, n_sku}]), pra sempre saber o que saiu mesmo que o pedido mude
    depois no resto do sistema."""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            """INSERT INTO guias_retirada (romaneio_id, gaiola, motorista_nome, motorista_cpf, placa,
                                            transportadora, copias, pacotes)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) RETURNING id""",
            romaneio_id, gaiola, motorista_nome, motorista_cpf, placa, transportadora, copias,
            json.dumps(pacotes),
        )
        return row["id"]
    finally:
        await conn.close()
