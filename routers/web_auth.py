from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from templates_engine import templates
from services.session_auth import check_password, check_gerente_password

router = APIRouter(tags=["web-auth"])


@router.get("/login")
async def login_form(request: Request):
    if request.session.get("auth"):
        return RedirectResponse("/mural", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "erro": None})


@router.post("/login")
async def login_submit(request: Request, senha: str = Form(...)):
    if check_password(senha):
        request.session["auth"] = True
        return RedirectResponse("/mural", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "erro": "Senha incorreta."}, status_code=401,
    )


@router.get("/gerente/login")
async def gerente_login_form(request: Request):
    if request.session.get("gerente"):
        return RedirectResponse("/gerente", status_code=303)
    return templates.TemplateResponse("gerente_login.html", {"request": request, "erro": None})


@router.post("/gerente/login")
async def gerente_login_submit(request: Request, senha: str = Form(...)):
    if check_gerente_password(senha):
        request.session["gerente"] = True
        return RedirectResponse("/gerente", status_code=303)
    return templates.TemplateResponse(
        "gerente_login.html", {"request": request, "erro": "Senha incorreta."}, status_code=401,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
