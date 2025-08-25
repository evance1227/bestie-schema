# app/workers.py
import multiprocessing
multiprocessing.set_start_method("spawn", force=True)

from loguru import logger
from typing import Optional
import re, uuid

from sqlalchemy import text as sqltext
from app import db, models, ai, linkwrap, monetization, integrations

# -------------------- Rename flow -------------------- #
RENAME_PATTERNS = [
    r"\bname\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bi(?:'|)ll\s+call\s+you\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\byour\s+name\s+is\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bfrom\s+now\s+on\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
]

def try_handle_bestie_rename(user_id: int, convo_id: int, text: str) -> Optional[str]:
    """
    If the user is trying to rename the assistant, update user_profiles.bestie_name
    and return a confirmation SMS. Otherwise return None.
    """
    t = text.strip().lower()
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
        logger.info("✨ Bestie renamed for user {} -> {}", user_id, new_name)
        return f"Okay! You can call me {new_name} from now on 💖"
    return None


# -------------------- Worker job -------------------- #
def generate_reply_job(convo_id: int, user_id: int, text: str):
    """
    Main worker entrypoint: handles rename logic, AI response,
    saving outbound message, and calling LeadConnector.
    ALWAYS calls _store_and_send so outbound webhook logs fire.
    """
    logger.info("🚀 Worker started job: convo_id={}, user_id={}, text={}", convo_id, user_id, text)

    reply = None

    # Step 1: rename handling
    rename_reply = try_handle_bestie_rename(user_id, convo_id, text)
    if rename_reply:
        logger.info("💡 Rename handled: {}", rename_reply)
        reply = rename_reply
    else:
        # Step 2: call AI
        try:
            reply = ai.generate_reply(convo_id, user_id, text)
            logger.info("🤖 AI reply generated: {}", reply)
        except Exception as e:
            logger.exception("❌ AI generation failed")
            reply = "Sorry, I glitched out. Try again?"

    # Step 3: save + send (always runs, even if reply is None)
    if not reply:
        reply = "⚠️ No reply was generated, but forcing outbound send for debugging."

    _store_and_send(user_id, convo_id, reply)


def _store_and_send(user_id: int, convo_id: int, text: str):
    """
    Helper to insert outbound message in DB and send via LeadConnector.
    """
    message_id = str(uuid.uuid4())
    try:
        with db.session() as s:
            models.insert_message(s, convo_id, "out", message_id, text)
            s.commit()
        logger.info("💾 Outbound message stored: convo_id={}, user_id={}, text={}", convo_id, user_id, text)
    except Exception:
        logger.exception("❌ Failed to insert outbound message into DB (but still sending webhook)")

    try:
        integrations.send_sms_reply(user_id, text)
        logger.info("📤 Outbound SMS attempted for user {}", user_id)
    except Exception:
        logger.exception("💥 Exception while calling send_sms_reply")


# -------------------- Debug job -------------------- #
def debug_job(convo_id: int, user_id: int, text: str):
    logger.info("🐛 Debug job received! convo_id={}, user_id={}, text={}", convo_id, user_id, text)
    return f"Debug reply: got text='{text}'"
