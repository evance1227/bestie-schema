# app/integrations.py
import os, httpx
from loguru import logger
from app import db
from sqlalchemy import text

# Default to your provided GHL webhook, can be overridden in .env
LC_URL = os.getenv(
    "GHL_OUTBOUND_WEBHOOK_URL",
    "https://services.leadconnectorhq.com/hooks/oQvU5iYAEPQwj7sQq3h0/webhook-trigger/3f7b89d3-afa3-4657-844f-eb5cd25eb3e4"
).strip()


def _get_phone(user_id: int) -> str:
    """Look up phone number for given user_id from DB."""
    try:
        with db.session() as s:
            row = s.execute(text("select phone from users where id=:uid"), {"uid": user_id}).first()
            phone = row[0] if row and row[0] else ""
            logger.info("📞 _get_phone: user_id={} -> phone={}", user_id, phone)
            return phone
    except Exception as e:
        logger.exception("❌ Exception in _get_phone for user_id={}", user_id)
        return ""


def send_sms_reply(user_id: int, text: str):
    """
    Send outbound SMS via LeadConnector webhook.
    Posts to the configured GHL webhook with {phone, message}.
    """
    logger.info("🚦 send_sms_reply called: user_id={} text='{}'", user_id, text)

    phone = _get_phone(user_id)
    if not phone:
        logger.error("❌ No phone found for user_id={}, aborting send", user_id)
        return

    if not LC_URL:
        logger.error("❌ No GHL_OUTBOUND_WEBHOOK_URL configured, cannot send message")
        return

    payload = {"phone": phone, "message": text}
    headers = {"Content-Type": "application/json"}

    logger.info("📤 Sending payload to LeadConnector: URL={} | Payload={}", LC_URL, payload)

    try:
        with httpx.Client(timeout=8) as client:
            r = client.post(LC_URL, json=payload, headers=headers)
            if r.status_code >= 400:
                logger.error("❌ LeadConnector send failed! status={} body={}", r.status_code, r.text)
            else:
                logger.success("✅ LeadConnector send OK for phone={} body={}", phone, r.text)
    except Exception:
        logger.exception("💥 Exception while sending SMS via LeadConnector")