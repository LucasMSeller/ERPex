import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from models.product import Product
from models.order import OrderItem
from services.meli_service import (MeliService, _origem_code, _ddmmaa_br, ORIGEM_FULL,
                                   fase_do_retorno, FASE_CHEGOU, FASE_DESCONHECIDA,
                                   DESTINO_NOSSO, DESTINO_ML)
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


async def _skus_por_loja(stores: list[dict], token_store: TokenStore) -> dict[str, set[str]]:
    """Busca os SKUs reais (sku_is_real) direto no Mercado Livre e devolve
    {user_id: {skus}} — mesma chamada `get_all_items` + resolução de SKU que a
    sincronização de Produtos usa.

    Guardar por loja (e não a união) é o que permite gravar o vínculo
    SKU↔loja: é essa resposta do ML que diz quem anuncia o quê.

    Loja que falhar fica de fora do resultado — assim ela não é confundida com
    "loja de catálogo vazio", que apagaria todos os vínculos dela."""
    por_loja: dict[str, set[str]] = {}
    for store in stores:
        meli = MeliService(store, token_store=token_store)
        try:
            items = await meli.get_all_items()
        except Exception as e:
            logger.error("Erro ao buscar itens do ML pra reconciliar Endereçamento (%s): %s",
                         store.get("company_key"), e)
            continue
        skus = {p.sku for p in (Product.from_meli_item(i, store["company_key"]) for i in items)
                if p.sku_is_real}
        por_loja[str(store["user_id"])] = skus
    return por_loja


async def reconciliar_enderecos(company_key: str | None = None) -> dict:
    """Botão "Vincular SKUs" em /enderecos: busca os SKUs reais DIRETO no
    Mercado Livre — não depende de rodar antes uma sincronização de Produtos
    nem da aba Produtos do Sheets estar em dia. Funciona pra qualquer loja já
    conectada, inclusive uma nova, sem precisar mexer no código.

    Atualiza duas coisas: o Endereçamento (1 linha por SKU, em branco pros
    novos) e o vínculo SKU↔loja (1 linha por par), que é o que monta as
    gavetas da tela e guarda o interruptor `ativo`.

    `company_key` informado (botão por loja): só essa loja. `None`: todas.

    Um endereço só é apagado quando o SKU sai do catálogo de uma loja E
    nenhuma outra loja o anuncia. O vínculo responde isso com precisão, sem
    depender do prefixo do nome — que deixou de indicar posse no dia em que
    uma conta passou a vender SKU de outra linha."""
    token_store = TokenStore()
    if company_key:
        store = await token_store.get_by_company_or_nickname(company_key)
        stores = [store] if store else []
    else:
        stores = await token_store.list_stores()

    catalogos = await _skus_por_loja(stores, token_store)
    todos: set[str] = set().union(*catalogos.values()) if catalogos else set()

    # Endereço primeiro: a FK de sku_lojas exige que o SKU já exista.
    novos = await enderecos_db.ensure_addresses_for_skus(list(todos))
    orfaos: set[str] = set()
    for user_id, skus in catalogos.items():
        orfaos |= set(await enderecos_db.set_vinculos_da_loja(user_id, list(skus)))
    removidos = await enderecos_db.remover_orfaos(list(orfaos))

    return {"novos": novos, "removidos": removidos, "total": len(todos)}


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


async def _handle_cancelamento(company_key: str, order_id: str, order: dict,
                               meli: "MeliService | None" = None) -> dict:
    """order.status == "cancelled" — só age se a venda já tiver virado um card real
    (senão não há nada pra travar). Trava o Status e registra o evento no Postgres
    pro painel de Notificações do Gerente. Idempotente — webhooks de cancelamento
    podem chegar mais de uma vez.

    Nem todo "cancelled" do ML é cancelamento de verdade: quando o envio volta sem
    ser entregue (`shipment.status == "not_delivered"`), a mercadoria SAIU e está
    voltando — é devolução, não venda desfeita antes do despacho. Os dois casos
    exigem tratamento físico diferente no galpão, então aqui eles são separados e o
    retorno vira também um registro em `devolucoes` (visto em 11/08/2026 com a venda
    2000017658785874, que aparecia como "Cancelado" sem nada cancelado no ML)."""
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

    devolvido = await _registrar_retorno_como_devolucao(
        company_key, venda, order_id, order, data_evento, motivo, meli)

    logger.info("[%s] Pedido %s cancelado%s — Status=Cancelado.",
                company_key, order_id, " (envio devolvido)" if devolvido else "")
    return {"status": "devolvido_sem_entrega" if devolvido else "cancelled",
            "venda": venda, "order_id": order_id}


