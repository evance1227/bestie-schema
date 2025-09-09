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
from typing import Optional, List
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
from app.linkwrap import make_sms_reply, ensure_not_link_ending
# ----------------------------- Third party ----------------------------- #
import redis
from loguru import logger
from sqlalchemy import text as sqltext

# ------------------------------ App deps ------------------------------- #
from app import db, models, ai, ai_intent, integrations, linkwrap
from app.product_search import build_product_candidates, prefer_amazon_first

# ---------------------------------------------------------------------- #
# Environment and globals
# ---------------------------------------------------------------------- #
REDIS_URL = os.getenv("REDIS_URL", "")
_rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None

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
# VIP soft pitch helpers (OFF by default)
# ---------------------------------------------------------------------- #
def _recent_vip_stats_by_convo(convo_id: int, minutes: int = 1440) -> Tuple[int, Optional[datetime]]:
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    count_24h = 0
    recent_ts = None
    try:
        with db.session() as s:
            rows = s.execute(sqltext("""
                SELECT content, created_at
                FROM messages
                WHERE conversation_id = :cid
                ORDER BY created_at DESC
                LIMIT 100
            """), {"cid": convo_id}).fetchall()
        for content, ts in rows:
            txt = (content or "").lower()
            if "gumroad.com" in txt or "vip" in txt:
                count_24h += 1
                if ts and ts > cutoff and (recent_ts is None or ts > recent_ts):
                    recent_ts = ts
    except Exception:
        pass
    return count_24h, recent_ts

def _should_soft_pitch_vip_convo(convo_id: int, user_text: str, reply_text: str) -> bool:
    if not VIP_SOFT_ENABLED:      # global kill switch
        return False
    if _VIP_STOP.search(user_text or ""):
        return False
    count_24h, recent_ts = _recent_vip_stats_by_convo(convo_id, minutes=1440)
    if count_24h >= VIP_DAILY_MAX:
        return False
    if recent_ts:
        mins_since = (datetime.utcnow() - recent_ts).total_seconds() / 60.0
        if mins_since < VIP_COOLDOWN_MIN:
            return False
    txt = f"{user_text} {reply_text}".lower()
    friction = any(w in txt for w in ["overwhelmed", "confused", "stuck", "frustrated", "help", "support", "struggling"])
    momentum = any(w in txt for w in ["recommend", "options", "compare", "ideas", "plan", "next step", "next level", "upgrade", "more"])
    asked    = any(w in txt for w in ["vip", "membership", "trial"])
    return asked or friction or momentum

def _vip_soft_line() -> str:
    # copy locked to current pricing/business plan
    return "First week free, then $17/month. Cancel anytime."

def _maybe_inject_vip_by_convo(reply: str, convo_id: int, user_text: str) -> str:
    if not VIP_SOFT_ENABLED:
        return reply
    try:
        if _should_soft_pitch_vip_convo(convo_id, user_text, reply):
            if "gumroad.com" not in (reply or "").lower():
                reply = (reply or "").rstrip() + "\n\n" + _vip_soft_line()
    except Exception:
        pass
    return reply

# ---------------------------------------------------------------------- #
# Link hygiene and affiliate helpers
# ---------------------------------------------------------------------- #
def _amazon_search_url(q: str) -> str:
    """Build a safe Amazon search URL without backslashes in f-string expressions."""
    try:
        term = quote_plus((q or "").strip())
    except Exception:
        term = (q or "").strip().replace(" ", "+")
    return "https://www.amazon.com/s?k=" + term

def _ensure_amazon_links(text: str) -> str:
    """
    If there are no links at all, add a safe Amazon search link under numbered bold product headers:
    "1. **Name**"
    """
    if not text:
        return text
    if "amazon.com/dp/" in text.lower():
        return text  # keep direct DP links untouched
    if re.search(r"https?://\S+", text):
        return text  # some link already present

    def _inject(m):
        name = m.group(1).strip()
        return f"{m.group(0)}\n    [Amazon link]({_amazon_search_url(name)})"

    return re.sub(r"(?m)^\s*\d+\.\s+\*\*(.+?)\*\*.*$", _inject, text)

