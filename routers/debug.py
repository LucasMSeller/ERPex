"""Rotas de manutenção pontual (rodadas manualmente via browser/curl)."""
from fastapi import APIRouter, Query, HTTPException
from services.sheets_service import SheetsService
from services.token_store import TokenStore
from services.meli_service import MeliService
from services import enderecos_db
from models.product import Product

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/sku/{mlb_id}")
async def sku_sources(mlb_id: str, company: str = Query(...)):
    """Mostra ONDE está (ou não) o SKU de um anúncio — p/ achar SKU que cai no MLB."""
    store = TokenStore().get_by_company(company)
    if not store:
        raise HTTPException(404, f"Loja '{company}' não conectada.")
    meli = MeliService(store, token_store=TokenStore())
    body = await meli._get(f"/items/{mlb_id}")
    attrs = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}
    cache: dict = {}
    variacoes = []
    for v in body.get("variations") or []:
        va = {a["id"]: a.get("value_name") for a in v.get("attributes", [])}
        upid = v.get("user_product_id")
        variacoes.append({
            "variation_id": v.get("id"),
            "seller_sku": v.get("seller_sku"),
            "seller_custom_field": v.get("seller_custom_field"),
            "SELLER_SKU_attr": va.get("SELLER_SKU"),
            "GTIN_attr": va.get("GTIN"),
            "user_product_id": upid,
            "user_product_SELLER_SKU": await meli._user_product_sku(str(upid), cache) if upid else None,
        })
    prod = Product.from_meli_item(body, store["company_key"])
    return {
        "mlb": mlb_id,
        "resultado_from_meli_item": {"sku": prod.sku, "sku_is_real": prod.sku_is_real},
        "item_seller_custom_field": body.get("seller_custom_field"),
        "item_seller_sku": body.get("seller_sku"),
        "item_SELLER_SKU_attr": attrs.get("SELLER_SKU"),
        "item_resolved_sku": body.get("_resolved_sku"),
        "tem_variacoes": bool(body.get("variations")),
        "variacoes": variacoes,
    }


@router.get("/apply-layout")
async def apply_layout_now():
    """Aplica o tema visual unificado às abas (Vendas, Endereçamento, Fiscal)."""
    return SheetsService().apply_layout()


@router.get("/dedupe-vendas")
async def dedupe_vendas_now():
    """Remove duplicatas da aba Vendas (mesmo order_id + SKU)."""
    return {"linhas_apagadas": SheetsService().dedupe_vendas()}


@router.get("/clean-enderecamento")
async def clean_enderecamento_now():
    """Remove do Endereçamento as linhas de MLB de fallback (anúncios sem SKU real)."""
    return {"linhas_removidas": await enderecos_db.remove_mlb_addresses()}


@router.get("/migrate-enderecos")
async def migrate_enderecos_now():
    """Migração pontual (fase 1 Sheets->Postgres): copia o Endereçamento do Sheets
    pro Postgres. Só LÊ o Sheets, escreve numa tabela nova/vazia — remover esta
    rota depois que a migração for confirmada e cortada."""
    linhas = SheetsService().get_addresses_full("")
    await enderecos_db.bulk_upsert(linhas)

    pg = await enderecos_db.get_addresses_full("")
    pg_por_sku = {e["sku"]: e for e in pg}
    divergencias = []
    for e in linhas:
        pg_e = pg_por_sku.get(e["sku"])
        if pg_e is None:
            divergencias.append({"sku": e["sku"], "problema": "faltando no Postgres"})
            continue
        for campo in ("corredor", "estante", "prateleira"):
            if (e.get(campo) or "") != (pg_e.get(campo) or ""):
                divergencias.append({"sku": e["sku"], "campo": campo,
                                      "sheets": e.get(campo), "postgres": pg_e.get(campo)})

    return {
        "sheets_linhas": len(linhas), "postgres_linhas": len(pg),
        "contagem_bate": len(linhas) == len(pg),
        "divergencias": divergencias,
    }


@router.get("/vincular-skus")
async def vincular_skus_now(company: str | None = Query(None), sku_prefixo: str | None = Query(None)):
    """Reconcilia o Endereçamento com os SKUs reais atuais (mesma ação do botão
    "Vincular SKUs" em /enderecos). Sem `company`: todas as lojas. Com `company`
    (+ opcionalmente `sku_prefixo`): só 1 loja, mesmo escopo do botão por loja."""
    from services.sync_service import reconciliar_enderecos
    return await reconciliar_enderecos(company_key=company, sku_prefixo=sku_prefixo)


@router.get("/refresh-fiscal")
async def refresh_fiscal_now(company: str | None = Query(None)):
    """Preenche a NF (J..O) das vendas pendentes lendo o ML agora."""
    from services.sync_service import refresh_pending_fiscal
    return await refresh_pending_fiscal(company)


@router.get("/backfill-venda-ml")
async def backfill_venda_ml_now(company: str = Query(...)):
    """Preenche a coluna Nº venda (ML) das vendas já existentes."""
    from services.sync_service import backfill_venda_ml
    return await backfill_venda_ml(company)


@router.get("/backfill-expedicao")
async def backfill_expedicao_now(company: str = Query(...)):
    """Gera o ID de expedição das vendas já existentes que ainda não têm."""
    from services.sync_service import backfill_expedition_ids
    return await backfill_expedition_ids(company)
