"""Painel do Gerente: auditoria de todos os pedidos (qualquer status), reversão
manual de status (correção quando uma etiqueta física se perde) e conexão/
desconexão de lojas do Mercado Livre. Acesso por senha própria (GERENTE_PASSWORD),
separada da senha operacional do Mural/Embalagem/Gaiolas.
"""
import json
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request, Form, Query, Depends
from fastapi.responses import RedirectResponse, Response
from templates_engine import templates
from services import vendas_db
from services import dashboard_db
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
# 'Expedição' = nosso galpão separa/embala/expede; 'Full' = Mercado Envios Full
# (o próprio ML cuida) — nunca aparece no Mural/Embalagem/Gaiolas, só aqui.
# 'Em análise' = classificação ainda não concluída. Não costuma aparecer: vendas nesse
# estado ficam escondidas durante a janela de `vendas_db.JANELA_ORIGEM_INDEFINIDA`, e o
# webhook de shipments resolve bem antes disso. Continua na lista porque, se alguma
# escapar da janela, ela reaparece aqui — e sem a opção o filtro (que vem todo marcado
# por padrão) esconderia de novo justo o pedido que precisa de atenção.
ORIGEM_OPCOES = ["Expedição", "Full", "Em análise"]


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


async def _opcoes_filtro() -> list[str]:
    """Lojas distintas com venda + devoluções — base estável pro dropdown de loja do
    Gerente (não é afetada pelos filtros atualmente aplicados). O filtro de dia virou
    um intervalo de calendário (dia_de/dia_ate), não precisa mais de opções
    pré-computadas.

    Usa o DISTINCT do banco: antes carregava TODOS os pedidos com todos os itens só
    pra ler um campo, e isso acontecia a cada render da tabela — uma varredura inteira
    além da que já busca os pedidos."""
    lojas = set(await vendas_db.listar_empresas())

    devolucoes = await db_service.listar_devolucoes(pendentes=False)
    for d in devolucoes:
        if d.get("empresa"):
            lojas.add(d["empresa"])

    return sorted(lojas)


def _devolucao_para_pedido(d: dict, pedidos_por_venda: dict[str, dict]) -> dict:
    """Converte um registro de devolução (Postgres) num dict no mesmo formato dos
    pedidos de venda, pra poder entrar na mesma tabela/filtros do Gerente.

    Recebe os pedidos já carregados em vez de buscar o seu: buscando um por um, cada
    devolução na tela custava uma varredura completa da tabela de vendas (ver
    `vendas_db.get_pedidos_por_venda`)."""
    pedido = pedidos_por_venda.get(d["venda_ml"]) if d.get("venda_ml") else None
    # A data que representa a devolução na tabela é a da CHEGADA quando ela existe:
    # `criado_em` é só quando nós registramos, que pode ser dias antes do pacote
    # aparecer no galpão (ou depois, se o registro veio atrasado).
    quando = d.get("chegou_em") or d.get("criado_em")
    data_limite = quando.astimezone(_BR_TZ).strftime("%d/%m/%Y") if quando else ""
    return {
        "venda": d.get("venda_ml") or d.get("order_id") or d["claim_id"],
        "order_id": d.get("order_id"),
        "empresa": d.get("empresa") or "",
        "id_exped": "—",
        "status": "Devolução",
        "impresso_em": None,
        "enviado_em": "",   # devolução não tem esse conceito — a venda já tinha sido enviada antes
        "itens": pedido["itens"] if pedido else [],
        "gaiola": "",
        "data_limite": data_limite,
        "origem": "Expedição",   # devolução nunca é de pedido Full (o ML trata isso sozinho)
        "criado_em": d.get("criado_em"),
        "chegou_em": d.get("chegou_em"),
        "_devolucao": d,
    }


