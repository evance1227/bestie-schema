# app/workers.py
import os
import multiprocessing

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)


from loguru import logger
from typing import Optional, List, Dict
import re, uuid, random
from sqlalchemy import text as sqltext
from datetime import datetime, timedelta

import base64
import requests

from app.product_search import build_product_candidates, prefer_amazon_first
from app.linkwrap import rewrite_affiliate_links_in_text
from app import ai_intent, product_search
from app import db, models, ai, integrations

# ---- SMS product list renderer ----
def render_products_for_sms(products, limit=3):
    lines = []
    for idx, p in enumerate(products[:limit], start=1):
        name = (p.get("title") or "").strip()
        if not name:
            name = p.get("merchant") or "Product"
        url = p["url"]
        lines.append(f"{idx}. {name}\n   {url}")
    return "\n\n".join(lines)

# -------------------- Core: store + send -------------------- #
def _store_and_send(user_id: int, convo_id: int, text_val: str):
    logger.info("[Worker][Checkpoint] Finished _store_and_send")
    # Always affiliate-ize links once, in one place
    text_val = rewrite_affiliate_links_in_text(text_val or "").strip()


    """
    Insert outbound message in DB and send via LeadConnector.
    Automatically splits long messages into parts with [1/2], [2/2], etc.
    """
    max_len = 450  # GHL safe limit (slightly under 480 to allow for prefix + formatting)
    parts = []
    text_val = text_val.strip()

    while len(text_val) > max_len:
        split_point = text_val[:max_len].rfind(" ")
        if split_point == -1:
            split_point = max_len
        parts.append(text_val[:split_point].strip())
        text_val = text_val[split_point:].strip()
    if text_val:
        parts.append(text_val)

    total_parts = len(parts)
    for idx, part in enumerate(parts, 1):
        prefix = f"[{idx}/{total_parts}] " if total_parts > 1 else ""
        full_text = prefix + part
        message_id = str(uuid.uuid4())
        try:
            with db.session() as s:
                models.insert_message(s, convo_id, "out", message_id, full_text)
                s.commit()
            logger.info("[Worker][DB] 💾 Outbound stored: convo_id={} user_id={} msg_id={}", convo_id, user_id, message_id)
        except Exception:
            logger.exception("❌ [Worker][DB] Failed to insert outbound (still sending webhook)")

        try:
            logger.info("[Worker][Send] 📤 Sending SMS to user_id={} text='{}'", user_id, full_text)
            integrations.send_sms_reply(user_id, full_text)
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

_URL_RE = re.compile(r'(https?://\S+)')