async def _registrar_retorno_como_devolucao(company_key: str, venda: str, order_id: str,
                                            order: dict, data_evento, motivo: str | None,
                                            meli: "MeliService | None") -> bool:
    """Registra em `devolucoes` quando o cancelamento veio de envio não entregue.

    Usa um `claim_id` sintético (`retorno-<order_id>`) porque não existe claim do ML
    aqui — o UNIQUE dessa coluna é o que mantém a operação idempotente entre webhooks
    repetidos. Falha aqui nunca derruba o cancelamento: o status da venda já foi
    gravado antes, e perder o detalhe é menos grave que perder o travamento."""
    shipping_id = (order.get("shipping") or {}).get("id")
    if not shipping_id or meli is None:
        return False
    try:
        sh = await meli.get_shipment(str(shipping_id))
    except Exception as e:
        logger.warning("Não deu pra checar se %s foi devolvido: %s", order_id, e)
        return False
    if sh.get("status") != "not_delivered":
        return False
    try:
        await db_service.registrar_devolucao(
            claim_id=f"retorno-{order_id}", venda_ml=venda, order_id=order_id,
            empresa=company_key, tipo="retorno_sem_entrega", stage=sh.get("substatus"),
            motivo=motivo or "Envio devolvido sem entrega", data_evento=data_evento,
            raw={"shipment_status": sh.get("status"), "substatus": sh.get("substatus")},
            status_ml="not_delivered",
            # Guardado porque é por ele que a volta do pacote é acompanhada daqui em
            # diante — sem isso, seria preciso buscar o pedido de novo só pra
            # redescobrir o envio a cada consulta.
            shipping_id=str(shipping_id),
        )
        await db_service.salvar_fase_devolucao(f"retorno-{order_id}", fase_do_retorno(sh))
        return True
    except Exception:
        logger.exception("Falha ao registrar retorno de %s como devolução.", venda)
        return False


async def atualizar_fase_do_retorno(meli: "MeliService", shipping_id: str, sh: dict) -> dict | None:
    """Reavalia a etapa de um retorno sem entrega a partir do envio. Devolve o que
    mudou, ou None se este envio não pertence a nenhuma devolução.

    Chamado pelo webhook de shipments: é assim que o card sai de "A caminho" para
    "Chegou" sozinho. Antes nada reconsultava o envio depois do cancelamento — o
    `not_delivered` era congelado no registro e a volta do pacote passava batida.

    Recebe o `sh` que o chamador já buscou: é o mesmo envio, e buscar de novo seria
    uma segunda ida ao ML em todo webhook."""
    dev = await db_service.get_devolucao_por_shipping(str(shipping_id))
    if not dev:
        return None

    fase = fase_do_retorno(sh)
    if fase == dev.get("fase"):
        return None
    # A data real da volta só é buscada na transição pra "Chegou" — é uma chamada a
    # mais no ML, e só nesse instante ela existe pra ser encontrada.
    chegou_em = await meli.data_de_retorno(str(shipping_id)) if fase == FASE_CHEGOU else None
    await db_service.salvar_fase_devolucao(dev["claim_id"], fase, chegou_em)
    logger.info("Devolução %s: fase %s -> %s", dev["claim_id"], dev.get("fase"), fase)
    return {"claim_id": dev["claim_id"], "antes": dev.get("fase"), "depois": fase}


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
        return await _handle_cancelamento(company_key, order_id, order, meli)

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


