import logging
from datetime import datetime, timezone
from collections import defaultdict
from models.product import Product
from models.order import OrderItem
from services.meli_service import MeliService, _origem_code, _ddmmaa_br, ORIGEM_FULL
from services.sheets_service import SheetsService
from services.token_store import TokenStore
from services import db as db_service
from services import enderecos_db
from services import vendas_db

logger = logging.getLogger(__name__)

# Sigla da loja usada no ID de expedição (ex.: PREPLOG -> PLG). Cada loja tem a sua.
# Pode vir do cadastro da loja (campo "sigla"); senão, usa este mapa; senão, 3 letras.
_SIGLAS = {"PREPLOG": "PLG"}


def _sigla(store: dict) -> str:
    ck = (store.get("company_key") or "").upper()
    return (store.get("sigla") or _SIGLAS.get(ck) or ck[:3] or "LOJ").upper()


def _fiscal_cols(f: dict) -> list:
    """Converte o dict de fiscal_summary nas 6 colunas fiscais (venda_orders).

    Sempre em string — o ML às vezes devolve `nf_valor` como número, e as
    colunas fiscais no Postgres são TEXT (asyncpg rejeita int onde espera str)."""
    def _s(v):
        return "" if v is None else str(v)
    return [
        _s(f.get("nf_numero_serie")), _s(f.get("nf_valor")), _s(f.get("nf_cfop")),
        _s(f.get("nf_chave")), _s(f.get("comprador")), _s(f.get("documento")),
    ]


async def sync_products_for_store(store: dict, sheets: SheetsService, token_store: TokenStore) -> dict:
    company_key = store["company_key"]
    tab = store["sheet_tab"]
    sheets.ensure_store_tab(tab)   # cria a aba a partir do modelo, se faltar
    meli = MeliService(store, token_store=token_store)
    try:
        items = await meli.get_all_items()
    except Exception as e:
        logger.error("Erro ao buscar itens do ML para %s: %s", company_key, e)
        return {"company": company_key, "error": str(e)}

    products_map = sheets.get_products_map(tab)
    next_id = sheets.next_id(tab)
    inserted = updated = 0
    skus = []
    fiscal_items: list[tuple[str, str, str, dict]] = []   # (sku, nome, loja, fiscal) p/ aba Fiscal
    fiscal_cache: dict[str, dict] = {}
    for item in items:
        product = Product.from_meli_item(item, company_key)
        # Só SKU REAL entra no Endereçamento/Fiscal (anúncio sem SKU cai no MLB de
        # fallback — não é endereço físico nem item fiscal).
        if product.sku_is_real:
            skus.append(product.sku)
            fis = await meli.get_item_fiscal(product.sku, fiscal_cache)
            if fis["ncm"]:
                product.ncm = fis["ncm"]
            if fis["cest"]:
                product.cest = fis["cest"]
            if fis["ean"]:
                product.ean = fis["ean"]
            if fis["origin"]:
                product.origin = fis["origin"]
            fiscal_items.append((product.sku, product.name, company_key, fis))
        action = sheets.upsert_product(tab, product, products_map, next_id)
        if action == "inserted":
            next_id += 1
            inserted += 1
        else:
            updated += 1

    # Todo SKU único entra automaticamente no Endereçamento (endereço em branco)
    novos_enderecos = await enderecos_db.ensure_addresses_for_skus(skus)
    # E também na aba Fiscal: SKU novo entra pré-preenchido com o que o ML já tem;
    # células vazias de SKUs existentes são completadas (sem sobrescrever digitação).
    novos_fiscais = sheets.sync_fiscal_tab(fiscal_items)

    logger.info("[%s] sync_products: %d inseridos, %d atualizados, %d novos Endereçamento, %d novos Fiscal",
                company_key, inserted, updated, novos_enderecos, novos_fiscais)
    return {"company": company_key, "inserted": inserted, "updated": updated,
            "novos_enderecos": novos_enderecos, "novos_fiscais": novos_fiscais}


async def sync_all_products() -> list[dict]:
    sheets = SheetsService()
    token_store = TokenStore()
    results = []
    for store in await token_store.list_stores():
        results.append(await sync_products_for_store(store, sheets, token_store))
    return results


