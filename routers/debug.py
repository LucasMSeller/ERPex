"""Rotas de manutenção pontual (rodadas manualmente via browser/curl)."""
from fastapi import APIRouter, Query, HTTPException
from services.sheets_service import SheetsService, VENDAS_DATA_START_ROW
from services.token_store import TokenStore
from services.meli_service import MeliService, ORIGEM_FULL
from services import enderecos_db
from services import vendas_db
from models.product import Product

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/sku/{mlb_id}")
async def sku_sources(mlb_id: str, company: str = Query(...)):
    """Mostra ONDE está (ou não) o SKU de um anúncio — p/ achar SKU que cai no MLB."""
    store = await TokenStore().get_by_company(company)
    if not store:
        raise HTTPException(404, f"Loja '{company}' não conectada.")
    meli = MeliService(store, token_store=TokenStore())
    body = await meli._get(f"/items/{mlb_id}")
    attrs = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}
    cache: dict = {}
    variacoes = []
    for v in body.get("variations") or []:
        va = {a["id"]: a.get("value_name") for a in v.get("attributes", [])}
        upid = v.get("user_product_id")
        variacoes.append({
            "variation_id": v.get("id"),
            "seller_sku": v.get("seller_sku"),
            "seller_custom_field": v.get("seller_custom_field"),
            "SELLER_SKU_attr": va.get("SELLER_SKU"),
            "GTIN_attr": va.get("GTIN"),
            "user_product_id": upid,
            "user_product_SELLER_SKU": await meli._user_product_sku(str(upid), cache) if upid else None,
        })
    prod = Product.from_meli_item(body, store["company_key"])
    return {
        "mlb": mlb_id,
        "resultado_from_meli_item": {"sku": prod.sku, "sku_is_real": prod.sku_is_real},
        "item_seller_custom_field": body.get("seller_custom_field"),
        "item_seller_sku": body.get("seller_sku"),
        "item_SELLER_SKU_attr": attrs.get("SELLER_SKU"),
        "item_resolved_sku": body.get("_resolved_sku"),
        "tem_variacoes": bool(body.get("variations")),
        "variacoes": variacoes,
    }


@router.get("/clean-enderecamento")
async def clean_enderecamento_now():
    """Remove do Endereçamento as linhas de MLB de fallback (anúncios sem SKU real)."""
    return {"linhas_removidas": await enderecos_db.remove_mlb_addresses()}


@router.get("/migrate-enderecos")
async def migrate_enderecos_now():
    """Migração pontual (fase 1 Sheets->Postgres): copia o Endereçamento do Sheets
    pro Postgres. Só LÊ o Sheets, escreve numa tabela nova/vazia — remover esta
    rota depois que a migração for confirmada e cortada."""
    linhas = SheetsService().get_addresses_full("")
    await enderecos_db.bulk_upsert(linhas)

    pg = await enderecos_db.get_addresses_full("")
    pg_por_sku = {e["sku"]: e for e in pg}
    divergencias = []
    for e in linhas:
        pg_e = pg_por_sku.get(e["sku"])
        if pg_e is None:
            divergencias.append({"sku": e["sku"], "problema": "faltando no Postgres"})
            continue
        for campo in ("corredor", "estante", "prateleira"):
            if (e.get(campo) or "") != (pg_e.get(campo) or ""):
                divergencias.append({"sku": e["sku"], "campo": campo,
                                      "sheets": e.get(campo), "postgres": pg_e.get(campo)})

    return {
        "sheets_linhas": len(linhas), "postgres_linhas": len(pg),
        "contagem_bate": len(linhas) == len(pg),
        "divergencias": divergencias,
    }


@router.get("/migrate-lojas")
async def migrate_lojas_now():
    """Migração pontual (Credenciais Firestore->Postgres): copia os tokens de
    cada loja já conectada pra tabela `lojas`. Upsert (idempotente, pode rodar
    de novo sem duplicar) — remover esta rota depois que a migração for
    confirmada e as lojas continuarem funcionando 100% pelo Postgres."""
    from google.cloud import firestore
    from config.settings import get_settings

    settings = get_settings()
    kwargs = {"database": settings.firestore_database}
    if settings.gcp_project:
        kwargs["project"] = settings.gcp_project
    if settings.google_service_account_json.endswith(".json"):
        client = firestore.Client.from_service_account_json(settings.google_service_account_json, **kwargs)
    else:
        client = firestore.Client(**kwargs)
    col = client.collection(settings.firestore_collection)

    token_store = TokenStore()
    migradas = []
    for doc in col.stream():
        s = doc.to_dict()
        user_id = s.get("user_id") or doc.id
        await token_store.save_store(
            user_id=user_id,
            company_key=s.get("company_key", ""),
            sheet_tab=s.get("sheet_tab", ""),
            access_token=s.get("access_token", ""),
            refresh_token=s.get("refresh_token", ""),
            nickname=s.get("nickname", ""),
        )
        if s.get("cor"):
            await token_store.set_color(user_id, s["cor"])
        if s.get("sku_prefixo"):
            await token_store.set_sku_prefixo(user_id, s["sku_prefixo"])
        migradas.append({"user_id": user_id, "company_key": s.get("company_key"), "nickname": s.get("nickname")})

    return {"lojas_migradas": len(migradas), "detalhe": migradas}


