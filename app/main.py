# app/main.py
from __future__ import annotations

import os, re, time
import json
from typing import Any, List, Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text as sqltext
from fastapi import Query
from app.integrations_serp import lens_products
from app.task_queue import enqueue_generate_reply, q as task_q
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

from fastapi import Response
from fastapi.responses import PlainTextResponse

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "bestie-backend"}

@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)

@app.get("/health", include_in_schema=False)
def health():
    return PlainTextResponse("ok", status_code=200)

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

# ================== Process & Webhook ================== #
def process_incoming(
    message_id: str,
    user_phone: str,
    text_val: str,
    raw_body: dict,
    media_urls: Optional[List[str]] = None,
) -> dict:
    """
    DB insert + enqueue; no recursion. Always enqueues a worker job even if DB is down.
    """
    msg_id = raw_body.get("customData", {}).get("message_id") or message_id
    phone  = user_phone
    text   = text_val or (raw_body.get("customData", {}).get("text") or "")
    # --- BEGIN PREAMBLE: normalize ids/text/media for this webhook call ---
    import json

    # Pick the inbound body object regardless of the caller's name (safe lookups)
    rb = locals().get("raw_body")
    if rb is None:
        rb = locals().get("body")  # do not reference 'body' directly


    # Always promote to a dict
    if not isinstance(rb, dict):
        try:
            rb = dict(rb or {})
        except Exception:
            rb = {}
    raw_body = rb

    # Custom-data dict if present (GHL puts your “Custom Data” here)
    cd = raw_body.get("customData", {}) if isinstance(raw_body, dict) else {}
    # --- BEGIN robust attachment harvesting ---
    std_msg = raw_body.get("message") or {}
    media_urls: list[str] = []

    def _maybe_add(u: str | None):
        if isinstance(u, str) and u and not u.strip().startswith("{{"):
            media_urls.append(u.strip())

    # 1) Standard arrays (preferred)
    for att in (std_msg.get("attachments") or []):
        _maybe_add((att or {}).get("url"))

    for m in (std_msg.get("media") or []):
        _maybe_add((m or {}).get("url"))

    # 2) CustomData fields (handle both scalar and array/json)
    for k, v in (cd or {}).items():
        # direct url
        if isinstance(v, str) and v.startswith("http"):
            _maybe_add(v)
        # json-ish list of attachments/media
        elif isinstance(v, str) and (v.startswith("[") or v.startswith("{")):
            try:
                import json as _json
                parsed = _json.loads(v)
                if isinstance(parsed, list):
                    for item in parsed:
                        _maybe_add((item or {}).get("url"))
                elif isinstance(parsed, dict):
                    _maybe_add(parsed.get("url"))
            except Exception:
                pass

    # 3) Known custom keys (defensive duplicates)
    _maybe_add = _maybe_add  # (name alias to avoid shadowing)
    for key in (
        "message.attachments[0].url",
        "message.media[0].url",
        "message.file_url",
        "message.attachments_url",
    ):
        val = cd.get(key)
        if isinstance(val, str):
            _maybe_add(val)

    # de-dupe while preserving order
    _seen = set()
    media_urls = [u for u in media_urls if (u not in _seen and not _seen.add(u))]
    # --- END robust attachment harvesting ---

    # Basic identifiers with safe fallbacks
    msg_id = (
        (locals().get("message_id") or "")                  # function arg if provided
        or cd.get("message_id")
        or (raw_body.get("message", {}) or {}).get("id")
        or ""
    )

    phone = (
        (locals().get("user_phone") or "")
        or cd.get("user_phone")
        or (raw_body.get("contact", {}) or {}).get("phone")
        or ""
    )

    # Prefer explicit text_val; fall back to customData.text, then standard message body
    text = (
        (locals().get("text_val") or "").strip()
        or (cd.get("text") or "")
        or (raw_body.get("message", {}) or {}).get("body")
        or ""
    )

    # Build/augment media_urls from several places (standard + your custom keys)
    media_urls = list(locals().get("media_urls") or [])

    # 1) Standard LeadConnector/GHL: message.attachments is a list of objects with url/fileUrl/link
    try:
        std_atts = (raw_body.get("message", {}) or {}).get("attachments") or []
        for att in std_atts:
            if isinstance(att, dict):
                u = att.get("url") or att.get("fileUrl") or att.get("link")
                if u:
                    media_urls.append(u)
    except Exception:
        pass

    # 2) Your workflow “Custom Data” key you showed in the screenshot (use either name safely)
    for key in ("message.attachments", "message.attach"):
        val = cd.get(key)
        if not val:
            continue
        try:
            # Sometimes GHL sends this as a JSON string; sometimes as a list
            if isinstance(val, str):
                s = val.strip()
                if s.startswith("[") or s.startswith("{"):
                    val = json.loads(s)
                elif s.startswith("http"):
                    media_urls.append(s)
                    continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.startswith("http"):
                        media_urls.append(item)
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("fileUrl") or item.get("link")
                        if u:
                            media_urls.append(u)
        except Exception:
            pass

    # 3) Legacy fallbacks in case some workflows still use them
    for k in ("customData.media[0]", "maybe_media_1", "maybe_media_2", "maybe_media_3"):
        v = cd.get(k)
        if v:
            media_urls.append(v)
    # --- END PREAMBLE ---

    logger.info("[API][Process] Starting DB insert for msg_id=%s phone=%s text=%s", msg_id, phone, text)

    user_id: Optional[int] = None
    convo_id: Optional[int] = None

    # ---------- DB write / lookup (tolerant) ----------
    try:
        from sqlalchemy import text as sqltext

        with db.session() as s:
            # 1) store inbound message
            s.execute(sqltext("""
                INSERT INTO inbound_messages (message_id, user_phone, text, created_at)
                VALUES (:mid, :phone, :txt, NOW())
                ON CONFLICT (message_id) DO NOTHING
            """), {"mid": msg_id, "phone": phone, "txt": text})
            s.commit()

            # 2) look up (or create) the user by phone so we get stable ids
            row = s.execute(sqltext("""
                SELECT id
                FROM users
                WHERE phone = :phone
                LIMIT 1
            """), {"phone": phone}).first()

            if row:
                user_id = int(row[0])
            else:
                # create a lightweight user if not present
                s.execute(sqltext("""
                    INSERT INTO users (phone, created_at)
                    VALUES (:phone, NOW())
                    ON CONFLICT (phone) DO NOTHING
                """), {"phone": phone})
                s.commit()
                row2 = s.execute(sqltext("""
                    SELECT id FROM users WHERE phone = :phone LIMIT 1
                """), {"phone": phone}).first()
                if row2:
                    user_id = int(row2[0])

            # 3) find or create a conversation for this user
            if user_id is not None:
                row3 = s.execute(sqltext("""
                    SELECT id
                    FROM conversations
                    WHERE user_id = :uid
                    ORDER BY created_at DESC
                    LIMIT 1
                """), {"uid": user_id}).first()
                if row3:
                    convo_id = int(row3[0])
                else:
                    s.execute(sqltext("""
                        INSERT INTO conversations (user_id, created_at)
                        VALUES (:uid, NOW())
                    """), {"uid": user_id})
                    s.commit()
                    row4 = s.execute(sqltext("""
                        SELECT id FROM conversations
                        WHERE user_id = :uid
                        ORDER BY created_at DESC
                        LIMIT 1
                    """), {"uid": user_id}).first()
                    if row4:
                        convo_id = int(row4[0])

        logger.info("[API][Process] 💾 Stored inbound: convo_id=%s user_id=%s", convo_id, user_id)

    except Exception as e:
        # DB unavailable → still send SMS using fallback ids
        logger.warning("[API][Process] DB unavailable; skipping store and using fallback ids: %s", e)
        fallback = int(os.getenv("DEV_USER_ID", "6"))
        user_id  = user_id  or fallback
        convo_id = convo_id or fallback

    # If everything somehow is still None, choose deterministic fallbacks
    if user_id is None:
        user_id = int(os.getenv("DEV_USER_ID", "6"))
    if convo_id is None:
        convo_id = user_id

    # ---------- Always enqueue the worker job ----------
    from redis import Redis
    from rq import Queue
    from app.task_queue import enqueue_generate_reply

    try:
        # Build a Queue here; do not pass this Queue into job args, only to the helper.
        _conn  = Redis.from_url(os.getenv("REDIS_URL"))
        _queue = Queue(os.getenv("QUEUE_NAME", "bestie_queue"), connection=_conn)
        # ---- Save incoming turn to Redis (conversation buffer) ----
        try:
            from redis import Redis
            _r = Redis.from_url(os.getenv("REDIS_URL"))
            key = f"conv:{convo_id}:turns"
            _r.lpush(key, json.dumps({"role": "user", "content": text or ""}))
            _r.ltrim(key, 0, 23)  # keep last ~24 turns
        except Exception:
            pass

        job = enqueue_generate_reply(
        _queue,                # your Queue object (whatever you named it)
        user_id,               # int
        convo_id,              # int
        text,                  # resolved text from the preamble above
        media_urls=media_urls, # <-- the list we just built
        user_phone=user_phone,     # <-- pass phone so worker has it
    )

        logger.success(
            "[API][Queue] ✅ Enqueued job=%s convo_id=%s user_id=%s text_len=%d",
            getattr(job, "id", "n/a"), convo_id, user_id, len(text or "")
        )
    except Exception as e:
        logger.exception("[API][Queue] ❌ Failed to enqueue job")
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