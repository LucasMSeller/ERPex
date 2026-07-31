"""Cadastro de endereçamento (SKU -> corredor/estante/prateleira). Postgres
(services/enderecos_db.py) — fase 1 da migração pra fora do Sheets."""
from fastapi import APIRouter, Request, Form, Query, Depends
from templates_engine import templates
from services import enderecos_db
from services.sync_service import reconciliar_enderecos
from services.token_store import TokenStore
from services.session_auth import require_login

router = APIRouter(prefix="/enderecos", tags=["enderecos"], dependencies=[Depends(require_login)])


async def _lojas() -> list[dict]:
    """Lojas conectadas com nome de exibição (nickname do ML, senão company_key)
    e o prefixo de SKU informado pelo Gerente em /gerente/lojas (pode estar
    vazio, se ainda não foi preenchido)."""
    lojas = []
    for s in await TokenStore().list_stores():
        lojas.append({
            "user_id": s.get("user_id"),
            "nome": s.get("nickname") or s.get("company_key"),
            "prefixo": (s.get("sku_prefixo") or "").strip().upper(),
        })
    return lojas


def _loja_do_sku(sku: str, lojas: list[dict]) -> str:
    """Agrupamento só visual (accordion) — o endereço em si não pertence a
    nenhuma loja (ver services/enderecos_db.py). Sem prefixo configurado pra
    nenhuma loja, cai em "Outras"."""
    for loja in lojas:
        if loja["prefixo"] and sku.startswith(loja["prefixo"]):
            return loja["nome"]
    return "Outras"


async def _lista_context(request: Request, mensagem: str = "", busca: str = "") -> dict:
    enderecos = await enderecos_db.get_addresses_full(busca)
    lojas = await _lojas()
    grupos: dict[str, list] = {}
    for e in enderecos:
        grupos.setdefault(_loja_do_sku(e["sku"], lojas), []).append(e)
    nomes = [l["nome"] for l in lojas]
    # Toda loja conectada ganha uma seção, mesmo com 0 SKU ainda — senão uma
    # loja recém-conectada não teria onde colocar o botão "Vincular SKUs"
    # antes do 1º endereço existir pra ela.
    for nome in nomes:
        grupos.setdefault(nome, [])
    ordem = nomes + ["Outras"]
    grupos_ordenados = [(nome, grupos[nome]) for nome in ordem if nome in grupos]
    return {
        "request": request,
        "grupos": grupos_ordenados,
        "mensagem": mensagem,
        "busca": busca,
        "lojas": nomes,
    }


@router.get("")
async def enderecos_page(request: Request):
    return templates.TemplateResponse("enderecos.html", await _lista_context(request))


@router.get("/lista")
async def enderecos_lista(request: Request, busca: str = Query("")):
    return templates.TemplateResponse("_enderecos_lista.html", await _lista_context(request, busca=busca))


@router.post("")
async def salvar_endereco(request: Request, sku: str = Form(...), corredor: str = Form(""),
                           estante: str = Form(""), prateleira: str = Form(""), busca: str = Form("")):
    sku = sku.strip()
    if not sku:
        ctx = await _lista_context(request, mensagem="SKU não pode ser vazio.", busca=busca)
        return templates.TemplateResponse("_enderecos_lista.html", ctx)
    await enderecos_db.set_address_for_sku(sku, corredor.strip(), estante.strip(), prateleira.strip())
    ctx = await _lista_context(request, mensagem=f"Endereço de {sku} salvo.", busca=busca)
    return templates.TemplateResponse("_enderecos_lista.html", ctx)


@router.post("/vincular-skus/{loja}")
async def vincular_skus(request: Request, loja: str, busca: str = Form("")):
    """Traz os SKUs reais de 1 loja só, direto do Mercado Livre: adiciona os
    novos em branco, mantém os já cadastrados. Se a loja já tem um prefixo de
    SKU configurado (/gerente/lojas), também remove com segurança os SKUs que
    sumiram do catálogo dessa loja — sem prefixo configurado, só insere.

    Roda "amarrado" na própria requisição de propósito (fica até terminar) —
    o Cloud Run hoje só garante CPU enquanto a requisição estiver ativa, então
    "soltar" isso em segundo plano (BackgroundTasks) trava no meio sem aviso."""
    prefixo = next((l["prefixo"] for l in await _lojas() if l["nome"] == loja and l["prefixo"]), None)
    resultado = await reconciliar_enderecos(company_key=loja, sku_prefixo=prefixo)
    mensagem = (f"{loja}: {resultado['total']} SKU(s) no catálogo — "
                f"{resultado['novos']} novo(s) adicionado(s), "
                f"{resultado['removidos']} removido(s).")
    ctx = await _lista_context(request, mensagem=mensagem, busca=busca)
    return templates.TemplateResponse("_enderecos_lista.html", ctx)