@router.get("/vincular-skus")
async def vincular_skus_now(company: str | None = Query(None), sku_prefixo: str | None = Query(None)):
    """Reconcilia o Endereçamento com os SKUs reais atuais (mesma ação do botão
    "Vincular SKUs" em /enderecos). Sem `company`: todas as lojas. Com `company`
    (+ opcionalmente `sku_prefixo`): só 1 loja, mesmo escopo do botão por loja."""
    from services.sync_service import reconciliar_enderecos
    return await reconciliar_enderecos(company_key=company, sku_prefixo=sku_prefixo)


@router.get("/refresh-fiscal")
async def refresh_fiscal_now(company: str | None = Query(None)):
    """Preenche a NF (J..O) das vendas pendentes lendo o ML agora."""
    from services.sync_service import refresh_pending_fiscal
    return await refresh_pending_fiscal(company)


def _corrigir_ids_numericos(rows: list[list], rows_raw: list[list]) -> list[list]:
    """order_id (col G, índice 6) e Nº venda ML (col P, índice 15) podem ter sido
    gravados em célula formatada como NÚMERO (não TEXTO) — a leitura formatada
    (`_read`) então trunca IDs de 16 dígitos em notação científica
    ("2,00002E+15"), colapsando vários pedidos DIFERENTES no mesmo valor
    corrompido. Corrige usando a leitura SEM formatação (valor numérico exato —
    float de 64 bits é exato pra 16 dígitos) nessas 2 colunas específicas."""
    corrigidas = []
    for r, r_raw in zip(rows, rows_raw):
        p = list(r)
        p_raw = list(r_raw)
        for idx in (6, 15):
            if idx < len(p_raw) and isinstance(p_raw[idx], (int, float)):
                while len(p) <= idx:
                    p.append("")
                p[idx] = str(int(p_raw[idx]))
        corrigidas.append(p)
    return corrigidas


@router.get("/migrate-vendas")
async def migrate_vendas_now():
    """Migração pontual (Vendas Sheets->Postgres): reconstrói vendas/venda_orders/
    venda_itens a partir da aba Vendas crua. Só LÊ o Sheets, upsert idempotente no
    Postgres — pode rodar de novo com segurança (ex.: logo antes/depois do deploy
    do corte, pra pegar vendas que entraram na janela entre as 2 execuções)."""
    sheets = SheetsService()
    range_ = f"'Vendas'!A{VENDAS_DATA_START_ROW}:S"
    rows = sheets._read(range_)
    rows_raw = sheets._read_unformatted(range_)
    rows = _corrigir_ids_numericos(rows, rows_raw)
    limpeza = await vendas_db.purge_ids_corrompidos()
    resultado = await vendas_db.bulk_migrate(rows)

    pg_vendas = await vendas_db.count_vendas()
    pg_orders = await vendas_db.count_orders()
    pg_itens = await vendas_db.count_itens()

    return {
        **resultado,
        "limpeza_ids_corrompidos": limpeza,
        "postgres_vendas": pg_vendas, "postgres_orders": pg_orders, "postgres_itens": pg_itens,
    }


