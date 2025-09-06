# app/workers.py
"""
RQ worker job for SMS Bestie.
- Clean single send path
- Paywall + trial gates
- Media routing (image describe, audio transcribe)
- Product intent → candidates → Bestie tone reply
- Routine/overlap audit path
- General chat fallback with tone guards
- VIP soft pitch with cooldown + daily cap
- SMS link hygiene (Amazon search injection + Geniuslink + affiliate)
- Re-engagement job (48h quiet)
"""

from __future__ import annotations

# --------------------------- Standard imports --------------------------- #
import os
import re
import uuid
import hashlib
import random
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta

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

DEV_BYPASS_PHONE = os.getenv("DEV_BYPASS_PHONE", "").strip()

# Gumroad and trial settings
VIP_URL = os.getenv("VIP_URL", "https://schizobestie.gumroad.com/l/gexqp")
TRIAL_URL = os.getenv("TRIAL_URL", "https://schizobestie.gumroad.com/l/gexqp")
FULL_URL = os.getenv("FULL_URL", "https://schizobestie.gumroad.com/l/ibltj")
QUIZ_URL = os.getenv("QUIZ_URL", "https://tally.so/r/YOUR_QUIZ_ID")

ENFORCE_SIGNUP = os.getenv("ENFORCE_SIGNUP_BEFORE_CHAT", "0") == "1"
FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "7"))

# VIP soft-pitch throttles
VIP_COOLDOWN_MIN = int(os.getenv("VIP_COOLDOWN_MIN", "20"))
VIP_DAILY_MAX = int(os.getenv("VIP_DAILY_MAX", "2"))
_VIP_STOP = re.compile(r"(stop( trying)? to sell|don'?t sell|no vip|quit pitching|stop pitching)", re.I)

# Geniuslink / Amazon link hygiene
GENIUSLINK_DOMAIN = os.getenv("GENIUSLINK_DOMAIN", "").strip()
GENIUSLINK_WRAP = os.getenv("GENIUSLINK_WRAP", "").strip()  # eg: https://geni.us/redirect?url={url}
GL_REWRITE = os.getenv("GL_REWRITE", "1").lower() not in ("0", "false", "")
_AMZN_RE = re.compile(r"https?://(?:www\.)?amazon\.[^\s)\]]+", re.I)

