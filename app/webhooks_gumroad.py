# app/webhooks_gumroad.py
import os, hmac, hashlib
from fastapi import APIRouter, Request, Header
from loguru import logger
from sqlalchemy import text as sqltext
from datetime import datetime, timezone, timedelta
from app import db
from app.workers import _store_and_send

router = APIRouter()

TRIAL_PRODUCT_ID = os.getenv("TRIAL_PRODUCT_ID", "gexqp")
FULL_PRODUCT_ID  = os.getenv("FULL_PRODUCT_ID", "ibltj")
QUIZ_URL         = os.getenv("QUIZ_URL", "https://tally.so/r/YOUR_QUIZ_ID")
FREE_TRIAL_DAYS  = int(os.getenv("FREE_TRIAL_DAYS","14"))
INTRO_DAYS       = int(os.getenv("INTRO_DAYS","14"))
SIGNING_SECRET   = os.getenv("GUMROAD_SIGNING_SECRET")

def _find_user_by_email(email: str):
    with db.session() as s:
        r = s.execute(sqltext("SELECT id FROM users WHERE lower(email)=:e"), {"e": email.lower()}).first()
        return r[0] if r else None

def _latest_convo(user_id: int):
    with db.session() as s:
        r = s.execute(sqltext("SELECT id FROM conversations WHERE user_id=:u ORDER BY id DESC LIMIT 1"), {"u": user_id}).first()
        return r[0] if r else None

@router.post("/webhooks/gumroad")
async def gumroad(req: Request, x_gumroad_signature: str = Header(None)):
    raw = await req.body()
    # verify signature if you want
    if SIGNING_SECRET:
        digest = hmac.new(SIGNING_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if x_gumroad_signature and not hmac.compare_digest(x_gumroad_signature, digest):
            logger.warning("[Gumroad] signature mismatch")

    try:
        data = await req.form()
        payload = dict(data)
    except Exception:
        payload = await req.json()

    logger.info("[Gumroad] webhook: {}", payload)

    email = (payload.get("email") or payload.get("purchaser_email") or "").strip().lower()
    prod = (payload.get("permalink") or payload.get("product_permalink") or payload.get("product_id") or "").strip().lower()
    cust = (payload.get("customer_id") or payload.get("purchaser_id") or "").strip()
    now  = datetime.now(timezone.utc)

    user_id = _find_user_by_email(email) if email else None
    if not user_id:
        return {"ok": True}

    # Decide plan based on product
    if prod == TRIAL_PRODUCT_ID:
        plan_status = "trial"
        trial_start = now
        renews = now + timedelta(days=FREE_TRIAL_DAYS)
    elif prod == FULL_PRODUCT_ID:
        plan_status = "active"
        trial_start = None
        renews = now + timedelta(days=30)
    else:
        return {"ok": True}

    with db.session() as s:
        s.execute(sqltext("""
            UPDATE user_profiles
            SET gumroad_customer_id = COALESCE(:cid, gumroad_customer_id),
                gumroad_email       = COALESCE(:em, gumroad_email),
                plan_status         = :st,
                trial_start_date    = COALESCE(trial_start_date, :ts),
                plan_renews_at      = :renews
            WHERE user_id = :u
        """), {"cid": cust or None, "em": email, "st": plan_status,
               "ts": trial_start, "renews": renews, "u": user_id})
        s.commit()

    # If it’s a trial, send the quiz link immediately
    if prod == TRIAL_PRODUCT_ID:
        convo_id = _latest_convo(user_id)
        if convo_id:
            _store_and_send(
                user_id, convo_id,
                f"You’re in. Take your quiz so I can customize your Bestie — it’s quick and makes me scary accurate:\n{QUIZ_URL}"
            )
    return {"ok": True}
