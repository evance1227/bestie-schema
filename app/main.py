# app/main.py
from __future__ import annotations

import os, re, time
from typing import Any, List, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text as sqltext
from fastapi import Query
from app.integrations_serp import lens_products

from app import db
from app.webhooks_gumroad import router as gumroad_router

# -------------------- Env -------------------- #
CRON_SECRET    = os.getenv("CRON_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# -------------------- URL & media helpers -------------------- #
_URL_RE     = re.compile(r"https?://[^\s)\]]+", re.I)
_IMG_EXTS   = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_AUD_EXTS   = (".mp3", ".m4a", ".wav", ".ogg")

def _collect_urls_anywhere(obj: Any, bucket: List[str]) -> None:
    """Recursively walk the inbound payload collecting any http(s) URLs."""
    try:
        if isinstance(obj, dict):
            for v in obj.values():
                _collect_urls_anywhere(v, bucket)
        elif isinstance(obj, list):
            for v in obj:
                _collect_urls_anywhere(v, bucket)
        elif isinstance(obj, str):
            for m in _URL_RE.findall(obj):
                if m.startswith("http"):
                    bucket.append(m.strip())
    except Exception:
        # best-effort harvesting; never break the webhook
        pass

def _extract_media_urls(body: dict) -> List[str]:
    """Try well-known locations first; fall back to recursive scan of payload."""
    urls: List[str] = []

    # GHL attachment shapes
    top = body.get("attachments")
    if isinstance(top, list):
        for a in top:
            u = (a.get("url") or a.get("file_url") or a.get("link") or a.get("source") or "").strip()
            if u.startswith("http"):
                urls.append(u)

    msg = body.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("attachments"), list):
        for a in msg["attachments"]:
            u = (a.get("url") or a.get("file_url") or a.get("link") or a.get("source") or "").strip()
            if u.startswith("http"):
                urls.append(u)

    # If still empty, deep scan the whole payload
    if not urls:
        _collect_urls_anywhere(body, urls)

    # Dedup while preserving order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# -------------------- App -------------------- #
app = FastAPI(
    title="Bestie Backend",
    docs_url="/docs",
    openapi_url="/openapi.json",
    redoc_url=None,
)
logger.info("[API][Boot] Using REDIS_URL={}", os.getenv("REDIS_URL"))
app.include_router(gumroad_router)

@app.get("/debug/enqueue-ping")
def enqueue_ping():
    from app.workers import _ping_job  # lazy import to avoid circulars
    q.enqueue(_ping_job, job_timeout=30)
    return {"enqueued": True, "queue": q.name}

@app.get("/healthz")
def healthz():
    return {"ok": True}

# -------------------- Secure env snapshot -------------------- #
SENSITIVE = re.compile(r"(secret|key|token|pwd|password|auth|api)", re.I)

def _masked_env(snapshot: dict[str, str]) -> dict[str, str]:
    masked: dict[str, str] = {}
    for k, v in snapshot.items():
        if v is None:
            masked[k] = None
        else:
            masked[k] = "***" if SENSITIVE.search(k) else (v if len(v) < 80 else v[:76] + "…")
    return masked

@app.get("/debug/env")
def debug_env(secret: str):
    if secret != os.getenv("CRON_SECRET"):
        raise HTTPException(status_code=403, detail="forbidden")
    snap = {
        "BESTIE_BASIC_URL": os.getenv("BESTIE_BASIC_URL"),
        "BESTIE_PLUS_URL": os.getenv("BESTIE_PLUS_URL"),
        "BESTIE_ELITE_URL": os.getenv("BESTIE_ELITE_URL"),
        "BASIC_SMS_CAP": os.getenv("BASIC_SMS_CAP"),
        "PLUS_SMS_CAP": os.getenv("PLUS_SMS_CAP"),
        "ELITE_SMS_CAP": os.getenv("ELITE_SMS_CAP"),
        "BASIC_MAX_CHARS": os.getenv("BASIC_MAX_CHARS"),
        "PLUS_MAX_CHARS": os.getenv("PLUS_MAX_CHARS"),
        "ELITE_MAX_CHARS": os.getenv("ELITE_MAX_CHARS"),
        "AMAZON_ASSOCIATE_TAG": os.getenv("AMAZON_ASSOCIATE_TAG"),
        "GENIUSLINK_WRAP": os.getenv("GENIUSLINK_WRAP"),
        "GENIUSLINK_DOMAIN": os.getenv("GENIUSLINK_DOMAIN"),
        "ENFORCE_SIGNUP_BEFORE_CHAT": os.getenv("ENFORCE_SIGNUP_BEFORE_CHAT"),
        "FREE_TRIAL_DAYS": os.getenv("FREE_TRIAL_DAYS"),
        "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET"),  # masked
    }
    return _masked_env(snap)

