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
SMS_PART_DELAY_MS = int(os.getenv("SMS_PART_DELAY_MS", "1600"))
REDIS_URL = os.getenv("REDIS_URL", "")
_rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
USE_GHL_ONLY = (os.getenv("USE_GHL_ONLY", "1").lower() not in ("0","false","no"))
SEND_FALLBACK_ON_ERROR = True  # keep it True so we still send if GPT path hiccups
SYL_ENABLED = (os.getenv("SYL_ENABLED") or "0").lower() in ("1","true","yes")
SYL_PUBLISHER_ID = (os.getenv("SYL_PUBLISHER_ID") or "").strip()
AMAZON_ASSOCIATE_TAG = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()

logger.info("[Boot] USE_GHL_ONLY=%s  SEND_FALLBACK_ON_ERROR=%s", USE_GHL_ONLY, SEND_FALLBACK_ON_ERROR)

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
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)

# --- SMS segmentation (URL-safe) ---------------------------------------------
def _segments_for_sms(body: str,
                      per: int = int(os.getenv("SMS_PER_PART", "320")),
                      max_parts: int = int(os.getenv("SMS_MAX_PARTS", "3")),
                      prefix_reserve: int = 8) -> list[str]:
    """
    Split `body` into <= per-char chunks, up to `max_parts`.
    - avoids splitting inside URLs
    - prefers whitespace boundaries
    - prefixes like "[1/3] " need space; we reserve `prefix_reserve` chars per part
    - last part may be longer if we must avoid breaking a URL; never exceeds per
    """
    text = (body or "").strip()
    if not text:
        return []
    parts: list[str] = []

    # precompute URL spans so we don't cut in the middle
    url_spans = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]

    def _in_url(ix: int) -> tuple[int, int] | None:
        for a, b in url_spans:
            if a <= ix < b:
                return (a, b)
        return None

    i, n = 0, len(text)
    # leave room for "[i/n] "
    limit_per = max(10, per - prefix_reserve)

    while i < n and len(parts) < max_parts - 1:
        remaining = n - i
        if remaining <= limit_per:
            break

        cut = i + limit_per
        # back up to last whitespace
        ws = text.rfind(" ", i, cut)
        if ws != -1 and ws > i:
            cut = ws

        # if cut is inside a URL, snap to URL start
        span = _in_url(cut)
        if span:
            a, b = span
            # if the url started after the segment start, cut before the URL
            if a > i:
                cut = a - 1
            else:
                # URL started at the beginning of the segment; try after the URL if it fits
                if b - i <= limit_per:
                    cut = b
                else:
                    # fallback hard split
                    cut = i + limit_per

        chunk = text[i:cut].strip()
        if chunk:
            parts.append(chunk)
        i = cut
        # skip a single space if present
        if i < n and text[i].isspace():
            i += 1

    # tail
    tail = text[i:].strip()
    if tail:
        parts.append(tail)

    # clamp to max_parts (merge overflow into last)
    if len(parts) > max_parts:
        head = parts[:max_parts - 1]
        tail = " ".join(parts[max_parts - 1:]).strip()
        parts = head + [tail]

    return parts

from urllib.parse import quote_plus

def _amz_search_url(name: str) -> str:
    q = quote_plus((name or "").strip())
    return f"https://www.amazon.com/s?k={q}"

# --- Retailer search URLs for SYL wrapping -----------------------------------
_RETAILER_SEARCH = {
    "sephora":  "https://www.sephora.com/search?keyword={q}",
    "ulta":     "https://www.ulta.com/search?searchText={q}",
    "nordstrom":"https://www.nordstrom.com/sr?keyword={q}",
    "revolve":  "https://www.revolve.com/r/search?search={q}",
    "shopbop":  "https://www.shopbop.com/actions/searchResultsAction.action?query={q}",
    "target":   "https://www.target.com/s?searchTerm={q}",
    "walmart":  "https://www.walmart.com/search?q={q}",
}

