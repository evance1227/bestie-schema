# app/workers.py
"""
RQ worker job for SMS Bestie.

Core:
- Clean single send path
- Paywall + trial gates (invite-only upgrades handled by Gumroad webhooks)
- Media routing (image describe, audio transcribe)
- Product intent → candidates → Bestie tone reply
- Routine/overlap audit path (optionally followed by products when asked/ implied)
- General chat fallback with tone guards
- SMS link hygiene (Amazon search injection + affiliate)
- Re-engagement job (48h quiet)

House policy:
- 1-week free, then $17/month. Cancel anytime. Upgrades unlock by invitation.
- No "Bestie Team Faves" CTA. No default sales CTA from code unless explicitly enabled.
"""

from __future__ import annotations

# --------------------------- Standard imports --------------------------- #
import os
import re
import uuid
import hashlib
import random
import time
import requests
import json
from app.ai import generate_contextual_closer
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
from app.linkwrap import wrap_all_affiliates, ensure_not_link_ending


# ----------------------------- Third party ----------------------------- #
import redis
from loguru import logger
from sqlalchemy import text as sqltext

# ------------------------------ App deps ------------------------------- #
from app import db, models, ai, integrations, linkwrap

# ---------------------------------------------------------------------- #
# Environment and globals
# ---------------------------------------------------------------------- #
REDIS_URL = os.getenv("REDIS_URL", "")
_rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
USE_GHL_ONLY = (os.getenv("USE_GHL_ONLY", "1").lower() not in ("0","false","no"))
SEND_FALLBACK_ON_ERROR = (os.getenv("SEND_FALLBACK_ON_ERROR", "0").lower() in ("1","true","yes"))
logger.info("[Boot] USE_GHL_ONLY=%s  SEND_FALLBACK_ON_ERROR=%s", USE_GHL_ONLY, SEND_FALLBACK_ON_ERROR)
# Message pacing so carriers preserve multipart ordering
SMS_PART_DELAY_MS = int(os.getenv("SMS_PART_DELAY_MS", "800"))

# Dev bypass
DEV_BYPASS_PHONE = os.getenv("DEV_BYPASS_PHONE", "").strip()

# Trial / plan
ENFORCE_SIGNUP = os.getenv("ENFORCE_SIGNUP_BEFORE_CHAT", "0") == "1"
FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "7"))

# Gumroad/links (names kept for back-compat)
VIP_URL   = os.getenv("VIP_URL",   "https://schizobestie.gumroad.com/l/bestie_basic")
TRIAL_URL = os.getenv("TRIAL_URL", "https://schizobestie.gumroad.com/l/bestie_basic")
FULL_URL  = os.getenv("FULL_URL",  "https://schizobestie.gumroad.com/l/bestie_basic")
QUIZ_URL  = os.getenv("QUIZ_URL",  "https://tally.so/r/YOUR_QUIZ_ID")

# Optional toggles (default OFF)
VIP_SOFT_ENABLED = os.getenv("VIP_SOFT_ENABLED", "0").lower() not in ("0","false","no","off")
BESTIE_PRODUCT_CTA_ENABLED = os.getenv("BESTIE_PRODUCT_CTA_ENABLED", "0").lower() not in ("0","false","no","off")

# VIP soft-pitch throttles (used only if VIP_SOFT_ENABLED)
VIP_COOLDOWN_MIN = int(os.getenv("VIP_COOLDOWN_MIN", "20"))
VIP_DAILY_MAX    = int(os.getenv("VIP_DAILY_MAX", "2"))
_VIP_STOP        = re.compile(r"(stop( trying)? to sell|don'?t sell|no vip|quit pitching|stop pitching)", re.I)

def _maybe_inject_vip_by_convo(reply: str, convo_id: int, user_text: str) -> str:
    # VIP not used in this product anymore; keep as hard no-op
    return reply

def _fix_vip_links(text: str) -> str:
    # VIP not used; hard no-op
    return text
       
