# app/main.py
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from loguru import logger
import time, re

# ✅ Use relative imports so "app" doesn’t break
from . import db, models
from .task_queue import enqueue_generate_reply
from app.workers import send_reengagement_job

app = FastAPI(title="Bestie Backend")

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
        return JSONResponse(status_code=200, content={"ok": False, "error": "invalid json"})

    # ✅ Use customData if present, else fallback to body
    cd = body.get("customData") or body.get("custom_data")
    if cd and isinstance(cd, dict):
        payload = cd
    else:
        payload = body

    # ✅ Always force unique message_id by appending timestamp
    base_id = (
        payload.get("message_id")
        or body.get("message_id")
        or f"{body.get('contact', {}).get('id', 'contact')}"
    )
    message_id = f"{base_id}-{int(time.time())}"
    logger.info("[API][Webhook] Generated unique message_id={}", message_id)

    # Extract phone + normalize
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

    # Extract text value
    text_val = (
        payload.get("text")
        or body.get("text")
        or body.get("message", {}).get("body")
        or body.get("activity", {}).get("body")
        or body.get("contact", {}).get("last_message")
    )
    logger.info("[API][Webhook] Extracted text={}", text_val)

    if not user_phone or not text_val:
        logger.warning("⚠️ [API][Webhook] Missing required fields. phone={} text={}",
                       user_phone, text_val)
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": "missing required fields"}
        )

    # ✅ Fire background worker so GHL doesn’t wait
    logger.info("[API][Webhook] Handing off to background task for msg_id={}", message_id)
    background_tasks.add_task(process_incoming, message_id, user_phone, text_val, body)

    # ✅ Always ACK immediately
    logger.info("[API][Webhook] ✅ ACK sent to GHL for msg_id={}", message_id)
    return {"ok": True, "message_id": message_id, "echo_text": text_val or ""}

# -------------------- Re-engagement endpoint -------------------- #
@app.post("/tasks/trigger_reengagement")
async def trigger_reengagement(background_tasks: BackgroundTasks):
    """Manually or via cron: enqueue re-engagement nudges for inactive users."""
    logger.info("[API][Reengage] 🔔 Trigger received")
    background_tasks.add_task(send_reengagement_job)
    return {"ok": True, "message": "Re-engagement job queued"}