async def _skus_reais_do_ml(stores: list[dict], token_store: TokenStore) -> set[str]:
    """Busca os SKUs reais (sku_is_real) direto no Mercado Livre pra cada loja
    da lista — mesma chamada `get_all_items` + resolução de SKU que a
    sincronização de Produtos usa."""
    skus: set[str] = set()
    for store in stores:
        meli = MeliService(store, token_store=token_store)
        try:
            items = await meli.get_all_items()
        except Exception as e:
            logger.error("Erro ao buscar itens do ML pra reconciliar Endereçamento (%s): %s",
                         store.get("company_key"), e)
            continue
        for item in items:
            product = Product.from_meli_item(item, store["company_key"])
            if product.sku_is_real:
                skus.add(product.sku)
    return skus


async def reconciliar_enderecos(company_key: str | None = None, sku_prefixo: str | None = None) -> dict:
    """Botão "Vincular SKUs" em /enderecos: busca os SKUs reais DIRETO no
    Mercado Livre — não depende de rodar antes uma sincronização de Produtos
    nem da aba Produtos do Sheets estar em dia. Funciona pra qualquer loja já
    conectada (Firestore), inclusive uma nova, sem precisar mexer no código.

    `company_key` informado (botão por loja): busca só essa loja. Se
    `sku_prefixo` também vier (loja com prefixo configurado em
    /gerente/lojas), remove com segurança os SKUs que sumiram do catálogo
    dessa loja (só entre os que começam com esse prefixo — nunca mexe no
    espaço de SKUs de outra loja). Sem prefixo configurado, só insere (nunca
    remove — não dá pra saber com segurança quais endereços existentes
    "pertencem" só a essa loja). `company_key=None` (reconciliação global,
    todas as lojas juntas): insere os novos E remove os que sumiram de TODAS
    as lojas — seguro, porque compara contra a união de todo mundo."""
    token_store = TokenStore()
    if company_key:
        store = await token_store.get_by_company_or_nickname(company_key)
        skus = await _skus_reais_do_ml([store] if store else [], token_store)
        if sku_prefixo:
            return await enderecos_db.sync_skus(list(skus), sku_prefixo=sku_prefixo)
        novos = await enderecos_db.ensure_addresses_for_skus(list(skus))
        return {"novos": novos, "removidos": 0, "total": len(skus)}

    skus = await _skus_reais_do_ml(await token_store.list_stores(), token_store)
    return await enderecos_db.sync_skus(list(skus))


async def sync_one_company(company_key: str) -> dict:
    token_store = TokenStore()
    store = await token_store.get_by_company(company_key) or await token_store.get_by_company(company_key.upper())
    if not store:
        return {"company": company_key, "error": "loja não conectada"}
    return await sync_products_for_store(store, SheetsService(), token_store)


async def backfill_orders_for_company(company_key: str, max_orders: int = 50) -> dict:
    """Importa as vendas recentes de uma loja (ignora duplicatas)."""
    token_store = TokenStore()
    store = await token_store.get_by_company(company_key)
    if not store:
        return {"company": company_key, "error": "loja não conectada"}

    meli = MeliService(store, token_store=token_store)
    account_name = store.get("nickname", "")

    try:
        orders = await meli.get_recent_orders(max_orders)
    except Exception as e:
        logger.error("Erro ao buscar pedidos de %s: %s", company_key, e)
        return {"company": company_key, "error": str(e)}

    existing = await vendas_db.get_sale_order_ids()
    addresses = await enderecos_db.get_addresses()
    new_orders = new_items = 0

    for order in orders:
        oid = str(order.get("id", ""))
        if not oid or oid in existing:
            continue
        deadline = await meli.get_delivery_deadline(order)
        fiscal = _fiscal_cols(await meli.get_fiscal_summary(order))
        venda_ml = str(order.get("pack_id") or oid)   # nº que aparece no painel do ML
        items = OrderItem.from_meli_order(order, store["company_key"],
                                          account_name=account_name, deadline=deadline)
        empresa = items[0].to_sheet_row()[0] if items else (account_name or company_key)
        ddmmaa = _ddmmaa_br(order.get("date_created", "")) or datetime.now().strftime("%d%m%y")
        origem = await meli.detectar_origem(order)
        exped_id = await vendas_db.get_or_create_venda_and_expedition_id(
            venda_ml, empresa, deadline, _sigla(store), ddmmaa, origem)
        itens = [{"sku": item.sku, "nome": item.product_name, "qtd": item.quantity,
                  "endereco": addresses.get(item.sku, "Sem endereço")} for item in items]
        await vendas_db.registrar_pedido(venda_ml, oid, empresa, itens, fiscal)
        existing.add(oid)
        new_orders += 1
        new_items += len(items)

    logger.info("[%s] backfill: %d pedidos novos, %d itens", company_key, new_orders, new_items)
    return {"company": company_key, "orders": new_orders, "items": new_items}


