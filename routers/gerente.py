"""Painel do Gerente: auditoria de todos os pedidos (qualquer status), reversão
manual de status (correção quando uma etiqueta física se perde) e conexão/
desconexão de lojas do Mercado Livre. Acesso por senha própria (GERENTE_PASSWORD),
separada da senha operacional do Mural/Embalagem/Gaiolas.
"""
from datetime import date
from fastapi import APIRouter, Request, Form, Query, Depends
from fastapi.responses import RedirectResponse
from templates_engine import templates
from services.sheets_service import SheetsService
from services.token_store import TokenStore
from services.session_auth import require_gerente
from services import db as db_service
from services.meli_service import _BR_TZ
from routers.print_labels import _meli_for

router = APIRouter(prefix="/gerente", tags=["gerente"], dependencies=[Depends(require_gerente)])

# Status reais da planilha (usado no seletor "corrigir status" por linha — nunca
# inclui "Devolução", que não é um Status de verdade, é só uma categoria de exibição).
STATUS_OPCOES = ["Separando", "Separado", "Embalado", "Enviado", "Cancelado"]
# Categorias pro filtro de cima (checkboxes) — inclui "Devolução" pra poder mostrar/
# esconder as linhas sintéticas de devolução junto com as vendas normais.
FILTRO_STATUS_OPCOES = STATUS_OPCOES + ["Devolução"]
POR_PAGINA_OPCOES = [25, 50, 100, 0]   # 0 = "Todos"


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


async def _opcoes_filtro() -> list[str]:
    """Lojas distintas vistas na aba Vendas + devoluções — base estável pro dropdown
    de loja do Gerente (não é afetada pelos filtros atualmente aplicados). O filtro de
    dia virou um intervalo de calendário (dia_de/dia_ate), não precisa mais de opções
    pré-computadas."""
    todos = SheetsService().get_all_pedidos()
    lojas = {p["empresa"] for p in todos if p["empresa"]}

    devolucoes = await db_service.listar_devolucoes(pendentes=False)
    for d in devolucoes:
        if d.get("empresa"):
            lojas.add(d["empresa"])

    return sorted(lojas)


def _devolucao_para_pedido(d: dict, sheets: SheetsService) -> dict:
    """Converte um registro de devolução (Postgres) num dict no mesmo formato dos
    pedidos da planilha, pra poder entrar na mesma tabela/filtros do Gerente."""
    pedido_sheets = sheets.get_pedido_by_venda(d["venda_ml"]) if d.get("venda_ml") else None
    data_limite = d["criado_em"].astimezone(_BR_TZ).strftime("%d/%m/%Y") if d.get("criado_em") else ""
    return {
        "venda": d.get("venda_ml") or d.get("order_id") or d["claim_id"],
        "order_id": d.get("order_id"),
        "empresa": d.get("empresa") or "",
        "id_exped": "—",
        "status": "Devolução",
        "impresso_em": None,
        "itens": pedido_sheets["itens"] if pedido_sheets else [],
        "gaiola": "",
        "data_limite": data_limite,
        "_devolucao": d,
    }


async def _pedidos_unificados(status: list[str], busca: str, loja: str,
                               dia_de: str, dia_ate: str) -> list[dict]:
    """Vendas normais (planilha) + devoluções (Postgres) numa lista só, com os
    mesmos filtros aplicados nas duas fontes. `dia_de`/`dia_ate` no formato ISO
    ("AAAA-MM-DD", o que um <input type="date"> manda)."""
    sheets = SheetsService()
    status_sheets = [s for s in status if s in STATUS_OPCOES] if status else None
    pedidos = sheets.get_all_pedidos(status_sheets, busca, loja, dia_de, dia_ate)

    # Cancelado nunca muda de Status na planilha — o que diz se já foi tratado é
    # só o Postgres (ver routers/mural.py, mesma regra aplicada aqui pro badge).
    arquivados = await db_service.listar_cancelados_arquivados()
    for p in pedidos:
        if p["status"].lower() == "cancelado":
            p["_cancelado_resolvido"] = p["venda"] in arquivados

    if not status or "Devolução" in status:
        de = date.fromisoformat(dia_de) if dia_de else None
        ate = date.fromisoformat(dia_ate) if dia_ate else None
        devolucoes = await db_service.listar_devolucoes(pendentes=False)
        busca_norm = (busca or "").strip().lower()
        for d in devolucoes:
            p = _devolucao_para_pedido(d, sheets)
            if loja and p["empresa"].strip().lower() != loja.strip().lower():
                continue
            if de or ate:
                data_p = _parse_data_br(p["data_limite"])
                if data_p is None or (de and data_p < de) or (ate and data_p > ate):
                    continue
            if busca_norm:
                campos = [p["venda"], p["order_id"] or "", p["empresa"]] + [i["sku"] for i in p["itens"]]
                if not any(busca_norm in str(c).lower() for c in campos if c):
                    continue
            pedidos.append(p)

    return pedidos


