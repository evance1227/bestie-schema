# app/workers.py
import os
import multiprocessing

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)

from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from loguru import logger
from typing import Optional, List, Dict
import re, uuid, random
from sqlalchemy import text as sqltext

import base64
import requests

from app.product_search import build_product_candidates, prefer_amazon_first
from app.linkwrap import rewrite_affiliate_links_in_text
from app import ai_intent, product_search
from app import db, models, ai, integrations

import os, re, hashlib
import redis  # already present as an RQ dep

REDIS_URL = os.getenv("REDIS_URL", "")
_rds = redis.from_url(REDIS_URL, decode_responses=True, ssl=True) if REDIS_URL else None

GENIUSLINK_DOMAIN = os.getenv("GENIUSLINK_DOMAIN", "").strip()
GL_REWRITE = os.getenv("GL_REWRITE", "1").lower() not in ("0", "false", "")

def _send_dedupe_guard(conversation_id: int, text: str, ttl: int = 120) -> bool:
    """Return True if we should send; False if an identical message was sent recently."""
    if not _rds:
        return True
    key = f"dedup:out:{conversation_id}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
    return bool(_rds.set(key, "1", ex=ttl, nx=True))

def _rewrite_links_to_genius(msg: str) -> str:
    """Rewrite Amazon links to Geniuslink shortlinks (keeps everything else intact)."""
    if not (GL_REWRITE and GENIUSLINK_DOMAIN):
        return msg
    host = GENIUSLINK_DOMAIN.rstrip("/")

    def repl(m):
        asin = m.group(1)
        return f"https://{host}/{asin}"

    # …/dp/ASIN[…]
    msg = re.sub(r"https?://(?:www\.)?amazon\.[^/\s]+/[^)\s]*?/dp/([A-Z0-9]{10})", repl, msg)
    msg = re.sub(r"https?://(?:www\.)?amazon\.[^/\s]+/dp/([A-Z0-9]{10})", repl, msg)
    return msg

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
    """
    Insert outbound message in DB and send via LeadConnector.
    Automatically splits long messages into parts with [1/2], [2/2], etc.
    """
    logger.info("[Worker][Checkpoint] Finished _store_and_send")
    max_len = 450  # GHL safe limit (slightly under 480 to allow for prefix + formatting)
    parts: list[str] = []
    text_val = (text_val or "").strip()

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

def _finalize_and_send(user_id: int, convo_id: int, text_val: str, *, add_cta: bool = False):
    """
    One place to apply:
      - affiliate link rewrite
      - freshness enforcement
      - optional CTA (only when add_cta=True and no link is present)
    """
# workers.py
import random
from loguru import logger
from sqlalchemy import text as sqltext
from app import db, integrations

def _recent_outbound_texts(convo_id: int, limit: int = 12) -> list[str]:
    """
    Return recent outbound message texts. Tries several column names to avoid
    schema mismatches (text/body/message/content).
    """
    candidate_cols = ("text", "body", "message", "content")
    with db.session() as s:
        for col in candidate_cols:
            try:
                rows = s.execute(
                    sqltext(f"""
                        SELECT {col}
                        FROM messages
                        WHERE conversation_id = :cid AND direction = 'out'
                        ORDER BY created_at DESC
                        LIMIT :lim
                    """),
                    {"cid": convo_id, "lim": limit},
                ).fetchall()
                return [r[0] for r in rows]
            except Exception as e:
                # If the column doesn't exist, try the next candidate.
                if "UndefinedColumn" in str(e):
                    continue
                logger.warning("[Freshness] Failed reading recent texts with column '{}': {}", col, e)
                return []
    logger.warning("[Freshness] No known message-content column found; skipping freshness.")
    return []