def _parse_ml_datetime(iso: str | None) -> datetime:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


async def _handle_cancelamento(company_key: str, order_id: str, order: dict) -> dict:
    """order.status == "cancelled" — só age se a venda já tiver virado um card real
    (senão não há nada pra travar). Trava o Status e registra o evento no Postgres
    pro painel de Notificações do Gerente. Idempotente — webhooks de cancelamento
    podem chegar mais de uma vez."""
    pedido = await vendas_db.get_pedido_by_order_id(order_id)
    if not pedido:
        return {"status": "cancelled_ignored", "order_id": order_id,
                "detail": "pedido nunca chegou a ser processado — nada pra cancelar"}

    venda = pedido["venda"]
    await vendas_db.set_status_for_venda(venda, "Cancelado")

    cancel_detail = order.get("cancel_detail") or {}
    motivo = cancel_detail.get("description") if isinstance(cancel_detail, dict) else None
    data_evento = _parse_ml_datetime(order.get("last_updated") or order.get("date_closed"))
    try:
        await db_service.registrar_cancelamento(venda, order_id, company_key, "cancelled", motivo, data_evento)
    except Exception:
        logger.exception("Falha ao registrar cancelamento de %s no Postgres (Status já foi marcado "
                          "Cancelado no Sheets — só o registro de detalhe falhou).", venda)

    logger.info("[%s] Pedido %s cancelado — Status=Cancelado.", company_key, order_id)
    return {"status": "cancelled", "venda": venda, "order_id": order_id}


async def process_order_notification(store: dict, order_id: str) -> dict:
    company_key = store["company_key"]
    account_name = store.get("nickname", "")
    token_store = TokenStore()
    meli = MeliService(store, token_store=token_store)

    try:
        order = await meli.get_order(order_id)
    except Exception as e:
        logger.error("Erro ao buscar pedido %s: %s", order_id, e)
        return {"status": "error", "order_id": order_id, "detail": str(e)}

    # Checado ANTES dos dedups abaixo de propósito: um pedido cancelado pode já
    # ter sido processado (Separando/Separado/Embalado) antes do cancelamento —
    # o dedup existente ("already_processed"/"already_claimed") faria essa
    # notificação nova ser ignorada e o cancelamento nunca apareceria.
    if order.get("status") == "cancelled":
        return await _handle_cancelamento(company_key, order_id, order)

    # Dedup 1: pedido já registrado (cobre pedidos antigos)
    if order_id in await vendas_db.get_sale_order_ids():
        return {"status": "already_processed", "order_id": order_id}
    # Dedup 2: reserva ATÔMICA no Postgres (à prova de webhooks concorrentes/reenvios)
    if not await token_store.claim_order(order_id):
        return {"status": "already_claimed", "order_id": order_id}

    if order.get("status") not in ("paid", "payment_required"):
        return {"status": "skipped", "order_status": order.get("status")}

    addresses = await enderecos_db.get_addresses()
    deadline = await meli.get_delivery_deadline(order)
    fiscal = _fiscal_cols(await meli.get_fiscal_summary(order))
    venda_ml = str(order.get("pack_id") or order_id)   # nº que aparece no painel do ML
    order_items = OrderItem.from_meli_order(order, company_key, account_name=account_name, deadline=deadline)
    empresa = order_items[0].to_sheet_row()[0] if order_items else (account_name or company_key)
    ddmmaa = _ddmmaa_br(order.get("date_created", "")) or datetime.now().strftime("%d%m%y")
    origem = await meli.detectar_origem(order)
    exped_id = await vendas_db.get_or_create_venda_and_expedition_id(
        venda_ml, empresa, deadline, _sigla(store), ddmmaa, origem)   # ID físico p/ etiqueta/gaiola

    itens = [{"sku": item.sku, "nome": item.product_name, "qtd": item.quantity,
              "endereco": addresses.get(item.sku, "Sem endereço")} for item in order_items]
    await vendas_db.registrar_pedido(venda_ml, order_id, empresa, itens, fiscal)

    logger.info("[%s] Pedido %s → %d item(s) registrado(s)", company_key, order_id, len(order_items))
    return {"status": "processed", "order_id": order_id, "items": len(order_items)}


