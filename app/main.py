from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from loguru import logger
import time, re, os

# ✅ Use relative imports so "app" doesn’t break
from . import db, models
from .task_queue import enqueue_generate_reply

app = FastAPI(title="Bestie Backend")

def process_incoming(message_id: str, user_phone: str, text_val: str, raw_body: dict):
    """Run DB insert + enqueue safely in background after GHL gets 200."""
    try:
        with db.session() as s:
            # ✅ Don’t block follow-ups as duplicates — every inbound should be unique
            user = models.get_or_create_user_by_phone(s, user_phone)
            convo = models.get_or_create_conversation(s, user.id)
            models.insert_message(s, convo.id, "in", message_id, text_val)
            s.commit()

        job = enqueue_generate_reply(convo.id, user.id, text_val)
        logger.success(
            "✅ Enqueued job {} for convo_id={} user_id={} text={}",
            job.id, convo.id, user.id, text_val,
        )

    except Exception as e:
        logger.exception("💥 Exception in background process: {}", e)


@app.post("/webhook/incoming_message")
async def incoming_message_any(req: Request, background_tasks: BackgroundTasks):
    """Inbound webhook from GHL → normalize payload → enqueue reply job."""
    try:
        body = await req.json()
        logger.info(">>> Webhook hit! Raw body: {}", body)
    except Exception:
        logger.exception("Invalid JSON received")
        # ✅ Respond 200 so GHL doesn’t retry forever
        return JSONResponse(status_code=200, content={"ok": False, "error": "invalid json"})

    # Use customData if present
    cd = body.get("customData") or body.get("custom_data")
    payload = cd if isinstance(cd, dict) else {}

    # ✅ Always force unique message_id by appending timestamp
    base_id = (
        payload.get("message_id")
        or body.get("message_id")
        or f"{body.get('contact', {}).get('id', 'contact')}"
    )
    message_id = f"{base_id}-{int(time.time())}"

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

    # Extract text value
    text_val = (
        payload.get("text")
        or body.get("text")
        or body.get("message", {}).get("body")
        or body.get("activity", {}).get("body")
        or body.get("contact", {}).get("last_message")
    )

    if not user_phone or not text_val:
        logger.warning("⚠️ Missing required fields. phone={} text={}", user_phone, text_val)
        return JSONResponse(
            status_code=200,  # still ACK so GHL doesn’t retry
            content={"ok": False, "error": "missing required fields"}
        )

    # ✅ Fire background worker so GHL doesn’t wait
    background_tasks.add_task(process_incoming, message_id, user_phone, text_val, body)

    # ✅ Always ACK immediately
    return {"ok": True, "message_id": message_id, "echo_text": text_val or ""}
