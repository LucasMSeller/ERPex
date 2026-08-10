"""Vendas (Sheets -> Postgres, 2026-07-29) — substituto 1:1 dos métodos de Vendas
do SheetsService, devolvendo os mesmos formatos de dict/lista que os templates já
esperam. `venda` = venda_ml (pack_id) ou order_id (fallback) — chave central usada
em todo o app (Mural/Gerente/Embalagem/Gaiolas).

status/gaiola/id_exped/impresso_em ficam 1x por venda (não 1x por SKU, como era no
Sheets) — cada mudança agora é 1 UPDATE, não N (1 por SKU do pedido). Dados fiscais
ficam 1x por order_id (`venda_orders`), porque 1 venda pode combinar >1 order_id.

Mesmo estilo enxuto de services/db.py: sem pool/ORM, conexão nova por chamada.
"""
from datetime import date, datetime, timedelta, timezone
from services.db import _get_connection
from services.meli_service import _BR_TZ, ORIGEM_INDEFINIDA

# Por quanto tempo uma venda com a origem ainda indefinida fica escondida do Mural e do
# Gerente — a ideia é só mostrar o que já foi decidido. Na prática o webhook de shipments
# resolve em segundos. Passado o limite, ela VOLTA a aparecer (com aviso): não há job
# periódico neste projeto pra resgatar nada, então esconder pra sempre significaria perder
# o prazo de despacho de um pedido nosso sem ninguém ficar sabendo.
JANELA_ORIGEM_INDEFINIDA = timedelta(minutes=30)


def _aguardando_classificacao(p: dict) -> bool:
    """True enquanto a origem do pedido ainda está sendo decidida e dentro da janela."""
    if p.get("origem") != ORIGEM_INDEFINIDA:
        return False
    criado = p.get("criado_em")
    if not criado:
        return False
    return datetime.now(timezone.utc) - criado < JANELA_ORIGEM_INDEFINIDA

GAIOLAS = ["Gaiola 1", "Gaiola 2", "Gaiola 3", "Gaiola 4"]
AGUARDANDO_BOX = "Aguardando box"


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


# ── Escrita de vendas novas (webhook / backfill) ────────────────────────────

async def get_sale_order_ids() -> set[str]:
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT order_id FROM venda_orders")
        return {r["order_id"] for r in rows}
    finally:
        await conn.close()


async def get_or_create_venda_and_expedition_id(venda: str, empresa: str, data_limite: str,
                                                 sigla: str, ddmmaa: str, origem: str = "Expedição") -> str:
    """Cria a linha da venda se não existir e devolve o ID de expedição, gerando um
    novo de forma atômica se ainda não tiver. `SELECT ... FOR UPDATE` serializa só
    chamadas da MESMA venda (vendas diferentes seguem em paralelo) — é o que
    garante que 2 order_ids do mesmo pack (2 webhooks separados) nunca ganhem 2 IDs
    de expedição diferentes. `origem` só é gravado na CRIAÇÃO (não sobrescreve se a
    venda já existir)."""
    conn = await _get_connection()
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO vendas (venda, empresa, data_limite, status, origem)
                   VALUES ($1, $2, $3, 'Separando', $4) ON CONFLICT (venda) DO NOTHING""",
                venda, empresa, data_limite, origem)
            row = await conn.fetchrow("SELECT id_exped FROM vendas WHERE venda = $1 FOR UPDATE", venda)
            if row["id_exped"]:
                return row["id_exped"]
            prefix = f"{sigla}{ddmmaa}"
            seq = await conn.fetchrow(
                """INSERT INTO expedicao_sequencias (prefixo, ultimo_seq) VALUES ($1, 1)
                   ON CONFLICT (prefixo) DO UPDATE SET ultimo_seq = expedicao_sequencias.ultimo_seq + 1
                   RETURNING ultimo_seq""", prefix)
            exped_id = f"{prefix}{seq['ultimo_seq']:03d}"
            await conn.execute("UPDATE vendas SET id_exped = $2 WHERE venda = $1", venda, exped_id)
            return exped_id
    finally:
        await conn.close()


async def registrar_pedido(venda: str, order_id: str, empresa: str, itens: list[dict],
                            fiscal: list) -> None:
    """Grava o pedido (1 order_id + seus itens) — a linha da venda já deve existir
    (criada por `get_or_create_venda_and_expedition_id`, chamada antes). `itens` =
    [{"sku","nome","qtd","endereco"}]. `fiscal` = [nf_numero_serie, nf_valor, nf_cfop,
    nf_chave, comprador, documento]. Idempotente (ON CONFLICT DO NOTHING) — webhook
    reenviado não duplica."""
    conn = await _get_connection()
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO venda_orders (order_id, venda, empresa, nf_numero_serie, nf_valor,
                                              nf_cfop, nf_chave, comprador, documento)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT (order_id) DO NOTHING""",
                order_id, venda, empresa, *fiscal)
            await conn.executemany(
                """INSERT INTO venda_itens (venda, order_id, sku, nome, qtd, endereco)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (order_id, sku) DO NOTHING""",
                [(venda, order_id, it["sku"], it["nome"], it["qtd"], it["endereco"]) for it in itens])
    finally:
        await conn.close()