# -------------------- Plan rollover (trial -> active) -------------------- #
@app.post("/tasks/plan_rollover")
def plan_rollover(request: Request):
    if CRON_SECRET and request.headers.get("x-cron-secret") != CRON_SECRET:
        return {"ok": False, "error": "forbidden"}
    with db.session() as s:
        s.execute(sqltext("""
            UPDATE public.user_profiles
               SET plan_status='active',
                   plan_renews_at = NOW() + INTERVAL '30 days'
             WHERE plan_status='trial'
               AND trial_start_date IS NOT NULL
               AND NOW() > trial_start_date + INTERVAL '7 days'
        """))
        s.commit()
    return {"ok": True}

# -------------------- Queue probe -------------------- #
from redis import Redis
from rq import Queue

@app.get("/debug/queue")
def debug_queue():
    url = os.getenv("REDIS_URL")
    r = Redis.from_url(url)
    q = Queue("bestie_queue", connection=r)
    jobs = q.jobs
    return {
        "redis_url": url,
        "queue": q.name,
        "queued_count": len(jobs),
        "sample_job_ids": [j.id for j in jobs[:5]],
    }
# -------------------- Debug: enqueue ping -------------------- #
from app.task_queue import q  # same Queue object the API uses

@app.get("/debug/enqueue-ping")
def enqueue_ping():
    from app.workers import _ping_job  # import lazily to avoid circular import at import-time
    q.enqueue(_ping_job, job_timeout=30)
    return {"enqueued": True, "queue": q.name}

@app.get("/debug/visual-search-serp")
def debug_visual_search_serp(url: str = Query(..., description="Image URL")):
    return lens_products(url, topn=5)
# ---------- RQ snapshot (debug) ----------
from app.task_queue import q

@app.get("/debug/rq")
def rq_snapshot():
    # Show the queue name, visible count, and raw job ids present in the queue list
    conn = q.connection
    raw = conn.lrange(q.key, 0, -1)
    return {
        "queue": q.name,
        "key": q.key,            # e.g., "rq:queue:bestie_queue"
        "count": q.count,
        "job_ids": [rid.decode() if isinstance(rid, bytes) else rid for rid in raw][:20],
        "redis_url_note": "DB index forced via REDIS_URL .../0 in env",
    }

# -------------------- Worker helpers -------------------- #
from . import models
from .task_queue import enqueue_generate_reply
from .workers import send_reengagement_job

# -------------------- Webhook auth helper -------------------- #
def _auth_ok(req: Request) -> bool:
    """Accept multiple common secret formats so GHL/curl both work."""
    secret = (WEBHOOK_SECRET or "").strip()
    if not secret:
        return True  # no secret set -> allow all
    h = req.headers
    qp = req.query_params
    candidates = [
        h.get("Authorization"),
        h.get("X-Webhook-Secret"),
        h.get("X-Hook-Secret"),
        h.get("X-GHL-Secret"),
        h.get("LeadConnector-Secret"),
        qp.get("secret"),
        qp.get("token"),
    ]
    for c in candidates:
        if not c:
            continue
        if c == secret:
            return True
        if c.startswith("Bearer ") and c.split(" ", 1)[1] == secret:
            return True
    return False

