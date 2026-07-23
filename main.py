import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from routers import (
    auth, webhooks, sync, debug, documents, print_labels, web_auth,
    mural, gaiolas, embalagem, gerente, enderecos,
)
from services.session_auth import NotAuthenticated
from services import db as db_service
from config.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger(__name__).info("ERPex backend iniciado.")
    await db_service.ensure_schema()
    yield
    await db_service.close()


app = FastAPI(
    title="ERPex – ERP / WMS Mercado Livre + Google Sheets",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=settings.session_cookie_https_only,
    max_age=60 * 60 * 24 * 30,  # 30 dias
)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": exc.redirect_to})
    return RedirectResponse(exc.redirect_to, status_code=303)


app.include_router(auth.router)
app.include_router(webhooks.router)
app.include_router(sync.router)
app.include_router(debug.router)
app.include_router(documents.router)
app.include_router(print_labels.router)
app.include_router(web_auth.router)
app.include_router(mural.router)
app.include_router(gaiolas.router)
app.include_router(embalagem.router)
app.include_router(enderecos.router)
app.include_router(gerente.router)


@app.get("/")
async def root():
    return RedirectResponse("/mural")


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=True)