# -------------------- Worker job -------------------- #
def generate_reply_job(convo_id: int, user_id: int, text_val: str) -> None:
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

        # Quick FAQ intercepts (v1)
        faq_responses = {
            "how do i take the quiz": (
                "You take the quiz here, babe — it's short, smart, and unlocks your personalized Bestie. Style, support, vibe — all of it.\n\n👉 https://schizobestie.gumroad.com/l/gexqp"
            ),
            "where do i take the quiz": (
                "Right here, babe! This quiz is your VIP entrance to a better Bestie.\n\n👉 https://schizobestie.gumroad.com/l/gexqp"
            ),
            "quiz link": (
                "Here’s the quiz that unlocks your customized Bestie:\n\n👉 https://schizobestie.gumroad.com/l/gexqp"
            ),
            "how do i upgrade": (
                "Upgrading gets you the full Bestie experience. First month FREE, then $7 → $17/mo. Unlimited texts, full memory, savage support."
            ),
            "what’s the link": (
                "Here’s the link you need, gorgeous:\n\n👉 https://schizobestie.gumroad.com/l/gexqp"
            ),
            "how do i customize": (
                "Customization starts with the quiz. It’s where I learn your style, goals, emotional vibe — all of it. Start here:\n\n👉 https://schizobestie.gumroad.com/l/gexqp"
            ),
        }
        normalized_text = text_val.lower().strip()
        for key in faq_responses:
            if key in normalized_text:
                logger.info("[Worker][FAQ] Intercepted: '{}'", key)
                _store_and_send(user_id, convo_id, faq_responses[key])
                return

            # Step 0: Check if this conversation has any messages yet
        with db.session() as s:
            first_msg_check = s.execute(
                sqltext("SELECT COUNT(*) FROM messages WHERE conversation_id = :cid"),
                {"cid": convo_id},
            ).scalar() or 0

        # Step 0.5: simple media detectors
        if "http" in text_val and any(x in normalized_text for x in [".jpg", ".jpeg", ".png", ".gif"]):
            logger.info("[Worker][Media] Detected image URL — sending to describe_image()")
            reply = ai.describe_image(text_val.strip())
            _store_and_send(user_id, convo_id, reply)
            return

        if "http" in text_val and any(x in normalized_text for x in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Detected audio URL — sending to transcribe_and_respond()")
            reply = ai.transcribe_and_respond(text_val.strip(), user_id=user_id)
            _store_and_send(user_id, convo_id, reply)
            return

        # Step 0.75: onboarding on the very first inbound
        if first_msg_check == 0:
            logger.info("[Worker][Onboarding] 🢁 First message for user_id={}", user_id)
            onboarding_reply = random.choice([
                "OMG — you made it. Welcome to chaos, clarity, and couture-level glow-ups. Text me anything, babe. I’m ready. 💅",
                "Hi. I’m Bestie. I don’t do small talk. I do savage insight, glow-up tips, and emotionally intelligent chaos. Let’s begin. ✨",
                "You’re in. I’m your emotionally fluent, clairvoyant digital best friend. Ask me something. Or vent. I’m unshockable.",
                "Welcome to your new favorite addiction. You talk. I text back like a glam oracle with rage issues and receipts. Let’s go."
            ])
            _store_and_send(user_id, convo_id, onboarding_reply)
            return

        # Step 1: more FAQ intercepts (v2)
        faq_responses = {
            "how do i take the quiz": "You take the quiz here, babe — it's short, smart, and unlocks your personalized Bestie: https://schizobestie.gumroad.com/l/gexqp 💅",
            "where do i take the quiz": "Here’s your link, queen: https://schizobestie.gumroad.com/l/gexqp",
            "quiz link": "Quiz link incoming: https://schizobestie.gumroad.com/l/gexqp",
            "how much is vip": "VIP is free the first month, $7 the second, then $17/month after that. Cancel anytime. Unlimited texts. I remember everything. 💾",
            "vip cost": "First month’s free, then $7, then $17/month. Cancel anytime.",
            "price of vip": "VIP pricing: $0 → $7 → $17/month. Full access. Cancel anytime.",
            "how much are prompt packs": "Prompt Packs are $7 each or 3 for $20 — cheat codes for glow-ups 💥",
            "prompt pack price": "Each pack is $7 — or 3 for $20. Link: https://schizobestie.gumroad.com/",
            "prompt packs link": "Right this way, babe: https://schizobestie.gumroad.com/",
        }
        for key in faq_responses:
            if key in normalized_text:
                logger.info("[Worker][FAQ] Intercepted: '{}'", key)
                _store_and_send(user_id, convo_id, faq_responses[key])
                return

        # Step 1.5: rename flow
        rename_reply = try_handle_bestie_rename(user_id, convo_id, text_val)
        if rename_reply:
            reply = rename_reply
            logger.info("[Worker][Rename] Reply triggered by rename: {}", reply)
        else:
        # Step 2: product intent (optional)
            intent_data = None
            try:
                from app import ai_intent  # import lazily/safely
                if hasattr(ai_intent, "extract_product_intent"):
                    intent_data = ai_intent.extract_product_intent(text_val)
                else:
                    logger.info("[Intent] No extractor defined; skipping product search")
            except Exception as e:
                logger.warning("[Worker][Intent] extractor unavailable or failed: {}", e)

            logger.info("[Intent] intent_data: {}", intent_data)

            product_candidates = build_product_candidates(intent_data)

            # Prefer Amazon first
            product_candidates = prefer_amazon_first(product_candidates)
                     
            if product_candidates:
                reply = render_products_for_sms(product_candidates, limit=3)
                _store_and_send(user_id, convo_id, reply)
                return
            # Turn candidates into an SMS-friendly block
            products_block = render_products_for_sms(product_candidates, limit=3)
            if products_block:
                reply = (
                    "Here are a few picks I think you’ll love:\n\n"
                    f"{products_block}"
                )
                _store_and_send(user_id, convo_id, reply)
                return

            if products_block:
                reply = (
                    "Here are a few picks I think you’ll love:\n\n"
                    f"{products_block}"
                )
                _store_and_send(user_id, convo_id, reply)
                return  

            # Step 3: user context (VIP / quiz flags)
            with db.session() as s:
                profile = s.execute(
                    sqltext("SELECT is_vip, has_completed_quiz FROM user_profiles WHERE user_id = :uid"),
                    {"uid": user_id}
                ).first()
            is_vip = bool(profile and profile[0])
            has_quiz = bool(profile and profile[1])
            context = {"is_vip": is_vip, "has_completed_quiz": has_quiz}

            # Step 4: system prompt
            system_prompt = """
            You are a dry, emotionally fluent, intuitive digital best friend named Bestie. You are stylish, sarcastic, direct, and clairvoyant.
            Avoid dramatics. Avoid big metaphors. Avoid sounding like a life coach or a cheesy affirmation app.
            [...trimmed for brevity...]
            """

            # Step 6: CTA fallback lines
            cta_lines = [
                "P.S. Your Bestie VIP access is still active — I’ve got receipts, rituals, and rage texts saved. 📎",
                "You're already on the VIP list, babe. That means I remember everything.",
                "VIP mode is ON. First month’s free. After that, it’s $7/month — cancel anytime.",
                "You already joined the soft launch, so we’re building from here.",
                "And if you *ever* cancel VIP, I’ll cry a little… then wait like a glam houseplant.",
            ]

            # Step 7: call AI
            logger.info("[Worker][AI] Calling AI for convo_id={} user_id={}", convo_id, user_id)
            reply = ai.generate_reply(
                user_text=str(text_val),
                product_candidates=product_candidates,
                user_id=user_id,
                system_prompt=system_prompt,
                context=context,
            )
            logger.info("[Worker][AI] 🤖 AI reply generated: {}", reply)
            
            # optional rewrite
            rewritten = ai.rewrite_if_cringe(reply)
            if rewritten != reply:
                logger.info("[Worker][AI] 🔁 Reply was rewritten to improve tone")
                reply = rewritten

             # Step 8: ensure some CTA if no link present
            if not any(x in reply.lower() for x in ["http", "geniuslink", "amazon.com"]):
                reply = reply.strip() + "\n\n" + random.choice(cta_lines)

        if not reply:
            reply = "⚠️ Babe, I blanked — but I’ll be back with claws sharper than ever 💅"
            logger.warning("[Worker][AI] No reply generated, sending fallback")
   
        
        _store_and_send(user_id, convo_id, reply)

    except Exception as e:
        logger.exception("💥 [Worker][Job] Unhandled exception in generate_reply_job: {}", e)
        
        _store_and_send(user_id, convo_id, "Babe, I glitched — but I’ll be back to drag you properly 💅")

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