# -------------------- Process & Webhook -------------------- #
def process_incoming(message_id: str, user_phone: str, text_val: str, raw_body: dict,
                     media_urls: Optional[List[str]] = None):
    """DB insert + enqueue; no recursion, no background task."""
    try:
        logger.info("[API][Process] Starting DB insert for msg_id={} phone={} text={}",
                    message_id, user_phone, text_val)

        with db.session() as s:
            user = models.get_or_create_user_by_phone(s, user_phone)
            convo = models.get_or_create_conversation(s, user.id)
            models.insert_message(s, convo.id, "in", message_id, text_val)
            s.commit()
            logger.info("[API][Process] 💾 Stored inbound: convo_id={} user_id={}", convo.id, user.id)

        job = enqueue_generate_reply(convo.id, user.id, text_val, user_phone=user_phone, media_urls=media_urls)
        logger.success("[API][Queue] ✅ Enqueued job={} convo_id={} user_id={} text={}",
                       getattr(job, "id", "n/a"), convo.id, user.id, text_val)
        return {"ok": True, "job_id": getattr(job, "id", None), "convo_id": convo.id, "user_id": user.id}
    except Exception as e:
        logger.exception("💥 [API][Process] Exception: {}", e)
        return {"ok": True, "error": "process_incoming_failed"}

@app.post("/webhook/incoming_message")
async def incoming_message_any(req: Request):
    # 🔐 shared secret
    if not _auth_ok(req):
        logger.warning("[API][Webhook] Forbidden: missing/invalid secret")
        return JSONResponse(status_code=403, content={"ok": False, "error": "forbidden"})

    # Parse body
    try:
        body = await req.json()
        logger.info("[API][Webhook] >>> Incoming: {}", body)
    except Exception:
        logger.exception("[API][Webhook] Invalid JSON")
        return JSONResponse(status_code=200, content={"ok": True, "error": "invalid json"})

    # Choose correct payload root (GHL sends customData sometimes)
    cd = body.get("customData") or body.get("custom_data")
    payload = cd if cd and isinstance(cd, dict) else body

    # IDs & phone
    base_id = payload.get("message_id") or body.get("message_id") or f"{body.get('contact', {}).get('id','contact')}"
    message_id = f"{base_id}-{int(time.time())}"

    user_phone = (
        payload.get("user_phone") or body.get("user_phone") or
        body.get("phone") or body.get("contact", {}).get("phone")
    )
    if user_phone:
        digits = re.sub(r"\D", "", str(user_phone))
        if len(digits) == 10:
            user_phone = "+1" + digits
        elif not str(user_phone).startswith("+"):
            user_phone = "+" + digits

    # Text & media
    text_val = (
        payload.get("text") or body.get("text") or body.get("message", {}).get("body")
        or body.get("activity", {}).get("body") or body.get("contact", {}).get("last_message")
    )
    text_val = str(text_val or "")
    media_urls: List[str] = _extract_media_urls(body)

    # Guard: require either text or media
    if not (text_val.strip() or media_urls):
        logger.warning("⚠️ [API][Webhook] Missing fields: phone=%s text='' media_cnt=0", user_phone)
        return {"ok": True}

    logger.info("[API] Handoff → process_incoming msg_id=%s phone=%s text_len=%d media_cnt=%d",
                message_id, user_phone, len(text_val or ""), len(media_urls or []))

    # Synchronous handoff (no BackgroundTasks for now)
    _ = process_incoming(message_id, user_phone, text_val, body, media_urls)

    logger.info("[API][Webhook] ✅ Final ACK sent to GHL")
    return {"ok": True}

# -------------------- Secure re-engagement (Cron) -------------------- #
@app.post("/jobs/reengage")
def jobs_reengage(request: Request):
    secret = request.headers.get("X-Cron-Secret") or request.query_params.get("secret")
    if not secret or secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    send_reengagement_job()
    return {"ok": True}