# Geniuslink / Amazon wrap toggles (leave blank to disable)
GENIUSLINK_DOMAIN = (os.getenv("GENIUSLINK_DOMAIN") or "").strip()
GENIUSLINK_WRAP   = (os.getenv("GENIUSLINK_WRAP") or "").strip()  # e.g. https://geni.us/redirect?url={url}
GL_REWRITE        = os.getenv("GL_REWRITE", "1").lower() not in ("0", "false", "")
_AMZN_RE          = re.compile(r"https?://(?:www\.)?amazon\.[^\s)\]]+", re.I)

_AMZ_SEARCH_RE = re.compile(r"https?://(?:www\.)?amazon\.[^/\s]+/s\?[^)\s]+", re.I)

def _strip_amazon_search_links(text: str) -> str:
    """Remove Amazon search URLs; we only allow direct DP links (handled upstream) or non-Amazon tutorials."""
    try:
        return _AMZ_SEARCH_RE.sub("", text or "")
    except Exception:
        return text
# Strip bracketed link placeholders like: [link for ideas: ...]
_LINK_PLACEHOLDER_RE = re.compile(r"\[(?:link|links)[^\]]*\]", re.I)
_URL_WORD_RE = re.compile(r"\bURL\b[: ]?", re.I)

def _strip_link_placeholders(text: str) -> str:
    try:
        t = _LINK_PLACEHOLDER_RE.sub("", text or "")
        t = _URL_WORD_RE.sub("", t)
        return t
    except Exception:
        return text

# Opening/tone guards
OPENING_BANNED = [
    "it sounds like", "i understand that", "you're not alone",
    "i'm sorry you're", "technology can be", "i get that",
]
BANNED_STOCK_PHRASES = [
    # kill any stale cringe if it sneaks in
    "I’ll cry a little", "houseplant", "you’re already on the VIP list",
]

# ---------------------------------------------------------------------- #
# Utilities
# ---------------------------------------------------------------------- #
_URL_END_RE = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

def _maybe_append_ai_closer(reply: str, user_text: str, category: str | None, convo_id: int) -> str:
    """
    If reply ends abruptly or on a URL, ask AI for a closer.
    Avoid repeating last few outbounds. If AI returns "", skip.
    """
    try:
        abrupt = bool(_URL_END_RE.search(reply or "")) or (len((reply or "").splitlines()) <= 2)
        if not abrupt:
            return reply

        recent = _recent_outbound_texts(convo_id, limit=6)
        closer = generate_contextual_closer(user_text, category=category, recent_lines=recent, max_len=90)
        if not closer:
            return reply

        # naive anti-repeat check
        if any(closer.lower() in (r or "").lower() for r in recent):
            return reply
        return (reply or "").rstrip() + "\n" + closer
    except Exception:
        return reply