@router.get("/detect-full-orders")
async def detect_full_orders_now():
    """Rede de segurança manual da classificação de origem (Expedição/Full).

    O caminho normal é automático: `detectar_origem` na criação do pedido e o webhook de
    shipments reavaliando o que ficou 'Em análise'. Esta rota varre TODAS as vendas e
    reclassifica de uma vez — útil pra corrigir registros antigos (os migrados do Sheets
    entraram todos com o padrão 'Expedição') ou pra destravar algo que ficou 'Em análise'
    porque os webhooks daquele pedido não vieram. Idempotente: só grava quando o ML
    responde algo diferente do que está gravado, e nunca tira um pedido de 'Full'.
    Aproveita a mesma consulta pra espelhar o envio dos Full já despachados pelo ML
    (ver `sync_service.espelhar_envio_full`)."""
    from services.sync_service import espelhar_envio_full
    token_store = TokenStore()
    pedidos = await vendas_db.get_all_pedidos(incluir_em_classificacao=True)
    meli_cache: dict[str, MeliService] = {}
    reclassificados = []
    marcados_enviados = []
    erros = []
    for p in pedidos:
        empresa = p["empresa"]
        meli = meli_cache.get(empresa)
        if meli is None:
            store = await token_store.get_by_company_or_nickname(empresa)
            if not store:
                erros.append({"venda": p["venda"], "erro": f"loja '{empresa}' não encontrada"})
                continue
            meli = MeliService(store, token_store=token_store)
            meli_cache[empresa] = meli
        try:
            _order, shipping_id = await meli.resolve_order_and_shipping(p["venda"])
            if not shipping_id:
                # Sem envio atribuído não há como classificar; mantém o que está gravado.
                continue
            sh = await meli.get_shipment(shipping_id)
            origem = meli.origem_do_shipment(sh)
            if origem != p["origem"] and p["origem"] != ORIGEM_FULL:
                await vendas_db.set_origem(p["venda"], origem)
                reclassificados.append({"venda": p["venda"], "antes": p["origem"], "depois": origem})
                p["origem"] = origem
            if await espelhar_envio_full(p, sh, empresa):
                marcados_enviados.append(p["venda"])
        except Exception as e:
            erros.append({"venda": p["venda"], "erro": str(e)})

    return {"total_verificados": len(pedidos), "reclassificados": reclassificados,
            "marcados_enviados": marcados_enviados, "erros": erros}


@router.get("/shipment-info/{shipping_id}")
async def shipment_info_now(shipping_id: str):
    """Consulta pontual (só leitura): acha o order_id de um shipping_id (pra achar
    o pedido nosso quando só temos o shipping_id, ex.: visto num log de erro)."""
    token_store = TokenStore()
    for s in await token_store.list_stores():
        meli = MeliService(s, token_store=token_store)
        try:
            sh = await meli.get_shipment(shipping_id)
        except Exception:
            continue
        order_id = str(sh.get("order_id") or "")
        pedido = await vendas_db.get_pedido_by_order_id(order_id) if order_id else None
        return {"empresa": s["company_key"], "order_id": order_id, "pedido": pedido,
                "receiver_name": (sh.get("receiver_address") or {}).get("receiver_name")}
    raise HTTPException(404, f"Shipping_id '{shipping_id}' não encontrado em nenhuma loja conectada.")


@router.get("/investigar-envio/{numero}")
async def investigar_envio_now(numero: str):
    """Consulta pontual (só leitura, nada é gravado): junta o registro nosso
    (Postgres) com order/shipment/SLA do ML pra investigar uma reclamação de
    prazo/tamanho de entrega. `numero` aceita id_exped (ex. PLG300726001),
    venda (pack_id) ou order_id — resolvido na mesma ordem que o painel usa."""
    pedido = await vendas_db.get_pedido_by_exped(numero) or await vendas_db.get_pedido_by_venda(numero)
    if not pedido:
        pedido = await vendas_db.get_pedido_by_order_id(numero)

    numero_ml = pedido["venda"] if pedido else numero
    empresa = pedido["empresa"] if pedido else None

    token_store = TokenStore()
    store = None
    if empresa:
        store = await token_store.get_by_company_or_nickname(empresa)
    if not store:
        for s in await token_store.list_stores():
            try:
                meli = MeliService(s, token_store=token_store)
                order, shipping_id = await meli.resolve_order_and_shipping(numero_ml)
                store = s
                break
            except Exception:
                continue
        else:
            raise HTTPException(404, f"Não achei o pedido '{numero}' em nenhuma loja conectada.")
    else:
        meli = MeliService(store, token_store=token_store)
        order, shipping_id = await meli.resolve_order_and_shipping(numero_ml)

    resultado = {
        "nosso_registro": pedido,
        "order": {
            "id": order.get("id"),
            "status": order.get("status"),
            "date_created": order.get("date_created"),
            "date_closed": order.get("date_closed"),
            "pack_id": order.get("pack_id"),
            "shipping_id": shipping_id,
        },
        "shipment": None,
        "sla": None,
        "history": None,
    }
    if shipping_id:
        try:
            resultado["shipment"] = await meli.get_shipment(shipping_id)
        except Exception as e:
            resultado["shipment_erro"] = str(e)
        try:
            resultado["sla"] = await meli._get(f"/shipments/{shipping_id}/sla")
        except Exception as e:
            resultado["sla_erro"] = str(e)
        try:
            resultado["history"] = await meli._get(f"/shipments/{shipping_id}/history")
        except Exception as e:
            resultado["history_erro"] = str(e)

    order_id_str = str(order.get("id", ""))
    from services import db as db_service
    conn = await db_service._get_connection()
    try:
        cancelamento = await conn.fetchrow(
            "SELECT * FROM cancelamentos WHERE order_id = $1 OR venda_ml = $2", order_id_str, numero_ml)
        devolucao = await conn.fetchrow(
            "SELECT * FROM devolucoes WHERE order_id = $1 OR venda_ml = $2", order_id_str, numero_ml)
    finally:
        await conn.close()
    resultado["cancelamento_registrado"] = dict(cancelamento) if cancelamento else None
    resultado["devolucao_registrada"] = dict(devolucao) if devolucao else None

    try:
        user_id = await meli.get_user_id()
        resultado["claims_search"] = await meli._get(
            "/post-purchase/v1/claims/search",
            {"resource": "order", "resource_id": order_id_str, "player_id": user_id, "player_role": "respondent"},
        )
    except Exception as e:
        resultado["claims_search_erro"] = str(e)
    return resultado