def _pedido_visivel(p: dict, status: list[str], busca: str, loja: str,
                     dia_de: str, dia_ate: str, origem: list[str],
                     dia_criado_de: str = "", dia_criado_ate: str = "") -> bool:
    """Confere se um pedido (venda normal OU devolução sintética, mesmo formato
    de dict) ainda bate com o filtro atualmente aplicado na tela — usado pelas
    ações por linha (status/gaiola/NF-e/devolução) pra decidir se a linha some
    ou continua visível depois da mudança, sem precisar re-buscar a lista
    inteira (isso é o que preservava as edições pendentes de OUTRAS linhas)."""
    if status and p["status"].strip().lower() not in {s.strip().lower() for s in status}:
        return False
    if origem and p.get("origem", "Expedição") not in origem:
        return False
    if loja and (p["empresa"] or "").strip().lower() != loja.strip().lower():
        return False
    if dia_de or dia_ate:
        de = date.fromisoformat(dia_de) if dia_de else None
        ate = date.fromisoformat(dia_ate) if dia_ate else None
        d = _parse_data_br(p.get("data_limite"))
        if d is None or (de and d < de) or (ate and d > ate):
            return False
    if dia_criado_de or dia_criado_ate:
        de_c = date.fromisoformat(dia_criado_de) if dia_criado_de else None
        ate_c = date.fromisoformat(dia_criado_ate) if dia_criado_ate else None
        criado = p.get("criado_em")
        d_criado = criado.astimezone(_BR_TZ).date() if criado else None
        if d_criado is None or (de_c and d_criado < de_c) or (ate_c and d_criado > ate_c):
            return False
    busca_norm = (busca or "").strip().lower()
    if busca_norm:
        campos = [p["venda"], p.get("order_id") or "", p["empresa"], p.get("id_exped") or ""]
        campos += [i["sku"] for i in p.get("itens", [])]
        if not any(busca_norm in str(c).lower() for c in campos if c):
            return False
    return True


# Anterior a qualquer registro do sistema — usado só como piso de ordenação, pra que
# uma linha sem data vá pro fim em vez de estourar a comparação.
_SEM_DATA = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _quando_entrou(p: dict) -> datetime:
    """Momento que ordena a tabela do Gerente: para uma devolução é a chegada do
    pacote (caindo pro registro enquanto o ML não confirmou), e para uma venda é
    quando ela entrou. Sem isso as devoluções eram jogadas no fim da lista pelo
    append, fora de qualquer ordem cronológica."""
    return p.get("chegou_em") or p.get("criado_em") or _SEM_DATA


async def _pedidos_unificados(status: list[str], busca: str, loja: str,
                               dia_de: str, dia_ate: str, origem: list[str] | None = None,
                               dia_criado_de: str = "", dia_criado_ate: str = "") -> list[dict]:
    """Vendas normais (planilha) + devoluções (Postgres) numa lista só, com os
    mesmos filtros aplicados nas duas fontes. `dia_de`/`dia_ate` (data-limite de
    despacho) e `dia_criado_de`/`dia_criado_ate` (quando a venda foi registrada)
    no formato ISO ("AAAA-MM-DD", o que um <input type="date"> manda)."""
    # "Devolução" é categoria de exibição, não Status da planilha. Filtrar SÓ por ela
    # tem que devolver zero vendas — antes sobrava uma lista vazia de status, que
    # `get_all_pedidos` lia como "sem filtro" e trazia a tabela inteira de volta.
    status_sheets = [s for s in status if s in STATUS_OPCOES] if status else None
    so_devolucoes = bool(status) and not status_sheets
    origem_filter = origem or None

    if so_devolucoes:
        pedidos: list[dict] = []
    else:
        pedidos = await vendas_db.get_all_pedidos(status_sheets, busca, loja, dia_de, dia_ate,
                                                   origem_filter, dia_criado_de, dia_criado_ate)
        # Cancelado nunca muda de Status na planilha — o que diz se já foi tratado é
        # só o Postgres (ver routers/mural.py, mesma regra aplicada aqui pro badge).
        arquivados = await db_service.listar_cancelados_arquivados()
        # Pedido que virou "Cancelado" no ML por causa de uma devolução concluída
        # (reembolso total) já aparece como "Devolução" logo abaixo — tira daqui pra
        # não duplicar o mesmo pedido nos 2 grupos.
        com_devolucao = await db_service.listar_vendas_com_devolucao()
        pedidos = [p for p in pedidos if not (p["status"].lower() == "cancelado" and p["venda"] in com_devolucao)]
        for p in pedidos:
            if p["status"].lower() == "cancelado":
                p["_cancelado_resolvido"] = p["venda"] in arquivados

    if (not status or "Devolução" in status) and (not origem_filter or "Expedição" in origem_filter):
        de = date.fromisoformat(dia_de) if dia_de else None
        ate = date.fromisoformat(dia_ate) if dia_ate else None
        de_c = date.fromisoformat(dia_criado_de) if dia_criado_de else None
        ate_c = date.fromisoformat(dia_criado_ate) if dia_criado_ate else None
        devolucoes = await db_service.listar_devolucoes(pendentes=False)
        # Uma consulta pros pedidos de TODAS as devoluções, antes do laço.
        pedidos_dev = await vendas_db.get_pedidos_por_venda(
            [d["venda_ml"] for d in devolucoes if d.get("venda_ml")])
        busca_norm = (busca or "").strip().lower()
        for d in devolucoes:
            p = _devolucao_para_pedido(d, pedidos_dev)
            if loja and p["empresa"].strip().lower() != loja.strip().lower():
                continue
            if de or ate:
                data_p = _parse_data_br(p["data_limite"])
                if data_p is None or (de and data_p < de) or (ate and data_p > ate):
                    continue
            if de_c or ate_c:
                d_criado = p["criado_em"].astimezone(_BR_TZ).date() if p.get("criado_em") else None
                if d_criado is None or (de_c and d_criado < de_c) or (ate_c and d_criado > ate_c):
                    continue
            if busca_norm:
                campos = [p["venda"], p["order_id"] or "", p["empresa"]] + [i["sku"] for i in p["itens"]]
                if not any(busca_norm in str(c).lower() for c in campos if c):
                    continue
            pedidos.append(p)

    pedidos.sort(key=_quando_entrou, reverse=True)
    return pedidos