# simple brand→retailer hints (extend anytime)
_BRAND_HINTS = {
    # beauty (prestige)
    r"charlotte\s*tilbury|drunk\s*elephant|tatcha|rare\s*beauty|kosas|saie|milk\s*makeup|sunday\s*riley": "sephora",
    # beauty (masstige/drugstore)
    r"cerave|cera\s*ve|la\s*roche|the\s*ordinary|elf|e\.l\.f|neutrogena|loreal|l'or[eé]al|olay|aveeno": "ulta",
    # hair
    r"olaplex|k[ée]rastase|moroccanoil|living\s*proof|amika": "sephora",
    # fashion
    r"reformation|agolde|vince|sam\s*edelman|madewell|rag\s*&\s*bone|stuart\s*weitzman": "nordstrom",
}

_BEAUTY_WORDS  = re.compile(r"(?i)\b(serum|moisturizer|cleanser|toner|retinol|vitamin\s*c|spf|sunscreen|hyaluronic|glycolic|niacinamide|collagen|peptide)\b")
_FASHION_WORDS = re.compile(r"(?i)\b(dress|jeans|denim|sweater|coat|boots?|heels?|sneakers?|top|skirt|bag|handbag|purse)\b")

def _syl_search_url(name: str, user_text: str) -> str:
    """Pick a SYL-friendly retailer by brand + context; no hard-wiring."""
    q = quote_plus((name or "").strip())

    # 0) optional nudge (only if you set SYL_DEFAULT; otherwise ignored)
    default = (os.getenv("SYL_DEFAULT") or "").lower().strip()
    if default in _RETAILER_SEARCH:
        return _RETAILER_SEARCH[default].format(q=q)

    t = f"{name} {user_text}".lower()

    # 1) brand hints
    for patt, retailer in _BRAND_HINTS.items():
        if re.search(patt, t, flags=re.I):
            return _RETAILER_SEARCH[retailer].format(q=q)

    # 2) context fallback
    if _BEAUTY_WORDS.search(t):
        # if drugstore-y words appear, bias ulta; otherwise sephora
        prefer = "ulta" if re.search(r"(?i)\b(cera ?ve|la roche|the ordinary|elf|neutrogena|loreal|olay|aveeno)\b", t) else "sephora"
        return _RETAILER_SEARCH[prefer].format(q=q)

    if _FASHION_WORDS.search(t):
        return _RETAILER_SEARCH["nordstrom"].format(q=q)

    # 3) otherwise, mass fallback
    return _RETAILER_SEARCH["target"].format(q=q)

_BOLD_NAME = re.compile(r"\*\*(.+?)\*\*")
_NUM_NAME  = re.compile(r"^\s*\d+[\.\)]\s+([^\-–—:]+)", re.M)
_BUL_NAME  = re.compile(r"^\s*[-•]\s+([^\-–—:]+)", re.M)
_LABEL_WORDS      = {"best", "mid", "budget"}
_LABEL_TWO_BOLDS  = re.compile(r"\*\*\s*(?:best|mid|budget)\s*\*\*\s*:\s*\*\*([^*]+)\*\*", re.I)
_LABEL_AFTER_COLON= re.compile(r"\*\*\s*(?:best|mid|budget)\s*\*\*\s*:\s*([^\n\r\(\-–—:]+)", re.I)
_LIKE_BRAND       = re.compile(r"\(\s*.*?\blike\s+([^)]+?)\b.*?\)", re.I)

def _extract_pick_names(text: str, maxn: int = 3) -> list[str]:
    t = text or ""
    names: list[str] = []

    # 1) prefer "**Best:** <name>" / "**Best**: **<name>**"
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_TWO_BOLDS.search(line) or _LABEL_AFTER_COLON.search(line)
        if m:
            prod = m.group(1).strip()
            b = _LIKE_BRAND.search(line)
            if b:  # brand hint makes Amazon/SYL searches better
                prod = f"{prod} {b.group(1).strip()}"
            names.append(prod)

    if names:
        seen, out = set(), []
        for n in names:
            n = n.strip()
            if n and n not in seen:
                seen.add(n)
                out.append(n)
            if len(out) >= maxn:
                break
        return out

    # 2) fallback to generic patterns (filter label tokens)
    raw = []
    raw += _BOLD_NAME.findall(t)
    raw += _NUM_NAME.findall(t)
    raw += _BUL_NAME.findall(t)

    seen, out = set(), []
    for n in raw:
        n = n.strip(" -*•")
        if not n:
            continue
        lab = re.sub(r"[^a-z]", "", n.lower())
        if lab in _LABEL_WORDS:  # drop "best/mid/budget"
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= maxn:
            break
    return out

