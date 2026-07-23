"""Mural de expedição (Separação): pedidos com Status=="Separando", agrupados por
venda/pack. A transição pra "Embalado" não é mais manual aqui — acontece na tela
/embalagem, amarrada à impressão confirmada das etiquetas (ver routers/embalagem.py).

Dividido em aba "Hoje" (data limite hoje ou atrasada) e aba "Próximos envios", que
por sua vez tem subabas: uma por dia (enquanto a data cair no mês atual) e uma por
mês (a partir daí) — sempre ordenado da data mais próxima pra mais distante.
"""
from datetime import date
from fastapi import APIRouter, Request, Query, Form, Depends
from templates_engine import templates
from services.sheets_service import SheetsService
from services.token_store import TokenStore
from services.session_auth import require_login
from services import db as db_service
from services.meli_service import _BR_TZ
from routers.print_labels import _meli_for

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
    pedidos = SheetsService().get_mural_pedidos()
    empresas = sorted({p["empresa"] for p in pedidos if p["empresa"]})
    return templates.TemplateResponse("mural.html", {"request": request, "empresas": empresas})


@router.get("/pedidos")
async def mural_pedidos(request: Request, empresa: list[str] = Query([])):
    pedidos = SheetsService().get_mural_pedidos()
    if empresa:
        alvo = set(empresa)
        pedidos = [p for p in pedidos if p["empresa"] in alvo]

    # Cancelado nunca muda de Status na planilha (é o tipo real do pedido, pra sempre) —
    # o que tira o card daqui é só o Postgres saber que o Gerente já finalizou.
    try:
        arquivados = await db_service.listar_cancelados_arquivados()
    except Exception:
        arquivados = set()
    pedidos = [p for p in pedidos
               if not (p["status"].lower() == "cancelado" and p["venda"] in arquivados)]

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

    cores = TokenStore().get_cores_por_empresa()
    return templates.TemplateResponse("_mural_pedidos.html", {
        "request": request, "hoje": de_hoje, "dias": dias, "meses": meses, "cores": cores,
    })


async def _devolucoes_context(request: Request) -> dict:
    sheets = SheetsService()
    devolucoes = await db_service.listar_devolucoes()
    for d in devolucoes:
        pedido = sheets.get_pedido_by_venda(d["venda_ml"]) if d["venda_ml"] else None
        d["itens"] = pedido["itens"] if pedido else []
        if d.get("criado_em"):
            d["criado_em"] = d["criado_em"].astimezone(_BR_TZ)
        # Fase física real (em preparação / a caminho / a revisar) — só existe
        # sentido em consultar pra devolução ainda aberta no ML (já filtrado por
        # listar_devolucoes); "Confirmar revisão" só aparece na fase "A revisar".
        try:
            meli = await _meli_for(d["empresa"])
            d["fase"] = await meli.get_devolucao_fase(d["claim_id"])
        except Exception:
            d["fase"] = "Em preparação"
    return {"request": request, "devolucoes": devolucoes}


@router.get("/devolucoes")
async def mural_devolucoes(request: Request):
    """Devoluções (claims do ML) pendentes de avaliação física pela expedição —
    separado das entregas normais de propósito (ver services/db.py::listar_devolucoes)."""
    return templates.TemplateResponse("_mural_devolucoes.html", await _devolucoes_context(request))


@router.post("/devolucoes/{claim_id}/avaliar")
async def mural_avaliar_devolucao(request: Request, claim_id: str, observacao: str = Form(...)):
    """Equipe de expedição avalia o produto fisicamente e registra o que encontrou —
    não é ação de Gerente, qualquer um logado no Mural pode avaliar."""
    await db_service.avaliar_devolucao(claim_id, observacao)
    return templates.TemplateResponse("_mural_devolucoes.html", await _devolucoes_context(request))