def _rewrite_links_to_genius(text: str) -> str:
    """
    Wrap Amazon URLs via Geniuslink when explicitly configured ONLY.
      1) If GENIUSLINK_WRAP template is set, wrap any Amazon URL with it.
      2) Else if GL_REWRITE and GENIUSLINK_DOMAIN are set, rewrite /dp/ASIN to https://{domain}/{ASIN}.
      3) Else return text unmodified.
    """
    if not text:
        return text

    if GENIUSLINK_WRAP:
        def _wrap(url: str) -> str:
            return GENIUSLINK_WRAP.format(url=re.sub(r'\s', '%20', url))
        text = re.sub(_AMZN_RE, lambda m: _wrap(m.group(0)), text)

        def _md_repl(m):
            label, url = m.group(1), m.group(2)
            return f"[{label}]({_wrap(url)})" if _AMZN_RE.match(url) else m.group(0)
        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", _md_repl, text)

    if GL_REWRITE and GENIUSLINK_DOMAIN:
        host = GENIUSLINK_DOMAIN.rstrip("/")
        def repl(m):
            url = m.group(0)
            m_asin = re.search(r"/dp/([A-Z0-9]{10})", url, re.I)
            return f"https://{host}/{m_asin.group(1)}" if m_asin else url
        return re.sub(_AMZN_RE, repl, text)

    return text

def _fix_vip_links(text: str) -> str:
    """Ensure VIP mention is clickable (used only if VIP soft-line appears)."""
    if not text:
        return text
    text = re.sub(r"(?mi)^\s*\[?(vip|vip\s*sign[- ]?up|vip\s*signup)\]?\s*$", f"VIP Sign-Up: {FULL_URL or VIP_URL}", text)
    if "vip" in text.lower() and "gumroad.com" not in text.lower():
        text = text.rstrip() + ("\n" if not text else "")
                                
# ---------------------------------------------------------------------- #
# Final storage and SMS send
# ---------------------------------------------------------------------- #
def _add_personality_if_flat(text: str) -> str:
    if not text:
        return text
    if text.count("http") >= 2 and len(text) < 480:
        opener = "Got you, babe. Here are a couple that actually work:"
        text = opener + "\n" + text
    return text