async def conferir_vendas_recentes(horas: int = 3, importar: bool = True) -> dict:
    """Confere se todo pedido pago recente do ML está registrado aqui, e importa o que
    faltar. Rede de segurança para webhook que não chegou ou morreu no meio.

    Compara IDs, nunca contagens. Contagem parece suficiente ("10 lá, 10 aqui, ok"),
    mas erra justamente no caso ruim: falta uma venda nova e sobra um registro antigo,
    o total bate e o pedido segue invisível. Foi o cenário de 10/08/2026. E comparar
    um a um não custa nada a mais — a lista de IDs vem na MESMA chamada que daria a
    contagem, então contar seria jogar fora informação já paga.

    Não usa `backfill_orders_for_company`: aquele traz "as N vendas mais recentes da
    loja", sem recorte de tempo, e foi o que arrastou vendas de maio pro Mural. Aqui o
    corte é por `date_created`, então o que está fora da janela nunca entra.
    """
    token_store = TokenStore()
    corte = datetime.now(timezone.utc) - timedelta(hours=horas)
    registrados = await vendas_db.get_sale_order_ids()
    lojas: list[dict] = []
    recuperados: list[dict] = []

    for store in await token_store.list_stores():
        company_key = store["company_key"]
        meli = MeliService(store, token_store=token_store)
        try:
            recentes = await meli.get_recent_orders(50)
        except Exception as e:
            lojas.append({"loja": company_key, "erro": str(e)})
            continue

        no_periodo = {str(o.get("id")) for o in recentes
                      if _parse_ml_datetime(o.get("date_created")) >= corte}
        faltando = sorted(no_periodo - registrados)
        lojas.append({"loja": company_key, "no_ml": len(no_periodo),
                      "faltando": faltando})

        for order_id in faltando if importar else []:
            # O webhook pode ter reservado o pedido e morrido antes de gravar; sem
            # liberar, o próprio anti-duplicata bloquearia esta recuperação.
            await token_store.liberar_order(order_id)
            try:
                r = await process_order_notification(store, order_id)
            except Exception as e:
                recuperados.append({"order_id": order_id, "loja": company_key, "erro": str(e)})
                continue
            recuperados.append({"order_id": order_id, "loja": company_key,
                                "resultado": r.get("status")})
            # Log em nível de alerta de propósito: se a rede de segurança começar a
            # pescar peixe com frequência, o problema é o webhook, não a rede.
            logger.warning("[%s] Conferência recuperou o pedido %s, que o webhook não "
                           "registrou.", company_key, order_id)

    return {"janela_horas": horas, "lojas": lojas, "recuperados": recuperados}


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

    # Antes de tudo: este envio pode ser o de um pacote voltando. Fica aqui em cima
    # porque a venda dele já foi cancelada, e as correções abaixo (prazo, origem)
    # não se aplicam mais — mas a volta do pacote precisa ser acompanhada.
    retorno = await atualizar_fase_do_retorno(meli, shipping_id, sh)

    order_id = str(sh.get("order_id") or "")
    if not order_id:
        return {"status": "sem_order_id", "shipping_id": shipping_id, "retorno": retorno}

    pedido = await vendas_db.get_pedido_by_order_id(order_id)
    if not pedido:
        return {"status": "pedido_nao_encontrado", "shipping_id": shipping_id,
                "order_id": order_id, "retorno": retorno}

    mudancas: dict = {}
    if retorno:
        mudancas["devolucao"] = retorno
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
    """Registra em `devolucoes` (Postgres) o claim que tem PACOTE VOLTANDO, e só ele.

    Não mexe no Status da venda: a essa altura ela já está "Enviado", e uma devolução
    não deve reabrir o fluxo de separação/embalagem. O `raw` guarda o payload inteiro
    porque a Claims API muda sem aviso.

    O filtro (2026-08-19): `post_purchase` cobre reclamação, mediação, pergunta
    pós-venda e troca — coisa que não tem pacote nenhum voltando pro galpão. Até aqui
    o código gravava QUALQUER claim, adiado até existir um caso real ("o filtro fica
    pra depois"); o caso apareceu. Registrar tudo encheria a aba Devoluções de
    reclamação sem devolução, e devolução tem que ser tratada como devolução,
    cancelamento como cancelamento.

    O critério é o que a doc do ML manda usar: `related_entities` contendo "return"
    significa que existe devolução associada à reclamação. Quem não tem é ignorado —
    e sem prejuízo, porque o ML notifica o mesmo claim várias vezes (o 5562216393
    disparou 12 notificações): quando a devolução nascer, a próxima notificação já
    traz "return" e o registro acontece ali.

    Idempotente — o ML reenvia o mesmo webhook mais de uma vez (claim_id é UNIQUE).
    """
    company_key = store["company_key"]
    token_store = TokenStore()
    meli = MeliService(store, token_store=token_store)

    try:
        claim = await meli.get_claim(claim_id)
    except Exception as e:
        logger.error("Erro ao buscar claim %s: %s", claim_id, e)
        return {"status": "error", "claim_id": claim_id, "detail": str(e)}

    entidades = [str(e).lower() for e in (claim.get("related_entities") or [])]
    if "return" not in entidades:
        # `related_entities` ESVAZIA quando a devolução encerra — medido em 21/08/2026
        # no claim 5562216393: no dia 20 vinha ["return"], no dia seguinte veio []. Um
        # claim notificado só depois do encerramento seria descartado como "sem
        # devolução", e o pacote (que continua vindo pro galpão) nunca teria card.
        # Por isso o campo vazio não decide sozinho: pergunta direto ao recurso de
        # devoluções, que é a fonte real. Só nesse caso, então não custa nada no
        # caminho normal.
        try:
            confirmacao = await meli.get_return_detail(claim_id)
        except Exception:
            confirmacao = None
        if isinstance(confirmacao, list):
            confirmacao = confirmacao[0] if confirmacao else None
        if not (confirmacao or {}).get("id"):
            logger.info("[%s] Claim %s ignorado: sem devolução associada (related_entities=%s).",
                        company_key, claim_id, entidades or "vazio")
            return {"status": "sem_devolucao", "claim_id": claim_id, "related_entities": entidades}
        logger.info("[%s] Claim %s: related_entities vazio, mas o ML tem a devolução %s.",
                    company_key, claim_id, confirmacao.get("id"))

    # `resource_id` só é um pedido quando `resource` diz que é. A doc lista outros
    # valores (claim, shipment, other) e, num claim desses, usar o id como order_id
    # buscaria um pedido que não existe: a devolução entrava órfã, sem venda, sem
    # itens e com a loja errada — sem erro nenhum na tela.
    recurso = (claim.get("resource") or "").strip().lower()
    order_id = str(claim.get("resource_id") or "") if recurso in ("", "order") else ""
    if recurso not in ("", "order"):
        logger.warning("[%s] Claim %s aponta pra '%s', não pra um pedido — registrando sem venda.",
                       company_key, claim_id, recurso)
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

    # O envio de volta só existe depois que o ML o cria, então nas primeiras
    # notificações isso vem vazio — e o COALESCE do upsert garante que uma consulta
    # vazia não apague o que uma notificação posterior já tiver gravado.
    estado = await meli.estado_da_devolucao(claim_id)

    try:
        await db_service.registrar_devolucao(
            claim_id=claim_id, venda_ml=venda, order_id=order_id or None, empresa=empresa,
            tipo=claim.get("type"), stage=claim.get("stage"), motivo=motivo,
            data_evento=data_evento, raw=claim, status_ml=claim.get("status"),
            destino=estado.get("destino"), return_id=estado.get("return_id"),
        )
        if estado["fase"] != FASE_DESCONHECIDA:
            await db_service.salvar_fase_devolucao(claim_id, estado["fase"])
    except Exception:
        logger.exception("Falha ao registrar devolução (claim %s) no Postgres.", claim_id)
        return {"status": "error", "claim_id": claim_id, "detail": "falha ao gravar no Postgres"}

    logger.info("[%s] Claim %s registrado (order_id=%s, venda=%s, destino=%s, fase=%s).",
                company_key, claim_id, order_id, venda, estado.get("destino"), estado["fase"])
    return {"status": "registered", "claim_id": claim_id, "order_id": order_id, "venda": venda,
            "destino": estado.get("destino"), "return_id": estado.get("return_id"),
            "fase": estado["fase"]}


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


