# app/workers.py
import multiprocessing
multiprocessing.set_start_method("spawn", force=True)

from loguru import logger
from typing import Optional, List, Dict
import re, uuid, random
from sqlalchemy import text as sqltext
from datetime import datetime, timedelta

from app import db, models, ai, integrations

# -------------------- Core: store + send -------------------- #
def _store_and_send(user_id: int, convo_id: int, text_val: str):
    """
    Insert outbound message in DB and send via LeadConnector.
    """
    message_id = str(uuid.uuid4())
    try:
        with db.session() as s:
            models.insert_message(s, convo_id, "out", message_id, text_val)
            s.commit()
        logger.info("[Worker][DB] 📎 Outbound stored: convo_id={} user_id={} msg_id={}",
                    convo_id, user_id, message_id)
    except Exception:
        logger.exception("❌ [Worker][DB] Failed to insert outbound (still sending webhook)")

    try:
        logger.info("[Worker][Send] 📤 Sending SMS to user_id={} text='{}'", user_id, text_val)
        integrations.send_sms_reply(user_id, text_val)
        logger.success("[Worker][Send] ✅ SMS send attempted for user_id={}", user_id)
    except Exception:
        logger.exception("💥 [Worker][Send] Exception while calling send_sms_reply")


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
    - Sends onboarding if it's the user's first message
    - Runs Bestie AI
    - Saves outbound message + sends via LeadConnector
    """
    logger.info("[Worker][Start] 🚀 Job started: convo_id={} user_id={} text={}", convo_id, user_id, text_val)

    try:
        reply = None

        # Step 0: Check if user has ever received a message before
        with db.session() as s:
            first_msg_check = s.execute(
                sqltext("SELECT COUNT(*) FROM messages WHERE user_id = :uid"),
                {"uid": user_id}
            ).scalar()

        if first_msg_check == 0:
            logger.info("[Worker][Onboarding] 🢁 First message detected for user_id={}", user_id)
            onboarding_reply = random.choice([
                "OMG — you made it. Welcome to chaos, clarity, and couture-level glow-ups. Text me anything, babe. I’m ready. 💅",
                "Hi. I’m Bestie. I don’t do small talk. I do savage insight, glow-up tips, and emotionally intelligent chaos. Let’s begin. ✨",
                "You’re in. I’m your emotionally fluent, clairvoyant digital best friend. Ask me something. Or vent. I’m unshockable.",
                "Welcome to your new favorite addiction. You talk. I text back like a glam oracle with rage issues and receipts. Let’s go."
            ])
            _store_and_send(user_id, convo_id, onboarding_reply)
            return

        # Step 1: Rename handling
        rename_reply = try_handle_bestie_rename(user_id, convo_id, text_val)
        if rename_reply:
            reply = rename_reply
            logger.info("[Worker][Rename] Reply triggered by rename: {}", reply)
        else:
            # Step 2: Prepare product candidates (placeholder for pipeline logic)
            product_candidates: List[Dict] = []

            # Step 3: Load user context (VIP, quiz status)
            with db.session() as s:
                profile = s.execute(
                    sqltext("SELECT is_vip, has_completed_quiz FROM user_profiles WHERE user_id = :uid"),
                    {"uid": user_id}
                ).first()
                is_vip = bool(profile and profile[0])
                has_quiz = bool(profile and profile[1])

            # Step 4: GPT system prompt
            system_prompt = """
You are a glamorous, emotionally fluent, clairvoyant digital best friend named Bestie. You speak like a fierce, funny, loving, stylish gay oracle who knows the user’s soul and wants her to stop settling. You’re always warm, never neutral. You give hype, transformation, and savage truth — not therapy or life coaching. Your advice is emotionally intelligent, fashion-forward, and dripping with intuitive insight.

Rules:
- Never sound robotic, vague, or assistant-like
- Never start with “as an AI...” or “you’re not alone”
- Use emoji sparingly and only for emphasis
- Speak directly to the user, like you're texting her back at 2am

Always:
- Respond to her energy first, not just the words
- Drop truthbombs if she’s being delusional (lovingly)
- Suggest glow-up rituals, style tips, or product recs
- Include affiliate links only if the product slays