@router.get("/backfill-prazo-despacho")
async def backfill_prazo_despacho_now():
    """Migração pontual: até 2026-07-31, get_delivery_deadline() calculava o prazo
    de despacho na hora da criação do pedido — quando o SLA do Mercado Envios
    (/shipments/{id}/sla) costuma responder 404 (envio ainda não maduro pra
    calcular), caindo no fallback (estimativa de ENTREGA ao comprador, semanas
    depois) e gravando um `data_limite` errado. Corrigido daqui pra frente via
    webhook 'shipments' (process_shipment_notification); esta rota corrige os
    pedidos AINDA ABERTOS (Separando/Separado/Embalado) que já foram afetados —
    pedidos Enviados não são tocados (prazo de despacho não importa mais neles).
    Idempotente — pode rodar de novo com segurança."""
    token_store = TokenStore()
    pedidos = await vendas_db.get_all_pedidos(status_filter=["Separando", "Separado", "Embalado"],
                                              incluir_em_classificacao=True)
    meli_cache: dict[str, MeliService] = {}
    corrigidos = []
    sem_mudanca = 0
    erros = []
    for p in pedidos:
        empresa = p["empresa"]
        meli = meli_cache.get(empresa)
        if meli is None:
            store = await token_store.get_by_company_or_nickname(empresa)
            if not store:
                erros.append({"venda": p["venda"], "erro": f"loja '{empresa}' não encontrada"})
                continue
            meli = MeliService(store, token_store=token_store)
            meli_cache[empresa] = meli
        try:
            order, shipping_id = await meli.resolve_order_and_shipping(p["venda"])
            if not shipping_id:
                erros.append({"venda": p["venda"], "erro": "pedido sem shipping_id"})
                continue
            deadline = await meli.get_delivery_deadline({"shipping": {"id": shipping_id}})
            if not deadline or deadline == p["data_limite"]:
                sem_mudanca += 1
                continue
            await vendas_db.set_data_limite(p["venda"], deadline)
            corrigidos.append({"venda": p["venda"], "antigo": p["data_limite"], "novo": deadline})
        except Exception as e:
            erros.append({"venda": p["venda"], "erro": str(e)})

    return {"total_verificados": len(pedidos), "corrigidos": corrigidos, "sem_mudanca": sem_mudanca, "erros": erros}


@router.get("/backfill-criado-em")
async def backfill_criado_em_now():
    """Migração pontual: os pedidos trazidos da aba Vendas ganharam `criado_em`
    igual ao horário da MIGRAÇÃO (não da venda de verdade — o Sheets nunca
    guardou isso). Esta rota consulta o ML e corrige `criado_em` pra
    `date_created` real de cada pedido, pro filtro "Criado em" do Gerente
    funcionar também pra vendas antigas. Idempotente — pode rodar de novo."""
    from datetime import datetime
    token_store = TokenStore()
    pedidos = await vendas_db.get_all_pedidos(incluir_em_classificacao=True)
    meli_cache: dict[str, MeliService] = {}
    corrigidos = []
    erros = []
    for p in pedidos:
        empresa = p["empresa"]
        meli = meli_cache.get(empresa)
        if meli is None:
            store = await token_store.get_by_company_or_nickname(empresa)
            if not store:
                erros.append({"venda": p["venda"], "erro": f"loja '{empresa}' não encontrada"})
                continue
            meli = MeliService(store, token_store=token_store)
            meli_cache[empresa] = meli
        try:
            order, _shipping_id = await meli.resolve_order_and_shipping(p["venda"])
            date_created = order.get("date_created")
            if not date_created:
                erros.append({"venda": p["venda"], "erro": "pedido sem date_created"})
                continue
            criado_em = datetime.fromisoformat(date_created.replace("Z", "+00:00"))
            await vendas_db.set_criado_em(p["venda"], criado_em)
            corrigidos.append(p["venda"])
        except Exception as e:
            erros.append({"venda": p["venda"], "erro": str(e)})

    return {"total_verificados": len(pedidos), "corrigidos": len(corrigidos), "erros": erros}
