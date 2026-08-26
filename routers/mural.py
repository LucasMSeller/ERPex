"""Mural de expedição (Separação): pedidos com Status=="Separando", agrupados por
venda/pack. A transição pra "Embalado" não é mais manual aqui — acontece na tela
/embalagem, amarrada à impressão confirmada das etiquetas (ver routers/embalagem.py).

Dividido em aba "Hoje" (data limite hoje ou atrasada) e aba "Próximos envios", que
por sua vez tem subabas: uma por dia (enquanto a data cair no mês atual) e uma por
mês (a partir daí) — sempre ordenado da data mais próxima pra mais distante.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request, Query, Depends, BackgroundTasks
from templates_engine import templates
from services import vendas_db
from services.token_store import TokenStore
from services.session_auth import require_login
from services.qz_signing import QZ_CERT
from services import db as db_service
from services.meli_service import (_BR_TZ, fase_do_retorno, FASE_CHEGOU,
                                   FASE_ENCERRADA, FASE_DESCONHECIDA, DESTINO_ML)
from routers.print_labels import _meli_for, QZ_TRAY_CDN
from services.sync_service import revalidar_prazos_pendentes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mural", tags=["mural"], dependencies=[Depends(require_login)])

_MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


def _chave_ordenacao(p: dict) -> tuple:
    """Data (mais próxima primeiro) e, dentro da mesma data, agrupado por loja —
    mesmo no mesmo dia, vendas da mesma loja ficam juntas na lista em vez de
    intercaladas com as de outra loja."""
    d = _parse_data_br(p.get("data_limite"))
    return (d or date.min, p.get("empresa") or "")


@router.get("")
async def mural_page(request: Request):
    pedidos = await vendas_db.get_mural_pedidos()
    empresas = sorted({p["empresa"] for p in pedidos if p["empresa"]})
    return templates.TemplateResponse("mural.html", {
        "request": request, "empresas": empresas,
        "cdn": QZ_TRAY_CDN, "cert_js": json.dumps(QZ_CERT),
    })


@router.get("/pedidos")
async def mural_pedidos(request: Request, background_tasks: BackgroundTasks,
                        empresa: list[str] = Query([])):
    # O poll desta rota (a cada ~15s, o dia todo) é o único relógio que o projeto tem.
    # Depois de responder, ele pergunta ao ML o prazo das vendas que ainda estão sem
    # data — ver `revalidar_prazos_pendentes`. A resposta ao usuário não espera por
    # isso, e o próximo poll já mostra o prazo preenchido.
    background_tasks.add_task(revalidar_prazos_pendentes)
    pedidos = await vendas_db.get_mural_pedidos()
    if empresa:
        alvo = set(empresa)
        pedidos = [p for p in pedidos if p["empresa"] in alvo]

    # Cancelado nunca muda de Status na planilha (é o tipo real do pedido, pra sempre) —
    # o que tira o card daqui é só o Postgres saber que o Gerente já finalizou.
    try:
        arquivados = await db_service.listar_cancelados_arquivados()
    except Exception:
        arquivados = set()
    # Some pedidos viram "Cancelado" no ML por causa de uma devolução concluída (reembolso
    # total), não por cancelamento antes do envio — esses já têm card próprio na aba
    # Devoluções, então tirar daqui evita mostrar o mesmo pedido 2x de forma confusa.
    try:
        com_devolucao = await db_service.listar_vendas_com_devolucao()
    except Exception:
        com_devolucao = set()
    pedidos = [p for p in pedidos
               if not (p["status"].lower() == "cancelado"
                       and (p["venda"] in arquivados or p["venda"] in com_devolucao))]

    hoje = date.today()
    de_hoje: list[dict] = []
    por_dia: dict[date, list] = {}
    por_mes: dict[tuple[int, int], list] = {}

    for p in pedidos:
        d = _parse_data_br(p.get("data_limite"))
        if d is None or d <= hoje:
            de_hoje.append(p)                          # sem data ou atrasado = urgente, junto com "hoje"
        elif d.year == hoje.year and d.month == hoje.month:
            por_dia.setdefault(d, []).append(p)         # ainda no mês atual: subaba por dia
        else:
            por_mes.setdefault((d.year, d.month), []).append(p)   # mês futuro: subaba por mês

    de_hoje.sort(key=_chave_ordenacao)
    dias = [
        {"label": d.strftime("%d/%m"), "pedidos": sorted(por_dia[d], key=_chave_ordenacao)}
        for d in sorted(por_dia)
    ]
    meses = [
        {"label": f"{_MESES_PT[mes]}/{ano}",
         "pedidos": sorted(pedidos_mes, key=_chave_ordenacao)}
        for (ano, mes), pedidos_mes in sorted(por_mes.items())
    ]

    cores = await TokenStore().get_cores_por_empresa()
    return templates.TemplateResponse("_mural_pedidos.html", {
        "request": request, "hoje": de_hoje, "dias": dias, "meses": meses, "cores": cores,
    })


# Por quanto tempo a fase guardada no banco vale sem reconsultar o ML. O Mural tem
# poll, e antes cada ciclo custava uma chamada HTTP por devolução na tela; o webhook
# de shipments já atualiza a fase na hora, então isto é só uma rede de segurança.
_FASE_VALIDA_POR = timedelta(minutes=10)


def _e_retorno_sem_entrega(d: dict) -> bool:
    """Devolução nascida de um pacote que voltou, não de uma reclamação. Não tem
    claim no ML — o `claim_id` é sintético (ver sync_service)."""
    return d.get("tipo") == "retorno_sem_entrega" or str(d.get("claim_id", "")).startswith("retorno-")


async def _descobrir_shipping(meli, d: dict) -> str | None:
    """Acha o envio de um retorno gravado antes de existir a coluna `shipping_id` e
    guarda o achado. Custa uma consulta ao pedido, uma única vez por devolução."""
    order_id = d.get("order_id")
    if not order_id:
        return None
    try:
        order = await meli.get_order(str(order_id))
    except Exception:
        return None
    shipping_id = ((order or {}).get("shipping") or {}).get("id")
    if not shipping_id:
        return None
    await db_service.definir_shipping_devolucao(d["claim_id"], str(shipping_id))
    return str(shipping_id)


async def _fase_atual(d: dict) -> str:
    """Fase da devolução, do banco quando recente e do ML quando envelheceu.

    Os dois fluxos consultam lugares diferentes: reclamação do comprador tem claim e
    se lê em /claims/{id}/returns; retorno sem entrega não tem claim nenhum e se lê
    pelo envio original. Perguntar pelo claim sintético devolvia 404, e o card ficava
    preso em "Em preparação" pra sempre — foi o caso de 11/08/2026."""
    guardada = d.get("fase")
    fase_em = d.get("fase_em")
    if guardada and fase_em and (datetime.now(timezone.utc) - fase_em) < _FASE_VALIDA_POR:
        return guardada
    try:
        meli = await _meli_for(d["empresa"])
        if _e_retorno_sem_entrega(d):
            shipping_id = d.get("shipping_id") or await _descobrir_shipping(meli, d)
            if not shipping_id:
                return guardada or FASE_DESCONHECIDA
            sh = await meli.get_shipment(str(shipping_id))
            fase = fase_do_retorno(sh)
            chegou = await meli.data_de_retorno(str(shipping_id)) if fase == FASE_CHEGOU else None
        else:
            estado = await meli.estado_da_devolucao(d["claim_id"])
            fase = estado["fase"]
            chegou = None
            # O destino só nasce junto com o envio de volta, então costuma vir vazio
            # nas primeiras consultas — quando aparece, fica guardado.
            if estado.get("destino") and estado["destino"] != d.get("destino"):
                await db_service.definir_destino_devolucao(d["claim_id"], estado["destino"])
                d["destino"] = estado["destino"]
    except Exception:
        return guardada or FASE_DESCONHECIDA
    if fase == FASE_DESCONHECIDA:
        # Não sobrescreve um fato conhecido com uma consulta que falhou.
        return guardada or FASE_DESCONHECIDA
    await db_service.salvar_fase_devolucao(d["claim_id"], fase, chegou)
    if chegou:
        d["chegou_em"] = chegou
    return fase


async def _devolucoes_context(request: Request, erro: dict | None = None) -> dict:
    devolucoes = await db_service.listar_devolucoes()
    # Uma consulta pros pedidos de todas as devoluções, em vez de uma por devolução.
    pedidos_dev = await vendas_db.get_pedidos_por_venda(
        [d["venda_ml"] for d in devolucoes if d["venda_ml"]])
    for d in devolucoes:
        pedido = pedidos_dev.get(d["venda_ml"]) if d["venda_ml"] else None
        d["itens"] = pedido["itens"] if pedido else []
        if d.get("criado_em"):
            d["criado_em"] = d["criado_em"].astimezone(_BR_TZ)
        d["fase"] = await _fase_atual(d)
        d["retorno_sem_entrega"] = _e_retorno_sem_entrega(d)
        # Retorno sem entrega volta sempre pro remetente — somos nós. Só a devolução
        # por reclamação pode ser endereçada ao depósito do ML.
        d["vem_pra_ca"] = d["retorno_sem_entrega"] or d.get("destino") != DESTINO_ML
        if d.get("chegou_em"):
            d["chegou_em"] = d["chegou_em"].astimezone(_BR_TZ)
    # As fases vão pro template como constantes, não como texto solto: renomear uma
    # delas aqui não pode fazer o card parar de reconhecê-la em silêncio.
    return {"request": request, "devolucoes": devolucoes, "FASE_CHEGOU": FASE_CHEGOU,
            "FASE_ENCERRADA": FASE_ENCERRADA, "FASE_DESCONHECIDA": FASE_DESCONHECIDA,
            "erros": erro or {}}


@router.get("/devolucoes")
async def mural_devolucoes(request: Request):
    """Devoluções (claims do ML) pendentes de avaliação física pela expedição —
    separado das entregas normais de propósito (ver services/db.py::listar_devolucoes)."""
    return templates.TemplateResponse("_mural_devolucoes.html", await _devolucoes_context(request))


@router.post("/devolucoes/{claim_id}/recebi")
async def mural_recebi_devolucao(request: Request, claim_id: str):
    """Caminho comum: o produto voltou e está bom. Um clique, sem texto a digitar —
    a expedição faz isso de pé, no galpão.

    A baixa aqui só acontece DEPOIS que o ML aceita a revisão (decidido em 21/08/2026).
    Até então este botão só escrevia no nosso banco: o card sumia da tela, e do lado do
    ML a revisão seguia pendente até vencer — e revisão vencida o ML resolve a favor do
    comprador (`seller_status: "success"` inclui "Seller não revisou a tempo"). Foi o
    que aconteceu com o claim 5562216393. A tela dizia "Conferido" enquanto o dinheiro
    ia embora.

    Se o ML recusar, nada é gravado e o card continua na fila com o motivo. Sem baixa
    local de consolo: um registro que diz "resolvido" sem estar resolvido no ML é pior
    que nenhum, porque ninguém volta pra conferir.

    Retorno sem entrega não tem claim nem revisão no ML — esse segue com baixa direta."""
    dev = await db_service.get_devolucao(claim_id)
    if dev and not _e_retorno_sem_entrega(dev):
        erro = await _confirmar_revisao_no_ml(dev)
        if erro:
            return templates.TemplateResponse(
                "_mural_devolucoes.html", await _devolucoes_context(request, erro={claim_id: erro}))
    await db_service.avaliar_devolucao(claim_id, "Recebido, sem problemas.")
    await db_service.finalizar_devolucao(claim_id)
    return templates.TemplateResponse("_mural_devolucoes.html", await _devolucoes_context(request))


async def _confirmar_revisao_no_ml(dev: dict) -> str:
    """Manda a revisão OK ao ML. Devolve "" se passou, ou o motivo da recusa.

    O motivo é escrito pra quem está no galpão com a caixa na mão, não pra quem lê
    log: precisa dizer o que fazer em seguida."""
    return_id = dev.get("return_id")
    if not return_id:
        return ("Não sei o número da devolução no Mercado Livre (return_id). "
                "Ela pode ter sido registrada antes desse dado passar a ser guardado — "
                "confirme pelo painel do ML.")
    try:
        meli = await _meli_for(dev["empresa"])
    except Exception:
        return "Não consegui falar com o Mercado Livre agora. Tente de novo em um minuto."
    # Revisão que já está lá não precisa ser reenviada — e um clique repetido (ou o
    # reenvio depois de uma falha nossa) não pode virar erro na cara de quem está com a
    # caixa na mão.
    if await meli.revisao_ja_enviada(str(return_id)):
        return ""
    if not await meli.revisao_liberada(dev["claim_id"]):
        return ("O Mercado Livre ainda não liberou a revisão desta devolução. "
                "Isso costuma ser porque o ML ainda não registrou a entrega do pacote a nós, "
                "ou porque a reclamação já foi encerrada por lá. Confira no painel do ML.")
    try:
        await meli.revisar_devolucao_ok(str(return_id))
    except Exception as e:
        # O motivo real de um 4xx do ML vem no CORPO, não na linha de status: sem ler
        # `response.text` sobra só "400 Bad Request", que não diz o que corrigir.
        detalhe = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                corpo = resp.json()
                detalhe = (corpo.get("message") or corpo.get("error")
                           or corpo.get("cause") or resp.text)
                if isinstance(detalhe, (list, dict)):
                    detalhe = json.dumps(detalhe, ensure_ascii=False)
            except Exception:
                detalhe = resp.text
        logger.warning("revisao da devolucao %s recusada pelo ML (return_id=%s): %s | corpo: %s",
                       dev["claim_id"], return_id, e, detalhe)
        return f"O Mercado Livre recusou a revisão: {(detalhe or str(e))[:300]}"
    return ""


@router.post("/devolucoes/{claim_id}/problema")
async def mural_problema_devolucao(request: Request, claim_id: str):
    """Caminho raro: o produto voltou com problema. Marca e mostra o aviso pra tratar
    no painel do ML — contestar exige motivo padronizado e foto anexada, e construir
    isso aqui não se paga pela frequência. NÃO encerra: o card fica visível."""
    await db_service.avaliar_devolucao(claim_id, "Problema no produto — tratar no painel do ML.")
    return templates.TemplateResponse("_mural_devolucoes.html", await _devolucoes_context(request))