# Opening/tone guards
OPENING_BANNED = [
    "it sounds like", "i understand that", "you're not alone",
    "i'm sorry you're", "technology can be", "i get that",
]
BANNED_STOCK_PHRASES = [
    "I’ll cry a little… then wait like a glam houseplant",
    "You're already on the VIP list, babe. That means I remember everything.",
    "P.S. Your Bestie VIP access is still active — I’ve got receipts, rituals, and rage texts saved.",
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
    """Ask user to start access and take the quiz. Chooses trial vs full link."""
    link = TRIAL_URL if not _has_ever_started_trial(user_id) else FULL_URL
    if link == TRIAL_URL:
        return (
            "Before we chat, start your access so I remember everything and tailor recs.\n"
            f"{link}\n"
            "1 week free, then $17/mo. Cancel anytime. No refunds."
        )
    return (
        "Your trial window ended. To keep going it’s $17/mo. Cancel anytime. No refunds.\n"
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
            UPDATE user_profiles
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

    gum_id, gum_email, plan_status, trial_start, renews_at, is_quiz, daily_used, daily_date = row

    # Reset daily counters if the date changed
    today = datetime.now(timezone.utc).date()
    if daily_date != today:
        with db.session() as s:
            s.execute(sqltext("""
                UPDATE user_profiles
                SET daily_counter_date = CURRENT_DATE,
                    daily_msgs_used    = 0
                WHERE user_id = :u
            """), {"u": user_id})
            s.commit()

    # Plan gate
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
# VIP soft pitch helpers
# ---------------------------------------------------------------------- #
def _recent_vip_stats_by_convo(convo_id: int, minutes: int = 1440) -> tuple[int, Optional[datetime]]:
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
    asked = any(w in txt for w in ["vip", "membership", "trial"])
    return asked or friction or momentum

def _vip_soft_line() -> str:
    return f"No pressure, but if you want me at full throttle, VIP will level you up. Free for 30 days, cancel anytime. Try it: {VIP_URL}"

def _maybe_inject_vip_by_convo(reply: str, convo_id: int, user_text: str) -> str:
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
    return f"https://www.amazon.com/s?k={re.sub(r'\\s+', '+', q.strip())}"

def _ensure_amazon_links(text: str) -> str:
    """
    If there are no links at all, add a safe Amazon search link under numbered bold product headers:
    "1. **Name**"
    """
    if not text:
        return text
    if "amazon.com/dp/" in text.lower():
        return text  # keep direct DP links untouched
    if re.search(r"https?://\\S+", text):
        return text  # some link already present

    def _inject(m):
        name = m.group(1).strip()
        return f"{m.group(0)}\n    [Amazon link]({_amazon_search_url(name)})"

    return re.sub(r"(?m)^\\s*\\d+\\.\\s+\\*\\*(.+?)\\*\\*.*$", _inject, text)

def _rewrite_links_to_genius(text: str) -> str:
    """
    Wrap Amazon URLs via Geniuslink:
      1) If GENIUSLINK_WRAP template is set, wrap any Amazon URL with it.
      2) Else if GL_REWRITE and GENIUSLINK_DOMAIN are set, rewrite /dp/ASIN to https://{domain}/{ASIN}.
    """
    if not text:
        return text

    if GENIUSLINK_WRAP:
        def _wrap(url: str) -> str:
            return GENIUSLINK_WRAP.format(url=re.sub(r'\\s', '%20', url))
        text = re.sub(_AMZN_RE, lambda m: _wrap(m.group(0)), text)

        def _md_repl(m):
            label, url = m.group(1), m.group(2)
            return f"[{label}]({_wrap(url)})" if _AMZN_RE.match(url) else m.group(0)
        return re.sub(r"\\[([^\\]]+)\\]\\((https?://[^\\)]+)\\)", _md_repl, text)

    if GL_REWRITE and GENIUSLINK_DOMAIN:
        host = GENIUSLINK_DOMAIN.rstrip("/")
        def repl(m):
            url = m.group(0)
            m_asin = re.search(r"/dp/([A-Z0-9]{10})", url, re.I)
            return f"https://{host}/{m_asin.group(1)}" if m_asin else url
        return re.sub(_AMZN_RE, repl, text)

    return text

def _fix_vip_links(text: str) -> str:
    """Ensure VIP mention is clickable."""
    if not text:
        return text
    text = re.sub(r"(?mi)^\\s*\\[?(vip|vip\\s*sign[- ]?up|vip\\s*signup)\\]?\\s*$", f"VIP Sign-Up: {VIP_URL}", text)
    if "vip" in text.lower() and "gumroad.com" not in text.lower():
        text = text.rstrip() + ("\n" if not text.endswith("\n") else "") + VIP_URL
    return text

def render_products_for_sms(products: List[Dict], limit: int = 3) -> str:
    """Compact SMS-friendly list renderer for quick lists (not used in GPT replies)."""
    lines: List[str] = []
    for idx, p in enumerate(products[:limit], start=1):
        name = (p.get("title") or "").strip() or (p.get("merchant") or "Product")
        url = p.get("url", "").strip()
        if url:
            lines.append(f"{idx}. **{name}**\n   {url}")
        else:
            lines.append(f"{idx}. **{name}**")
    return "\n\n".join(lines)

# ---------------------------------------------------------------------- #
# Final storage and SMS send
# ---------------------------------------------------------------------- #
def _store_and_send(user_id: int, convo_id: int, text_val: str) -> None:
    """
    Single place to store and send. Splits long messages to ~450 chars with [1/2] prefix.
    """
    max_len = 450
    parts: List[str] = []
    text_val = (text_val or "").strip()
    if not text_val:
        return

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

# ---------------------------------------------------------------------- #
# Finalize + send (formatting, links, optional VIP CTA)
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
    - Optional CTA based on env BESTIE_APPEND_CTA
    - Link hygiene (Amazon search injection only if no links, Geniuslink wrapping)
    - VIP links made clickable
    - Optional affiliate transforms via linkwrap
    - SMS formatting for links
    """
    reply = (reply or "").strip()
    if not reply:
        logger.warning("[Worker][Send] Empty reply. Skipping.")
        return

    try:
        if add_cta and os.getenv("BESTIE_APPEND_CTA") == "1":
            reply += (
                "\n\nBabe, your VIP is open: first week FREE, then $17/mo. "
                "Unlimited texts. https://schizobestie.gumroad.com/l/gexqp"
            )
    except Exception:
        pass

    # Product-friendly daily CTA tail
    try:
        if add_cta:
            reply += (
                "\n\nPS: New “Bestie Team Faves” drop daily—peek back tomorrow. "
                "Savings tip: try WELCOME10 or search brand + coupon."
            )
    except Exception:
        pass

    # Link hygiene order
    reply = _ensure_amazon_links(reply)           # add Amazon search link under numbered items only if no links exist
    reply = _rewrite_links_to_genius(reply)       # wrap Amazon links to Geniuslink
    reply = _fix_vip_links(reply)                 # ensure VIP mention is clickable

    # Optional affiliate rewrite + SMS-safe formatting
    try:
        aff = linkwrap.rewrite_affiliate_links_in_text(reply)
        if aff:
            reply = aff
    except Exception as e:
        logger.debug("[Affiliate] rewrite_affiliate_links_in_text skipped: {}", e)

    try:
        if hasattr(linkwrap, "make_sms_reply"):
            reply = linkwrap.make_sms_reply(reply, amazon_tag="schizobestie-20")
        else:
            if hasattr(linkwrap, "sms_ready_links"):
                reply = linkwrap.sms_ready_links(reply)
            if hasattr(linkwrap, "enforce_affiliate_tags"):
                reply = linkwrap.enforce_affiliate_tags(reply, "schizobestie-20")
    except Exception as e:
        logger.warning("[Linkwrap] SMS formatting fallback failed: {}", e)

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
def generate_reply_job(convo_id: int, user_id: int, text_val: str, user_phone: Optional[str] = None) -> None:
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

    try:
        # Gate defaults and plan status
        gate_snapshot = _ensure_profile_defaults(user_id)
        logger.info("[Gate] user_id={} -> {}", user_id, gate_snapshot)

        # Dev bypass by phone match
        np = _norm_phone(user_phone)
        nb = _norm_phone(DEV_BYPASS_PHONE)
        dev_bypass = bool(np and nb and np == nb)

        if not dev_bypass:
            if not gate_snapshot.get("allowed", False):
                reason = gate_snapshot.get("reason", "pending")
                if reason in ("pending", "canceled"):
                    _store_and_send(user_id, convo_id, _wall_start_message(user_id))
                    return
                if reason == "expired":
                    _store_and_send(user_id, convo_id, _wall_trial_expired_message())
                    return

        # Check if this is the very first message in the conversation
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

        # Media routing
        if "http" in user_text:
            if any(ext in normalized_text for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                logger.info("[Worker][Media] Image URL detected, describing.")
                reply = ai.describe_image(user_text.strip())
                reply = _maybe_inject_vip_by_convo(reply, convo_id, user_text)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return
            if any(ext in normalized_text for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                logger.info("[Worker][Media] Audio URL detected, transcribing.")
                reply = ai.transcribe_and_respond(user_text.strip(), user_id=user_id)
                reply = _maybe_inject_vip_by_convo(reply, convo_id, user_text)
                _finalize_and_send(user_id, convo_id, reply, add_cta=False)
                return

        # FAQs
        faq_map = {
            "how do i take the quiz": f"Take the quiz here, babe. It unlocks your personalized Bestie: {QUIZ_URL}",
            "where do i take the quiz": f"Here’s your link: {QUIZ_URL}",
            "quiz link": f"Quiz link incoming: {QUIZ_URL}",
            "how much is vip": "VIP is free the first month, $7 the second, then $17/month. Cancel anytime. Unlimited texts.",
            "vip cost": "First month free, then $7, then $17/month. Cancel anytime.",
            "price of vip": "VIP pricing: $0 → $7 → $17/month. Full access. Cancel anytime.",
            "how much are prompt packs": "Prompt Packs are $7 each or 3 for $20.",
            "prompt pack price": "Each pack is $7 — or 3 for $20. Link: https://schizobestie.gumroad.com/",
            "prompt packs link": "Right this way: https://schizobestie.gumroad.com/",
        }
        for key, canned in faq_map.items():
            if key in normalized_text:
                logger.info("[Worker][FAQ] Intercepted '{}'", key)
                _store_and_send(user_id, convo_id, canned)
                return

        # Rename flow
        rename_reply = try_handle_bestie_rename(user_id, convo_id, user_text)
        if rename_reply:
            _store_and_send(user_id, convo_id, rename_reply)
            return

        # ---------- Intent extraction ----------
        intent_data = None
        try:
            if hasattr(ai_intent, "extract_product_intent"):
                intent_data = ai_intent.extract_product_intent(user_text)
            else:
                logger.info("[Intent] No extractor defined.")
        except Exception as e:
            logger.warning("[Intent] Extractor failed: {}", e)
        logger.info("[Intent] intent_data: {}", intent_data)

        # Routine audit path
        if intent_data and intent_data.get("intent") == "routine_audit":
            reply = ai.audit_routine(user_text, constraints=intent_data.get("constraints") or {}, user_id=user_id)
            reply = _maybe_inject_vip_by_convo(reply, convo_id, user_text)
            _finalize_and_send(user_id, convo_id, reply, add_cta=True)
            return

        # ---------- Product candidates ----------
        product_candidates: List[Dict] = []
        try:
            product_candidates = prefer_amazon_first(build_product_candidates(intent_data))
        except Exception as e:
            logger.warning("[Products] Candidate build failed: {}", e)

        if product_candidates:
            # Normalize for GPT
            gpt_products: List[Dict] = []
            for c in product_candidates[:3]:
                gpt_products.append({
                    "name": c.get("title") or c.get("name") or "Product",
                    "category": (intent_data or {}).get("category", ""),
                    "url": c.get("url", ""),
                    "review": c.get("review", ""),
                })

            # VIP/quiz flags for tone shaping
            with db.session() as s:
                profile = s.execute(
                    sqltext("SELECT is_vip, has_completed_quiz FROM user_profiles WHERE user_id = :uid"),
                    {"uid": user_id}
                ).first()
            context = {
                "is_vip": bool(profile and profile[0]),
                "has_completed_quiz": bool(profile and profile[1]),
            }

            # Enumerated format ensures Amazon fallback injection works when URLs are missing
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
            reply = _maybe_inject_vip_by_convo(reply, convo_id, user_text)
            _finalize_and_send(user_id, convo_id, reply, add_cta=True)  # enable product CTA tail
            return

        # ---------- General chat fallback ----------
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

        reply = _maybe_inject_vip_by_convo(reply, convo_id, user_text)
        _finalize_and_send(user_id, convo_id, reply, add_cta=False)  # no product CTA for general replies
        return

    except Exception as e:
        logger.exception("Worker job failed: {}", e)
        _finalize_and_send(
            user_id,
            convo_id,
            "Babe, I glitched. Give me one sec to reboot my attitude. 💅",
            add_cta=False,
            force_send=True,
        )

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
