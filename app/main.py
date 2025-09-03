import os
import time, re
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text as sqltext
from app import db
from app.webhooks_gumroad import router as gumroad_router

CRON_SECRET = os.getenv("CRON_SECRET")

# Explicitly set docs & openapi so /docs exists
app = FastAPI(
    title="Bestie Backend",
    docs_url="/docs",
    openapi_url="/openapi.json",
    redoc_url=None,
)
logger.info("[API][Boot] Using REDIS_URL={}", os.getenv("REDIS_URL"))
app.include_router(gumroad_router)

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/tasks/plan_rollover")
def plan_rollover(request: Request):
    if CRON_SECRET and request.headers.get("x-cron-secret") != CRON_SECRET:
        return {"ok": False, "error": "forbidden"}
    with db.session() as s:
        s.execute(sqltext("""
            UPDATE public.user_profiles
            SET plan_status='intro',
                plan_renews_at = NOW() + INTERVAL '14 days'
            WHERE plan_status='trial'
              AND trial_start_date IS NOT NULL
              AND NOW() > trial_start_date + INTERVAL '14 days'
        """))
        s.commit()
    return {"ok": True}

# -------------------- DEBUG: queue probe -------------------- #
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

# ✅ Use package-relative imports
from . import db, models
from .task_queue import enqueue_generate_reply
from .workers import send_reengagement_job

# -------------------- Core processing -------------------- #
def process_incoming(message_id: str, user_phone: str, text_val: str, raw_body: dict):
    """Run DB insert + enqueue safely in background after GHL gets 200."""
    try:
        logger.info("[API][Process] Starting DB insert for msg_id={} phone={} text={}",
                    message_id, user_phone, text_val)

        with db.session() as s:
            # Always allow follow-ups — each inbound SMS is unique
            user = models.get_or_create_user_by_phone(s, user_phone)
            convo = models.get_or_create_conversation(s, user.id)
            models.insert_message(s, convo.id, "in", message_id, text_val)
            s.commit()
            logger.info("[API][Process] 💾 Stored inbound: convo_id={} user_id={}",
                        convo.id, user.id)

        # Hand off to worker
        job = enqueue_generate_reply(convo.id, user.id, text_val)
        logger.success("[API][Queue] ✅ Enqueued job={} convo_id={} user_id={} text={}",
                       job.id, convo.id, user.id, text_val)

    except Exception as e:
        logger.exception("💥 [API][Process] Exception in background process: {}", e)

# -------------------- Inbound webhook -------------------- #
@app.post("/webhook/incoming_message")
async def incoming_message_any(req: Request, background_tasks: BackgroundTasks):
    """Inbound webhook from GHL → normalize payload → enqueue reply job."""
    try:
        body = await req.json()
        logger.info("[API][Webhook] >>> Incoming webhook hit! Raw body: {}", body)
    except Exception:
        logger.exception("[API][Webhook] Invalid JSON received")
        return JSONResponse(status_code=200, content={"ok": True, "error": "invalid json"})

    try:
        # ✅ Use customData if present, else fallback to body
        cd = body.get("customData") or body.get("custom_data")
        payload = cd if cd and isinstance(cd, dict) else body

        # ✅ Always force unique message_id by appending timestamp
        base_id = (
            payload.get("message_id")
            or body.get("message_id")
            or f"{body.get('contact', {}).get('id', 'contact')}"
        )
        message_id = f"{base_id}-{int(time.time())}"
        logger.info("[API][Webhook] Generated unique message_id={}", message_id)

        # Extract and normalize phone number
        user_phone = (
            payload.get("user_phone")
            or body.get("user_phone")
            or body.get("phone")
            or body.get("contact", {}).get("phone")
        )
        if user_phone:
            digits = re.sub(r"\D", "", str(user_phone))
            if len(digits) == 10:
                user_phone = "+1" + digits
            elif not user_phone.startswith("+"):
                user_phone = "+" + digits
        logger.info("[API][Webhook] Normalized phone={}", user_phone)

        # Extract text
        text_val = (
            payload.get("text")
            or body.get("text")
            or body.get("message", {}).get("body")
            or body.get("activity", {}).get("body")
            or body.get("contact", {}).get("last_message")
        )

        # ✅ Detect image/audio attachments
        attachments = body.get("message", {}).get("attachments", [])
        if attachments:
            for a in attachments:
                url = a.get("url") or a.get("file_url")
                filetype = a.get("type", "").lower()
                if url:
                    if filetype == "image":
                        text_val += f"\n[User sent an image: {url}]"
                    elif filetype == "audio":
                        text_val += f"\n[User sent a voice note: {url}]"
                    else:
                        text_val += f"\n[User sent a file: {url}]"

        logger.info("[API][Webhook] Extracted text={}", text_val)

        if not user_phone or not text_val:
            logger.warning("⚠️ [API][Webhook] Missing required fields. phone={} text={}",
                           user_phone, text_val)
            return JSONResponse(
                status_code=200,
                content={"ok": True, "error": "missing required fields"}
            )

        # ✅ Fire background worker so GHL doesn’t wait
        logger.info("[API][Webhook] Handing off to background task for msg_id={}", message_id)
        background_tasks.add_task(process_incoming, message_id, user_phone, text_val, body)

    except Exception as e:
        logger.exception("💥 [API][Webhook] Unexpected failure, but ACKing anyway: {}", e)

    # ✅ Always ACK immediately, even if processing failed
    logger.info("[API][Webhook] ✅ Final ACK sent to GHL")
    return {
        "ok": True,
        "message_id": locals().get("message_id", "fallback"),
        "echo_text": locals().get("text_val", "")
    }

# -------------------- Re-engagement endpoint -------------------- #
@app.post("/tasks/trigger_reengagement")
async def trigger_reengagement(background_tasks: BackgroundTasks):
    """Manually or via cron: enqueue re-engagement nudges for inactive users."""
    logger.info("[API][Reengage] 🔔 Trigger received")
    background_tasks.add_task(send_reengagement_job)
    return {"ok": True, "message": "Re-engagement job queued"}
