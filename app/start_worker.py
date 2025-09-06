# app/webhooks_gumroad.py
from __future__ import annotations

import os, hmac, hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Header, HTTPException
from loguru import logger
from sqlalchemy import text as sqltext

from app import db
from app.workers import _store_and_send

router = APIRouter()

# ---------- Env / product mapping ----------
BESTIE_BASIC_URL = os.getenv("BESTIE_BASIC_URL", "https://schizobestie.gumroad.com/l/bestie_basic")
BESTIE_PLUS_URL  = os.getenv("BESTIE_PLUS_URL",  "https://schizobestie.gumroad.com/l/bestie_plus")
BESTIE_ELITE_URL = os.getenv("BESTIE_ELITE_URL", "https://schizobestie.gumroad.com/l/bestie_elite")

# Back-compat with old IDs if Gumroad sends product_id or legacy permalinks
TRIAL_PRODUCT_ID = os.getenv("TRIAL_PRODUCT_ID", "")
FULL_PRODUCT_ID  = os.getenv("FULL_PRODUCT_ID", "")

QUIZ_URL        = os.getenv("QUIZ_URL", "https://tally.so/r/YOUR_QUIZ_ID")
FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "7"))
SIGNING_SECRET  = os.getenv("GUMROAD_SIGNING_SECRET")

def _slug(url: str) -> str:
    try:
        p = urlparse(url)
        return p.path.strip("/").split("/")[-1].lower()
    except Exception:
        return ""

BASIC_SLUG = _slug(BESTIE_BASIC_URL) or "bestie_basic"
PLUS_SLUG  = _slug(BESTIE_PLUS_URL)  or "bestie_plus"
ELITE_SLUG = _slug(BESTIE_ELITE_URL) or "bestie_elite"