def _finalize_and_send(user_id: int, convo_id: int, reply: str, add_cta: bool = False, force_send: bool = True) -> None:
    """
    Final formatting + safe send to SMS. When force_send=True, we skip hard dedup
    so messages never get silently dropped.
    """
    reply = (reply or "").strip()
    if not reply:
        logger.warning("[Worker][Send] Empty reply; nothing to send.")
        return

    # Optional CTA toggleable via env
    try:
        import os  # local import to avoid top-of-file churn
        if add_cta and os.getenv("BESTIE_APPEND_CTA") == "1":
            reply += (
                "\n\nBabe, your VIP is open: first month FREE, $7 your second, then $17/mo. "
                "Unlimited texts. https://schizobestie.gumroad.com/l/gexqp"
            )
    except Exception:
        pass

    # Soft de-dup only if NOT forcing send
    if not force_send:
        try:
            last = next(iter(_recent_outbound_texts(convo_id, limit=1)), "")
            if (last or "").strip() == reply:
                reply = reply + " ✨"
                logger.info("[Freshness] Nudged reply to avoid duplicate drop.")
        except Exception as e:
            logger.warning("[Freshness] Skipping dedup due to error: {}", e)

    logger.info("[Worker][Send] → user_id={} convo_id={} chars={} preview={!r}", user_id, convo_id, len(reply), reply[:200])

    # Send with loud success/failure logging
    try:
        # 1) enforce Geniuslink, 2) suppress dupes, 3) send
        final_text = _rewrite_links_to_genius(reply)

        if not force_send and not _send_dedupe_guard(convo_id, final_text):
            logger.warning("[Dedup] Skipping duplicate outbound for convo_id=%s", convo_id)
            return

        resp = integrations.send_sms(user_id=user_id, convo_id=convo_id, text=final_text)


        # Try to surface response metadata if available
        status = None
        sid = None
        try:
            status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
        except Exception:
            pass
        try:
            sid = getattr(resp, "sid", None)
        except Exception:
            pass

        if status:
            logger.info("[Worker][Send][OK] status={} sid={}", status, sid)
        else:
            logger.info("[Worker][Send][OK] (no response metadata)")
    except Exception as e:
        logger.exception("[Worker][Send][FAIL] {}", e)
        # Re-raise so the job is marked failed (visible in logs/dash)
        raise

    # Soft dedup: if identical to the last message, nudge instead of dropping.
    try:
        last = next(iter(_recent_outbound_texts(convo_id, limit=1)), "")
        if (last or "").strip() == reply:
            reply = reply + " " + random.choice(["✨", "💫", "💕", "🌟"])
            logger.info("[Freshness] Nudged reply to avoid duplicate drop.")
    except Exception as e:
        logger.warning("[Freshness] Skipping dedup due to error: {}", e)

    # Actually send via your integration
    integrations.send_sms(user_id=user_id, convo_id=convo_id, text=reply)

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

# --- anti-repetition helpers ---
BANNED_STOCK_PHRASES = [
    "I’ll cry a little… then wait like a glam houseplant",
    "You're already on the VIP list, babe. That means I remember everything.",
    "P.S. Your Bestie VIP access is still active — I’ve got receipts, rituals, and rage texts saved."
]

def _recent_outbound_texts(convo_id: int, limit: int = 12) -> list[str]:
    with db.session() as s:
        rows = s.execute(
            sqltext("""
                SELECT text
                FROM messages
                WHERE conversation_id = :cid AND direction = 'out'
                ORDER BY created_at DESC
                LIMIT :lim
            """),
            {"cid": convo_id, "lim": limit},
        ).fetchall()
    return [r[0] for r in rows]

def _too_similar(a: str, b: str, thresh: float = 0.85) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= thresh

def _enforce_freshness(reply: str, recent_texts: list[str]) -> str:
    """If the reply repeats recent phrasing, rewrite with new wording."""
    if any(p.lower() in reply.lower() for p in BANNED_STOCK_PHRASES) or \
       any(_too_similar(reply, prev) for prev in recent_texts):
        try:
            # ask the model to rephrase with totally different wording
            return ai.rewrite_different(
                reply,
                avoid="\n".join(recent_texts + BANNED_STOCK_PHRASES)
            )
        except Exception:
            logger.warning("[Freshness] rewrite_different failed; sending original")
    return reply

