# app/integrations.py

import os
import re
import httpx
from typing import Optional
from loguru import logger
from app import db
from sqlalchemy import text

# Default to your provided GHL webhook; can be overridden via env
LC_URL = os.getenv(
    "GHL_OUTBOUND_WEBHOOK_URL",
    "https://services.leadconnectorhq.com/hooks/oQvU5iYAEPQwj7sQq3h0/webhook-trigger/3f7b89d3-afa3-4657-844f-eb5cd25eb3e4",
).strip()

__all__ = ["send_sms", "send_sms_reply"]


# ---------- helpers ----------

def _col_exists(table: str, col: str) -> bool:
    try:
        with db.session() as s:
            row = s.execute(
                text(
                    """
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_name = :t AND column_name = :c
                     LIMIT 1
                    """
                ),
                {"t": table, "c": col},
            ).first()
        return bool(row)
    except Exception:
        logger.exception("[Integrations] Column existence check failed for %s.%s", table, col)
        return False


def _normalize_phone(raw: Optional[str]) -> str:
    """Normalize to E.164 (assume US if 10 digits)."""
    if not raw:
        return ""
    s = str(raw).strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits  # last resort


def _strip_bestie_prefix(msg: str) -> str:
    """
    Remove any leading 'Bestie' label like:
      'Bestie:', 'Bestie -', 'Bestie —', 'Bestie –', 'Bestie,'
    (case-insensitive, trims following space).
    """
    if not msg:
        return ""
    return re.sub(r"^\s*bestie\s*[:,\-–—]\s*", "", msg, flags=re.IGNORECASE)


def _phone_from_users(user_id: int) -> Optional[str]:
    try:
        with db.session() as s:
            row = s.execute(
                text("SELECT phone FROM users WHERE id = :uid"),
                {"uid": user_id},
            ).first()
        return row[0] if row and row[0] else None
    except Exception:
        logger.exception("💥 [Integrations][Lookup] users.phone lookup failed (user_id=%s)", user_id)
        return None


# Back-compat alias used by existing code paths
def _get_phone(user_id: int) -> Optional[str]:
    return _phone_from_users(user_id)


def _phone_from_messages(convo_id: int) -> Optional[str]:
    """
    Try to derive a phone from the most recent inbound message.
    Checks messages.phone first (if exists), then messages.meta JSON.
    """
    try:
        # messages.phone
        if _col_exists("messages", "phone"):
            with db.session() as s:
                r = s.execute(
                    text(
                        """
                        SELECT phone
                          FROM messages
                         WHERE conversation_id = :cid AND direction = 'in'
                      ORDER BY created_at DESC
                         LIMIT 1
                        """
                    ),
                    {"cid": convo_id},
                ).first()
            if r and r[0]:
                return r[0]

        # messages.meta JSON
        if _col_exists("messages", "meta"):
            with db.session() as s:
                r = s.execute(
                    text(
                        """
                        SELECT meta
                          FROM messages
                         WHERE conversation_id = :cid AND direction = 'in'
                      ORDER BY created_at DESC
                         LIMIT 1
                        """
                    ),
                    {"cid": convo_id},
                ).first()
            meta = r[0] if r else None
            if isinstance(meta, dict):
                for k in ("phone", "user_phone", "userPhone", "UserPhone"):
                    v = meta.get(k)
                    if v:
                        return v
                contact = meta.get("contact") or {}
                v = (
                    contact.get("phone")
                    or contact.get("phoneNumber")
                    or contact.get("Phone")
                )
                if v:
                    return v
    except Exception:
        logger.exception("💥 [Integrations][Lookup] messages-derived phone lookup failed (convo_id=%s)", convo_id)

    return None


def _resolve_phone(user_id: int, convo_id: int) -> Optional[str]:
    """Prefer users.phone; fallback to latest inbound message metadata."""
    p = _phone_from_users(user_id)
    if p:
        return _normalize_phone(p)

    p = _phone_from_messages(convo_id)
    if p:
        return _normalize_phone(p)

    return None


def _post_to_leadconnector(phone: str, message: str) -> None:
    if not LC_URL:
        raise RuntimeError("GHL_OUTBOUND_WEBHOOK_URL is not configured")

    payload = {"phone": phone, "message": message}
    headers = {"Content-Type": "application/json"}

    logger.info("[Integrations][Send] 📤 LeadConnector POST url={} payload={}", LC_URL, payload)
    with httpx.Client(timeout=15) as client:
        r = client.post(LC_URL, json=payload, headers=headers)
        if r.status_code // 100 != 2:
            logger.error(
                "[Integrations][Send] ❌ LeadConnector failed status=%s body=%s",
                r.status_code,
                r.text,
            )
            r.raise_for_status()

    logger.success("[Integrations][Send] ✅ Delivered to %s (%d chars)", phone, len(message))


# ---------- public API (called by worker) ----------

def send_sms_reply(user_id: int, text: str):
    """
    Send outbound SMS via LeadConnector webhook.
    Posts to the configured GHL webhook with {phone, message}.
    Returns a small dict with ok/status so callers can log intelligently.
    """
    # normalize & defensively strip accidental "Bestie:" prefixes (we do NOT add it)
    msg = (text or "").strip()
    if msg.lower().startswith("bestie:"):
        msg = msg.split(":", 1)[-1].strip()

    logger.info("[Integrations][Send] 🚦 Preparing to send SMS: user_id={} text='{}'", user_id, msg)

    phone = _get_phone(user_id)
    if not phone:
        logger.error("[Integrations][Send] ❌ No phone found for user_id={}, aborting send", user_id)
        return {"ok": False, "reason": "no_phone"}

    if not LC_URL:
        logger.error("[Integrations][Send] ❌ No GHL_OUTBOUND_WEBHOOK_URL configured, cannot send message")
        return {"ok": False, "reason": "no_webhook"}

    payload = {"phone": phone, "message": msg}
    headers = {"Content-Type": "application/json"}

    logger.info("[Integrations][Send] 📤 Sending to LeadConnector: URL={} | Payload={}", LC_URL, payload)

    try:
        with httpx.Client(timeout=8) as client:
            r = client.post(LC_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            logger.error("[Integrations][Send] ❌ Failed! status={} body={}", r.status_code, r.text)
            return {"ok": False, "status": r.status_code, "body": r.text}
        logger.success("[Integrations][Send] ✅ Success for phone={} body={}", phone, r.text)
        return {"ok": True, "status": r.status_code, "body": r.text}
    except Exception as e:
        logger.exception("💥 [Integrations][Send] Exception while posting to LeadConnector")
        return {"ok": False, "exception": str(e)}


# --- Compatibility adapter for legacy worker calls ---
def send_sms(user_id: int, convo_id: int, text: str):
    """
    Adapter to keep existing worker code working.
    We ignore convo_id here (GHL send only needs phone + message).
    """
    logger.warning("[Integrations][Compat] send_sms() called; forwarding to send_sms_reply()")
    return send_sms_reply(user_id=user_id, text=text)
