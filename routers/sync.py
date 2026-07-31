from fastapi import APIRouter, BackgroundTasks, Query
from services.sync_service import (
    sync_all_products, sync_one_company, backfill_orders_for_company,
    refresh_pending_fiscal, send_fiscal_to_meli,
)
from services.token_store import TokenStore
from services import enderecos_db

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/products")
async def sync_products(
    background_tasks: BackgroundTasks,
    company: str | None = Query(None, description="Nome da loja ou vazio para todas"),
):
    if company:
        background_tasks.add_task(sync_one_company, company)
        return {"status": "started", "company": company}
    background_tasks.add_task(sync_all_products)
    return {"status": "started", "scope": "todas as lojas conectadas"}


@router.post("/orders")
async def sync_orders(
    background_tasks: BackgroundTasks,
    company: str = Query(..., description="Nome da loja"),
    max: int = Query(50, description="Quantos pedidos recentes importar"),
):
    """Importa vendas passadas (backfill). Novas vendas chegam pelo webhook."""
    background_tasks.add_task(backfill_orders_for_company, company, max)
    return {"status": "started", "company": company, "max": max}


@router.post("/fiscal")
async def sync_fiscal(
    background_tasks: BackgroundTasks,
    company: str | None = Query(None, description="Nome da loja ou vazio para todas"),
):
    """Preenche a NF (J..O) das vendas cuja nota o ML só emitiu depois do pedido."""
    background_tasks.add_task(refresh_pending_fiscal, company)
    return {"status": "started", "company": company or "todas"}


@router.post("/fiscal/send")
async def send_fiscal(
    background_tasks: BackgroundTasks,
    company: str | None = Query(None, description="Nome da loja ou vazio para todas"),
):
    """Envia ao ML os dados fiscais preenchidos na aba Fiscal (status grava na própria aba)."""
    background_tasks.add_task(send_fiscal_to_meli, company)
    return {"status": "started", "company": company or "todas"}


@router.get("/stores")
async def list_stores():
    stores = await TokenStore().list_stores()
    # não expõe os tokens
    return {"stores": [
        {"company_key": s.get("company_key"), "sheet_tab": s.get("sheet_tab"),
         "user_id": s.get("user_id"), "nickname": s.get("nickname")}
        for s in stores
    ]}


@router.get("/addresses")
async def list_addresses():
    return {"addresses": await enderecos_db.get_addresses()}
