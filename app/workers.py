# app/workers.py
import multiprocessing
multiprocessing.set_start_method("spawn", force=True)

from loguru import logger
from typing import Optional, List, Dict
import re, uuid
from sqlalchemy import text as sqltext

from app import db, models, ai, integrations

# -------------------- Rename flow -------------------- #
RENAME_PATTERNS = [
    r"\bname\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bi(?:'|)ll\s+call\s+you\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\byour\s+name\s+is\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bfrom\s+now\s+on\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
]

def try_handle_bestie_rename(user_id: int, convo_id: int, text_val: str) -> Optional[str]:
    """
    Check if user is renaming Bestie. If yes, update DB and return a witty confirmation.
    """
    t = str(text_val).strip().lower()
    new_name: Optional[str] = None
    for pat in RENAME_PATTERNS:
        m = re.search(pat, t)
        if m:
            new_name = m.group(1).strip()
            break

    if new_name:
        with db.session() as s:
            s.execute(
                sqltext("update user_profiles set bestie_name=:n where user_id=:u"),
                {"n": new_name, "u": user_id},
            )
            s.commit()
        logger.info("[Worker][Rename] Bestie renamed for user_id={} → {}", user_id, new_name)
        return ai.witty_rename_response(new_name)
    return None

# -------------------- Worker job -------------------- #
def generate_reply_job(convo_id: int, user_id: int, text_val: str):
    """
    Main worker entrypoint:
    - Checks rename flow
    - Runs Bestie AI
    - Saves outbound message + sends via LeadConnector
    """
    logger.info("[Worker][Start] 🚀 New job started: convo_id={} user_id={} text={}", convo_id, user_id, text_val)

    try:
        reply = None

        # Step 1: Rename handling
        rename_reply = try_handle_bestie_rename(user_id, convo_id, text_val)
        if rename_reply:
            reply = rename_reply
            logger.info("[Worker][Rename] Reply triggered by rename: {}", reply)
        else:
            # Step 2: Prepare product candidates (pipeline can inject later)
            product_candidates: List[Dict] = []

            # Step 3: Call AI
            logger.info("[Worker][AI] Calling AI for convo_id={} user_id={}", convo_id, user_id)
            reply = ai.generate_reply(str(text_val), product_candidates, user_id)
            logger.info("[Worker][AI] 🤖 AI reply generated: {}", reply)

        if not reply:
            reply = "⚠️ Babe, I blanked — but I’ll be back with claws sharper than ever 💅"
            logger.warning("[Worker][AI] No reply generated, sending fallback")

        _store_and_send(user_id, convo_id, reply)

    except Exception as e:
        logger.exception("💥 [Worker][Job] Unhandled exception in generate_reply_job: {}", e)
        _store_and_send(user_id, convo_id,
                        "Bestie: Babe, I glitched — but I’ll be back to drag you properly 💅")

def _store_and_send(user_id: int, convo_id: int, text_val: str):
    """
    Insert outbound message in DB and send via LeadConnector.
    """
    message_id = str(uuid.uuid4())
    try:
        with db.session() as s:
            models.insert_message(s, convo_id, "out", message_id, text_val)
            s.commit()
        logger.info("[Worker][DB] 💾 Outbound stored: convo_id={} user_id={} msg_id={}",
                    convo_id, user_id, message_id)
    except Exception:
        logger.exception("❌ [Worker][DB] Failed to insert outbound (still sending webhook)")

    try:
        logger.info("[Worker][Send] 📤 Sending SMS to user_id={} text='{}'", user_id, text_val)
        integrations.send_sms_reply(user_id, text_val)
        logger.success("[Worker][Send] ✅ SMS send attempted for user_id={}", user_id)
    except Exception:
        logger.exception("💥 [Worker][Send] Exception while calling send_sms_reply")

# -------------------- Debug job -------------------- #
def debug_job(convo_id: int, user_id: int, text_val: str):
    logger.info("[Worker][Debug] 🐛 Debug job: convo_id={} user_id={} text={}", convo_id, user_id, text_val)
    return f"Debug reply: got text='{text_val}'"