async def _paginar(request: Request, pedidos: list[dict], status: list[str], busca: str,
                    loja: str, dia_de: str, dia_ate: str, pagina: int, por_pagina: int,
                    mensagem: str = "", dia_criado_de: str = "", dia_criado_ate: str = ""):
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
        "dia_criado_de": dia_criado_de, "dia_criado_ate": dia_criado_ate,
        "pagina": pagina, "total_paginas": total_paginas, "total": total,
    })


@router.get("")
async def gerente_page(request: Request):
    lojas_disponiveis = await _opcoes_filtro()
    return templates.TemplateResponse("gerente.html", {
        "request": request, "status_opcoes": FILTRO_STATUS_OPCOES,
        "origem_opcoes": ORIGEM_OPCOES,
        "lojas_disponiveis": lojas_disponiveis,
    })


@router.get("/dashboard")
async def gerente_dashboard(request: Request):
    lojas_disponiveis = await _opcoes_filtro()
    hoje = date.today()
    return templates.TemplateResponse("gerente_dashboard.html", {
        "request": request,
        "origem_opcoes": ORIGEM_OPCOES,
        "lojas_disponiveis": lojas_disponiveis,
        # Mesmos 14 dias que o endpoint de dados assume por padrão — os campos já
        # abrem preenchidos pra ficar explícito qual período está na tela.
        "padrao_de": (hoje - timedelta(days=13)).isoformat(),
        "padrao_ate": hoje.isoformat(),
    })