# ---------- Helpers ----------
async def _payload_dict(req: Request) -> Dict[str, Any]:
    """Gumroad can send JSON or form-encoded."""
    raw = await req.body()
    # optional signature check
    if SIGNING_SECRET:
        try:
            expected = hmac.new(SIGNING_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            sig = req.headers.get("X-Gumroad-Signature") or req.headers.get("x_gumroad_signature") or ""
            if sig and not hmac.compare_digest(sig, expected):
                logger.warning("[Gumroad] signature mismatch")
        except Exception as e:
            logger.warning("[Gumroad] signature check error: {}", e)

    try:
        data = await req.json()
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    try:
        form = await req.form()
        return dict(form.items())
    except Exception:
        return {}

def _event_name(p: Dict[str, Any]) -> str:
    # Classic Gumroad uses alert_name; newer webhooks may use event/type
    return str(p.get("alert_name") or p.get("event") or p.get("type") or "").lower()

def _product_hint(p: Dict[str, Any]) -> str:
    # Try permalink/url fields first, then product_name, then product_id fallback
    for k in ("product_permalink", "permalink", "short_url", "url", "product_url"):
        v = str(p.get(k) or "")
        if v:
            return _slug(v)
    name = str(p.get("product_name") or "").lower().replace(" ", "_")
    if name:
        return name
    return str(p.get("product_id") or "").lower()

def _map_tier(p: Dict[str, Any]) -> Optional[str]:
    s = _product_hint(p)
    if BASIC_SLUG in s or (TRIAL_PRODUCT_ID and TRIAL_PRODUCT_ID == s) or (FULL_PRODUCT_ID and FULL_PRODUCT_ID == s):
        return "basic"
    if PLUS_SLUG in s:
        return "plus"
    if ELITE_SLUG in s:
        return "elite"
    return None

def _next_charge_at(p: Dict[str, Any]) -> Optional[datetime]:
    for k in ("next_charge_date", "subscription_next_charge_date", "next_charge_at", "charge_occurs_on"):
        v = p.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                continue
    # Fallbacks
    if FREE_TRIAL_DAYS > 0:
        return datetime.now(timezone.utc) + timedelta(days=FREE_TRIAL_DAYS)
    return datetime.now(timezone.utc) + timedelta(days=30)

def _find_user_id(p: Dict[str, Any]) -> Optional[int]:
    # Prefer explicit custom_fields.user_id if you send it
    try:
        custom = p.get("custom_fields") or p.get("custom") or {}
        if isinstance(custom, dict):
            uid = custom.get("user_id") or custom.get("uid")
            if uid:
                return int(uid)
    except Exception:
        pass
    # Fallback to email
    email = (p.get("email") or p.get("purchaser_email") or p.get("customer_email") or "").strip().lower()
    if not email:
        return None
    with db.session() as s:
        r = s.execute(sqltext("SELECT id FROM users WHERE lower(email)=:e"), {"e": email}).first()
        return int(r[0]) if r else None

def _latest_convo(user_id: int) -> Optional[int]:
    with db.session() as s:
        r = s.execute(sqltext("SELECT id FROM conversations WHERE user_id=:u ORDER BY id DESC LIMIT 1"), {"u": user_id}).first()
        return int(r[0]) if r else None

def _set_status(
    user_id: int,
    *,
    plan_status: str,
    next_renew: Optional[datetime],
    gumroad_id: Optional[str],
    email: Optional[str],
    start_trial: bool = False,
) -> None:
    """Upsert user profile with new plan status. Reset daily counter on change."""
    with db.session() as s:
        if start_trial and FREE_TRIAL_DAYS > 0:
            s.execute(sqltext("""
                UPDATE public.user_profiles
                SET plan_status        = :st,
                    trial_start_date   = COALESCE(trial_start_date, NOW()),
                    plan_renews_at     = :rn,
                    gumroad_customer_id= COALESCE(:gid, gumroad_customer_id),
                    gumroad_email      = COALESCE(:em, gumroad_email),
                    daily_msgs_used    = 0
                WHERE user_id = :u
            """), {"st": "trial", "rn": next_renew, "gid": gumroad_id, "em": email, "u": user_id})
        else:
            s.execute(sqltext("""
                UPDATE public.user_profiles
                SET plan_status        = :st,
                    plan_renews_at     = :rn,
                    gumroad_customer_id= COALESCE(:gid, gumroad_customer_id),
                    gumroad_email      = COALESCE(:em, gumroad_email),
                    daily_msgs_used    = 0
                WHERE user_id = :u
            """), {"st": plan_status, "rn": next_renew, "gid": gumroad_id, "em": email, "u": user_id})
        s.commit()

# ---------- Webhook endpoint ----------
@router.post("/webhooks/gumroad")
async def gumroad(req: Request):
    payload = await _payload_dict(req)
    if not payload:
        raise HTTPException(status_code=400, detail="Empty Gumroad payload")

    event = _event_name(payload)
    tier  = _map_tier(payload)
    user_id = _find_user_id(payload)
    gum_id  = str(payload.get("customer_id") or payload.get("gumroad_customer_id") or payload.get("subscriber_id") or "")
    email   = (payload.get("email") or payload.get("purchaser_email") or payload.get("customer_email") or "")

    logger.info("[Gumroad] event={} tier={} email={} user_id={} keys={}", event, tier, email, user_id, list(payload.keys())[:12])

    if not tier or not user_id:
        # Accept but ignore unknown products or unresolved users
        return {"ok": True, "ignored": True}

    e = event

    # BASIC — internal trial allowed for first week
    if tier == "basic":
        if e in ("sale", "subscription_started"):
            renew = _next_charge_at(payload)
            _set_status(user_id, plan_status=("active" if FREE_TRIAL_DAYS == 0 else "trial"),
                        next_renew=renew, gumroad_id=gum_id, email=email, start_trial=(FREE_TRIAL_DAYS > 0))
            # Send quiz link on join
            convo_id = _latest_convo(user_id)
            if convo_id and QUIZ_URL:
                _store_and_send(
                    user_id, convo_id,
                    f"You’re in. Take your quiz so I can customize your Bestie — it is quick and makes me scary accurate:\n{QUIZ_URL}"
                )
        elif e in ("subscription_payment", "charge", "recurring_charge"):
            _set_status(user_id, plan_status="active", next_renew=_next_charge_at(payload), gumroad_id=gum_id, email=email)
        elif e in ("subscription_cancelled", "refund", "chargeback", "subscription_stopped"):
            _set_status(user_id, plan_status="canceled", next_renew=None, gumroad_id=gum_id, email=email)
        else:
            logger.info("[Gumroad] Basic: unhandled event='{}' accepted", e)

    # PLUS
    elif tier == "plus":
        if e in ("sale", "subscription_started", "subscription_payment", "charge"):
            _set_status(user_id, plan_status="plus", next_renew=_next_charge_at(payload), gumroad_id=gum_id, email=email)
        elif e in ("subscription_cancelled", "refund", "chargeback", "subscription_stopped"):
            _set_status(user_id, plan_status="canceled", next_renew=None, gumroad_id=gum_id, email=email)
        else:
            logger.info("[Gumroad] Plus: unhandled event='{}' accepted", e)

    # ELITE
    elif tier == "elite":
        if e in ("sale", "subscription_started", "subscription_payment", "charge"):
            _set_status(user_id, plan_status="elite", next_renew=_next_charge_at(payload), gumroad_id=gum_id, email=email)
        elif e in ("subscription_cancelled", "refund", "chargeback", "subscription_stopped"):
            _set_status(user_id, plan_status="canceled", next_renew=None, gumroad_id=gum_id, email=email)
        else:
            logger.info("[Gumroad] Elite: unhandled event='{}' accepted", e)

    return {"ok": True}
