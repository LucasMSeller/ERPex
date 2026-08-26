"""Rotas de manutenção pontual, abertas pelo navegador já logado como Gerente.

Exigem a senha do Gerente desde 11/08/2026. Antes eram públicas — e isso incluía
`/investigar-envio`, que devolve nome e documento do comprador, e rotas que APAGAM
e alteram dados de produção. Como efeito colateral deliberado, deixaram de poder
ser chamadas por `curl` sem sessão: manutenção aqui passa a exigir alguém logado.
"""
from fastapi import APIRouter, Query, HTTPException, Depends
from services.sheets_service import SheetsService, VENDAS_DATA_START_ROW
from services.token_store import TokenStore
from services.meli_service import MeliService, ORIGEM_FULL
from services.session_auth import require_gerente
from services.sync_service import process_claim_notification
from services import enderecos_db
from services import vendas_db
from models.product import Product

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[Depends(require_gerente)])


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


@router.get("/conferir-vendas")
async def conferir_vendas_now(horas: int = Query(3, description="Janela em horas"),
                              importar: bool = Query(True, description="Importar o que faltar")):
    """Confere pedido a pedido se o ML tem venda paga que não chegou aqui, e importa
    o que faltar (ver `sync_service.conferir_vendas_recentes`). `importar=false` só
    lista, sem gravar nada."""
    from services.sync_service import conferir_vendas_recentes
    return await conferir_vendas_recentes(horas, importar)


@router.post("/reprocessar-pedido")
async def reprocessar_pedido_now(order_id: str = Query(..., description="order_id do ML")):
    """Reprocessa um pedido que se perdeu — o webhook chegou, falhou (tipicamente
    queda de conexão com o banco) e o Mercado Livre desistiu de reenviar.

    Libera a reserva de `claim_order` antes, senão o próprio anti-duplicata bloqueia
    a segunda tentativa. Descobre a loja sozinho, testando as conectadas: só a dona
    do pedido consegue lê-lo na API. Idempotente — se a venda já existir, o dedup
    devolve "already_processed" sem duplicar nada."""
    from services.sync_service import process_order_notification
    token_store = TokenStore()
    for store in await token_store.list_stores():
        meli = MeliService(store, token_store=token_store)
        try:
            await meli.get_order(order_id)
        except Exception:
            continue   # pedido não é desta loja
        await token_store.liberar_order(order_id)
        resultado = await process_order_notification(store, order_id)
        return {"loja": store["company_key"], "resultado": resultado}
    raise HTTPException(404, f"Pedido '{order_id}' não encontrado em nenhuma loja conectada.")