async def _paginar(request: Request, pedidos: list[dict], status: list[str], busca: str,
                    loja: str, dia_de: str, dia_ate: str, pagina: int, por_pagina: int,
                    mensagem: str = ""):
    lojas_disponiveis = await _opcoes_filtro()

    total = len(pedidos)
    efetivo = por_pagina if por_pagina and por_pagina > 0 else total or 1
    total_paginas = max(1, -(-total // efetivo))   # ceil
    pagina = min(max(1, pagina), total_paginas)
    inicio = (pagina - 1) * efetivo
    pedidos_pagina = pedidos[inicio:inicio + efetivo]

    return templates.TemplateResponse("_gerente_pedidos.html", {
        "request": request, "pedidos": pedidos_pagina, "status_opcoes": STATUS_OPCOES,
        "mensagem": mensagem,
        "lojas_disponiveis": lojas_disponiveis,
        "por_pagina_opcoes": POR_PAGINA_OPCOES,
        "loja": loja, "dia_de": dia_de, "dia_ate": dia_ate, "por_pagina": por_pagina,
        "pagina": pagina, "total_paginas": total_paginas, "total": total,
    })


@router.get("")
async def gerente_page(request: Request):
    lojas_disponiveis = await _opcoes_filtro()
    return templates.TemplateResponse("gerente.html", {
        "request": request, "status_opcoes": FILTRO_STATUS_OPCOES,
        "lojas_disponiveis": lojas_disponiveis,
    })


@router.get("/logout")
async def gerente_logout(request: Request):
    """Sai só da área do Gerente (mantém a sessão operacional, se houver) e volta
    pro painel de colaborador — diferente do /logout geral, que desloga de tudo."""
    request.session.pop("gerente", None)
    return RedirectResponse("/mural", status_code=303)


@router.get("/pedidos")
async def gerente_pedidos(request: Request, status: list[str] = Query([]), busca: str = Query(""),
                           loja: str = Query(""), dia_de: str = Query(""), dia_ate: str = Query(""),
                           pagina: int = Query(1), por_pagina: int = Query(50)):
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina)


@router.post("/pedidos/{venda}/status")
async def reverter_status(request: Request, venda: str, novo_status: str = Form(...),
                           status: list[str] = Form([]), busca: str = Form(""),
                           loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                           pagina: int = Form(1), por_pagina: int = Form(50)):
    """Correção manual do Gerente — ex.: reabrir um pedido preso em "Separado"
    (etiqueta perdida) de volta pra "Separando" pra poder reimprimir."""
    SheetsService().set_status_for_venda(venda, novo_status)
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina)


@router.post("/pedidos/{venda}/remover-gaiola")
async def remover_da_gaiola(request: Request, venda: str,
                             status: list[str] = Form([]), busca: str = Form(""),
                             loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                             pagina: int = Form(1), por_pagina: int = Form(50)):
    """Tira o pedido da gaiola em que estava (volta pra "Aguardando box" em
    /gaiolas) — correção manual do Gerente, ex.: item bipado na gaiola errada.
    Recusa se o pedido já foi Enviado (ver SheetsService.remover_da_gaiola)."""
    resultado = SheetsService().remover_da_gaiola(venda)
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina,
                           mensagem="" if resultado["ok"] else resultado["msg"])


@router.post("/pedidos/{venda}/emitir-nf")
async def emitir_nf_manual(request: Request, venda: str,
                            status: list[str] = Form([]), busca: str = Form(""),
                            loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                            pagina: int = Form(1), por_pagina: int = Form(50)):
    """Botão manual do Gerente pra consultar/re-tentar a emissão da NF-e — usado
    quando o gatilho automático (separação/embalagem) ainda não rodou ou falhou
    (ex.: pedido antecipado que ainda não passou pela embalagem)."""
    pedido = SheetsService().get_pedido_by_venda(venda)
    if not pedido:
        mensagem = f'Pedido "{venda}" não encontrado na aba Vendas.'
    else:
        try:
            meli = await _meli_for(pedido["empresa"])
            order, _shipping_id = await meli.resolve_order_and_shipping(venda)
            resultado = await meli.ensure_invoice(str(order["id"]))
            if resultado["erro"]:
                mensagem = f"NF-e: erro ao emitir — {resultado['erro']}"
            elif resultado["ja_existia"]:
                mensagem = f"NF-e já emitida/em andamento (status: {resultado['status']})."
            else:
                mensagem = f"NF-e emitida (status: {resultado['status']})."
        except Exception as e:
            mensagem = f"Falha ao consultar/emitir NF-e: {e}"
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina,
                           mensagem=mensagem)