async def get_expedition_id(numero: str) -> str:
    """ID de expedição da venda cujo order_id OU nº venda (pack) == numero."""
    if not numero:
        return ""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow("SELECT id_exped FROM vendas WHERE venda = $1", numero)
        if row:
            return row["id_exped"]
        row = await conn.fetchrow(
            """SELECT v.id_exped FROM vendas v JOIN venda_orders vo ON vo.venda = v.venda
               WHERE vo.order_id = $1""", numero)
        return row["id_exped"] if row else ""
    finally:
        await conn.close()


# ── Fiscal (NF por order_id) ─────────────────────────────────────────────────

async def get_sales_needing_fiscal() -> list[tuple[str, str]]:
    """[(empresa, order_id)] dos pedidos ainda sem NF (chave vazia) e não enviados."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """SELECT vo.empresa, vo.order_id FROM venda_orders vo
               JOIN vendas v ON v.venda = vo.venda
               WHERE vo.nf_chave = '' AND lower(v.status) != 'enviado'""")
        return [(r["empresa"], r["order_id"]) for r in rows]
    finally:
        await conn.close()


async def set_fiscal_for_order(order_id: str, fiscal_cols: list) -> int:
    """Preenche os dados fiscais do order_id. Retorna 1 se atualizou, 0 se não achou."""
    conn = await _get_connection()
    try:
        result = await conn.execute(
            """UPDATE venda_orders SET nf_numero_serie=$2, nf_valor=$3, nf_cfop=$4,
                                        nf_chave=$5, comprador=$6, documento=$7
               WHERE order_id=$1""",
            order_id, *fiscal_cols)
    finally:
        await conn.close()
    return int(result.split()[-1])


# ── Mural / Gerente (leitura agrupada por venda) ─────────────────────────────

async def _fetch_pedidos() -> dict[str, dict]:
    """Todas as vendas com seus itens, agrupadas por venda — base compartilhada
    por get_mural_pedidos/get_all_pedidos/get_pedido_by_venda/get_pedido_by_exped
    (mesmo papel que _agrupar_pedidos tinha no SheetsService).

    Ordenado pelo próprio número da venda (decrescente), não por `criado_em`:
    os 111 pedidos trazidos da migração do Sheets todos ganharam o MESMO
    `criado_em` (o horário da migração, não da venda de verdade), então essa
    coluna não serve pra ordenar o que já existia antes do corte. `venda` é
    sempre um pack_id/order_id do ML — número atribuído de forma crescente ao
    longo do tempo pela própria plataforma — então ordenar por ele reflete a
    ordem real, migrado ou não."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """SELECT v.venda, v.empresa, v.status, v.data_limite, v.id_exped, v.gaiola, v.impresso_em,
                      v.enviado_em, v.origem, v.criado_em, vo.order_id, vi.sku, vi.nome, vi.qtd, vi.endereco
               FROM vendas v
               JOIN venda_itens vi ON vi.venda = v.venda
               JOIN venda_orders vo ON vo.order_id = vi.order_id
               ORDER BY (CASE WHEN v.venda ~ '^[0-9]+$' THEN v.venda::BIGINT ELSE 0 END) DESC, vi.id""")
    finally:
        await conn.close()
    pedidos: dict[str, dict] = {}
    for r in rows:
        pedido = pedidos.setdefault(r["venda"], {
            "venda": r["venda"], "order_id": r["order_id"], "empresa": r["empresa"],
            "id_exped": r["id_exped"], "data_limite": r["data_limite"], "status": r["status"],
            "impresso_em": r["impresso_em"], "enviado_em": r["enviado_em"], "gaiola": r["gaiola"],
            "origem": r["origem"], "criado_em": r["criado_em"], "itens": [],
        })
        pedido["itens"].append({"sku": r["sku"], "nome": r["nome"], "qtd": r["qtd"], "endereco": r["endereco"]})
    return pedidos


async def get_mural_pedidos() -> list[dict]:
    """Pedidos ainda não impressos (exclui Separado/Embalado/Enviado) — fila do mural.

    Exclui SEMPRE origem=="Full" — Mercado Envios Full é separado/embalado pelo
    próprio ML, nunca deve aparecer pro nosso galpão bipar/imprimir. Esconde também
    o que ainda está sendo classificado (ver `_aguardando_classificacao`): só entra
    na fila o que já foi decidido."""
    pedidos = await _fetch_pedidos()
    return [p for p in pedidos.values()
            if p["status"].lower() not in ("separado", "embalado", "enviado")
            and p["origem"] != "Full"
            and not _aguardando_classificacao(p)]


async def get_all_pedidos(status_filter: list[str] | None = None, busca: str = "",
                           loja: str = "", dia_de: str = "", dia_ate: str = "",
                           origem_filter: list[str] | None = None,
                           dia_criado_de: str = "", dia_criado_ate: str = "",
                           incluir_em_classificacao: bool = False) -> list[dict]:
    """Todos os pedidos (qualquer status) — tela de auditoria do Gerente.

    `dia_de`/`dia_ate` filtram pela data-limite de despacho; `dia_criado_de`/
    `dia_criado_ate` filtram por quando a venda foi de fato registrada
    (`criado_em`) — datas diferentes, perguntas diferentes ("quando precisa
    sair" vs "quando entrou").

    Assim como o Mural, esconde o que ainda está sendo classificado — o Gerente também
    só vê o que já foi decidido (ver `_aguardando_classificacao`). As rotas de manutenção
    passam `incluir_em_classificacao=True`: elas existem justamente pra resolver esses."""
    pedidos = [p for p in (await _fetch_pedidos()).values()
               if incluir_em_classificacao or not _aguardando_classificacao(p)]
    if status_filter:
        alvo = {s.strip().lower() for s in status_filter}
        pedidos = [p for p in pedidos if p["status"].strip().lower() in alvo]
    if origem_filter:
        alvo_origem = {o.strip().lower() for o in origem_filter}
        pedidos = [p for p in pedidos if p["origem"].strip().lower() in alvo_origem]
    if loja:
        pedidos = [p for p in pedidos if (p["empresa"] or "").strip().lower() == loja.strip().lower()]
    if dia_de or dia_ate:
        de = date.fromisoformat(dia_de) if dia_de else None
        ate = date.fromisoformat(dia_ate) if dia_ate else None

        def _no_intervalo(p: dict) -> bool:
            d = _parse_data_br(p.get("data_limite"))
            if d is None:
                return False
            if de and d < de:
                return False
            if ate and d > ate:
                return False
            return True

        pedidos = [p for p in pedidos if _no_intervalo(p)]
    if dia_criado_de or dia_criado_ate:
        de_c = date.fromisoformat(dia_criado_de) if dia_criado_de else None
        ate_c = date.fromisoformat(dia_criado_ate) if dia_criado_ate else None

        def _criado_no_intervalo(p: dict) -> bool:
            c = p.get("criado_em")
            if c is None:
                return False
            d = c.astimezone(_BR_TZ).date()
            if de_c and d < de_c:
                return False
            if ate_c and d > ate_c:
                return False
            return True

        pedidos = [p for p in pedidos if _criado_no_intervalo(p)]
    busca = (busca or "").strip().lower()
    if busca:
        def _match(p: dict) -> bool:
            campos = [p["venda"], p.get("order_id", ""), p["empresa"], p["id_exped"]]
            campos += [item["sku"] for item in p["itens"]]
            return any(busca in str(c).lower() for c in campos if c)
        pedidos = [p for p in pedidos if _match(p)]
    return pedidos


async def get_pedido_by_venda(venda_key: str) -> dict | None:
    return (await _fetch_pedidos()).get(venda_key)


async def get_pedido_by_order_id(order_id: str) -> dict | None:
    """Pedido pelo order_id bruto do ML — resolve pela venda_orders (indexa CADA
    order_id individualmente), diferente do antigo comportamento do Sheets que só
    resolvia pelo PRIMEIRO order_id encontrado num pack com vários."""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow("SELECT venda FROM venda_orders WHERE order_id = $1", order_id)
    finally:
        await conn.close()
    if not row:
        return None
    return (await _fetch_pedidos()).get(row["venda"])


async def get_pedido_by_exped(exped_id: str) -> dict | None:
    alvo = (exped_id or "").strip().upper()
    for pedido in (await _fetch_pedidos()).values():
        if pedido["id_exped"].strip().upper() == alvo:
            return pedido
    return None


async def set_criado_em(venda_key: str, criado_em: datetime) -> int:
    """Corrige `criado_em` de uma venda já existente — usado no backfill pontual
    que busca a data real da venda no ML pros pedidos migrados do Sheets (lá
    `criado_em` só reflete o horário da migração em si, não da venda)."""
    conn = await _get_connection()
    try:
        result = await conn.execute(
            "UPDATE vendas SET criado_em=$2 WHERE venda=$1", venda_key, criado_em)
    finally:
        await conn.close()
    return int(result.split()[-1])


async def set_data_limite(venda_key: str, data_limite: str) -> int:
    """Corrige o prazo de DESPACHO (não o de entrega ao comprador) de uma venda já
    existente — usado quando o webhook 'shipments' chega e o SLA do Mercado Envios
    (que na criação do pedido costuma responder 404, envio ainda não maduro pra
    calcular) já está disponível."""
    conn = await _get_connection()
    try:
        result = await conn.execute(
            "UPDATE vendas SET data_limite=$2, atualizado_em=now() WHERE venda=$1", venda_key, data_limite)
    finally:
        await conn.close()
    return int(result.split()[-1])


async def set_origem(venda_key: str, origem: str) -> int:
    """Corrige a origem (Expedição/Full) de uma venda já existente — usado na
    detecção pontual de pedidos Full que já tinham sido migrados do Sheets com
    o valor padrão 'Expedição' (o Sheets nunca rastreou isso)."""
    conn = await _get_connection()
    try:
        result = await conn.execute(
            "UPDATE vendas SET origem=$2, atualizado_em=now() WHERE venda=$1", venda_key, origem)
    finally:
        await conn.close()
    return int(result.split()[-1])


async def set_status_for_venda(venda_key: str, status: str, impresso_em: str | None = None) -> int:
    """Grava Status (e Impresso em, se informado) — 1 UPDATE, não N por SKU.

    Quando o novo status é "Enviado", grava também `enviado_em` (data/hora de
    agora) — não importa por qual caminho chegou lá (coleta de gaiola ou
    correção manual do Gerente); `COALESCE` preserva o valor atual quando o
    status não é "Enviado"."""
    enviado_em = datetime.now().strftime("%d/%m/%Y %H:%M") if status.strip().lower() == "enviado" else None
    conn = await _get_connection()
    try:
        if impresso_em is not None:
            result = await conn.execute(
                """UPDATE vendas SET status=$2, impresso_em=$3, enviado_em=COALESCE($4, enviado_em),
                                      atualizado_em=now() WHERE venda=$1""",
                venda_key, status, impresso_em, enviado_em)
        else:
            result = await conn.execute(
                """UPDATE vendas SET status=$2, enviado_em=COALESCE($3, enviado_em), atualizado_em=now()
                   WHERE venda=$1""",
                venda_key, status, enviado_em)
    finally:
        await conn.close()
    return int(result.split()[-1])


# ── Gaiolas ───────────────────────────────────────────────────────────────

async def get_gaiolas_estado(gaiolas: list[str] | None = None) -> dict[str, list[dict]]:
    """Pacotes embalados, agrupados por ID de expedição e organizados por zona:
    "Aguardando box" (sem gaiola) + cada gaiola."""
    gaiolas = gaiolas or GAIOLAS
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """SELECT v.id_exped, v.venda, v.empresa, v.gaiola, COUNT(vi.id) AS n_sku
               FROM vendas v JOIN venda_itens vi ON vi.venda = v.venda
               WHERE lower(v.status) = 'embalado'
               GROUP BY v.id_exped, v.venda, v.empresa, v.gaiola""")
    finally:
        await conn.close()
    estado: dict[str, list[dict]] = {AGUARDANDO_BOX: [], **{g: [] for g in gaiolas}}
    for r in rows:
        zona = r["gaiola"] if r["gaiola"] in gaiolas else AGUARDANDO_BOX
        estado[zona].append({"id_exped": r["id_exped"], "venda": r["venda"], "empresa": r["empresa"],
                              "gaiola": r["gaiola"], "n_sku": r["n_sku"]})
    return estado


async def get_pacotes_da_gaiola(gaiola: str) -> list[dict]:
    """Pacotes Embalado atualmente numa gaiola — usado pra montar a guia de
    retirada ANTES de confirmar a coleta (preparar/imprimir/confirmar, mesmo
    padrão da etiqueta de separação)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """SELECT v.id_exped, v.venda, v.empresa, COUNT(vi.id) AS n_sku
               FROM vendas v JOIN venda_itens vi ON vi.venda = v.venda
               WHERE lower(v.status) = 'embalado' AND v.gaiola = $1
               GROUP BY v.id_exped, v.venda, v.empresa""",
            gaiola)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def mover_para_gaiola(id_exped: str, gaiola: str) -> dict:
    """Bipagem: acha a venda Embalado com esse ID de expedição e grava a gaiola.
    Comparação case-insensitive — leitor de código de barras pode inverter caixa."""
    alvo = (id_exped or "").strip().upper()
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            """UPDATE vendas SET gaiola=$2, atualizado_em=now()
               WHERE upper(id_exped)=$1 AND lower(status)='embalado'
               RETURNING venda, empresa""",
            alvo, gaiola)
        if not row:
            return {"ok": False, "msg": f'ID "{id_exped}" não encontrado (aguardando embalagem).'}
        n_sku = await conn.fetchval("SELECT COUNT(*) FROM venda_itens WHERE venda=$1", row["venda"])
    finally:
        await conn.close()
    return {"ok": True, "venda": row["venda"], "empresa": row["empresa"], "n_sku": n_sku}


