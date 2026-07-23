"""Gaiolas: bipagem de pacotes embalados e coleta por gaiola."""
from fastapi import APIRouter, Request, Form, Depends
from templates_engine import templates
from services.sheets_service import SheetsService
from services.session_auth import require_login

router = APIRouter(prefix="/gaiolas", tags=["gaiolas"], dependencies=[Depends(require_login)])


def _grid_context(request: Request, mensagem: str = "", erro: bool = False) -> dict:
    estado = SheetsService().get_gaiolas_estado()
    return {"request": request, "estado": estado, "mensagem": mensagem, "erro": erro}


@router.get("")
async def gaiolas_page(request: Request):
    return templates.TemplateResponse("gaiolas.html", {"request": request})


@router.get("/estado")
async def gaiolas_estado(request: Request):
    return templates.TemplateResponse("_gaiolas_grid.html", _grid_context(request))


@router.post("/bipar")
async def bipar(request: Request, codigo: str = Form(...)):
    partes = codigo.strip().split("|", 1)
    if len(partes) < 2:
        ctx = _grid_context(request, mensagem='Formato inválido. Use "N|ID" (ex.: 2|PLG260726001).', erro=True)
        return templates.TemplateResponse("_gaiolas_grid.html", ctx)

    prefixo, id_exped = partes[0].strip(), partes[1].strip()
    gaiola = f"Gaiola {prefixo}" if prefixo.isdigit() else prefixo

    resultado = SheetsService().mover_para_gaiola(id_exped, gaiola)
    if resultado["ok"]:
        mensagem = f'{id_exped} → {gaiola} ({resultado["n_sku"]} item(ns))'
        ctx = _grid_context(request, mensagem=mensagem, erro=False)
    else:
        ctx = _grid_context(request, mensagem=resultado["msg"], erro=True)
    return templates.TemplateResponse("_gaiolas_grid.html", ctx)


@router.post("/{gaiola}/coletar")
async def coletar(request: Request, gaiola: str):
    resultado = SheetsService().coletar_gaiola(gaiola)
    mensagem = f'{gaiola}: {resultado["n_pacotes"]} pacote(s) coletado(s).'
    ctx = _grid_context(request, mensagem=mensagem, erro=False)
    return templates.TemplateResponse("_gaiolas_grid.html", ctx)
