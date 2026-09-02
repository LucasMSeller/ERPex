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
            "user_id": str(s.get("user_id") or ""),
            "nome": s.get("nickname") or s.get("company_key"),
            "prefixo": (s.get("sku_prefixo") or "").strip().upper(),
        })
    return lojas


def _grupo_pelo_prefixo(sku: str, lojas: list[dict]) -> str:
    """Rede de segurança pros SKUs sem vínculo registrado: loja que ainda não
    foi vinculada uma vez, e SKUs que nenhuma loja anuncia mais. Sem prefixo
    configurado pra nenhuma loja, cai em "Outras"."""
    for loja in lojas:
        if loja["prefixo"] and sku.startswith(loja["prefixo"]):
            return loja["nome"]
    return "Outras"


def _desativado(e: dict) -> bool:
    """Vínculo que existe e foi desligado à mão. SKU sem vínculo nenhum
    (`ativo` = None) não é "desativado" — é SKU que loja nenhuma reivindicou
    ainda, e vai pra lista normal."""
    return bool(e["loja_user_id"]) and not e["ativo"]


async def _lista_context(request: Request, mensagem: str = "", busca: str = "",
                         abrir: str = "", abrir_desativados: bool = False) -> dict:
    """Monta as gavetas a partir do vínculo SKU↔loja (services/enderecos_db.py):
    cada loja lista o que ELA anuncia, então um SKU vendido por duas contas
    aparece nas duas — continuando a ser um endereço só, editável de qualquer
    uma delas. O que foi desligado sai da lista principal e vai pra subgaveta
    daquela loja.

    `abrir` (nome de loja) deixa aquela gaveta aberta na próxima renderização:
    o htmx troca a lista inteira a cada clique, então sem isso o SKU que você
    acabou de desligar sumiria de vista junto com a gaveta fechando."""
    enderecos = await enderecos_db.get_addresses_full(busca)
    lojas = await _lojas()
    vinculos = await enderecos_db.get_vinculos()
    grupos: dict[str, list] = {}
    for e in enderecos:
        donas = [l for l in lojas if e["sku"] in vinculos.get(l["user_id"], {})]
        if donas:
            for l in donas:
                grupos.setdefault(l["nome"], []).append(
                    {**e, "loja_user_id": l["user_id"], "ativo": vinculos[l["user_id"]][e["sku"]]})
        else:
            grupos.setdefault(_grupo_pelo_prefixo(e["sku"], lojas), []).append(
                {**e, "loja_user_id": "", "ativo": None})
    nomes = [l["nome"] for l in lojas]
    # Toda loja conectada ganha uma seção, mesmo com 0 SKU ainda — senão uma
    # loja recém-conectada não teria onde colocar o botão "Vincular SKUs"
    # antes do 1º endereço existir pra ela.
    for nome in nomes:
        grupos.setdefault(nome, [])
    ordem = nomes + ["Outras"]
    grupos_ordenados = [
        {"nome": nome,
         "ativos": [e for e in grupos[nome] if not _desativado(e)],
         "desativados": [e for e in grupos[nome] if _desativado(e)]}
        for nome in ordem if nome in grupos
    ]
    return {
        "request": request,
        "grupos": grupos_ordenados,
        "mensagem": mensagem,
        "busca": busca,
        "lojas": nomes,
        "abrir": abrir,
        "abrir_desativados": abrir_desativados,
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


@router.post("/vinculo")
async def alternar_vinculo(request: Request, sku: str = Form(...), loja_user_id: str = Form(...),
                           ativo: str = Form(...), busca: str = Form("")):
    """Liga/desliga um SKU numa loja só. Não mexe no endereço nem nas outras
    lojas — a chave do vínculo é o par (sku, loja).

    Reabre a gaveta dessa loja (e a subgaveta, se o SKU acabou de ser
    desligado) pra você ver onde ele foi parar."""
    ligando = ativo == "1"
    await enderecos_db.set_vinculo_ativo(sku, loja_user_id, ligando)
    nome = next((l["nome"] for l in await _lojas() if l["user_id"] == loja_user_id), "")
    ctx = await _lista_context(request, busca=busca, abrir=nome, abrir_desativados=not ligando)
    return templates.TemplateResponse("_enderecos_lista.html", ctx)


@router.post("/vincular-skus/{loja}")
async def vincular_skus(request: Request, loja: str, busca: str = Form("")):
    """Traz os SKUs reais de 1 loja só, direto do Mercado Livre: adiciona os
    novos em branco, mantém os já cadastrados e atualiza o vínculo dessa loja.
    Um endereço só é removido se o SKU saiu do catálogo dela e nenhuma outra
    loja o anuncia. Não encosta no interruptor `ativo` de vínculo existente.

    Roda "amarrado" na própria requisição de propósito (fica até terminar) —
    o Cloud Run hoje só garante CPU enquanto a requisição estiver ativa, então
    "soltar" isso em segundo plano (BackgroundTasks) trava no meio sem aviso."""
    resultado = await reconciliar_enderecos(company_key=loja)
    mensagem = (f"{loja}: {resultado['total']} SKU(s) no catálogo — "
                f"{resultado['novos']} novo(s) adicionado(s), "
                f"{resultado['removidos']} removido(s).")
    ctx = await _lista_context(request, mensagem=mensagem, busca=busca)
    return templates.TemplateResponse("_enderecos_lista.html", ctx)