@router.post("/remover-vendas")
async def remover_vendas_now(exped: list[str] = Query(..., description="IDs de expedição a remover")):
    """Apaga vendas informadas explicitamente, por ID de expedição — só pra desfazer
    importação indevida (ver `vendas_db.remover_vendas_por_exped`). Recebe a lista
    exata, nunca um critério, justamente pra não haver "apagou mais do que devia".
    Irreversível."""
    return await vendas_db.remover_vendas_por_exped(exped)


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
            # Produto sob encomenda empurra o despacho: o anúncio declara N dias de
            # fabricação e o pedido ganha a data final. Suspeita nº 2 pra origem da
            # janela de despacho (a doc não descreve nenhum dos dois campos).
            "manufacturing_ending_date": order.get("manufacturing_ending_date"),
            "manufacturing_days": [i.get("manufacturing_days") for i in (order.get("order_items") or [])],
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
        # Tudo que o ML tem sobre PRAZO DE DESPACHO deste envio, junto, pra achar de
        # onde sai a janela "de tal dia até tal dia" que alguns anúncios mostram — a
        # doc que temos cita `pickup_promise` e `estimated_schedule_limit` só no JSON
        # de exemplo, sem explicar nenhum dos dois. Com um pedido real na mão dá pra
        # ver qual campo vem preenchido.
        try:
            resultado["lead_time"] = await meli._get(
                f"/shipments/{shipping_id}/lead_time", headers={"x-format-new": "true"})
        except Exception as e:
            resultado["lead_time_erro"] = str(e)
        try:
            sh_novo = await meli._get(f"/shipments/{shipping_id}",
                                       headers={"x-format-new": "true"})
            resultado["shipment_formato_novo"] = {
                "lead_time": sh_novo.get("lead_time"),
                "logistic": sh_novo.get("logistic"),
                "tem_order_id": "order_id" in sh_novo,
            }
        except Exception as e:
            resultado["shipment_formato_novo_erro"] = str(e)

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

    Desde 20/08/2026 também LIMPA: quando o ML responde 404 no SLA (Full, envio
    cancelado), a data que estiver lá só pode ser legado do fallback — a entrega ao
    comprador gravada como se fosse despacho. Só o 404 apaga; timeout ou 500 não
    tocam em nada, porque falha de consulta não é informação sobre o prazo.

    Idempotente — pode rodar de novo com segurança."""
    token_store = TokenStore()
    import httpx
    from services.meli_service import _fmt_date_br
    pedidos = await vendas_db.get_all_pedidos(status_filter=["Separando", "Separado", "Embalado"],
                                              incluir_em_classificacao=True)
    meli_cache: dict[str, MeliService] = {}
    corrigidos = []
    limpos = []
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
            try:
                sla = await meli._get(f"/shipments/{shipping_id}/sla")
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                # 404 = o ML não tem prazo de despacho pra este envio (Full, cancelado).
                # Uma data aqui só pode ser legado do fallback antigo, que gravava a
                # ENTREGA ao comprador. Apagar é o que alinha o registro com a regra
                # atual: ou é prazo de despacho de verdade, ou o campo fica vazio.
                if (p["data_limite"] or "").strip():
                    await vendas_db.set_data_limite(p["venda"], "")
                    limpos.append({"venda": p["venda"], "origem": p.get("origem"),
                                    "data_removida": p["data_limite"]})
                else:
                    sem_mudanca += 1
                continue
            deadline = _fmt_date_br(sla.get("expected_date") or "")
            if not deadline or deadline == p["data_limite"]:
                sem_mudanca += 1
                continue
            await vendas_db.set_data_limite(p["venda"], deadline)
            corrigidos.append({"venda": p["venda"], "antigo": p["data_limite"], "novo": deadline})
        except Exception as e:
            # Erro que não seja 404 (timeout, 500) NÃO apaga nem sobrescreve nada:
            # falha de consulta não é informação sobre o prazo.
            erros.append({"venda": p["venda"], "erro": str(e)[:200]})

    return {"total_verificados": len(pedidos), "corrigidos": corrigidos,
            "datas_removidas": limpos, "sem_mudanca": sem_mudanca, "erros": erros}


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


@router.get("/conferir-prazos")
async def conferir_prazos_now(limite: int = Query(300, description="Máximo de pedidos verificados")):
    """SÓ LEITURA (não grava nada): compara, pedido aberto por pedido aberto, o
    `data_limite` que está no nosso painel com o prazo de despacho que o ML diz
    AGORA (/shipments/{id}/sla → expected_date).

    Existe porque um prazo nosso maior que o do ML atrasa a entrega em silêncio:
    o pedido cai na aba "Próximos envios" do Mural em vez de "Hoje", e o gatilho
    de NF-e (que só dispara quando `data_limite <= hoje`) nunca roda. Hoje o prazo
    só se corrige quando chega um webhook 'shipments' — esta rota mostra quantos
    estão fora enquanto esse webhook não chega. Para CORRIGIR, use
    /debug/backfill-prazo-despacho."""
    from datetime import date, datetime as _dt
    from services.meli_service import _fmt_date_br

    def _dias(br: str) -> date | None:
        try:
            dia, mes, ano = br.split("/")
            return date(int(ano), int(mes), int(dia))
        except Exception:
            return None

    token_store = TokenStore()
    pedidos = await vendas_db.get_all_pedidos(status_filter=["Separando", "Separado", "Embalado"],
                                              incluir_em_classificacao=True)
    pedidos = pedidos[:limite]
    meli_cache: dict[str, MeliService] = {}
    divergentes, sem_sla, erros = [], [], []
    conferem = 0

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
                erros.append({"venda": p["venda"], "erro": "pedido sem shipping_id"})
                continue
            try:
                sla = await meli._get(f"/shipments/{shipping_id}/sla")
            except Exception as e:
                # 404 aqui é o caso comum: envio ainda não maduro, ou Full/cancelado
                # (o ML não calcula SLA nesses). Sem SLA, o prazo do painel veio do
                # fallback de ENTREGA ao comprador — semanas depois do despacho real.
                sem_sla.append({"venda": p["venda"], "id_exped": p.get("id_exped"),
                                "status": p.get("status"), "origem": p.get("origem"),
                                "data_limite_nosso": p.get("data_limite"), "motivo": str(e)[:120]})
                continue
            do_ml = _fmt_date_br(sla.get("expected_date") or "")
            nosso = (p.get("data_limite") or "").strip()
            if do_ml and do_ml != nosso:
                d_ml, d_nosso = _dias(do_ml), _dias(nosso)
                atraso = (d_nosso - d_ml).days if (d_ml and d_nosso) else None
                divergentes.append({
                    "venda": p["venda"], "id_exped": p.get("id_exped"), "empresa": empresa,
                    "status": p.get("status"), "nosso": nosso, "ml": do_ml,
                    "dias_a_mais_no_nosso": atraso,
                    "sla_status": sla.get("status"), "sla_last_updated": sla.get("last_updated"),
                })
            else:
                conferem += 1
        except Exception as e:
            erros.append({"venda": p["venda"], "erro": str(e)[:200]})

    # O que mais dói primeiro: prazo nosso mais FOLGADO que o do ML (risco de atraso).
    divergentes.sort(key=lambda d: (d["dias_a_mais_no_nosso"] is None, -(d["dias_a_mais_no_nosso"] or 0)))
    return {
        "verificado_em": _dt.now().isoformat(timespec="seconds"),
        "total_verificados": len(pedidos),
        "conferem": conferem,
        "divergentes": divergentes,
        "sem_sla_no_ml": sem_sla,
        "erros": erros,
    }


@router.get("/reprocessar-claim/{claim_id}")
async def reprocessar_claim_now(claim_id: str):
    """Processa à mão uma reclamação que o webhook já recebeu e descartou.

    Até 19/08/2026 o roteamento comparava o tópico com "claims" e o ML mandava
    "post_purchase", então toda devolução por reclamação era jogada fora (ver
    `_claim_id_do_resource`). Corrigido o roteamento, as notificações ANTIGAS não
    voltam — o ML não reenvia — e o pedido continua aparecendo só como "Cancelado".
    Esta rota faz o que o webhook deveria ter feito na época.

    Passa pelo mesmo `process_claim_notification` do webhook, com o mesmo filtro
    (só registra se houver pacote voltando) e a mesma idempotência. Rodar duas vezes
    no mesmo claim não duplica nada.

    Casos conhecidos que ficaram pra trás: 5562216393 (PLG100826001) e 5561967813."""
    token_store = TokenStore()
    erros = []
    for store in await token_store.list_stores():
        try:
            resultado = await process_claim_notification(store, claim_id)
        except Exception as e:
            erros.append({"loja": store.get("company_key"), "erro": str(e)[:200]})
            continue
        # "error" aqui é quase sempre o claim não existir NESTA loja — segue procurando.
        if resultado.get("status") != "error":
            return {"loja": store.get("company_key"), "resultado": resultado}
        erros.append({"loja": store.get("company_key"), "erro": resultado.get("detail")})
    raise HTTPException(404, {"msg": f"Claim '{claim_id}' não encontrado em nenhuma loja conectada.",
                              "tentativas": erros})


@router.get("/claim/{claim_id}")
async def claim_cru_now(claim_id: str):
    """SÓ LEITURA: o claim como o ML devolve, mais a devolução dele (se houver).

    Serve pra conferir `related_entities` antes de gravar qualquer coisa — é esse
    campo que decide se a reclamação tem pacote voltando (a doc do ML: se aparece
    "return", existe devolução associada) e, portanto, se ela vira ou não uma linha
    em `devolucoes`.

    Existe porque o `claims_search` do /investigar-envio responde 400: aquele
    endpoint de BUSCA precisa de parâmetros que a gente não montou direito. Aqui a
    consulta é direta pelo claim_id, que é o que os logs do webhook já entregam."""
    token_store = TokenStore()
    tentativas = []
    for store in await token_store.list_stores():
        meli = MeliService(store, token_store=token_store)
        try:
            claim = await meli._get(f"/post-purchase/v1/claims/{claim_id}")
        except Exception as e:
            tentativas.append({"loja": store.get("company_key"), "erro": str(e)[:150]})
            continue
        entidades = [str(e).lower() for e in (claim.get("related_entities") or [])]
        out = {
            "loja": store.get("company_key"),
            "tem_devolucao_associada": "return" in entidades,
            "related_entities": entidades,
            "resumo": {k: claim.get(k) for k in
                        ("id", "type", "stage", "status", "resource", "resource_id", "date_created")},
            "claim": claim,
        }
        try:
            out["returns"] = await meli._get(f"/post-purchase/v2/claims/{claim_id}/returns")
        except Exception as e:
            out["returns_erro"] = str(e)[:150]
        from services import db as db_service
        conn = await db_service._get_connection()
        try:
            row = await conn.fetchrow("SELECT * FROM devolucoes WHERE claim_id = $1", str(claim_id))
        finally:
            await conn.close()
        out["ja_registrado_aqui"] = dict(row) if row else None
        return out
    raise HTTPException(404, {"msg": f"Claim '{claim_id}' não encontrado em nenhuma loja.",
                              "tentativas": tentativas})



@router.get("/reverter-devolucao/{claim_id}")
async def reverter_devolucao_now(claim_id: str):
    """Desfaz a baixa de uma devolução, devolvendo o card pra fila do Mural.

    Existe por causa de 21/08/2026: a devolução do claim 5562216393 recebeu baixa com o
    pacote ainda em trânsito, e naquela versão o botão só escrevia no nosso banco — nada
    tinha sido enviado ao ML, então não há o que desfazer do lado de lá. Limpa apenas os
    campos do NOSSO fluxo (status, avaliação, datas de avaliação/finalização); o que veio
    do ML (tipo, stage, motivo, fase, destino, return_id, raw) fica intacto.

    Idempotente. Não recria devolução apagada — só reabre o que existe."""
    from services import db as db_service
    conn = await db_service._get_connection()
    try:
        antes = await conn.fetchrow("SELECT * FROM devolucoes WHERE claim_id = $1", str(claim_id))
        if not antes:
            raise HTTPException(404, f"Devolução '{claim_id}' não encontrada.")
        await conn.execute(
            "UPDATE devolucoes SET status = 'Recebida', avaliacao = NULL, "
            "avaliado_em = NULL, finalizado_em = NULL WHERE claim_id = $1", str(claim_id))
        depois = await conn.fetchrow("SELECT * FROM devolucoes WHERE claim_id = $1", str(claim_id))
    finally:
        await conn.close()
    campos = ("status", "avaliacao", "avaliado_em", "finalizado_em", "fase", "destino", "return_id")
    return {
        "claim_id": claim_id,
        "antes": {k: str(antes[k]) if antes[k] is not None else None for k in campos},
        "depois": {k: str(depois[k]) if depois[k] is not None else None for k in campos},
        "volta_pro_mural": depois["status"] != "Finalizada" and depois["fase"] != "Encerrada",
    }



@router.get("/diagnosticar-revisao/{claim_id}")
async def diagnosticar_revisao_now(claim_id: str, enviar: bool = Query(False)):
    """Por que o ML recusa a revisão desta devolução? Mostra TUDO que decide isso.

    Criada em 21/08/2026: o claim 5562216393 tinha `return_review_ok` disponível e mesmo
    assim o POST voltou 400. A mensagem que chegava na tela era só "400 Bad Request" —
    o motivo do ML vem no corpo da resposta, que ninguém estava lendo.

    Só leitura por padrão. Com `?enviar=true` tenta o POST de verdade e devolve a
    resposta crua do ML (status + corpo), sem gravar nada aqui de qualquer forma."""
    from services import db as db_service
    conn = await db_service._get_connection()
    try:
        dev = await conn.fetchrow("SELECT * FROM devolucoes WHERE claim_id = $1", str(claim_id))
    finally:
        await conn.close()
    if not dev:
        raise HTTPException(404, f"Devolução '{claim_id}' não encontrada.")

    store = await TokenStore().get_by_company_or_nickname(dev["empresa"])
    if not store:
        raise HTTPException(404, f"Loja '{dev['empresa']}' não conectada.")
    meli = MeliService(store, token_store=TokenStore())

    out = {"nosso_registro": {k: str(dev[k]) if dev[k] is not None else None
                               for k in ("claim_id", "venda_ml", "order_id", "empresa", "tipo",
                                         "stage", "status", "status_ml", "fase", "destino",
                                         "return_id", "avaliacao")}}
    try:
        claim = await meli.get_claim(claim_id)
        out["claim"] = {k: claim.get(k) for k in
                         ("id", "type", "stage", "status", "resource", "resource_id",
                          "related_entities", "date_created", "last_updated")}
        # É aqui que o ML diz o que o VENDEDOR pode fazer agora.
        out["players"] = [{"type": p.get("type"), "role": p.get("role"),
                            "available_actions": p.get("available_actions")}
                           for p in (claim.get("players") or [])]
    except Exception as e:
        out["claim_erro"] = str(e)[:300]

    try:
        ret = await meli.get_return_detail(claim_id)
        if isinstance(ret, list):
            ret = ret[0] if ret else {}
        out["return"] = {k: ret.get(k) for k in
                          ("id", "status", "subtype", "status_money", "refund_at",
                           "date_closed", "related_entities", "intermediate_check",
                           "resource_type", "resource_id")}
        out["return_shipments"] = [{"shipment_id": e.get("shipment_id"), "status": e.get("status"),
                                     "type": e.get("type"),
                                     "destination": (e.get("destination") or {}).get("name")}
                                    for e in (ret.get("shipments") or [])]
        out["return_orders"] = ret.get("orders")
        out["return_id_do_ml"] = ret.get("id")
        out["return_id_confere_com_o_nosso"] = str(ret.get("id")) == str(dev["return_id"])
    except Exception as e:
        out["return_erro"] = str(e)[:300]

    # A revisão já enviada aparece aqui; 404 significa que ainda não há nenhuma.
    rid = out.get("return_id_do_ml") or dev["return_id"]
    if rid:
        try:
            out["reviews"] = await meli._get(f"/post-purchase/v1/returns/{rid}/reviews")
        except Exception as e:
            out["reviews_erro"] = str(e)[:300]

    if enviar and rid:
        import httpx
        from services.meli_service import MELI_API
        # O painel do ML mostra "já revisei" disponível, então a ação EXISTE — o 400 é
        # da nossa chamada. A doc só documenta `-d '{}'`, mas o mesmo endpoint atende
        # revisão OK e com falha, e a de falha usa ARRAY: é plausível que ele espere
        # array nos dois casos. Em vez de um deploy por palpite, tenta as variações em
        # sequência e PARA na primeira que o ML aceitar.
        url = f"{MELI_API}/post-purchase/v1/returns/{rid}/return-review"
        variacoes = [
            ("objeto vazio (o que a doc mostra)", {}, {}),
            ("array vazio", [], {}),
            ("array com objeto vazio", [{}], {}),
            ("objeto vazio + x-format-new", {}, {"x-format-new": "true"}),
        ]
        # O return traz `orders`; mesmo não sendo carrinho, o ML pode exigir o pedido
        # explícito no corpo (é o formato que a doc mostra pro fluxo com falha).
        for o in (out.get("return_orders") or [])[:1]:
            if o.get("order_id"):
                variacoes.append(("array com order_id", [{"order_id": o["order_id"]}], {}))
        tentativas = []
        async with httpx.AsyncClient(timeout=30) as client:
            for rotulo, body, extra in variacoes:
                r = await client.post(url, headers={**meli._headers(), **extra}, json=body)
                tentativas.append({"variacao": rotulo, "body": body, "headers_extra": extra,
                                    "status": r.status_code, "resposta": r.text[:600]})
                if r.status_code < 300:
                    break        # aceitou: não insiste, senão revisaria duas vezes
        out["tentativas_post"] = tentativas
        out["aceita_por"] = next((t["variacao"] for t in tentativas if t["status"] < 300), None)
        # Estado do ML DEPOIS das tentativas — confirma se a revisão entrou de verdade.
        try:
            out["reviews_depois"] = await meli._get(f"/post-purchase/v1/returns/{rid}/reviews")
        except Exception as e:
            out["reviews_depois_erro"] = str(e)[:300]
    return out
