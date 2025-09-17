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
SEND_FALLBACK_ON_ERROR = True  # keep it True so we still send if GPT path hiccups

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
_ALLOW_AMZ_SEARCH_TOKEN = "[[ALLOW_AMZ_SEARCH]]"
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

# --- SMS segmentation (emoji-safe) -------------------------------------------
def _segments_for_sms(body: str) -> List[str]:
    """
    Split body into GSM-7 (153 chars) or UCS-2 (67 chars) segments with room
    for a "[1/3] " prefix. Conservative word-boundary split.
    """
    text = (body or "").strip()
    if not text:
        return []

    # crude but reliable: emojis/non-ascii => UCS-2
    is_basic = all(ord(c) < 128 for c in text)
    per = 153 if is_basic else 67
    limit = max(10, per - 8)  # reserve ~8 chars for "[1/2] "

    parts: List[str] = []
    t = text
    while len(t) > limit:
        cut = t.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(t[:cut].strip())
        t = t[cut:].strip()
    if t:
        parts.append(t)
    return parts

from urllib.parse import quote_plus

def _amz_search_url(name: str) -> str:
    q = quote_plus((name or "").strip())
    return f"https://www.amazon.com/s?k={q}"

_BOLD_NAME = re.compile(r"\*\*(.+?)\*\*")
_NUM_NAME  = re.compile(r"^\s*\d+[\.\)]\s+([^\-–—:]+)", re.M)
_BUL_NAME  = re.compile(r"^\s*[-•]\s+([^\-–—:]+)", re.M)

def _extract_pick_names(text: str, maxn: int = 3) -> list[str]:
    names = []
    names += _BOLD_NAME.findall(text or "")
    names += _NUM_NAME.findall(text or "")
    names += _BUL_NAME.findall(text or "")
    seen, out = set(), []
    for n in [x.strip(" *•-") for x in names if x.strip()]:
        if n not in seen:
            seen.add(n); out.append(n)
        if len(out) >= maxn: break
    return out

def _append_links_for_picks(reply: str, convo_id: Optional[int] = None) -> str:
    """
    Append search links for 2–3 pick names. If none found in `reply`,
    look back at recent outbound messages and reuse the last picks.
    """
    names = _extract_pick_names(reply, maxn=3)

    if not names and convo_id:
        recent = _recent_outbound_texts(convo_id, limit=3)
        for text in recent:
            names = _extract_pick_names(text or "", maxn=3)
            if names:
                break

    if not names:
        return reply

    lines = [reply.rstrip(), ""]
    for n in names:
        lines.append(f"{n}: {_amz_search_url(n)}")
    return "\n".join(lines).strip()

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

def _mini_fallback_reply(user_text: str) -> str:
    return "Babe, I blanked like a bad date. Try me again — I swear I’m listening now. 💅"
                                     
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

    text_val = (text_val or "").strip()
    if not text_val:
        return
    
    _allow_amz = False
    if text_val.startswith(_ALLOW_AMZ_SEARCH_TOKEN):
        _allow_amz = True
        text_val = text_val.replace(_ALLOW_AMZ_SEARCH_TOKEN, "", 1).strip()

    # ==== Final shaping ====
    text_val = _add_personality_if_flat(text_val)
    text_val = _strip_link_placeholders(text_val)
    if not _allow_amz:
        text_val = _strip_amazon_search_links(text_val)
    text_val = wrap_all_affiliates(text_val)
    text_val = ensure_not_link_ending(text_val)
    # ==== One-time debug marker ====
    DEBUG_MARKER = os.getenv("DEBUG_MARKER", "")
    if DEBUG_MARKER:
        text_val = text_val.rstrip() + f"\n{DEBUG_MARKER}"
    parts = _segments_for_sms(text_val)   
    # ==== Break into SMS chunks ====
    GHL_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL")
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

        # Primary send to GHL per-part (outside the DB session)
        if GHL_WEBHOOK_URL:
            try:
                ghl_payload = {
                    "phone": send_phone,
                    "message": full_text,
                    "user_id": user_id,
                    "convo_id": convo_id
                }
                resp = requests.post(GHL_WEBHOOK_URL, json=ghl_payload, timeout=8)
                logger.info("[GHL_SEND] status={} body={}",
                            getattr(resp, "status_code", None),
                            (getattr(resp, "text", "") or "")[:200])
            except Exception as e:
                logger.warning("[GHL_SEND] Failed to POST to GHL: {}", e)

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
#---------------------------------------------------------------------- #
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
# --- reply cleaner -----------------------------------------------------------
from typing import Optional  # keep once near your other imports

def _clean_reply(text: Optional[str]) -> Optional[str]:
    """
    Light, non-destructive cleanup:
    - trim whitespace
    - collapse excessive spaces / newlines
    - remove simple wrapping quotes/backticks
    Returns the original text if cleaning would produce an empty string.
    """
    if text is None:
        return None

    t = str(text).strip()
    # (Optional) very light normalization without importing regex:
    # collapse multiple spaces
    while "  " in t:
        t = t.replace("  ", " ")
    # collapse 3+ newlines to a max of two
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")
    # strip simple wrappers
    t = t.strip('`"\' ')

    # Non-destructive guard: never turn real content into ""
    return t or text

