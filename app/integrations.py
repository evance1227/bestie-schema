# app/integrations.py
import os, httpx
from loguru import logger
from app import db

LC_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL", "").strip()

def _get_phone(user_id: int) -> str:
    try:
        with db.session() as s:
            row = s.execute("select phone from users where id=:uid", {"uid": user_id}).first()
            phone = row[0] if row and row[0] else ""
            logger.info("📞 _get_phone: user_id={} -> phone={}", user_id, phone)
            return phone
    except Exception as e:
        logger.exception("❌ Exception in _get_phone for user_id={}", user_id)
        return ""

def send_sms_reply(user_id: int, text: str):
    """
    Send outbound SMS via LeadConnector webhook.
    This version ALWAYS attempts to post, even if URL or phone are blank,
    so we can debug logs end-to-end.
    """
    logger.info("🚦 send_sms_reply called: user_id={} text='{}'", user_id, text)

    phone = _get_phone(user_id)
    if not phone:
        phone = "+15555555555"  # fallback dummy number so payload is never empty
        logger.warning("⚠️ No phone found for user_id={}, using dummy={}", user_id, phone)

    if not LC_URL:
        logger.warning("⚠️ GHL_OUTBOUND_WEBHOOK_URL not set in env; using dummy URL")
        # still try with a dummy URL so logs fire
        test_url = "https://postman-echo.com/post"
    else:
        test_url = LC_URL

    payload = {"phone": phone, "message": text}
    logger.info("📤 Prepared payload for LeadConnector: URL={} | Payload={}", test_url, payload)

    try:
        with httpx.Client(timeout=8) as client:
            r = client.post(test_url, json=payload, headers={"Content-Type": "application/json"})
            logger.info("🔎 LeadConnector response: status={} body={}", r.status_code, r.text)

            if r.status_code >= 400:
                logger.error("❌ LeadConnector send failed! status={} body={}", r.status_code, r.text)
            else:
                logger.success("✅ LeadConnector send OK for phone={}", phone)

    except Exception:
        logger.exception("💥 Exception while sending SMS via LeadConnector")