@router.get("/dashboard/dados")
async def gerente_dashboard_dados(request: Request,
                                   loja: list[str] = Query([]), origem: list[str] = Query([]),
                                   dia_de: str = Query(""), dia_ate: str = Query(""),
                                   cancelados: bool = Query(False)):
    """Fragmento htmx com os números do painel. Sem período informado, mostra os
    últimos 14 dias — abrir a tela e ver a base inteira somada desde a migração
    diria pouco sobre a operação de agora."""
    de = date.fromisoformat(dia_de) if dia_de else date.today() - timedelta(days=13)
    ate = date.fromisoformat(dia_ate) if dia_ate else date.today()
    dados = await dashboard_db.carregar(de, ate, loja or None, origem or None, cancelados)
    # As mesmas cores que identificam a loja no Mural e na Embalagem — a cor da loja
    # é a mesma em todo o sistema, não uma paleta só do gráfico.
    cores = await TokenStore().get_cores_por_empresa()
    return templates.TemplateResponse("_gerente_dashboard_dados.html", {
        "request": request, "d": dados,
        "serie_json": json.dumps(dados["serie"]),
        "cores_json": json.dumps(cores),
        "cores": cores,
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
                           origem: list[str] = Query([]),
                           dia_criado_de: str = Query(""), dia_criado_ate: str = Query(""),
                           pagina: int = Query(1), por_pagina: int = Query(50)):
    pedidos = await _pedidos_unificados(status, busca, loja, dia_de, dia_ate, origem,
                                         dia_criado_de, dia_criado_ate)
    return await _paginar(request, pedidos, status, busca, loja, dia_de, dia_ate, pagina, por_pagina,
                           dia_criado_de=dia_criado_de, dia_criado_ate=dia_criado_ate)


def _linha_ou_vazio(request: Request, pedido: dict | None, status: list[str], busca: str,
                     loja: str, dia_de: str, dia_ate: str, origem: list[str],
                     dia_criado_de: str = "", dia_criado_ate: str = ""):
    """Devolve a linha atualizada se ainda bater com o filtro ativo, ou uma
    resposta vazia (o htmx remove a linha do DOM — `hx-swap="outerHTML"` com
    corpo vazio apaga o elemento) se o pedido não existir mais ou tiver saído
    do filtro (ex.: usuário filtrou só "Separando" e o pedido virou "Enviado")."""
    if not pedido or not _pedido_visivel(pedido, status, busca, loja, dia_de, dia_ate, origem,
                                          dia_criado_de, dia_criado_ate):
        return Response(content="", media_type="text/html")
    return templates.TemplateResponse("_gerente_pedido_linha.html", {
        "request": request, "p": pedido, "status_opcoes": STATUS_OPCOES,
    })


@router.post("/pedidos/{venda}/status")
async def reverter_status(request: Request, venda: str, novo_status: str = Form(...),
                           status: list[str] = Form([]), busca: str = Form(""),
                           loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                           origem: list[str] = Form([]),
                           dia_criado_de: str = Form(""), dia_criado_ate: str = Form("")):
    """Correção manual do Gerente — ex.: reabrir um pedido preso em "Separado"
    (etiqueta perdida) de volta pra "Separando" pra poder reimprimir.

    Atualiza SÓ a linha clicada (hx-target="closest tr" no template) — antes
    recarregava a tabela inteira a cada clique, o que descartava qualquer
    seleção ainda não aplicada em OUTRAS linhas no meio de uma correção em
    lote (usuário mudava o <select> de vários pedidos antes de clicar
    "Aplicar" em cada um; o refresh de um apagava a escolha dos demais). Ainda
    assim, se a mudança tira o pedido do filtro ativo, a linha some (só ELA,
    via `_pedido_visivel` + resposta vazia — não mexe nas demais)."""
    await vendas_db.set_status_for_venda(venda, novo_status)
    pedido = await vendas_db.get_pedido_by_venda(venda)
    if pedido and pedido["status"].lower() == "cancelado":
        arquivados = await db_service.listar_cancelados_arquivados()
        pedido["_cancelado_resolvido"] = pedido["venda"] in arquivados
    return _linha_ou_vazio(request, pedido, status, busca, loja, dia_de, dia_ate, origem,
                            dia_criado_de, dia_criado_ate)


@router.post("/pedidos/{venda}/remover-gaiola")
async def remover_da_gaiola(request: Request, venda: str,
                             status: list[str] = Form([]), busca: str = Form(""),
                             loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                             origem: list[str] = Form([]),
                             dia_criado_de: str = Form(""), dia_criado_ate: str = Form("")):
    """Tira o pedido da gaiola em que estava (volta pra "Aguardando box" em
    /gaiolas) — correção manual do Gerente, ex.: item bipado na gaiola errada.
    Recusa se o pedido já foi Enviado (ver vendas_db.remover_da_gaiola).
    Atualiza só a linha clicada — mesmo motivo do /status acima."""
    await vendas_db.remover_da_gaiola(venda)
    pedido = await vendas_db.get_pedido_by_venda(venda)
    return _linha_ou_vazio(request, pedido, status, busca, loja, dia_de, dia_ate, origem,
                            dia_criado_de, dia_criado_ate)


@router.post("/pedidos/{venda}/emitir-nf")
async def emitir_nf_manual(request: Request, venda: str,
                            status: list[str] = Form([]), busca: str = Form(""),
                            loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                            origem: list[str] = Form([]),
                            dia_criado_de: str = Form(""), dia_criado_ate: str = Form("")):
    """Botão manual do Gerente pra consultar/re-tentar a emissão da NF-e — usado
    quando o gatilho automático (separação/embalagem) ainda não rodou ou falhou
    (ex.: pedido antecipado que ainda não passou pela embalagem).
    Atualiza só a linha clicada — mesmo motivo do /status acima."""
    pedido = await vendas_db.get_pedido_by_venda(venda)
    if not pedido:
        return _linha_ou_vazio(request, pedido, status, busca, loja, dia_de, dia_ate, origem,
                                dia_criado_de, dia_criado_ate)
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
    pedido["_nf_mensagem"] = mensagem
    return _linha_ou_vazio(request, pedido, status, busca, loja, dia_de, dia_ate, origem,
                            dia_criado_de, dia_criado_ate)


@router.post("/pedidos/devolucao/{claim_id}/finalizar")
async def finalizar_devolucao_pedidos(request: Request, claim_id: str,
                                       status: list[str] = Form([]), busca: str = Form(""),
                                       loja: str = Form(""), dia_de: str = Form(""), dia_ate: str = Form(""),
                                       origem: list[str] = Form([]),
                                       dia_criado_de: str = Form(""), dia_criado_ate: str = Form("")):
    """Mesma ação de finalizar devolução, só que chamada da tabela unificada de
    pedidos (htmx). Atualiza só a linha clicada — mesmo motivo do /status acima."""
    await db_service.finalizar_devolucao(claim_id)
    d = await db_service.get_devolucao(claim_id)
    pedidos_dev = await vendas_db.get_pedidos_por_venda([d["venda_ml"]]) if d and d.get("venda_ml") else {}
    pedido = _devolucao_para_pedido(d, pedidos_dev) if d else None
    return _linha_ou_vazio(request, pedido, status, busca, loja, dia_de, dia_ate, origem,
                            dia_criado_de, dia_criado_ate)


@router.get("/lojas")
async def gerente_lojas_page(request: Request):
    lojas = await TokenStore().list_stores()
    return templates.TemplateResponse("gerente_lojas.html", {"request": request, "lojas": lojas})


@router.post("/lojas/{user_id}/desconectar")
async def desconectar_loja(user_id: str):
    await TokenStore().delete_store(user_id)
    return RedirectResponse("/gerente/lojas", status_code=303)


@router.post("/lojas/{user_id}/cor")
async def definir_cor_loja(user_id: str, cor: str = Form(...)):
    """Cor do card da loja, escolhida pelo Gerente — aparece no Mural e na fila
    da Embalagem pra diferenciar visualmente pedidos de lojas diferentes."""
    await TokenStore().set_color(user_id, cor)
    return RedirectResponse("/gerente/lojas", status_code=303)


@router.post("/lojas/{user_id}/prefixo")
async def definir_prefixo_loja(user_id: str, sku_prefixo: str = Form("")):
    """Prefixo do SKU dessa loja, informado pelo Gerente — usado só pra agrupar
    a tela de Endereçamento por loja (accordion) e pra reconciliar com
    segurança os SKUs removidos de 1 loja só."""
    await TokenStore().set_sku_prefixo(user_id, sku_prefixo.strip().upper())
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
    pedidos = await vendas_db.get_all_pedidos(status_filter=["Cancelado"])
    # Mesma regra do Mural/pedidos unificado: pedido "Cancelado" que na verdade é
    # uma devolução concluída não entra aqui — já tem o registro dele na seção de
    # devoluções logo abaixo, então listar os 2 seria o mesmo pedido duplicado.
    com_devolucao = await db_service.listar_vendas_com_devolucao()
    pedidos = [p for p in pedidos if p["venda"] not in com_devolucao]
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
        if d.get("chegou_em"):
            d["chegou_em"] = d["chegou_em"].astimezone(_BR_TZ)

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