async def remover_da_gaiola(venda_key: str) -> dict:
    """Limpa a gaiola de um pedido (correção manual do Gerente). Recusa se o
    pedido já foi Enviado — depois de enviado a gaiola é só histórico."""
    conn = await _get_connection()
    try:
        row = await conn.fetchrow("SELECT status FROM vendas WHERE venda=$1", venda_key)
        if not row:
            return {"ok": False, "msg": f'Pedido "{venda_key}" não encontrado.'}
        if row["status"].strip().lower() == "enviado":
            return {"ok": False, "msg": "Pedido já foi enviado — não é possível alterar a gaiola."}
        await conn.execute("UPDATE vendas SET gaiola='', atualizado_em=now() WHERE venda=$1", venda_key)
    finally:
        await conn.close()
    return {"ok": True, "n": 1}


async def coletar_gaiola(gaiola: str) -> dict:
    """Marca Enviado (+ enviado_em) todas as vendas Embalado que estão na gaiola informada."""
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """UPDATE vendas SET status='Enviado', enviado_em=$2, atualizado_em=now()
               WHERE lower(status)='embalado' AND gaiola=$1
               RETURNING venda""",
            gaiola, agora)
    finally:
        await conn.close()
    return {"n_pacotes": len(rows)}


# ── Migração pontual (Sheets -> Postgres) ────────────────────────────────────

