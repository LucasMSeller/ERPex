"""Rotas de documentos do pedido: etiqueta de envio e nota fiscal."""
import logging
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from services.token_store import TokenStore
from services.meli_service import MeliService

router = APIRouter(prefix="/docs", tags=["documents"])
logger = logging.getLogger(__name__)

# Consulta pública da NF-e no portal nacional da SEFAZ (usuário resolve o captcha lá)
_SEFAZ = "https://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx?tipoConsulta=resumo&nfe={chave}"

_PAGE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f1f3f4;
          margin:0; padding:24px; color:#202124; }}
  .card {{ max-width:640px; margin:0 auto; background:#fff; border-radius:12px;
           box-shadow:0 1px 4px rgba(0,0,0,.15); overflow:hidden; }}
  .head {{ background:{accent}; color:#fff; padding:18px 24px; font-size:22px; font-weight:700; }}
  .body {{ padding:24px; }}
  table {{ width:100%; border-collapse:collapse; font-size:16px; }}
  td {{ padding:10px 6px; border-bottom:1px solid #eee; }}
  td.k {{ color:#5f6368; width:42%; }}
  td.v {{ font-weight:600; }}
  .key {{ font-family:monospace; font-size:14px; word-break:break-all; }}
  .btn {{ display:inline-block; margin-top:20px; background:#1a73e8; color:#fff;
          text-decoration:none; padding:14px 22px; border-radius:8px; font-size:17px; font-weight:600; }}
  .warn {{ padding:24px; font-size:18px; line-height:1.5; }}
</style></head><body><div class="card">{content}</div></body></html>"""


async def _meli_for(company: str) -> MeliService:
    store = await TokenStore().get_by_company(company)
    if not store:
        raise HTTPException(404, f"Loja '{company}' não conectada.")
    return MeliService(store, token_store=TokenStore())


async def _shipping_id(meli: MeliService, order_id: str) -> str:
    order = await meli.get_order(order_id)
    sid = (order.get("shipping") or {}).get("id")
    if not sid:
        raise HTTPException(404, "Pedido sem envio (shipping) associado.")
    return str(sid)


@router.get("/nf-sefaz/{numero}")
async def nf_sefaz(numero: str, company: str = Query(...)):
    """Busca a chave da NF e REDIRECIONA direto pro portal da SEFAZ (sem tela intermediária)."""
    meli = await _meli_for(company)
    chave = ""
    try:
        _order, shipping_id = await meli.resolve_order_and_shipping(numero)
        if shipping_id:
            data = await meli.get_invoice_data(shipping_id)
            chave = (data or {}).get("fiscal_key") or ""
    except Exception as e:
        logger.warning("nf-sefaz %s: %s", numero, e)
    if chave:
        return RedirectResponse(_SEFAZ.format(chave=chave))
    html = _PAGE.format(
        title="NF ainda não emitida", accent="#cc6600",
        content='<div class="head">📄 NF ainda não emitida</div>'
                '<div class="warn">A nota fiscal deste pedido ainda não foi emitida pelo '
                'Mercado Livre (a chave de acesso aparece minutos após a venda). '
                'Tente novamente em instantes.</div>')
    return HTMLResponse(html, status_code=200)


@router.get("/nf/{order_id}")
async def get_nf(order_id: str, company: str = Query(...)):
    """Página formatada com os dados da NF-e (chave de acesso + consulta DANFE)."""
    meli = await _meli_for(company)
    shipping_id = await _shipping_id(meli, order_id)
    try:
        data = await meli.get_invoice_data(shipping_id)
    except Exception as e:
        html = _PAGE.format(
            title="NF indisponível", accent="#cc6600",
            content=f'<div class="head">📄 NF indisponível</div>'
                    f'<div class="warn">Não foi possível obter a nota deste pedido.<br>'
                    f'<small style="color:#888">{str(e)[:150]}</small></div>')
        return HTMLResponse(html, status_code=200)

    # dados do comprador (CPF/CNPJ + nome) — endpoint separado, tolerante a falha
    comprador = documento = ""
    try:
        from services.meli_service import _parse_billing
        comprador, documento = _parse_billing(await meli.get_billing_info(order_id))
    except Exception:
        pass

    fiscal_key = data.get("fiscal_key", "")
    danfe = (
        f"https://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx"
        f"?tipoConsulta=resumo&nfe={fiscal_key}" if fiscal_key else ""
    )
    valor = data.get("invoice_amount")
    valor_fmt = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if valor else "—"
    data_nf = (data.get("invoice_date") or "")[:10]
    if data_nf and "-" in data_nf:
        a, m, d = data_nf.split("-")
        data_nf = f"{d}/{m}/{a}"

    linhas = f"""
      <tr><td class="k">Pedido</td><td class="v">{order_id}</td></tr>
      <tr><td class="k">Comprador</td><td class="v">{comprador or '—'}</td></tr>
      <tr><td class="k">CPF / CNPJ</td><td class="v">{documento or '—'}</td></tr>
      <tr><td class="k">NF número / série</td><td class="v">{data.get('invoice_number','—')} / {data.get('invoice_serie','—')}</td></tr>
      <tr><td class="k">Emissão</td><td class="v">{data_nf or '—'}</td></tr>
      <tr><td class="k">Valor</td><td class="v">{valor_fmt}</td></tr>
      <tr><td class="k">Produto</td><td class="v">{data.get('item_title','—')}</td></tr>
      <tr><td class="k">CFOP</td><td class="v">{(data.get('additional_data') or {}).get('cfop','—')}</td></tr>
      <tr><td class="k">Chave de acesso</td><td class="v key">{fiscal_key or '—'}</td></tr>
    """
    botao = f'<a class="btn" href="{danfe}" target="_blank">🔎 Consultar DANFE na SEFAZ</a>' if danfe else ""
    content = (f'<div class="head">📄 Nota Fiscal — Pedido {order_id}</div>'
               f'<div class="body"><table>{linhas}</table>{botao}</div>')
    return HTMLResponse(_PAGE.format(title=f"NF Pedido {order_id}", accent="#1a73e8", content=content))