@router.post("/pedidos/devolucao/{claim_id}/finalizar")
async def finalizar_devolucao_pedidos(request: Request, claim_id: str,
                                       status: list[str] = Form([]), busca: str = Form(""),
                                       loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                                       pagina: int = Form(1), por_pagina: int = Form(50)):
    """Mesma ação de finalizar devolução, só que chamada da tabela unificada de
    pedidos (htmx, re-renderiza a tabela em vez de redirecionar)."""
    await db_service.finalizar_devolucao(claim_id)
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina)


@router.get("/lojas")
async def gerente_lojas_page(request: Request):
    lojas = TokenStore().list_stores()
    return templates.TemplateResponse("gerente_lojas.html", {"request": request, "lojas": lojas})


@router.post("/lojas/{user_id}/desconectar")
async def desconectar_loja(user_id: str):
    TokenStore().delete_store(user_id)
    return RedirectResponse("/gerente/lojas", status_code=303)


@router.post("/lojas/{user_id}/cor")
async def definir_cor_loja(user_id: str, cor: str = Form(...)):
    """Cor do card da loja, escolhida pelo Gerente — aparece no Mural e na fila
    da Embalagem pra diferenciar visualmente pedidos de lojas diferentes."""
    TokenStore().set_color(user_id, cor)
    return RedirectResponse("/gerente/lojas", status_code=303)


@router.post("/lojas/{user_id}/prefixo")
async def definir_prefixo_loja(user_id: str, sku_prefixo: str = Form("")):
    """Prefixo do SKU dessa loja, informado pelo Gerente — usado só pra agrupar
    a tela de Endereçamento por loja (accordion) e pra reconciliar com
    segurança os SKUs removidos de 1 loja só."""
    TokenStore().set_sku_prefixo(user_id, sku_prefixo.strip().upper())
    return RedirectResponse("/gerente/lojas", status_code=303)


@router.get("/notificacoes")
async def notificacoes_page(request: Request):
    """Registro permanente do Gerente — pedidos cancelados E devoluções, mesmo
    depois de resolvidos. Igual a um histórico de vendas: nada some daqui só
    porque já foi tratado, fica pra auditoria.

    Cancelado é o tipo real do pedido — o Status na aba Vendas fica "Cancelado"
    pra sempre, nunca muda (diferente de antes, quando virava "Arquivado"). O que
    diz se já foi tratado é só o flag `finalizado_por_gerente` no Postgres — o
    card fica esmaecido aqui e some do Mural, sem precisar mexer na planilha.
    Devoluções (claims do ML) na segunda seção — histórico completo
    (`pendentes=False`), incluindo as já finalizadas."""
    pedidos = SheetsService().get_all_pedidos(status_filter=["Cancelado"])
    notificacoes = []
    for p in pedidos:
        try:
            detalhe = await db_service.get_cancelamento(p["venda"])
        except Exception:
            detalhe = None
        if detalhe and detalhe.get("data_evento"):
            detalhe["data_evento"] = detalhe["data_evento"].astimezone(_BR_TZ)
        resolvido = bool(detalhe and detalhe.get("finalizado_por_gerente"))
        notificacoes.append({"pedido": p, "detalhe": detalhe, "resolvido": resolvido})

    devolucoes = await db_service.listar_devolucoes(pendentes=False)
    for d in devolucoes:
        if d.get("criado_em"):
            d["criado_em"] = d["criado_em"].astimezone(_BR_TZ)

    return templates.TemplateResponse("gerente_notificacoes.html", {
        "request": request, "notificacoes": notificacoes, "devolucoes": devolucoes,
    })


@router.post("/notificacoes/{venda}/finalizar")
async def finalizar_notificacao(venda: str):
    """Só o Gerente finaliza um cancelado — o Status na aba Vendas continua
    "Cancelado" pra sempre (nunca muda); só o Postgres marca como resolvido, e é
    esse flag que tira o card do Mural (ver routers/mural.py::mural_pedidos)."""
    try:
        await db_service.finalizar_cancelamento(venda)
    except Exception:
        pass
    return RedirectResponse("/gerente/notificacoes", status_code=303)


@router.post("/notificacoes/devolucao/{claim_id}/finalizar")
async def finalizar_devolucao_notificacao(claim_id: str):
    """Gerente finaliza uma devolução depois de acionar a equipe de soluções —
    sai da lista de pendentes, mas o registro continua no Postgres (auditoria)."""
    await db_service.finalizar_devolucao(claim_id)
    return RedirectResponse("/gerente/notificacoes", status_code=303)