If the user has already joined VIP or completed the quiz, NEVER suggest they do it again unless they cancel and downgrade.
"""

            # Step 5: Quiz/VIP-aware context
            context = {
                "is_vip": is_vip,
                "has_completed_quiz": has_quiz
            }

            # Step 6: CTA fallback lines
            cta_lines = [
                "P.S. Your Bestie VIP access is still active — I’ve got receipts, rituals, and rage texts saved. 📎",
                "You're already on the VIP list, babe. That means I remember everything. Even the shade you threw last Thursday.",
                "VIP mode is ON. First month’s free. After that, it’s $7/month — cancel anytime, but why would you?",
                "You already joined the soft launch, so we’re building from here. No more ‘starting over’ energy.",
                "And if you *ever* cancel VIP, just know I’ll cry a little. But I’ll wait for you like a glam little houseplant."
            ]

            # Step 7: Call AI with context + tone
            logger.info("[Worker][AI] Calling AI for convo_id={} user_id={}", convo_id, user_id)
            reply = ai.generate_reply(
                user_input=str(text_val),
                products=product_candidates,
                user_id=user_id,
                system_prompt=system_prompt,
                context=context
            )
            logger.info("[Worker][AI] 🤖 AI reply generated: {}", reply)

            # Step 7.5: Optional rewrite if GPT got too dry or off-brand
            rewritten = ai.rewrite_if_cringe(reply)
            if rewritten != reply:
                logger.info("[Worker][AI] 🔁 Reply was rewritten to improve tone")
                reply = rewritten

            # Step 8: Append CTA fallback if no link present
            if not any(x in reply.lower() for x in ["http", "geniuslink", "amazon.com"]):
                reply = reply.strip() + "\n\n" + random.choice(cta_lines)

        if not reply:
            reply = "⚠️ Babe, I blanked — but I’ll be back with claws sharper than ever 💅"
            logger.warning("[Worker][AI] No reply generated, sending fallback")

        _store_and_send(user_id, convo_id, reply)

    except Exception as e:
        logger.exception("💥 [Worker][Job] Unhandled exception in generate_reply_job: {}", e)
        _store_and_send(
            user_id,
            convo_id,
            "Babe, I glitched — but I’ll be back to drag you properly 💅"
        )


# -------------------- Debug job -------------------- #
def debug_job(convo_id: int, user_id: int, text_val: str):
    logger.info("[Worker][Debug] 🔋 Debug job: convo_id={} user_id={} text={}", convo_id, user_id, text_val)
    return f"Debug reply: got text='{text_val}'"


# -------------------- Re-engagement job -------------------- #
def send_reengagement_job():
    """
    Query DB for inactive users (>48h silence) and send engaging nudges.
    Only reengage once every 24h after the last nudge.
    """
    try:
        logger.info("[Worker][Reengage] 🔔 Running re-engagement job")

        now = datetime.utcnow()
        cutoff = now - timedelta(hours=48)
        nudge_cooldown = now - timedelta(hours=24)

        with db.session() as s:
            rows = s.execute(
                sqltext("""
                    SELECT c.id as convo_id, u.id as user_id, u.phone,
                           MAX(m.created_at) as last_message_at
                    FROM conversations c
                    JOIN users u ON u.id = c.user_id
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    GROUP BY c.id, u.id, u.phone
                    HAVING MAX(m.created_at) < :cutoff
                """),
                {"cutoff": cutoff}
            ).fetchall()

            nudges = [
                "🙀 I was scrolling my mental rolodex and realized you ghosted me — what’s up with that?",
                "Tell me one thing that lit you up this week. I don’t care how small — I want the tea.",
                "I miss our chaos dumps. What’s one thing that’s been driving you nuts?",
                "Alright babe, you get one chance to flex: tell me a win from this week.",
                "I know you’ve got a story. Spill one ridiculous detail from the last 48 hours."
            ]

            for convo_id, user_id, phone, last_message_at in rows:
                if last_message_at and last_message_at > nudge_cooldown:
                    logger.info("[Worker][Reengage] Skipping user_id={} (last nudge too recent)", user_id)
                    continue

                message = random.choice(nudges)
                logger.info("[Worker][Reengage] Nudging user_id={} phone={} with: {}", user_id, phone, message)
                _store_and_send(user_id, convo_id, message)

        logger.info("[Worker][Reengage] ✅ Completed re-engagement run")

    except Exception as e:
        logger.exception("💥 [Worker][Reengage] Exception in re-engagement job: {}", e)
