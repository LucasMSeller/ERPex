"""Embalagem: bipar o QR da etiqueta de separação valida o pedido e dispara a
impressão de 2 etiquetas (nosso QR + etiqueta do ML) em 2 impressoras diferentes.

O Status só vira "Embalado" depois que o navegador confirma (via QZ Tray) que as
impressões deram certo — ver POST /embalagem/confirmar, chamado pelo JS da página.
"""
import json
import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from templates_engine import templates
from services.sheets_service import SheetsService
from services.token_store import TokenStore
from services.session_auth import require_login
from services.qz_signing import QZ_CERT
from config.settings import get_settings
from routers.print_labels import _meli_for, _montar_zpl_embalagem, QZ_TRAY_CDN

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embalagem", tags=["embalagem"], dependencies=[Depends(require_login)])


class CodigoIn(BaseModel):
    codigo: str


class VendaIn(BaseModel):
    venda: str


@router.get("")
async def embalagem_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse("embalagem.html", {
        "request": request,
        "cdn": QZ_TRAY_CDN,
        "cert_js": json.dumps(QZ_CERT),
        "printer_pedido_js": json.dumps(settings.printer_pedido_name),
        "printer_meli_js": json.dumps(settings.printer_meli_name),
    })


@router.get("/pedidos")
async def embalagem_pedidos(request: Request):
    """Fila de pedidos aguardando embalagem (Status=="Separado"). Não mostra o
    nosso ID de expedição — só serve de referência visual, a validação de verdade
    continua sendo a bipagem do QR físico da etiqueta de separação."""
    pedidos = SheetsService().get_all_pedidos(["Separado"])
    cores = TokenStore().get_cores_por_empresa()
    return templates.TemplateResponse("_embalagem_pedidos.html", {
        "request": request, "pedidos": pedidos, "cores": cores,
    })


@router.post("/validar")
async def validar(body: CodigoIn):
    """Confere se o ID bipado é um pedido válido e pendente; monta os 2 ZPLs.
    NÃO altera nada no Sheets — só o /confirmar (depois da impressão) faz isso."""
    codigo = body.codigo.strip()
    if not codigo:
        return {"ok": False, "msg": "Código vazio."}

    pedido = SheetsService().get_pedido_by_exped(codigo)
    if not pedido:
        return {"ok": False, "msg": f'ID "{codigo}" não encontrado.'}

    status = pedido["status"].strip().lower()
    if status == "cancelado":
        return {"ok": False, "msg": f'Pedido {pedido["venda"]} foi CANCELADO no Mercado Livre — não pode ser embalado.'}
    if status == "separando":
        return {"ok": False, "msg": f'Pedido {pedido["venda"]} ainda não teve a etiqueta de separação impressa.'}
    if status in ("embalado", "enviado"):
        return {"ok": False, "msg": f'Pedido {pedido["venda"]} já está "{pedido["status"]}".'}
    if status != "separado":
        return {"ok": False, "msg": f'Pedido {pedido["venda"]} está em status "{pedido["status"]}", não pode embalar.'}

    try:
        zpl_pedido, zpl_ml, aviso, order_id, bloqueia = await _montar_zpl_embalagem(pedido, pedido["empresa"])
    except HTTPException as e:
        return {"ok": False, "msg": str(e.detail)}
    if bloqueia:
        return {"ok": False, "msg": f'Pedido {pedido["venda"]}: {aviso}'}

    # Gatilho de NF-e: aqui a etiqueta do ML só existe pq já é o dia real do envio,
    # então sempre tentamos (rede de segurança pra quando a separação foi antecipada
    # e o gatilho de lá não rodou ainda). Best-effort — nunca bloqueia a embalagem.
    if order_id:
        try:
            meli = await _meli_for(pedido["empresa"])
            resultado = await meli.ensure_invoice(order_id)
            if resultado["erro"]:
                aviso = (aviso + " " if aviso else "") + f"NF-e: erro ao emitir ({resultado['erro'][:120]})."
            elif resultado["ja_existia"]:
                aviso = (aviso + " " if aviso else "") + "NF-e já emitida anteriormente."
            else:
                aviso = (aviso + " " if aviso else "") + "NF-e emitida."
        except Exception as e:
            logger.warning("Gatilho NF-e (venda %s): falha inesperada — %s", pedido["venda"], e)

    return {
        "ok": True,
        "venda": pedido["venda"],
        "empresa": pedido["empresa"],
        "exped_id": pedido["id_exped"],
        "zpl_pedido": zpl_pedido,
        "zpl_ml": zpl_ml,
        "aviso": aviso,
    }


@router.post("/confirmar")
async def confirmar(body: VendaIn):
    """Chamado pelo JS só depois que o QZ Tray confirma as impressões com sucesso."""
    n = SheetsService().set_status_for_venda(body.venda, "Embalado")
    return {"ok": True, "linhas": n}