# Status do shipment que significam "o ML já tirou isso do CD dele".
_SHIPMENT_DESPACHADO = ("shipped", "delivered")


async def espelhar_envio_full(pedido: dict, sh: dict, company_key: str = "") -> bool:
    """Marca um pedido Full como "Enviado" quando o ML já despachou.

    Pedido Full não passa pelo nosso fluxo físico (separação/embalagem/gaiola), então
    nada aqui dentro jamais mudaria o status dele — ficava "Separando" pra sempre na
    auditoria do Gerente, mesmo com a mercadoria já a caminho. Quem despacha é o ML;
    isto espelha esse fato. Idempotente e restrito a Full: pedido de Expedição só vira
    "Enviado" pela coleta da gaiola ou pela correção manual, como sempre."""
    if pedido.get("origem") != ORIGEM_FULL:
        return False
    if sh.get("status") not in _SHIPMENT_DESPACHADO:
        return False
    if (pedido.get("status") or "").strip().lower() == "enviado":
        return False
    await vendas_db.set_status_for_venda(pedido["venda"], "Enviado")
    logger.info("[%s] Venda %s (Full): ML despachou (%s) -> status Enviado",
                company_key, pedido["venda"], sh.get("status"))
    return True


async def process_shipment_notification(store: dict, shipping_id: str) -> dict:
    """Webhook topic 'shipments'. Corrige DUAS coisas que na criação do pedido
    (process_order_notification) normalmente ainda não dá pra saber:

    1. `data_limite` — o SLA de despacho (/shipments/{id}/sla) costuma responder 404 na
       criação (o envio ainda não amadureceu o suficiente pro Mercado Envios calcular o
       prazo) e `get_delivery_deadline` cai no fallback (estimativa de ENTREGA ao
       comprador, semanas depois), gravando um prazo de despacho errado.
    2. `origem` — quando a detecção de Full não concluiu, a venda ficou ORIGEM_INDEFINIDA
       (ver `MeliService.detectar_origem`). O shipment já foi buscado logo acima, então a
       reclassificação sai sem nenhuma chamada extra ao ML. Origem já resolvida como Full
       nunca é reavaliada — isso não volta atrás.
    3. `status` — pedido Full que o ML já despachou vira "Enviado" (ver
       `espelhar_envio_full`), já que ele nunca passa pelo nosso fluxo físico.

    O ML dispara vários eventos de shipment por pedido, então uma venda indefinida se
    resolve sozinha em minutos. Idempotente."""
    meli = MeliService(store, token_store=TokenStore())
    try:
        sh = await meli.get_shipment(shipping_id)
    except Exception as e:
        return {"status": "error", "shipping_id": shipping_id, "detail": str(e)}

    order_id = str(sh.get("order_id") or "")
    if not order_id:
        return {"status": "sem_order_id", "shipping_id": shipping_id}

    pedido = await vendas_db.get_pedido_by_order_id(order_id)
    if not pedido:
        return {"status": "pedido_nao_encontrado", "shipping_id": shipping_id, "order_id": order_id}

    mudancas: dict = {}
    origem_atual = pedido["origem"]
    if origem_atual != ORIGEM_FULL:
        origem_nova = meli.origem_do_shipment(sh)
        if origem_nova != origem_atual:
            await vendas_db.set_origem(pedido["venda"], origem_nova)
            logger.info("[%s] Venda %s: origem %s -> %s",
                        store["company_key"], pedido["venda"], origem_atual, origem_nova)
            mudancas["origem"] = {"antes": origem_atual, "depois": origem_nova}
            pedido["origem"] = origem_nova   # o dict foi lido antes do update

    if await espelhar_envio_full(pedido, sh, store["company_key"]):
        mudancas["status"] = {"antes": pedido["status"], "depois": "Enviado"}

    deadline = await meli.get_delivery_deadline({"shipping": {"id": shipping_id}})
    if deadline and deadline != pedido["data_limite"]:
        await vendas_db.set_data_limite(pedido["venda"], deadline)
        logger.info("[%s] Venda %s: prazo de despacho corrigido %s -> %s",
                    store["company_key"], pedido["venda"], pedido["data_limite"], deadline)
        mudancas["data_limite"] = {"antes": pedido["data_limite"], "depois": deadline}

    if not mudancas:
        return {"status": "sem_mudanca", "venda": pedido["venda"],
                "data_limite": pedido["data_limite"], "origem": origem_atual}
    return {"status": "corrigido", "venda": pedido["venda"], **mudancas}