# --- De-productize / no-briefing scrubs --------------------------------------
import re  # no-op if already imported at top

def _deproductize(text: Optional[str]) -> Optional[str]:
    """
    Remove briefing-y asks (options/budget/vibe/specifics) and normalize tone
    so replies feel like a friend, not a form. Non-destructive: never returns ""
    if the original had content.
    """
    if text is None:
        return None
    s = (text or "").strip()
    # hard deletes for stock lines
    s = re.sub(r"(?i)\b(give|share)\s+(me\s+)?(1\s*[-–]\s*2|one\s*[-–]\s*two|\d+)\s+specifics.*$", "", s).strip()
    s = re.sub(r"(?i)\btell me .*constraint.*$", "", s).strip()
    # soften leftover lexicon
    s = re.sub(r"(?i)\b(options?|picks)\b", "next step", s)
    s = re.sub(r"(?i)\b(budget|price|vibe)\b", "context", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or text
# --- Anti-form guard: rewrite survey-y replies into answer-first -------------
_ANTI_FORM_RE = re.compile(
    r"(?i)^(what'?s your budget|let'?s narrow (it|this) down|let us narrow|"
    r"what are your preferences|tell me your preferences|"
    r"what'?s (your )?price range|"
    r"share 1-2 specifics|provide options|set constraints)\b.*"
)

def _anti_form_guard(text: Optional[str], user_text: str) -> Optional[str]:
    if not text:
        return text
    t = text.strip()
    # If the whole first line is a survey prompt, replace it with an answer-first opener
    first, *rest = t.splitlines()
    if _ANTI_FORM_RE.match(first.strip()):
        # Minimal, on-brand opener with a single follow-up at the end
        # Note: short, decisive, no survey
        body = "Here’s what I’d do: focus on what actually moves the needle, then tweak if needed."
        follow = "Want me to tailor this tighter — or are you ready to try it?"
        return f"{body}\n{(' '.join(rest)).strip() or follow}"
    # Also strip any repeat survey prompts anywhere
    t = _ANTI_FORM_RE.sub("", t).strip()
    # Kill redundant “let’s narrow it down” lines mid-reply
    t = re.sub(r"(?im)^\s*let'?s narrow.*$", "", t).strip()
    return t or text

_GREETING_RE = re.compile(r"^\s*(hi|hey|hello|yo|hiya|sup|good (morning|afternoon|evening))\b", re.I)

def _is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text or ""))
# --- Product-intent detector ---------------------------------------------------
_PRODUCT_INTENT_RE = re.compile(
    r"(?i)\b(recommend|rec(s)?|suggest|best|top|what should i (get|use)|"
    r"buy|purchase|which (one|product)|options?|help me (regrow|grow|fix)|"
    r"hair (regrowth|loss|fall)|minoxidil|ketoconazole|peptide serum)\b"
)
def _wants_products(text: str) -> bool:
    return bool(_PRODUCT_INTENT_RE.search(text or ""))

