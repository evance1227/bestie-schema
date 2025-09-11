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
from typing import Optional, List
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
from app.linkwrap import make_sms_reply, ensure_not_link_ending
from app.bestie_oneliners import render_oneliner_with_link

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
VIP_SOFT_ENABLED = (os.getenv("VIP_SOFT_ENABLED") or "0").strip() in ("1", "true", "yes")

_LAST_CANDS_KEY = "last_cands:{cid}"

def _maybe_inject_vip_by_convo(reply: str, convo_id: int, user_text: str) -> str:
    # VIP not used in this product anymore; keep as hard no-op
    return reply

def _fix_vip_links(text: str) -> str:
    # VIP not used; hard no-op
    return text
       
def _get_last_candidates(convo_id: int, limit: int = 6) -> List[Dict]:
    if not _rds: return []
    raw = _rds.get(_LAST_CANDS_KEY.format(cid=convo_id)) or ""
    try:
        data = json.loads(raw) if raw else []
        return data[:limit]
    except Exception:
        return []

def _set_last_candidates(convo_id: int, cands: List[Dict]) -> None:
    if not _rds: return
    try:
        _rds.set(_LAST_CANDS_KEY.format(cid=convo_id), json.dumps(cands or [])[:60000])  # cap
    except Exception:
        pass

# ---------- Retailer/brand hints (module-level) ----------
BRAND_TO_DOMAIN = {
    "free people": "freepeople.com",
    "fp": "freepeople.com",
    "spanx": "spanx.com",
    "everlane": "everlane.com",
    "madewell": "madewell.com",
    "skims": "skims.com",
    "ganni": "ganni.com",
    "samsung": "samsung.com",
    "dyson": "dyson.com",
}