# --- Ordinal/number parser so "link #2" selects the 2nd item -------------

_ORDINAL_RE = re.compile(r"(?i)\b(?:#?\s*(\d{1,2})\b|first|second|third)\b")

def _requested_index(text: str) -> Optional[int]:
    t = text or ""
    m = _ORDINAL_RE.search(t)
    if not m:
        return None
    if m.group(1):
        try:
            n = int(m.group(1))
            return n if 1 <= n <= 50 else None
        except Exception:
            return None
    # words  <-- these four lines belong inside this function
    if re.search(r"(?i)\bfirst\b", t):  return 1
    if re.search(r"(?i)\bsecond\b", t): return 2
    if re.search(r"(?i)\bthird\b", t):  return 3
    return None

# --- phrase extractor so "link to NeoCell Super Collagen" never returns empty ----
_PHRASE_RE = re.compile(
    r"(?i)\b(?:link|buy|purchase|shop|url|send)\s*(?:to|for|the)?\s*([A-Za-z0-9' \-\+\&]+)"
)

def _phrase_from_user_text(user_text: str) -> Optional[str]:
    t = (user_text or "").strip()
    m = _PHRASE_RE.search(t)
    if m:
        phrase = m.group(1).strip(" .?!")
        # avoid obviously generic words
        if len(phrase) >= 3 and not re.fullmatch(r"(it|this|that|one|two|three)", phrase, flags=re.I):
            return phrase
    # fall back to whole text (last resort)
    words = re.sub(r"(?i)\b(link|buy|purchase|shop|send|url|for|to)\b", "", t).strip()
    return words or None

def _pick_names_to_link(names: list[str], user_text: str) -> list[str]:
    """
    If the user said '#2' or 'second', pick that index.
    If they typed a product phrase, prefer the name that contains it.
    Otherwise, return the original names (max 3).
    """
    if not names:
        return []
    idx = _requested_index(user_text)
    if idx and 1 <= idx <= len(names):
        return [names[idx - 1]]
    phrase = (_phrase_from_user_text(user_text) or "").lower()
    if phrase:
        for n in names:
            if phrase and phrase in n.lower():
                return [n]
    return names[:3]