async def process_claim_notification(store: dict, claim_id: str) -> dict:
    """Fase 1 (2026-07-22): registra QUALQUER claim recebido em `devolucoes` (Postgres),
    sem alterar o Status na aba Vendas — a venda já está "Enviado" a essa altura, e uma
    devolução não deve reabrir o fluxo de separação/embalagem. Guarda o payload bruto
    (`raw`) porque a Claims API não está 100% documentada/verificada ainda; o filtro por
    tipo/estágio (ex.: só devolução física, não mediação genérica) fica pra depois, quando
    tivermos um caso real pra confirmar o formato.

    Idempotente — o ML pode reenviar o mesmo webhook mais de uma vez (claim_id é UNIQUE).
    """
    company_key = store["company_key"]
    token_store = TokenStore()
    meli = MeliService(store, token_store=token_store)

    try:
        claim = await meli.get_claim(claim_id)
    except Exception as e:
        logger.error("Erro ao buscar claim %s: %s", claim_id, e)
        return {"status": "error", "claim_id": claim_id, "detail": str(e)}

    order_id = str(claim.get("resource_id") or "")
    pedido = await vendas_db.get_pedido_by_order_id(order_id) if order_id else None
    venda = pedido["venda"] if pedido else None
    # Empresa tem que bater com o valor real da coluna "Empresa" na planilha (o
    # nickname da conta ML, ex.: "USER4_BRASIL") — não o company_key interno
    # (ex.: "User4"), senão vira um filtro "loja" duplicado/incoerente.
    empresa = pedido["empresa"] if pedido else (store.get("nickname") or company_key)

    reason_id = claim.get("reason_id")
    motivo = await meli.get_claim_reason(reason_id) if reason_id else None
    resolucao = claim.get("resolution") or None
    if resolucao:
        quem = {"complainant": "comprador", "respondent": "vendedor"}
        beneficiados = ", ".join(quem.get(b, b) for b in (resolucao.get("benefited") or []))
        motivo = f"{motivo} (resolvido a favor de: {beneficiados or '—'})"
    data_evento = _parse_ml_datetime(claim.get("date_created"))

    try:
        await db_service.registrar_devolucao(
            claim_id=claim_id, venda_ml=venda, order_id=order_id or None, empresa=empresa,
            tipo=claim.get("type"), stage=claim.get("stage"), motivo=motivo,
            data_evento=data_evento, raw=claim, status_ml=claim.get("status"),
        )
    except Exception:
        logger.exception("Falha ao registrar devolução (claim %s) no Postgres.", claim_id)
        return {"status": "error", "claim_id": claim_id, "detail": "falha ao gravar no Postgres"}

    logger.info("[%s] Claim %s registrado (order_id=%s, venda=%s).", company_key, claim_id, order_id, venda)
    return {"status": "registered", "claim_id": claim_id, "order_id": order_id, "venda": venda}