def _store_and_send(user_id: int, convo_id: int, text_val: str) -> None:
    """
    Single place to store and send. Splits long messages to ~450 chars with [1/2] prefix.
    Adds a tiny headway between multipart sends so carriers keep order.
    Applies final link and tone cleanup before sending and storage.
    """    
    max_len = 450
    parts: List[str] = []

    text_val = (text_val or "").strip()
    if not text_val:
        return

    # ==== Final shaping ====
    text_val = _add_personality_if_flat(text_val)
    text_val = make_sms_reply(text_val)
    text_val = ensure_not_link_ending(text_val)

    # ==== One-time debug marker ====
    DEBUG_MARKER = os.getenv("DEBUG_MARKER", "")
    if DEBUG_MARKER:
        text_val = text_val.rstrip() + f"\n{DEBUG_MARKER}"

    # ==== POST to GHL ====
    user_phone = os.getenv("TEST_PHONE") or "+15555555555"
    GHL_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL")
    if GHL_WEBHOOK_URL:
        ghl_payload = {
            "phone": user_phone,
            "message": text_val,
            "user_id": user_id,
            "convo_id": convo_id
        }
        try:
            requests.post(GHL_WEBHOOK_URL, json=ghl_payload, timeout=6)
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
# Finalize + send (formatting, links, optional CTA - OFF by default)
# ---------------------------------------------------------------------- #
def _finalize_and_send(
    user_id: int,
    convo_id: int,
    reply: str,
    *,
    add_cta: bool = False,
    force_send: bool = True,
) -> None:
    """
    Final formatting before a single send.
    - Link hygiene (Amazon + optional Geniuslink rewrite)
    - Personality injection if reply is flat
    - Optional CTA line (off by default)
    - Final cleanup for SMS delivery
    """
    from app.linkwrap import make_sms_reply, ensure_not_link_ending

    def _add_personality_if_flat(t: str) -> str:
        if not t:
            return t
        if t.count("http") >= 2 and len(t) < 480:
            opener = "Got you, babe. Here are a couple that actually work:"
            t = opener + "\n" + t
        return t

    reply = (reply or "").strip()
    if not reply:
        logger.warning("[Worker][Send] Empty reply. Skipping.")
        return

    try:
        # Optional CTA tail (globally disabled unless env enabled)
        if add_cta and BESTIE_PRODUCT_CTA_ENABLED:
            reply += "\n\nPS: Savings tip: try WELCOME10 or search brand + coupon."
    except Exception:
        pass

    # === Link and tone hygiene ===
    try:
        reply = _strip_link_placeholders(reply)
        reply = _strip_amazon_search_links(reply)
        reply = _add_personality_if_flat(reply)
        reply = make_sms_reply(reply)          # canonical Amazon links + tag
        reply = ensure_not_link_ending(reply)  # avoid ending on a naked URL
    except Exception as e:
        logger.warning("[Worker][Linkwrap/Tone] Error in reply cleanup: {}", e)

    _store_and_send(user_id, convo_id, reply)

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
      3) FAQs
      4) Rename
      5) Routine audit (AM/PM map)
      6) Product intent -> candidates -> GPT reply
      7) General chat GPT reply
    """
    logger.info("[Worker][Start] Job: convo_id={} user_id={} text={}", convo_id, user_id, text_val)

    user_text = str(text_val or "")
    normalized_text = user_text.lower().strip()
    logger.info("[Worker][Start] Job: convo_id=%s user_id=%s text_len=%d media_cnt=%d",

        # 0) Gate
    try:
        gate_snapshot = _ensure_profile_defaults(user_id)
        logger.info("[Gate] user_id={} -> {}", user_id, gate_snapshot)

        np = _norm_phone(user_phone)
        nb = _norm_phone(DEV_BYPASS_PHONE)
        dev_bypass = bool(np and nb and np == nb)

        allowed = gate_snapshot.get("allowed", False)

        if not (dev_bypass or allowed):
            _store_and_send(
                user_id,
                convo_id,
                "Before we chat, start your access so I can remember everything and tailor recs to you. Tap here and you’ll go straight to your quiz after signup:\nhttps://schizobestie.gumroad.com/l/gexqp\nNo refunds. Cancel anytime. 💅"
            )
            return

    except Exception as e:
        logger.exception("[Gate] snapshot/build error: {}", e)
        _store_and_send(user_id, convo_id, "Babe, I glitched. Give me one sec to reboot my attitude. 💅")
        return
   
    # 1) First message onboarding
    with db.session() as s:
        first_msg_count = s.execute(
            sqltext("SELECT COUNT(*) FROM messages WHERE conversation_id = :cid"),
            {"cid": convo_id},
        ).scalar() or 0

    if first_msg_count == 0:
        onboarding_reply = random.choice([
            "OMG, you made it. Welcome to chaos, clarity, and couture-level glow ups. Text me anything. 💅",
            "Hi. I’m Bestie. I don’t do small talk. I do savage insight and glow ups. Ask me something.",
            "You’re in. I’m your emotionally fluent digital best friend. Vent or ask. I’m unshockable.",
            "Welcome to your new favorite addiction. I talk back like a glam oracle with receipts. Let’s go.",
        ])
        _store_and_send(user_id, convo_id, onboarding_reply)
        return
        # 2) Media routing

    # --- attachments passed from webhook (preferred) ---
    if media_urls:
        first = (media_urls[0] or "").strip()
        lower = first.lower()
        try:
            if any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                logger.info("[Worker][Media] Attachment image detected: {}", first)
                reply = ai.describe_image(first)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return
            if any(lower.endswith(ext) for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                logger.info("[Worker][Media] Attachment audio detected: {}", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return
            # Extensionless: try image, then audio
            logger.info("[Worker][Media] Attachment extless; trying image describe: {}", first)
            try:
                reply = ai.describe_image(first)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return
            except Exception:
                logger.info("[Worker][Media] describe_image failed; trying audio transcribe: {}", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return
        except Exception as e:
            logger.warning("[Worker][Media] Attachment handling failed: {}", e)
            # fall through to your existing text-based routing below

    # 2) Media routing
    if "http" in user_text:
        if any(ext in normalized_text for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            logger.info("[Worker][Media] Image URL detected, describing.")
            reply = ai.describe_image(user_text.strip())
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)
            return
        if any(ext in normalized_text for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Audio URL detected, transcribing.")
            reply = ai.transcribe_and_respond(user_text.strip(), user_id=user_id)
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)
            return

    # 3) FAQs (pricing corrected)
    faq_map = {
        "how do i take the quiz": f"Take the quiz here, babe. It unlocks your personalized Bestie: {QUIZ_URL}",
        "where do i take the quiz": f"Here’s your link: {QUIZ_URL}",
        "quiz link": f"Quiz link incoming: {QUIZ_URL}",
        "how much is vip": "1 week free, then $17/month. Upgrades unlock by invitation (Plus $27, Elite $37). Cancel anytime.",
        "vip cost": "1 week free, then $17/month. Upgrades are invite-only when you hit caps.",
        "price of vip": "Start at $17/month after a 1-week free trial. Upgrades unlock by invitation.",
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

    # 5) Intent extraction (products/routine)
    intent_data = {}
    try:
        if hasattr(ai_intent, "extract_product_intent"):
            intent_data = ai_intent.extract_product_intent(user_text) or {}
        else:
            logger.info("[Intent] No extractor defined.")
    except Exception as e:
        logger.warning("[Intent] Extractor failed: {}", e)
        intent_data = {}
    logger.info("[Intent] intent_data: {}", intent_data)

    # 5a) Routine audit path (map first, optional product follow-up)
    if intent_data.get("intent") == "routine_audit":
        reply = ai.audit_routine(user_text, constraints=intent_data.get("constraints") or {}, user_id=user_id)
        _finalize_and_send(user_id, convo_id, reply, add_cta=False)

        # If the message implies shopping, send 1–3 picks (form inferred when possible)
        try:
            want_products = any(w in normalized_text for w in [
                "recommend", "recommendation", "suggest", "what should", "which", "looking for", "buy", "get"
            ]) or "peptide" in normalized_text

            if want_products:
                form = None
                if hasattr(ai_intent, "infer_form_factor"):
                    form = ai_intent.infer_form_factor(user_text)  # "topical" | "ingestible" | None

                base_query = "peptide face serum" if form == "topical" else (
                             "collagen peptides" if form == "ingestible" else "peptide")

                secondary_intent = {
                    "intent": "find_products",
                    "query": base_query,
                    "category": "skincare" if form == "topical" else "",
                    "constraints": {"form": form, "count": 3}
                }
                picks = prefer_amazon_first(build_product_candidates(secondary_intent))
                if picks:
                    gpt_products = [{
                        "name": p.get("title") or p.get("name") or "Product",
                        "category": "skincare" if form == "topical" else "supplements",
                        "url": p.get("url",""),
                        "review": p.get("review",""),
                    } for p in picks[:3]]

                    rec_text = ai.generate_reply(
                        user_text=user_text,
                        product_candidates=gpt_products,
                        user_id=user_id,
                        system_prompt=(
                            "You are Bestie. Use the provided product candidates (already monetized DP URLs).\n"
                            "FORMAT AS A NUMBERED LIST with bold names so link hygiene can attach if needed:\n"
                            "1. **Name**: one-liner benefit. URL\n"
                            "Keep the whole reply ~450 chars. 1–3 options max. No disclaimers. Do not alter or replace URLs."
                        ),
                        context={"session_goal": "offer quick product picks"},
                    )
                    rec_text = _fix_cringe_opening(rec_text)
                    _finalize_and_send(user_id, convo_id, rec_text, add_cta=False)  # no CTA tail
        except Exception as e:
            logger.warning("[Routine+Products] Secondary picks failed: {}", e)

        return

    # 6) Product candidates path
    product_candidates: List[Dict] = []
    try:
        product_candidates = prefer_amazon_first(build_product_candidates(intent_data))
    except Exception as e:
        logger.warning("[Products] Candidate build failed: {}", e)

    if product_candidates:
        gpt_products: List[Dict] = []
        for c in product_candidates[:3]:
            gpt_products.append({
                "name": c.get("title") or c.get("name") or "Product",
                "category": (intent_data or {}).get("category", ""),
                "url": c.get("url", ""),
                "review": c.get("review", ""),
            })

        with db.session() as s:
            profile = s.execute(
                sqltext("SELECT is_vip, is_quiz_completed FROM user_profiles WHERE user_id = :uid"),
                {"uid": user_id}
            ).first()
        context = {"is_vip": bool(profile and profile[0]), "has_completed_quiz": bool(profile and profile[1])}


        reply = ai.generate_reply(
            user_text=user_text,
            product_candidates=gpt_products,
            user_id=user_id,
            system_prompt=(
                "You are Bestie. Use the provided product candidates (already monetized DP URLs).\n"
                "FORMAT AS A NUMBERED LIST with bold names so link hygiene can attach if needed:\n"
                "1. **Name**: one-liner benefit. URL\n"
                "Keep the whole reply ~450 chars. 1–3 options max. No disclaimers. Do not alter or replace URLs."
            ),
            context=context,
        )
        reply = _fix_cringe_opening(reply)
        # do NOT inject VIP or CTA tail for product replies unless explicitly allowed
        _finalize_and_send(user_id, convo_id, reply, add_cta=False)
        return

    # 7) General chat fallback
    with db.session() as s:
        profile = s.execute(
            sqltext("SELECT is_vip, has_completed_quiz FROM user_profiles WHERE user_id = :uid"),
            {"uid": user_id}
        ).first()
    context = {"is_vip": bool(profile and profile[0]), "has_completed_quiz": bool(profile and profile[1])}

    reply = ai.generate_reply(
        user_text=user_text,
        product_candidates=[],
        user_id=user_id,
        system_prompt="You are Bestie. Be brief, helpful, stylish, emotionally fluent. No therapy cliches.",
        context=context,
    )

    try:
        if hasattr(ai, "rewrite_if_cringe"):
            rewritten = ai.rewrite_if_cringe(reply)
            if rewritten and rewritten != reply:
                logger.info("[Worker][AI] Reply rewritten to improve tone.")
                reply = rewritten
    except Exception:
        logger.warning("[Worker][AI] rewrite_if_cringe failed. Using original.")

    _finalize_and_send(user_id, convo_id, reply, add_cta=False)
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

