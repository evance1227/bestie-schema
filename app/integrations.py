# app/integrations.py
from __future__ import annotations

import os
import re
import time
import random
import hashlib
import httpx
from typing import Optional, Dict, Any
from loguru import logger
from app import db
from sqlalchemy import text
# --- GHL outbound wrapper ----------------------------------------------------
import os
import json
import requests  # make sure 'requests' is in requirements.txt

GHL_OUTBOUND_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL", "").strip()

def send_outbound(webhook_url: str, phone: str, text: str, user_id: int, convo_id: int) -> bool:
    """
    Post SMS to your GHL custom webhook. Returns True if accepted by GHL.
    """
    if not webhook_url or not phone or not (text or "").strip():
        return False

    payload = {
        "phone":   phone,
        "message": text,
        "user_id": user_id,
        "convo_id": convo_id,
    }

    try:
        r = requests.post(webhook_url, json=payload, timeout=8)
        return bool(getattr(r, "ok", False))
    except Exception:
        return False

# Optional Redis de-dupe (safe no-op if REDIS_URL missing)
try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore

REDIS_URL = os.getenv("REDIS_URL", "")
_rds = None
if redis and REDIS_URL:
    try:
        _rds = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _rds = None

# Outbound SMS endpoint (LeadConnector / GHL)
LC_URL = os.getenv(
    "GHL_OUTBOUND_WEBHOOK_URL",
    "https://services.leadconnectorhq.com/hooks/oQvU5iYAEPQwj7sQq3h0/webhook-trigger/3f7b89d3-afa3-4657-844f-eb5cd25eb3e4",
).strip()

# De-dupe window (seconds) to avoid accidental duplicates on retries/enqueues
SMS_DEDUPE_TTL_SEC = int(os.getenv("SMS_DEDUPE_TTL_SEC", "20"))

__all__ = ["send_sms", "send_sms_reply"]
def openai_complete(messages: list, user_id: Optional[int] = None, context: Optional[Dict] = None) -> str:
    """
    Centralized OpenAI chat call used by ai.generate_reply().
    Minimal dependencies; returns text or raises for caller to handle.
    """
    from openai import OpenAI  # local import so worker boot never fails if lib missing in other jobs
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.9,
        max_tokens=360,
    )
    return (resp.choices[0].message.content or "").strip()

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


def _mask_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) >= 4:
        return f"+**{d[-4:]}"
    return "+**"


def _strip_bestie_prefix(msg: str) -> str:
    """
    Remove leading 'Bestie' label like:
    'Bestie:', 'Bestie -', 'Bestie —', 'Bestie –', 'Bestie,'
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


def _phone_from_messages(convo_id: int) -> Optional[str]:
    """
    Derive a phone from the most recent inbound message in this conversation.
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


def _phone_from_any_message(user_id: int) -> Optional[str]:
    """
    Fallback: find latest inbound message across ANY conversation for this user.
    Helpful if users.phone is empty and worker did not pass convo_id.
    """
    try:
        with db.session() as s:
            r = s.execute(
                text(
                    """
                    SELECT m.phone, m.meta
                      FROM messages m
                      JOIN conversations c ON c.id = m.conversation_id
                     WHERE c.user_id = :u AND m.direction = 'in'
                  ORDER BY m.created_at DESC
                     LIMIT 1
                    """
                ),
                {"u": user_id},
            ).first()
        if not r:
            return None
        phone = r[0]
        if phone:
            return phone
        meta = r[1]
        if isinstance(meta, dict):
            return (
                meta.get("phone")
                or meta.get("user_phone")
                or (meta.get("contact") or {}).get("phone")
            )
    except Exception:
        logger.exception("💥 [Integrations][Lookup] any-message phone fallback failed (user_id=%s)", user_id)
    return None


def _resolve_phone(user_id: int, convo_id: Optional[int] = None) -> Optional[str]:
    """Prefer users.phone; then conversation; then latest inbound across any convo."""
    p = _phone_from_users(user_id)
    if p:
        return _normalize_phone(p)
    if convo_id:
        p = _phone_from_messages(convo_id)
        if p:
            return _normalize_phone(p)
    p = _phone_from_any_message(user_id)
    if p:
        return _normalize_phone(p)
    return None