async def send_fiscal_to_meli(company_key: str | None = None) -> dict:
    """Envia ao ML os dados fiscais preenchidos na aba Fiscal (PUT por SKU).

    Agrupa por loja (coluna Loja), reaproveita a 'Regra Basica' da loja e grava
    o resultado (✅/❌) na coluna Status de cada linha. Só envia linhas com NCM.
    """
    sheets = SheetsService()
    token_store = TokenStore()
    rows = sheets.get_fiscal_rows()
    if not rows:
        return {"enviados": 0, "erros": 0, "ignorados": 0}

    # Cada SKU é enviado a TODAS as lojas que o têm (fiscal "todo mundo junto").
    por_loja: dict[str, list] = defaultdict(list)
    ignorados = 0
    for r in rows:
        if not r["ncm"]:        # NCM é obrigatório p/ o ML; linha incompleta é ignorada
            ignorados += 1
            continue
        for loja in (r["lojas"] or []):
            if company_key and loja != company_key and loja.upper() != company_key.upper():
                continue
            por_loja[loja].append(r)

    when = datetime.now().strftime("%d/%m/%Y %H:%M")
    enviados = erros = 0
    resultados: dict[int, list[str]] = defaultdict(list)   # row_num → ["✅ LOJA", "❌ LOJA: msg"]

    for loja, items in por_loja.items():
        store = await token_store.get_by_company(loja) or await token_store.get_by_company(loja.upper())
        if not store:
            for it in items:
                resultados[it["row_num"]].append(f"❌ {loja}: não conectada")
                erros += 1
            continue
        meli = MeliService(store, token_store=token_store)
        default_trid = await meli.find_tax_rule_id([it["sku"] for it in items])
        for it in items:
            res = await meli.send_fiscal(
                it["sku"], ncm=it["ncm"], origin_detail=_origem_code(it["origem"]),
                cest=it["cest"], ean=it["ean"], default_tax_rule_id=default_trid,
            )
            if res["ok"]:
                resultados[it["row_num"]].append(f"✅ {loja}")
                enviados += 1
            else:
                resultados[it["row_num"]].append(f"❌ {loja}: {res['message'][:80]}")
                erros += 1

    for row_num, partes in resultados.items():
        sheets.set_fiscal_status(row_num, " | ".join(partes), when)

    logger.info("send_fiscal: %d enviados, %d erros, %d ignorados", enviados, erros, ignorados)
    return {"enviados": enviados, "erros": erros, "ignorados": ignorados}


async def refresh_pending_fiscal(company_key: str | None = None) -> dict:
    """Preenche a NF de vendas que ainda não tinham nota emitida na hora do pedido.

    A NF do ML costuma sair minutos após a venda; este job varre os pedidos sem
    chave de acesso (e não enviados) e tenta buscar a NF de novo.
    """
    token_store = TokenStore()

    pendentes = await vendas_db.get_sales_needing_fiscal()
    if not pendentes:
        return {"pendentes": 0, "preenchidos": 0}

    # cache de MeliService por empresa (evita recriar cliente por linha)
    meli_por_empresa: dict[str, MeliService] = {}
    preenchidos = 0

    for empresa, order_id in pendentes:
        if company_key and empresa != company_key and empresa.upper() != company_key.upper():
            continue
        meli = meli_por_empresa.get(empresa)
        if meli is None:
            store = await token_store.get_by_company(empresa) or await token_store.get_by_company(empresa.upper())
            if not store:
                continue
            meli = MeliService(store, token_store=token_store)
            meli_por_empresa[empresa] = meli
        try:
            order = await meli.get_order(order_id)
            fiscal = await meli.get_fiscal_summary(order)
        except Exception as e:
            logger.warning("refresh_fiscal: pedido %s falhou: %s", order_id, e)
            continue
        if fiscal.get("nf_chave") or fiscal.get("comprador"):
            await vendas_db.set_fiscal_for_order(order_id, _fiscal_cols(fiscal))
            preenchidos += 1

    logger.info("refresh_fiscal: %d pendentes, %d preenchidos", len(pendentes), preenchidos)
    return {"pendentes": len(pendentes), "preenchidos": preenchidos}