def _norm_phone(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    d = re.sub(r"\D", "", p)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return p if p.startswith("+") else ("+" + d if d else None)

def _recent_outbound_texts(convo_id: int, limit: int = 12) -> List[str]:
    """Fetch recent outbound texts for freshness checks without assuming a single column name."""
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
                if "UndefinedColumn" in str(e):
                    continue
                logger.warning("[Freshness] Failed using column '{}': {}", col, e)
                return []
    logger.warning("[Freshness] No known message column. Skipping.")
    return []

# ---------------------------------------------------------------------- #
# Paywall / plan state
# ---------------------------------------------------------------------- #
def _has_ever_started_trial(user_id: int) -> bool:
    with db.session() as s:
        r = s.execute(sqltext(
            "SELECT trial_start_date FROM public.user_profiles WHERE user_id=:u"
        ), {"u": user_id}).first()
    return bool(r and r[0])

def _wall_start_message(user_id: int) -> str:
    """
    Ask user to start access and take the quiz. Chooses trial vs full link.
    """
    link = TRIAL_URL if not _has_ever_started_trial(user_id) else FULL_URL
    if link == TRIAL_URL:
        return (
            "Before we chat, start your access so I remember everything and tailor recs.\n"
            f"{link}\n"
            "1 week free, then $17/mo. Cancel anytime. No refunds."
        )
    return (
        "Your trial ended. To keep going it’s $17/mo. Cancel anytime. No refunds.\n"
        f"{FULL_URL}"
    )

def _wall_trial_expired_message() -> str:
    return (
        "Your free week ended. Upgrade to keep the customized magic. $17/mo. Cancel anytime. No refunds.\n"
        f"{FULL_URL}"
    )

def _ensure_profile_defaults(user_id: int) -> Dict[str, object]:
    """Normalize profile counters and return current entitlement snapshot."""
    with db.session() as s:
        s.execute(sqltext("""
            UPDATE public.user_profiles
            SET plan_status        = COALESCE(plan_status, 'pending'),
                daily_counter_date = COALESCE(daily_counter_date, CURRENT_DATE),
                daily_msgs_used    = COALESCE(daily_msgs_used, 0),
                trial_msgs_used    = COALESCE(trial_msgs_used, 0),
                is_quiz_completed  = COALESCE(is_quiz_completed, false)
            WHERE user_id = :u
        """), {"u": user_id})
        s.commit()

        row = s.execute(sqltext("""
            SELECT gumroad_customer_id, gumroad_email, plan_status,
                   trial_start_date, plan_renews_at, is_quiz_completed,
                   daily_msgs_used, daily_counter_date
            FROM public.user_profiles
            WHERE user_id = :u
        """), {"u": user_id}).first()

    if not row:
        return {"allowed": False, "reason": "pending"}

    _, _, plan_status, trial_start, _, _, daily_used, daily_date = row

    # reset daily counters if date rolled
    if daily_date != datetime.now(timezone.utc).date():
        with db.session() as s:
            s.execute(sqltext("""
                UPDATE public.user_profiles
                SET daily_counter_date = CURRENT_DATE, daily_msgs_used = 0
                WHERE user_id = :u
            """), {"u": user_id})
            s.commit()

    # plan gate
    if ENFORCE_SIGNUP and (not plan_status or plan_status in ("pending", "")):
        return {"allowed": False, "reason": "pending"}

    if plan_status == "trial":
        if not trial_start:
            return {"allowed": False, "reason": "pending"}
        days_in = (datetime.now(timezone.utc) - trial_start).days
        if days_in >= FREE_TRIAL_DAYS:
            return {"allowed": False, "reason": "expired"}
        return {"allowed": True, "reason": "trial", "days_in": days_in}

    if plan_status in ("intro", "active"):
        return {"allowed": True, "reason": plan_status}

    if plan_status in ("expired", "canceled"):
        return {"allowed": False, "reason": plan_status}

    return {"allowed": False, "reason": "pending"}
                                 
# ---------------------------------------------------------------------- #
# Final storage and SMS send
# ---------------------------------------------------------------------- #
# --- outbound dedupe: skip if we just sent the exact same text in this convo ---


def _add_personality_if_flat(text: str) -> str:
    if not text:
        return text
    if text.count("http") >= 2 and len(text) < 480:
        opener = "Got you, babe. Here are a couple that actually work:"
        text = opener + "\n" + text
    return text

# after
def _store_and_send(
    user_id: int,
    convo_id: int,
    text_val: str,
    send_phone: Optional[str] = None,
) -> None:
    """
    Single place to store and send. Splits long messages to ~450 chars with [1/2] prefix.
    Adds a tiny headway between multipart sends so carriers keep order.
    Applies final link and tone cleanup before sending and storage.
    """
    # --- outbound dedupe: skip if we just sent the exact same text in this convo ---
    try:
        from hashlib import sha1
        sig = sha1((str(convo_id) + "::" + str(text_val)).encode("utf-8")).hexdigest()
        k   = f"sent:{convo_id}:{sig}"
        # try to set for 30s; if it already exists, someone just sent the same text
        if _rds and not _rds.set(k, "1", ex=30, nx=True):
            logger.info("[Send][Dedup] Skipping duplicate send for convo %s", convo_id)
            return
    except Exception:
        pass

    max_len = 450
    parts: List[str] = []

    text_val = (text_val or "").strip()
    if not text_val:
        return

    # ==== Final shaping ====
    text_val = _add_personality_if_flat(text_val)
    text_val = wrap_all_affiliates(text_val)      # affiliate/link wrap if you call it here
    text_val = ensure_not_link_ending(text_val)   # no naked URL at end

    # ==== One-time debug marker ====
    DEBUG_MARKER = os.getenv("DEBUG_MARKER", "")
    if DEBUG_MARKER:
        text_val = text_val.rstrip() + f"\n{DEBUG_MARKER}"

    # ==== POST to GHL (use the real inbound phone if provided) ====
    # Normalize: prefer job-supplied phone; fall back to TEST_PHONE only for dev
   # always use the real phone the webhook passed to the worker; no test fallbacks
    user_phone = _norm_phone(send_phone) if send_phone else None
    if not USE_GHL_ONLY:
        try:
            integrations.send_sms_reply(user_id, text_val)
            logger.success("[Worker][Send] SMS send attempted for user_id={}", user_id)
        except Exception:
            logger.exception("[Worker][Send] Exception while calling send_sms_reply")

    # ==== POST to GHL (use the real inbound phone if provided) ====
    GHL_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL")
    if GHL_WEBHOOK_URL:
        ghl_payload = {
            "phone": user_phone,
            "message": text_val,
            "user_id": user_id,
            "convo_id": convo_id
        }
        try:
            resp = requests.post(GHL_WEBHOOK_URL, json=ghl_payload, timeout=8)
            logger.info(
                "[GHL_SEND] status={} body={}",
                getattr(resp, "status_code", None),
                (getattr(resp, "text", "") or "")[:200],
            )
        except Exception as e:
            logger.warning("[GHL_SEND] Failed to POST to GHL: {}", e)

    # ==== Break into SMS chunks ====
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
            logger.info("[Worker][DB] Outbound stored: convo_id={} user_id={} msg_id={}", convo_id, user_id, message_id)
        except Exception:
            logger.exception("[Worker][DB] Failed to insert outbound, will still attempt send")

        if not USE_GHL_ONLY:
            try:
                integrations.send_sms_reply(user_id, full_text)
                logger.success("[Worker][Send] SMS send attempted for user_id={}", user_id)
            except Exception:
                logger.exception("[Worker][Send] Exception while calling send_sms_reply")


        # tiny pause so carriers preserve ordering
        try:
            if total_parts > 1 and idx < total_parts:
                time.sleep(SMS_PART_DELAY_MS / 1000.0)
        except Exception:
            pass
   
# ---------------------------------------------------------------------- #
# Rename flow
# ---------------------------------------------------------------------- #
RENAME_PATTERNS = [
    r"\bname\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bi(?:'|)ll\s+call\s+you\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\byour\s+name\s+is\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
    r"\bfrom\s+now\s+on\s+you\s+are\s+['\"]?([A-Za-z0-9\- _]{2,32})['\"]?",
]

def try_handle_bestie_rename(user_id: int, convo_id: int, text_val: str) -> Optional[str]:
    t = str(text_val).strip().lower()
    for pat in RENAME_PATTERNS:
        m = re.search(pat, t)
        if m:
            new_name = m.group(1).strip()
            with db.session() as s:
                s.execute(sqltext("UPDATE user_profiles SET bestie_name=:n WHERE user_id=:u"),
                          {"n": new_name, "u": user_id})
                s.commit()
            logger.info("[Worker][Rename] Bestie renamed for user_id={} → {}", user_id, new_name)
            return ai.witty_rename_response(new_name)
    return None

# ---------------------------------------------------------------------- #
# Tone freshness helpers
# ---------------------------------------------------------------------- #
def _fix_cringe_opening(reply: str) -> str:
    if not reply:
        return reply
    lines = reply.splitlines()
    if not lines:
        return reply
    first = lines[0]
    if any(first.lower().startswith(p) for p in OPENING_BANNED):
        try:
            return ai.rewrite_different(
                reply,
                avoid="\n".join(OPENING_BANNED + BANNED_STOCK_PHRASES),
                instruction="Rewrite the first line to be punchy, confident, and helpful. No therapy cliches."
            )
        except Exception:
            return "\n".join(lines[1:]) if len(lines) > 1 else reply
    return reply

# ---------------------------------------------------------------------- #
# Main worker entrypoint
# ---------------------------------------------------------------------- #
def generate_reply_job(
    convo_id: int,
    user_id: int,
    text_val: str,
    user_phone: Optional[str] = None,
    media_urls: Optional[List[str]] = None,
) -> None:
    """
    Routing:
      0) Plan gate
      1) First message onboarding
      2) Media routing (image/audio)
      ...
    """
    logger.info("[Worker][Start] Job: convo_id={} user_id={} text={}", convo_id, user_id, text_val)

    # 🔑 Normalize the inbound phone so it matches DEV_BYPASS_PHONE
    try:
        user_phone = _norm_phone(user_phone) or user_phone
    except Exception:
        pass

    user_text = str(text_val or "")
    normalized_text = user_text.lower().strip()

    logger.info(
        "[Worker][Start] Job: convo_id=%s user_id=%s text_len=%d media_cnt=%d",
        convo_id, user_id, len(text_val or ""), len(media_urls or [])
    )

    # 0) Gate
    try:
        gate_snapshot = _ensure_profile_defaults(user_id)
        logger.info("[Gate] user_id={} -> {}", user_id, gate_snapshot)

        np = _norm_phone(user_phone)
        nb = _norm_phone(DEV_BYPASS_PHONE)
        dev_bypass = bool(np and nb and np == nb)

        allowed = bool(gate_snapshot.get("allowed"))

        if not (dev_bypass or allowed):
            # Deduplicate paywall: if we just sent it, don’t spam.
            recent = _recent_outbound_texts(convo_id, limit=8)
            recent_has_paywall = any(
                ("gumroad.com" in (t or "").lower() or "quiz" in (t or "").lower())
                for t in recent
            )
            if recent_has_paywall:
                logger.info("[Gate] Paywall already sent recently; skipping re-send.")
                return

            msg = _wall_start_message(user_id)
            _store_and_send(user_id, convo_id, msg, send_phone=user_phone)
            return  # <- stop here when not allowed


    except Exception as e:
        logger.exception("[Gate] snapshot/build error: {}", e)
        _store_and_send(user_id, convo_id, "Babe, I glitched. Give me one sec to reboot my attitude. 💅")
        return

        # 2) Media routing

    # --- attachments passed from webhook (preferred) ---
    if media_urls:
        first = (media_urls[0] or "").strip()
        lower = first.lower()
        try:
            if any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                logger.info("[Worker][Media] Attachment image detected: {}", first)
                reply = ai.describe_image(first)              # <-- was transcribe_and_respond
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return

            if any(lower.endswith(ext) for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                logger.info("[Worker][Media] Attachment audio detected: {}", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
            # Extensionless: try image, then audio
            logger.info("[Worker][Media] Attachment extless; trying image describe: {}", first)
            try:
                reply = ai.describe_image(first)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
            except Exception:
                logger.info("[Worker][Media] describe_image failed; trying audio transcribe: {}", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
        except Exception as e:
            logger.warning("[Worker][Media] Attachment handling failed: {}", e)
            # fall through to your existing text-based routing below

    # 2) Media routing
    if "http" in user_text:
        if any(ext in normalized_text for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            logger.info("[Worker][Media] Image URL detected, describing.")
            reply = ai.describe_image(user_text.strip())
            _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
            return
        if any(ext in normalized_text for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Audio URL detected, transcribing.")
            reply = ai.transcribe_and_respond(user_text.strip(), user_id=user_id)
            _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
            return

    # 3) FAQs (pricing corrected)
    faq_map = {
        "how do i take the quiz": f"Take the quiz here, babe. It unlocks your personalized Bestie: {QUIZ_URL}",
        "where do i take the quiz": f"Here’s your link: {QUIZ_URL}",
        "quiz link": f"Quiz link incoming: {QUIZ_URL}",
        "how much is bestie": "7-day free trial (card required), then $17/month. If you hit texting caps you’re auto-promoted to $27 then $37. Cancel anytime.",
        "price": "Starts at $17/month after your 7-day trial. Heavy texters auto-promoted to $27 and $37 tiers.",
        "how much does it cost": "7-day free trial, then $17/month. Promotions to $27 and $37 happen automatically when you hit caps.",
        "how much are prompt packs": "Prompt Packs are $7 each or 3 for $20.",
        "prompt pack price": "Each pack is $7 — or 3 for $20. Link: https://schizobestie.gumroad.com/",
        "prompt packs link": "Right this way: https://schizobestie.gumroad.com/",
    }

    for key, canned in faq_map.items():
        if key in normalized_text:
            logger.info("[Worker][FAQ] Intercepted '{}'", key)
            _store_and_send(user_id, convo_id, canned)
            return

    # 4) Rename flow
    rename_reply = try_handle_bestie_rename(user_id, convo_id, user_text)
    if rename_reply:
        _store_and_send(user_id, convo_id, rename_reply)
        return
    # Base reply context (used by both chat + product paths)
    with db.session() as s:
        profile = s.execute(
            sqltext("SELECT is_quiz_completed FROM user_profiles WHERE user_id = :uid"),
            {"uid": user_id}
        ).first()      
    context = {"has_completed_quiz": bool(profile and profile[0])}

    # 5) Chat-first (single GPT pass)
    try:
        system_prompt = (
            "You are Bestie — blunt, witty, stylish, emotionally fluent. Relationship & loyalty first. "
            "Answer like a ride-or-die best friend. Be punchy, specific, no therapy clichés. "
            "If you recommend products, include 1–3 picks as a numbered list with **bold names** + a direct URL "
            "(no search links). Prefer trusted retailers (Nordstrom, Free People, Sephora, Ulta, Amazon DP pages). "
            "Keep the whole reply under ~450 characters."
        )

        reply = ai.generate_reply(
            user_text=user_text,
            user_id=user_id,
            product_candidates=[],       # no product pipeline
            system_prompt=system_prompt,
            context=context,
        )

        # NEW GUARD
        if not reply or not str(reply).strip():
            reply = "Babe, I glitched. Say it again and I’ll do better. 💅"

        reply = _fix_cringe_opening(reply)
        _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
        return

    except Exception:
        logger.exception("[ChatOnly] GPT pass failed")
        _store_and_send(user_id, convo_id, "Babe, I glitched. Give me one sec to reboot my attitude. 💅", send_phone=user_phone)
        
        return


# ---------------------------------------------------------------------- #
# Debug and re-engagement jobs
# ---------------------------------------------------------------------- #
def debug_job(convo_id: int, user_id: int, text_val: str):
    logger.info("[Worker][Debug] Debug job: convo_id={} user_id={} text={}", convo_id, user_id, text_val)
    return f"Debug reply: got text='{text_val}'"

def send_reengagement_job():
    """
    Find users quiet for >48h and send a nudge.
    Respect 24h cooldown since last nudge.
    """
    try:
        logger.info("[Worker][Reengage] Running re-engagement job")
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=48)
        nudge_cooldown = now - timedelta(hours=24)

        with db.session() as s:
            rows = s.execute(sqltext("""
                SELECT c.id AS convo_id, u.id AS user_id, u.phone,
                       MAX(m.created_at) AS last_message_at
                FROM conversations c
                JOIN users u ON u.id = c.user_id
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id, u.id, u.phone
                HAVING MAX(m.created_at) < :cutoff
            """), {"cutoff": cutoff}).fetchall()

        nudges = [
            "I was scrolling my mental rolodex and realized you ghosted me. What’s up?",
            "Tell me one thing that lit you up this week. I don’t care how small.",
            "I miss our chaos dumps. What’s one thing that’s been driving you nuts?",
            "Flex time: share one win from this week.",
            "Spill one ridiculous detail from the last 48 hours.",
        ]

        for convo_id, user_id, phone, last_message_at in rows:
            if last_message_at and last_message_at > nudge_cooldown:
                logger.info("[Worker][Reengage] Skipping user_id={} (recent nudge)", user_id)
                continue
            message = random.choice(nudges)
            _store_and_send(user_id, convo_id, message)

        logger.info("[Worker][Reengage] Completed re-engagement run")

    except Exception as e:
        logger.exception("[Worker][Reengage] Exception: {}", e)