_LISTY_RE = re.compile(r"(?i)\[(best|mid|budget)\]|http|•|- |1\)|2\)|3\)")
def _looks_like_picks(text: str) -> bool:
    t = text or ""
    # crude: looks like picks if it has labels/links/bullets or multiple short lines
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    return bool(_LISTY_RE.search(t) or len(lines) >= 3)

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
    Single-pass chat job:
      0) Plan gate (trial/active)
      1) Media routing (image/audio)
      2) Rename flow
      3) Chat-first GPT
      4) Affiliate/link hygiene + send
    """
    logger.info("[Worker][Start] Job: convo_id=%s user_id=%s text=%s", convo_id, user_id, text_val)
    reply: Optional[str] = None

    # Normalize phone for outbound
    try:
        user_phone = _norm_phone(user_phone) or user_phone
    except Exception:
        pass

    user_text = str(text_val or "")
    normalized_text = user_text.lower().strip()

    logger.info(
        "[Worker][Start] Job: convo_id=%s user_id=%s text_len=%d media_cnt=%d",
        convo_id, user_id, len(user_text), len(media_urls or [])
    )

    # 0) Plan gate ---------------------------------------------------------------
    try:
        gate_snapshot = _ensure_profile_defaults(user_id)
        logger.info("[Gate] user_id={} -> {}", user_id, gate_snapshot)

        # dev bypass (E.164 compare)
        np = _norm_phone(user_phone)
        nb = _norm_phone(DEV_BYPASS_PHONE)
        dev_bypass = bool(np and nb and np == nb)
        allowed = bool(gate_snapshot.get("allowed"))

        if not (dev_bypass or allowed):
            # Deduplicate paywall: if we just sent it, don’t spam
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
            return

        if dev_bypass:
            logger.info("[Gate][Bypass] forcing allow for DEV phone np=%s nb=%s", np, nb)

    except Exception as e:
        logger.exception("[Gate] snapshot/build error: {}", e)
        _store_and_send(
            user_id, convo_id,
            "Babe, I glitched. Give me one sec to reboot my attitude. 💅",
            send_phone=user_phone
        )
        return

    # 1) Media routing -----------------------------------------------------------
    if media_urls:
        first = (media_urls[0] or "").strip()
        lower = first.lower()
        try:
            if any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                logger.info("[Worker][Media] Attachment image detected: %s", first)
                reply = ai.describe_image(first)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return

            if any(lower.endswith(ext) for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                logger.info("[Worker][Media] Attachment audio detected: %s", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return

            # Extensionless: try image, then audio
            logger.info("[Worker][Media] Attachment extless; trying image describe: %s", first)
            try:
                reply = ai.describe_image(first)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
            except Exception:
                logger.info("[Worker][Media] describe_image failed; trying audio transcribe: %s", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
        except Exception as e:
            logger.warning("[Worker][Media] Attachment handling failed: %s", e)
         # ---- post-chat finishing + send ---------------------------------------------
        # If a naked URL is in the text, quick media sniff
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

    # 2) Rename flow -------------------------------------------------------------
    rename_reply = try_handle_bestie_rename(user_id, convo_id, user_text)
    if rename_reply:
        _store_and_send(user_id, convo_id, rename_reply, send_phone=user_phone)
        return

    # 3) Chat-first (single GPT pass) -------------------------------------------
    try:
        with db.session() as s:
            _row = s.execute(
                sqltext("SELECT is_quiz_completed FROM user_profiles WHERE user_id = :uid"),
                {"uid": user_id}
            ).first()
        has_quiz = bool(_row and _row[0])
    except Exception:
        # dev shouldn’t crash if the table/row isn’t present
        has_quiz = False

    # 5) Chat-first (single GPT pass)
    try:
        persona = (
            "You are Bestie — sharp, funny, emotionally fluent, and glamorously blunt. "
            "Answer now; don’t interview me. One playful follow-up at most. "
            "Do NOT ask for 'options', 'budget', 'goal/constraint'. "
            "If they greet you, greet them back playfully and ask one open-ended question. "
            "Only suggest products if they clearly ask for them, or if they paste a link you can critique/compare. "
            "Keep it to one SMS (<= 450 chars)."
        ).format(
            quiz=os.getenv("QUIZ_URL", "https://tally.so/r/YOUR_QUIZ_ID"),
            packs="https://schizobestie.gumroad.com/"
        )

        raw = ai.generate_reply(
            user_text=user_text,
            product_candidates=[],        # nothing scripted
            user_id=user_id,
            system_prompt=persona,
            context={"has_completed_quiz": has_quiz},
        )

        # keep it light — don't over-sanitize
        cleaned = _clean_reply(raw)
        reply = (cleaned.strip() if cleaned else (raw.strip() if raw else ""))

        # de-formalize if the model slipped into survey mode
        reply = _anti_form_guard(reply, user_text)

        # If user clearly asked for products and the reply is vague, do a tiny rescue pass
        if _wants_products(user_text) and not _looks_like_picks(reply):
            try:
                rescue = ai.rewrite_as_three_picks(
                    user_text=user_text,
                    base_reply=reply,
                    system_prompt=persona
                )
                if rescue and len(rescue.strip()) > len((reply or "").strip()):
                    reply = rescue.strip()
            except Exception:
                pass

    except Exception as e:
        logger.exception("[ChatOnly] GPT pass failed: {}", e)
        reply = ""
    # add closer if abrupt / ends on URL
    reply = _maybe_append_ai_closer(reply, user_text, category=None, convo_id=convo_id)

    # If they asked for links/buy, append search links and mark them to survive hygiene
    if re.search(r"(?i)\b(link|links|buy|purchase|where to buy|send.*link|shop)\b", (user_text or "")):
        reply = _append_links_for_picks(reply, convo_id=convo_id)
        reply = _ALLOW_AMZ_SEARCH_TOKEN + "\n" + reply

    # Greeting fallback (one friendly opener if reply is still blank)
    if not (reply or "").strip() and _is_greeting(user_text):
        reply = "Hey gorgeous — I’m here. What kind of trouble are we getting into today? Pick a lane or vent at me. 💅"

    # Safety net: guarantee exactly one message
    if not (reply or "").strip():
        reply = "Babe, I glitched. Say it again and I’ll do better. 💅"

    # Affiliate/link hygiene and send
    try:
        reply = linkwrap.make_sms_reply(reply)          # wraps Amazon/Geniuslink/SYL
        reply = ensure_not_link_ending(reply)
    except Exception:
        pass
    logger.info("[FINISH] sending reply len=%d", len(reply or ""))    
    _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
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