# ── Prazo de despacho: perguntar em vez de esperar ────────────────────────────
# O webhook 'shipments' do ML é push por evento: ele avisa quando o ENVIO muda de
# estado, não quando o SLA passa a existir. Um pedido pode ficar horas sem prazo
# sem que nada seja notificado — a venda 2000014601305665 (19/08/2026) só se
# corrigiu quando alguém emitiu a NF-e pelo site do ML, e foi essa ação, não o
# tempo, que acordou o ML. Como não há nada nosso perguntando de tempos em tempos
# (o projeto não tem scheduler), o prazo dependia de uma pessoa agir primeiro.
#
# Esta função é a pergunta que faltava, pendurada no poll que o Mural já faz a
# cada ~15s enquanto o galpão trabalha: a instância do Cloud Run já está de pé,
# então não custa cold start nem free tier — só as chamadas ao ML, e apenas pros
# pedidos que ainda não têm prazo. Sem Cloud Scheduler, sem sleep, sem coluna
# nova: `data_limite` vazio JÁ é o marcador de "o ML ainda não disse".
_ULTIMA_TENTATIVA: dict[str, float] = {}   # venda -> time.monotonic() da última pergunta
_INTERVALO_TENTATIVA = 90.0   # não pergunta a mesma venda mais que 1x a cada 90s
_MAX_POR_RODADA = 3           # teto por poll, pra não virar rajada contra o ML
_LOCK_PRAZOS = asyncio.Lock()


