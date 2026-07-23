import hmac, hashlib, logging
from fastapi import APIRouter, Request, HTTPException, Header, BackgroundTasks
from config.settings import get_settings
from services.token_store import TokenStore
from services.sync_service import process_order_notification, process_claim_notification

router = APIRouter(prefix="/webhook", tags=["webhook"])
logger = logging.getLogger(__name__)


async def _process_notification(user_id: str, order_id: str) -> None:
    """Processa a notificação em background (o webhook já respondeu 200 ao ML)."""
    store = TokenStore().get_store(user_id)
    if not store:
        logger.warning("user_id %s não encontrado no Firestore", user_id)
        return
    try:
        result = await process_order_notification(store, order_id)
        logger.info("Pedido %s processado: %s", order_id, result)
    except Exception as e:
        logger.exception("Falha ao processar pedido %s: %s", order_id, e)


async def _process_claim(user_id: str, claim_id: str) -> None:
    """Processa a notificação de claim (devolução/reclamação) em background."""
    store = TokenStore().get_store(user_id)
    if not store:
        logger.warning("user_id %s não encontrado no Firestore", user_id)
        return
    try:
        result = await process_claim_notification(store, claim_id)
        logger.info("Claim %s processado: %s", claim_id, result)
    except Exception as e:
        logger.exception("Falha ao processar claim %s: %s", claim_id, e)


def _verify_signature(body: bytes, signature: str | None) -> bool:
    secret = get_settings().webhook_secret
    if not secret or not signature:
        return True   # sem segredo configurado, aceita (ambiente de dev)
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/ml")
async def ml_webhook(request: Request, background_tasks: BackgroundTasks,
                     x_signature: str | None = Header(None)):
    body = await request.body()
    if not _verify_signature(body, x_signature):
        raise HTTPException(403, "Assinatura inválida")

    payload = await request.json()
    logger.info("Webhook ML recebido: %s", payload)

    topic = payload.get("topic", "")
    user_id = str(payload.get("user_id", ""))
    resource = payload.get("resource", "")   # ex: /orders/1234567890 ou /claims/1234567890

    if topic == "orders_v2":
        order_id = resource.split("/")[-1]
        # Responde 200 IMEDIATAMENTE e processa em background.
        # (o ML reenvia a notificação se demorarmos a responder → causava duplicatas)
        background_tasks.add_task(_process_notification, user_id, order_id)
        return {"status": "accepted", "order_id": order_id}

    if topic == "claims":
        claim_id = resource.split("/")[-1]
        background_tasks.add_task(_process_claim, user_id, claim_id)
        return {"status": "accepted", "claim_id": claim_id}

    return {"status": "ignored", "topic": topic}
