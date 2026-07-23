"""Cloud SQL (Postgres) — banco à parte só pra dados de notificação/cancelamento.
Não substitui o Sheets (Vendas/Endereçamento/Fiscal continuam lá); aqui mora só o
que não é tabular por natureza (motivo, histórico) e cresce com o tempo.

Sem pool/ORM: cada função abre e fecha sua própria conexão via Cloud SQL Python
Connector (que já mantém o túnel seguro internamente) — mesmo espírito enxuto do
sheets_service.py/token_store.py, adequado ao volume baixo de uma tela interna.
"""
import asyncio
import json
import logging
import asyncpg
from google.cloud.sql.connector import Connector
from config.settings import get_settings

logger = logging.getLogger(__name__)

_connector: Connector | None = None


def _get_connector() -> Connector:
    """O Connector prende internamente ao event loop em que foi criado — se não
    passarmos o loop em execução explicitamente, ele cria um loop próprio (em
    outra thread) e `connect_async` falha com ConnectorLoopError assim que
    chamado de dentro do loop real do FastAPI/uvicorn."""
    global _connector
    if _connector is None:
        _connector = Connector(loop=asyncio.get_running_loop())
    return _connector


async def _get_connection() -> asyncpg.Connection:
    settings = get_settings()
    connector = _get_connector()
    return await connector.connect_async(
        settings.cloudsql_instance_connection_name,
        "asyncpg",
        user=settings.cloudsql_user,
        password=settings.cloudsql_password,
        db=settings.cloudsql_db,
    )


async def close() -> None:
    """Fecha o connector (chamado no shutdown do app)."""
    global _connector
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
"""

# Colunas adicionadas depois da 1ª versão da tabela — ALTER separado porque
# CREATE TABLE IF NOT EXISTS não altera uma tabela que já existe em produção.
MIGRATIONS = """
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS prazo_devolucao TIMESTAMPTZ;
ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS status_ml TEXT;
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


async def registrar_devolucao(claim_id: str, venda_ml: str | None, order_id: str | None,
                               empresa: str | None, tipo: str | None, stage: str | None,
                               motivo: str | None, data_evento, raw: dict,
                               prazo_devolucao=None, status_ml: str | None = None) -> None:
    """Grava ou atualiza (o claim do ML pode ser consultado/reenviado várias vezes ao
    longo da vida da devolução, ex.: "opened" → "closed") — os campos que vêm do ML
    (status_ml/tipo/stage/motivo/raw) sempre são atualizados pro valor mais recente;
    os campos do NOSSO workflow (status/avaliacao/finalizado_em) nunca são tocados aqui.

    `prazo_devolucao`: data limite pro item físico chegar de volta (quando o ML informar
    isso no claim); enquanto não tivermos isso, fica None e o card mostra "Aguardando
    devolução" sem prazo."""
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO devolucoes (claim_id, venda_ml, order_id, empresa, tipo, stage, motivo,
                                        data_evento, raw, prazo_devolucao, status_ml)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
               ON CONFLICT (claim_id) DO UPDATE SET
                   tipo = EXCLUDED.tipo, stage = EXCLUDED.stage, motivo = EXCLUDED.motivo,
                   raw = EXCLUDED.raw, status_ml = EXCLUDED.status_ml,
                   prazo_devolucao = COALESCE(EXCLUDED.prazo_devolucao, devolucoes.prazo_devolucao)""",
            claim_id, venda_ml, order_id, empresa, tipo, stage, motivo, data_evento,
            json.dumps(raw), prazo_devolucao, status_ml,
        )
    finally:
        await conn.close()


async def listar_devolucoes(pendentes: bool = True) -> list[dict]:
    """`pendentes=True` (Mural, expedição): só devoluções que ainda precisam de ação —
    claim ainda aberto no ML E ainda não finalizado por nós. `pendentes=False` (Gerente):
    histórico completo, incluindo já concluídas — registro permanente, como as vendas."""
    conn = await _get_connection()
    try:
        query = "SELECT * FROM devolucoes"
        if pendentes:
            query += " WHERE status != 'Finalizada' AND status_ml IS DISTINCT FROM 'closed'"
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