def _pick_unique_cta(recent_texts: list[str]) -> str:
    cta_pool = [
        "Want me to tailor this to your routine? Give me one detail about your skin and I’ll dial it in.",
        "If you want a deeper push, say the word and I’ll build you a tiny game plan.",
        "If you’re stuck, describe the outcome you want and I’ll map the next 3 moves."
    ]
    for cta in cta_pool:
        if all(cta not in r for r in recent_texts):
            return cta
    return ""  # nothing fresh left; skip

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
        reply: Optional[str] = None
        normalized_text = (text_val or "").lower().strip()

        # Step 0: does this conversation have any messages yet?
        with db.session() as s:
            first_msg_check = s.execute(
                sqltext("SELECT COUNT(*) FROM messages WHERE conversation_id = :cid"),
                {"cid": convo_id},
            ).scalar() or 0

        # Step 0.5: simple media detectors
        if "http" in text_val and any(x in normalized_text for x in [".jpg", ".jpeg", ".png", ".gif"]):
            logger.info("[Worker][Media] Detected image URL — sending to describe_image()")
            reply = ai.describe_image(text_val.strip())
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)
            return

        if "http" in text_val and any(x in normalized_text for x in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Detected audio URL — sending to transcribe_and_respond()")
            reply = ai.transcribe_and_respond(text_val.strip(), user_id=user_id)
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)
            return

        # Step 0.75: onboarding on the very first inbound
        if first_msg_check == 0:
            logger.info("[Worker][Onboarding] 🢁 First message for user_id={}", user_id)
            onboarding_reply = random.choice([
                "OMG — you made it. Welcome to chaos, clarity, and couture-level glow-ups. Text me anything, babe. I’m ready. 💅",
                "Hi. I’m Bestie. I don’t do small talk. I do savage insight, glow-up tips, and emotionally intelligent chaos. Let’s begin. ✨",
                "You’re in. I’m your emotionally fluent, clairvoyant digital best friend. Ask me something. Or vent. I’m unshockable.",
                "Welcome to your new favorite addiction. You talk. I text back like a glam oracle with rage issues and receipts. Let’s go.",
            ])
            _store_and_send(user_id, convo_id, onboarding_reply)  # canned -> no transforms
            return

        # Step 1: quick FAQ intercepts
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
        for key, canned in faq_responses.items():
            if key in normalized_text:
                logger.info("[Worker][FAQ] Intercepted: '{}'", key)
                _store_and_send(user_id, convo_id, canned)  # canned -> no transforms
                return

        # Step 1.5: rename flow
        rename_reply = try_handle_bestie_rename(user_id, convo_id, text_val)
        if rename_reply:
            _store_and_send(user_id, convo_id, rename_reply)  # canned-ish
            return

        # Step 2: product intent
        intent_data = None
        try:
            if hasattr(ai_intent, "extract_product_intent"):
                intent_data = ai_intent.extract_product_intent(text_val)
            else:
                logger.info("[Intent] No extractor defined; skipping product search")
        except Exception as e:
            logger.warning("[Worker][Intent] extractor unavailable or failed: {}", e)

        logger.info("[Intent] intent_data: {}", intent_data)

        product_candidates: List[Dict] = build_product_candidates(intent_data)
        product_candidates = prefer_amazon_first(product_candidates)

        if product_candidates:
            reply = render_products_for_sms(product_candidates, limit=3)
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)  # product lists: no CTA
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
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system_prompt = f"""
You are Bestie: dry, emotionally fluent, stylish, direct.
Today is {today_utc} (UTC). Never contradict obvious calendar facts.
If the user says a past date is “in the past,” do not argue.
If a question is specific enough to act on, DO NOT ask a follow-up—just help.
For shopping requests: return 2–3 concrete options with one-sentence rationale each.
Prefer Amazon links first; if you can’t find a good Amazon match, use the brand’s site (raw URL).
Links must be direct (no trackers) and kept exactly as provided.
Avoid repeating wording you’ve used in this conversation. Vary phrasing.
""".strip()

        # Step 7: call AI
        logger.info("[Worker][AI] Calling AI for convo_id={} user_id={}", convo_id, user_id)
        reply = ai.generate_reply(
            user_text=str(text_val),
            product_candidates=[],
            user_id=user_id,
            system_prompt=system_prompt,
            context=context,
        )
        logger.info("[Worker][AI] 🤖 AI reply generated: {}", reply)
        _finalize_and_send(user_id, convo_id, reply, add_cta=True, force_send=True)

        # optional tone rewrite
        try:
            rewritten = ai.rewrite_if_cringe(reply)
            if rewritten and rewritten != reply:
                logger.info("[Worker][AI] 🔁 Reply was rewritten to improve tone")
                reply = rewritten
        except Exception:
            logger.warning("[Worker][AI] rewrite_if_cringe failed; using original reply")

        # Step 8: finalize the general-chat reply (freshness + optional CTA)
        _finalize_and_send(user_id, convo_id, reply, add_cta=True)
        return

    except Exception as e:
        logger.exception("💥 [Worker][Job] Unhandled exception in generate_reply_job: {}", e)
        _finalize_and_send(
            user_id, convo_id,
            "Babe, I glitched — but I’ll be back to drag you properly 💅",
            add_cta=False
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