def _esquecer_tentativas_antigas(agora: float) -> None:
    """`_ULTIMA_TENTATIVA` só cresce enquanto vendas entram e saem do Mural; sem isto
    ele viraria um vazamento lento de memória numa instância de vida longa."""
    if len(_ULTIMA_TENTATIVA) <= 500:
        return
    velhas = [v for v, t in _ULTIMA_TENTATIVA.items() if agora - t > 3600]
    for v in velhas:
        _ULTIMA_TENTATIVA.pop(v, None)


async def revalidar_prazos_pendentes() -> list[dict]:
    """Pergunta ao ML o prazo de despacho das vendas do Mural que ainda estão sem data.

    Roda em background (BackgroundTasks do Mural), então NUNCA pode estourar pra cima:
    qualquer falha vira log, nada propaga. Idempotente e auto-limitada — se duas
    requisições do poll caírem juntas, a segunda desiste no lock em vez de duplicar
    as chamadas ao ML."""
    if _LOCK_PRAZOS.locked():
        return []
    async with _LOCK_PRAZOS:
        agora = time.monotonic()
        try:
            pedidos = await vendas_db.get_mural_pedidos()
        except Exception as e:
            logger.warning("revalidar_prazos: nao consegui ler o Mural: %s", e)
            return []

        sem_prazo = [p for p in pedidos if not (p.get("data_limite") or "").strip()]
        alvos = [p for p in sem_prazo
                 if agora - _ULTIMA_TENTATIVA.get(p["venda"], 0.0) >= _INTERVALO_TENTATIVA]
        if not alvos:
            return []

        token_store = TokenStore()
        meli_cache: dict[str, MeliService] = {}
        preenchidos = []
        for p in alvos[:_MAX_POR_RODADA]:
            _ULTIMA_TENTATIVA[p["venda"]] = agora
            try:
                meli = meli_cache.get(p["empresa"])
                if meli is None:
                    store = await token_store.get_by_company_or_nickname(p["empresa"])
                    if not store:
                        continue
                    meli = MeliService(store, token_store=token_store)
                    meli_cache[p["empresa"]] = meli
                order, shipping_id = await meli.resolve_order_and_shipping(p["venda"])
                if not shipping_id:
                    continue
                deadline = await meli.get_delivery_deadline({"shipping": {"id": shipping_id}})
                if not deadline:
                    continue   # o ML ainda não sabe; tenta de novo na próxima rodada
                await vendas_db.set_data_limite(p["venda"], deadline)
                logger.info("[%s] Venda %s: prazo de despacho preenchido -> %s",
                            p["empresa"], p["venda"], deadline)
                preenchidos.append({"venda": p["venda"], "data_limite": deadline})
            except Exception as e:
                logger.warning("revalidar_prazos: venda %s falhou: %s", p["venda"], e)

        _esquecer_tentativas_antigas(agora)
        return preenchidos