RETAILER_DEFAULTS = {
    "boots": "nordstrom.com",
    "dress": "nordstrom.com",
    "cowgirl boots": "nordstrom.com",
    "western boots": "nordstrom.com",
    "mascara": "sephora.com",
    "sunscreen": "sephora.com",
    "serum": "sephora.com",
    "foundation": "sephora.com",
}

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
# Link hygiene and affiliate helpers
# ---------------------------------------------------------------------- #
# deterministic product formatter (guarantees URLs; skips blanks)
def _format_numbered_with_urls(rows: List[Dict], limit: int = 3) -> str:
    out = []
    count = 0
    for p in rows:
        url = (p.get("url") or "").strip()
        if not url:
            continue  # skip blank URL entries
        url = linkwrap.convert_to_geniuslink(url)  # wrap SYL/Amazon if needed
        name = (p.get("title") or p.get("name") or "Product").strip()
        blurb = (p.get("review") or "").strip()
        count += 1
        if blurb:
            out.append(f"{count}. **{name}**: {blurb}. {url}")
        else:
            out.append(f"{count}. **{name}**: {url}")
        if count >= limit:
            break
    return "\n".join(out)

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
            resp = requests.post(GHL_WEBHOOK_URL, json=ghl_payload, timeout=8)
            logger.info("[GHL_SEND] status={} body={}",
                        getattr(resp, "status_code", None),
                        (getattr(resp, "text", "") or "")[:200])
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
        # monetize/normalize all links GPT produced
        from app import linkwrap as _lw
        reply = _lw.wrap_all_affiliates(reply)
        reply = ensure_not_link_ending(reply)

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

       # --- Catch-all: fire product mode for suggestions/ideas OR clear “I need/looking for + noun” ---
    if not intent_data:
        low = normalized_text

        # Signals that clearly mean “give me product recs”
        SUGGEST_SIGNALS = re.compile(
            r"\b(recommend|recommendation|suggest|suggestions?|ideas?|picks?|starter\s*kit|essentials?|must[-\s]?haves?)\b",
            re.I
        )

        # Phrases that usually precede a shopping ask (when paired with a category noun)
        SEEK_SIGNALS = re.compile(r"\b(i\s+need|i\s+want|looking\s+for|shopping\s+for|find\s+me|help\s+me\s+pick)\b", re.I)

        # Category nouns we support (expand as needed)
        CATEGORY_NOUNS = (
            "wardrobe","outfit","closet","boots","dress","jeans","sneakers","heels","coat","jacket","sweater",
            "makeup","starter kit","routine","spf","sunscreen","serum","moisturizer","retinol","lipstick","mascara",
            "body wash","self tanner","peptides","hair mask","shampoo","conditioner","curl cream"
        )

        should_shop = False

        # Case 1: direct “give me suggestions/ideas/picks”
        if SUGGEST_SIGNALS.search(low):
            should_shop = True
        # Case 2: “I need/looking for …” + category noun
        elif SEEK_SIGNALS.search(low) and any(noun in low for noun in CATEGORY_NOUNS):
            should_shop = True

        if should_shop:
            constraints = {}
            m = re.search(r"\$?\s*(\d{2,4})\s*[-–]\s*\$?\s*(\d{2,4})", low)
            if m:
                lo, hi = sorted(map(int, [m.group(1), m.group(2)]))
                constraints["price_range"] = [lo, hi]
            m2 = re.search(r"\bunder\s*\$?\s*(\d{2,4})\b", low)
            if m2 and "price_range" not in constraints:
                constraints["max_price"] = int(m2.group(1))

            intent_data = {"intent": "find_products", "query": user_text.strip(), "constraints": constraints}
            logger.info("[Intent] Fallback product intent (suggestions/ideas or seek+category): {}", intent_data)

    # --- Retailer hint shim (no \"not amazon\" needed) ------------------------
    _SUPPORTED_RETAILERS = ("free people", "sephora", "ulta", "nordstrom", "madewell", "everlane", "spanx")

    def _detect_retailer_from_text(txt: str) -> Optional[str]:
        low = (txt or "").lower()
        # 1) direct retailer mention
        for key in _SUPPORTED_RETAILERS:
            if key in low:
                return BRAND_TO_DOMAIN.get(key, f"{key.replace(' ', '')}.com")
        # 2) brand mentions
        for brand, dom in BRAND_TO_DOMAIN.items():
            if brand in low:
                return dom
        # 3) category + quality/price heuristics
        price = None
        m = re.search(r"\$?\s*(\d{2,4})\s*[-–]\s*\$?\s*(\d{2,4})", low)
        if m:
            lo, hi = sorted(map(int, [m.group(1), m.group(2)]))
            price = (lo + hi) / 2
        m2 = re.search(r"\bunder\s*\$?\s*(\d{2,4})\b", low)
        if price is None and m2:
            price = int(m2.group(1)) * 0.8  # rough target
        quality_flag = any(w in low for w in ("quality", "high-quality", "nice", "premium", "designer"))
        for key, dom in RETAILER_DEFAULTS.items():
            if key in low and (quality_flag or (price and price >= 150)):
                return dom
        return None
    retailer_domain = _detect_retailer_from_text(user_text)

    # Build the intent we’ll actually search with (keep constraints from extractor/fallback)
    intent_for_search = dict(intent_data or {})
    if retailer_domain:
        q0 = (intent_for_search.get("query") or "").strip()
        retailer_words = retailer_domain.split(".")[0]  # e.g. "nordstrom"
        intent_for_search["query"] = f"{retailer_words} {q0}".strip()
    else:
        intent_for_search = intent_data

    # If no product intent → general conversation branch FIRST
    if not intent_data or intent_data.get("intent") not in ("find_products", "shopping"):
        reply = ai.generate_reply(
            user_text=user_text,
            user_id=user_id,
            product_candidates=[],  # pure chat
            system_prompt=(
                "You are Bestie: blunt, witty, stylish, emotionally fluent. "
                "Relationship & loyalty first. Only weave products in when the user clearly asks."
            ),
            context=context,
        )
        reply = _fix_cringe_opening(reply)
        _finalize_and_send(user_id, convo_id, reply, add_cta=False)
        return

    # 6) Product candidates path (hybrid: never block reply)
    product_candidates: List[Dict] = []
    try:
        product_candidates = prefer_amazon_first(build_product_candidates(intent_for_search))
    except Exception as e:
        logger.warning("[Products] Candidate build failed: {}", e)

    # Detect “more options” follow-ups and try to expand to ~6
    want_more = any(
        kw in normalized_text
        for kw in ("more options", "more picks", "another option", "other options", "show me more", "more?", "more", "another?")
    )
    if want_more:
        try:
            extra_intent = dict(intent_for_search or intent_data or {})
            cons = extra_intent.get("constraints") or {}
            cons["count"] = 6
            extra_intent["constraints"] = cons
            extra = prefer_amazon_first(build_product_candidates(extra_intent))
            # Merge + dedupe by URL/title
            seen, merged = set(), []
            for row in (product_candidates or []) + (extra or []):
                k = (row.get("url") or "")[:120] or (row.get("title") or row.get("name") or "")
                if k and k not in seen:
                    seen.add(k); merged.append(row)
            product_candidates = merged
        except Exception as e:
            logger.warning("[Products] more-options expansion failed: {}", e)

    # --- DEBUG: show first candidate URLs ---
    try:
        for i, p in enumerate(product_candidates[:3], 1):
            logger.info("[Products][DBG] %d) %s | url=%s",
                        i, (p.get('title') or p.get('name')), (p.get('url') or ''))
    except Exception:
        pass

    # Cache whatever we have (even empty) for next turns
    try:
        if product_candidates:
            _set_last_candidates(convo_id, product_candidates[:6])           
    except Exception:
        pass

    # Holy grail only if it’s a real product (not a retailer placeholder)
    if product_candidates and len(product_candidates) == 1:
        only = product_candidates[0]
        is_placeholder = bool((only.get("meta") or {}).get("retailer"))
        if not is_placeholder:
            reply = f"This is THE one: **{only.get('title') or only.get('name')}** {only.get('url')}"
            _finalize_and_send(user_id, convo_id, reply, add_cta=False)
            return

    # Prepare candidates for formatting (fallback to cached if this turn is empty)
    cands_for_gpt = product_candidates or _get_last_candidates(convo_id, limit=3)

    # Build gpt_products only for opener quip
    gpt_products: List[Dict] = [
        {"name": (c.get("title") or c.get("name") or "Product"),
         "category": (intent_data or {}).get("category",""),
         "url": (c.get("url") or ""),
         "review": (c.get("review") or "")}
        for c in cands_for_gpt[:3]
    ]

    # Optional opener
    opener = ""
    try:
        if gpt_products:
            t = " ".join([p["name"] for p in gpt_products]).lower()
            cat = (intent_data or {}).get("category") or (
                "fashion" if ("boot" in t or "dress" in t) else ("skincare" if ("spf" in t or "serum" in t) else "home")
            )
            opener = render_oneliner_with_link(
                category=cat, prefer_tag="rec",
                rec_product_name=gpt_products[0]["name"],
                rec_affiliate_url=linkwrap.convert_to_geniuslink(gpt_products[0]["url"]) if gpt_products[0]["url"] else "",
            ) or ""
    except Exception:
        logger.info("[Oneliner] skipped")

    body = _format_numbered_with_urls(product_candidates, limit=3)
    if not body:
        body = "I can’t pull the product URLs right now. Want me to try a different retailer or budget?"

    reply = (opener + "\n" + body).strip() if opener and len(opener) < 120 else body
    reply = _fix_cringe_opening(reply)
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