def _dedupe_guard(phone: str, message: str) -> bool:
    """
    Returns True if this (phone,message) was sent very recently.
    Uses Redis SETNX with TTL; no-op if Redis not configured.
    """
    if not (_rds and phone and message and SMS_DEDUPE_TTL_SEC > 0):
        return False
    key = "bestie:smsdedupe:" + hashlib.sha256(f"{phone}|{message}".encode()).hexdigest()
    try:
        if _rds.setnx(key, "1"):
            _rds.expire(key, SMS_DEDUPE_TTL_SEC)
            return False  # not a duplicate
        return True
    except Exception:
        return False


def _post_with_retry(url: str, payload: Dict[str, Any], headers: Dict[str, str], attempts: int = 3):
    """
    POST with backoff + jitter on 429/5xx/timeouts. Raises on final failure.
    """
    backoff = 0.8
    for i in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0, read=6.0), follow_redirects=True) as client:
                r = client.post(url, json=payload, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            return r
        except Exception as e:
            if i >= attempts:
                raise
            logger.warning("[Integrations][Send] Retry {}/{} after error: {}", i, attempts, e)
            # jitter
            time.sleep(backoff + random.random() * 0.4)
            backoff *= 2


def _send_outbound(phone: str, msg: str) -> Dict[str, Any]:
    """Core sender with de-dupe, retries, and masked logging."""
    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        return {"ok": False, "reason": "invalid_phone"}

    if not LC_URL:
        logger.error("[Integrations][Send] ❌ No GHL_OUTBOUND_WEBHOOK_URL configured, cannot send message")
        return {"ok": False, "reason": "no_webhook"}

    # De-dupe guard (optional)
    if _dedupe_guard(phone_norm, msg):
        logger.warning("[Integrations][Send] 🧯 Duplicate suppressed (phone={}, {} chars)", _mask_phone(phone_norm), len(msg))
        return {"ok": True, "deduped": True}

    payload = {"phone": phone_norm, "message": msg}
    headers = {"Content-Type": "application/json"}

    logger.info("[Integrations][Send] 📤 LeadConnector POST url={} to={} chars={}", LC_URL, _mask_phone(phone_norm), len(msg))

    try:
        r = _post_with_retry(LC_URL, payload, headers, attempts=3)
        if r.status_code >= 400:
            logger.error("[Integrations][Send] ❌ Failed! status={} body={}", r.status_code, r.text)
            return {"ok": False, "status": r.status_code, "body": r.text}
        logger.success("[Integrations][Send] ✅ Delivered to {} ({} chars)", _mask_phone(phone_norm), len(msg))
        return {"ok": True, "status": r.status_code, "body": r.text}
    except Exception as e:
        logger.exception("💥 [Integrations][Send] Exception while posting to LeadConnector")
        return {"ok": False, "exception": str(e)}


# ---------- public API (called by worker) ----------

def send_sms_reply(user_id: int, text: str):
    """
    Send outbound SMS via LeadConnector webhook.
    Posts to the configured GHL webhook with {phone, message}.
    Returns a small dict with ok/status so callers can log intelligently.
    """
    # Normalize + defensive cleanup
    msg = _strip_bestie_prefix((text or "").strip())
    if not msg:
        logger.warning("[Integrations][Send] Empty message for user_id={}, skipping", user_id)
        return {"ok": False, "reason": "empty"}

    # Resolve phone: users.phone -> latest inbound across any convo
    phone = _resolve_phone(user_id)
    if not phone:
        logger.error("[Integrations][Send] ❌ No phone found for user_id={}, aborting send", user_id)
        return {"ok": False, "reason": "no_phone"}

    return _send_outbound(phone, msg)


# --- Compatibility adapter for legacy worker calls ---
def send_sms(user_id: int, convo_id: int, text: str):
    """
    Legacy adapter used by older worker code.
    Prefer: send_sms_reply(user_id, text)
    """
    msg = _strip_bestie_prefix((text or "").strip())
    if not msg:
        logger.warning("[Integrations][Compat] Empty message for user_id={}, skipping", user_id)
        return {"ok": False, "reason": "empty"}
    phone = _resolve_phone(user_id, convo_id=convo_id)
    if not phone:
        logger.error("[Integrations][Compat] ❌ No phone for user_id={} convo_id={}, aborting", user_id, convo_id)
        return {"ok": False, "reason": "no_phone"}
    return _send_outbound(phone, msg)