def _append_links_for_picks(reply: str, convo_id: Optional[int] = None) -> str:
    """
    Append search links for 2–3 pick names. If none found in reply, look back.
    If still none, derive from user context keywords as a last resort.
    """
    names = _extract_pick_names(reply, maxn=3)

    if not names and convo_id:
        recent = _recent_outbound_texts(convo_id, limit=5)
        for text in recent:
            names = _extract_pick_names(text or "", maxn=3)
            if names:
                break

    if not names:
        # last-resort keyword guesses
        # keep this tiny so we don't invent a catalog
        kw = re.search(r"(?i)\b(minoxidil|ketoconazole|peptide serum|nutrafol|viviscal)\b", reply or "")
        base = kw.group(1) if kw else "5% minoxidil foam"
        names = [base]

    lines = [reply.rstrip(), ""]
    for n in names[:3]:
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
    Store once, send once. No manual segmentation or [1/3] prefixes.
    Carriers will stitch if the payload segments on their side.
    """
    # --- outbound dedupe (whole body) ----------------------------------------
    try:
        from hashlib import sha1
        sig = sha1((str(convo_id) + "::" + str(text_val)).encode("utf-8")).hexdigest()
        k   = f"sent:{convo_id}:{sig}"
        if _rds and not _rds.set(k, "1", ex=30, nx=True):
            logger.info("[Send][Dedup] Skipping duplicate send for convo %s", convo_id)
            return
    except Exception:
        pass

    text_val = (text_val or "").strip()
    if not text_val:
        return

    # ==== Preserve Amazon search links if allow-token is present ====
    _allow_amz = False
    if _ALLOW_AMZ_SEARCH_TOKEN in text_val:
        _allow_amz = True
        text_val = text_val.replace(_ALLOW_AMZ_SEARCH_TOKEN, "", 1).lstrip()

    # ==== Final shaping (single body) ====
    text_val = _add_personality_if_flat(text_val)
    text_val = _strip_link_placeholders(text_val)
    if not _allow_amz:
        text_val = _strip_amazon_search_links(text_val)
    text_val = wrap_all_affiliates(text_val)     # (adds Amazon ?tag= / SYL redirect)
    text_val = ensure_not_link_ending(text_val)

    # Optional debug marker (visible once)
    DEBUG_MARKER = os.getenv("DEBUG_MARKER", "")
    if DEBUG_MARKER:
        text_val = text_val.rstrip() + f"\n{DEBUG_MARKER}"
    OFFER_EMAIL = (os.getenv("OFFER_EMAIL_ON_OVERFLOW") or "0").lower() in ("1","true","yes")
    per  = int(os.getenv("SMS_PER_PART", "320"))
    maxp = int(os.getenv("SMS_MAX_PARTS", "3"))
    if OFFER_EMAIL and len(text_val) > (per * maxp):
        text_val = (
            text_val[: (per * maxp) - 80].rstrip()
            + "\n\nToo long to fit by SMS. Want the full list by email? Reply EMAIL."
        )

    # ==== Segment after shaping (URL-safe) ====
    parts = _segments_for_sms(text_val,
                              per=int(os.getenv("SMS_PER_PART", "320")),
                              max_parts=int(os.getenv("SMS_MAX_PARTS", "3")),
                              prefix_reserve=8)

    if not parts:
        return

    total_parts = len(parts)
    GHL_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL")
    delay_ms = int(os.getenv("SMS_PART_DELAY_MS", "3500"))

    for idx, part in enumerate(parts, 1):
        # prefix only when multipart
        # Add a zero-width timestamp to help carrier ordering without changing what users see
        prefix = f"[{idx}/{total_parts}] " if total_parts > 1 else ""
        full_text = prefix + part
        message_id = str(uuid.uuid4())

        # DB store each part
        try:
            with db.session() as s:
                models.insert_message(s, convo_id, "out", message_id, full_text)
                s.commit()
            logger.info("[Worker][DB] Outbound stored: convo_id={} user_id={} msg_id={}",
                        convo_id, user_id, message_id)
        except Exception:
            logger.exception("[Worker][DB] Failed to insert outbound, will still attempt send")

        # Primary send via GHL
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

        # Optional fallback (delivery safety while testing)
        if not USE_GHL_ONLY:
            try:
                integrations.send_sms_reply(user_id, full_text)
                logger.success("[Worker][Send] Fallback SMS send attempted for user_id={}", user_id)
            except Exception:
                logger.exception("[Worker][Send] Exception while calling send_sms_reply")

        # tiny pause for ordering
        try:
            if total_parts > 1 and idx < total_parts and delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
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
    first, *rest = t.splitlines()
    if _ANTI_FORM_RE.match(first.strip()):
        body = "Here’s what I’d do: focus on what actually moves the needle, then tweak if needed."
        follow = "Want me to tailor this tighter — or are you ready to try it?"
        t = f"{body}\n{(' '.join(rest)).strip() or follow}"
    t = _ANTI_FORM_RE.sub("", t).strip()
    t = re.sub(r"(?im)^\s*let'?s narrow.*$", "", t).strip()
    return t

_GREETING_RE = re.compile(r"^\s*(hi|hey|hello|yo|hiya|sup|good (morning|afternoon|evening))\b", re.I)

def _is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text or ""))
# --- Product-intent detector ---------------------------------------------------
_PRODUCT_INTENT_RE = re.compile(
    r"(?i)\b("
    r"recommend|recommendation|rec(s)?|suggest|best|top|"
    r"what should i (get|use)|which (one|product)|"
    r"product (pick|suggestion)|"
    r"collagen|retinol|vitamin c|peptide|serum|moisturizer|sunscreen|minoxidil|ketoconazole"
    r")\b"
)

def _looks_like_product_intent(text: str) -> bool:
    return bool(_PRODUCT_INTENT_RE.search(text or ""))

_LISTY_RE = re.compile(r"(?i)\[(best|mid|budget)\]|http|•|- |1\)|2\)|3\)")
def _looks_like_concrete_picks(text: str) -> bool:
    t = text or ""
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
        # remove survey-ish prompts
        reply = _anti_form_guard(reply, user_text)
        # If user clearly asked for products but reply is vague, rewrite to concrete picks
        if _looks_like_product_intent(user_text) and not _looks_like_concrete_picks(reply):
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

        reply = _maybe_append_ai_closer(reply, user_text, category=None, convo_id=convo_id)
        # is the user explicitly asking for links?
        link_request = bool(re.search(
            r"(?i)\b(link|links|website|websites|site|sites|url|buy|purchase|where to buy|map|maps|address|google|yelp|send.*(link|site|url))\b",
            (user_text or "")
        ))
        auto_link_flag = os.getenv("AUTO_LINK_ON_RECS", "1").lower() in ("1","true","yes")

        # don’t clamp when we’re about to append links automatically
        if not (link_request or (auto_link_flag and _looks_like_product_intent(user_text))):
            CLAMP = int(os.getenv("SMS_CLAMP_CHARS", "520"))
            if len(reply or "") > CLAMP:
                cut = (reply or "")[:CLAMP]
                sp = cut.rfind(" ")
                reply = (cut[:sp] if sp != -1 else cut).rstrip()

        # GPT pass-through links:
        # If user asked for links (or we auto-link product asks) AND GPT didn't include any URL,
        # add a minimal Amazon fallback; otherwise do nothing (we'll just wrap).
        make_links_now = link_request or (auto_link_flag and _looks_like_product_intent(user_text))
        if make_links_now and not _URL_RE.search(reply or ""):
            names = _extract_pick_names(reply, maxn=3)
            if not names:
                recent = _recent_outbound_texts(convo_id, limit=5)
                for t in recent:
                    names = _extract_pick_names(t or "", maxn=3)
                    if names: break
            if not names:
                phrase = _phrase_from_user_text(user_text)
                if phrase:
                    names = [phrase]
            if names:
                # synthesize Amazon searches; linkwrap will add ?tag= later
                strategy = (os.getenv("LINK_STRATEGY") or "dual").lower().strip()  # dual | syl-first | amazon-first | syl-only | amazon-only
                link_lines = []
                for n in _pick_names_to_link(names, user_text):
                    amz = _amz_search_url(n)
                    syl = _syl_search_url(n, user_text)
                    if   strategy == "syl-only":     link_lines.append(f"{n}: {syl}")
                    elif strategy == "amazon-only":  link_lines.append(f"{n}: {amz}")
                    elif strategy == "syl-first":    link_lines += [f"{n}: {syl}", f"{n} (alt): {amz}"]
                    elif strategy == "amazon-first": link_lines += [f"{n}: {amz}", f"{n} (alt): {syl}"]
                    else:                            link_lines += [f"{n}: {amz}", f"{n} (alt): {syl}"]
                link_block = "\n".join(link_lines)
                
                reply = ("Here you go:\n" + link_block) if link_request \
                        else (reply.rstrip() + "\n\nHere are the links:\n" + link_block)
                # keep Amazon searches so they won't be stripped; tagging happens downstream
                reply = _ALLOW_AMZ_SEARCH_TOKEN + "\n" + reply

        # keep the list crisp if the model rambled
        reply = re.sub(r"\s*\n\s*\n\s*", "\n", reply or "").strip()
       
    except Exception as e:
        logger.exception("[ChatOnly] GPT pass failed: {}", e)
        reply = ""

    # Greeting fallback (one friendly opener if reply is still blank)
    if not (reply or "").strip() and _is_greeting(user_text):
        reply = "Hey gorgeous — I’m here. What kind of trouble are we getting into today? Pick a lane or vent at me. 💅"

    # Safety net: guarantee exactly one message
    if not (reply or "").strip():
        reply = "Babe, I glitched. Say it again and I’ll do better. 💅"

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