_VENDAS_COLS = 19   # A..S


async def bulk_migrate(rows: list[list]) -> dict:
    """Reconstrói vendas/venda_orders/venda_itens a partir das linhas cruas da
    aba Vendas ('Vendas'!A3:S). Ignora linha sem SKU/order_id. Reporta (sem
    gravar) qualquer order_id associado a 2 vendas diferentes (indício de edição
    manual inconsistente na planilha). Idempotente (upsert) — pode rodar de novo
    com segurança."""
    vendas: dict[str, dict] = {}
    orders: dict[str, dict] = {}
    itens: list[tuple] = []
    ignoradas = 0
    conflitos = []

    for r in rows:
        p = (list(r) + [""] * _VENDAS_COLS)[:_VENDAS_COLS]
        sku, order_id = p[1], p[6]
        if not sku or not order_id:
            ignoradas += 1
            continue
        venda = p[15] or order_id
        empresa = p[0]
        status_row = str(p[7] or "Separando").strip()

        if order_id in orders and orders[order_id]["venda"] != venda:
            conflitos.append({"order_id": order_id, "venda_1": orders[order_id]["venda"], "venda_2": venda})
        else:
            orders.setdefault(order_id, {
                "venda": venda, "empresa": empresa,
                "nf_numero_serie": p[9], "nf_valor": p[10], "nf_cfop": p[11],
                "nf_chave": p[12], "comprador": p[13], "documento": p[14],
            })

        v = vendas.setdefault(venda, {
            "venda": venda, "empresa": empresa, "status": status_row,
            "data_limite": p[4], "id_exped": p[16], "gaiola": p[17], "impresso_em": p[18],
        })
        if status_row.lower() == "enviado":
            v["status"] = "Enviado"
        v["id_exped"] = v["id_exped"] or p[16]

        try:
            qtd = int(float(p[3])) if str(p[3]).strip() else 1
        except (ValueError, TypeError):
            qtd = 1
        itens.append((venda, order_id, sku, p[2], qtd, p[5]))

    conn = await _get_connection()
    try:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO vendas (venda, empresa, status, data_limite, id_exped, gaiola, impresso_em)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (venda) DO UPDATE SET
                       empresa=EXCLUDED.empresa, status=EXCLUDED.status, data_limite=EXCLUDED.data_limite,
                       id_exped=EXCLUDED.id_exped, gaiola=EXCLUDED.gaiola, impresso_em=EXCLUDED.impresso_em,
                       atualizado_em=now()""",
                [(v["venda"], v["empresa"], v["status"], v["data_limite"], v["id_exped"],
                  v["gaiola"], v["impresso_em"]) for v in vendas.values()])
            await conn.executemany(
                """INSERT INTO venda_orders (order_id, venda, empresa, nf_numero_serie, nf_valor,
                                              nf_cfop, nf_chave, comprador, documento)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (order_id) DO UPDATE SET
                       nf_numero_serie=EXCLUDED.nf_numero_serie, nf_valor=EXCLUDED.nf_valor,
                       nf_cfop=EXCLUDED.nf_cfop, nf_chave=EXCLUDED.nf_chave,
                       comprador=EXCLUDED.comprador, documento=EXCLUDED.documento""",
                [(oid, o["venda"], o["empresa"], o["nf_numero_serie"], o["nf_valor"], o["nf_cfop"],
                  o["nf_chave"], o["comprador"], o["documento"]) for oid, o in orders.items()])
            await conn.executemany(
                """INSERT INTO venda_itens (venda, order_id, sku, nome, qtd, endereco)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (order_id, sku) DO UPDATE SET
                       nome=EXCLUDED.nome, qtd=EXCLUDED.qtd, endereco=EXCLUDED.endereco""",
                itens)
    finally:
        await conn.close()

    return {
        "vendas": len(vendas), "orders": len(orders), "itens": len(itens),
        "linhas_ignoradas": ignoradas, "conflitos_order_id": conflitos,
    }


async def purge_ids_corrompidos() -> dict:
    """Remove linhas cujo venda/order_id ficou em notação científica (bug
    conhecido do Sheets: célula NÚMERO com 16 dígitos rende "2,00002E+15" na
    leitura formatada — ver `SheetsService._read_unformatted`). Nunca casa com
    um venda/order_id real (sempre dígitos puros ou sigla+dígitos) — seguro
    rodar sempre antes de `bulk_migrate`, inclusive quando não há nada pra
    limpar."""
    padrao = r"[Ee]\+[0-9]+$"
    conn = await _get_connection()
    try:
        async with conn.transaction():
            itens = await conn.fetch(
                "DELETE FROM venda_itens WHERE order_id ~ $1 OR venda ~ $1 RETURNING id", padrao)
            orders = await conn.fetch(
                "DELETE FROM venda_orders WHERE order_id ~ $1 OR venda ~ $1 RETURNING order_id", padrao)
            vendas_rows = await conn.fetch(
                "DELETE FROM vendas WHERE venda ~ $1 RETURNING venda", padrao)
    finally:
        await conn.close()
    return {"itens_removidos": len(itens), "orders_removidos": len(orders), "vendas_removidas": len(vendas_rows)}


async def count_vendas() -> int:
    conn = await _get_connection()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM vendas")
    finally:
        await conn.close()


async def count_orders() -> int:
    conn = await _get_connection()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM venda_orders")
    finally:
        await conn.close()


async def count_itens() -> int:
    conn = await _get_connection()
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM venda_itens")
    finally:
        await conn.close()
