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
from urllib.parse import urlparse
from app.integrations_serp import lens_products

from app.linkwrap import normalize_syl_links, _amz_search_url, _syl_search_url, wrap_all_affiliates, ensure_not_link_ending, normalize_syl_links
import app.integrations as integrations
import os, logging
from redis import Redis
from rq import Queue, Worker

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
REDIS_URL  = (os.getenv("REDIS_URL") or "").strip()
_rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
USE_GHL_ONLY = (os.getenv("USE_GHL_ONLY", "1").lower() not in ("0","false","no"))
SEND_FALLBACK_ON_ERROR = True  # keep it True so we still send if GPT path hiccups
SYL_ENABLED = (os.getenv("SYL_ENABLED") or "0").lower() in ("1","true","yes")
SYL_PUBLISHER_ID = (os.getenv("SYL_PUBLISHER_ID") or "").strip()
AMAZON_ASSOCIATE_TAG = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()
SYL_RETAILERS = {s.strip().lower() for s in (os.getenv("SYL_RETAILERS", "").split(",")) if s.strip()}
def _syl_allowed(merchant_key: str) -> bool:
    # allow all if wildcard present or if list is empty (no list given)
    return (not SYL_RETAILERS) or ("*" in SYL_RETAILERS) or (merchant_key in SYL_RETAILERS)

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
GENIUSLINK_WRAP   = (os.getenv("GENIUSLINK_WRAP") or "").strip() 
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

import random  # put this with your other imports

_EMAIL_LINES = [
    "I’ve got more to say—reply EMAIL and I’ll send a fully curated list. 💌",
    "Want the long-winded tea (with links stacked neatly)? Reply EMAIL. ✨",
    "If you want my no-character-limit brain, reply EMAIL and I’ll drop the whole shebang in your inbox. 📬",
    "Prefer a tidy one-pager with picks & why? Reply EMAIL and I’ll shoot it over. 📨",
    "Thirsty for receipts, not snippets? Reply EMAIL and I’ll send the deep dive. 🔎"
]

def _maybe_add_email_offer(text_val: str, per: int, maxp: int) -> str:
    enabled = (os.getenv("EMAIL_OFFER_ENABLED") or "1").lower() in ("1","true","yes")
    if not enabled:
        return text_val
    if "EMAIL" in (text_val or "").upper():
        return text_val

    link_cnt = len(_URL_RE.findall(text_val or ""))
    overflow = len(text_val or "") > (per * maxp)
    prob = float(os.getenv("EMAIL_OFFER_PROB", "0.35"))

    if not (overflow or link_cnt >= 3 or random.random() < prob):
        return text_val

    closer = random.choice(_EMAIL_LINES)
    extra = " " + closer
    space = (per * maxp) - len(text_val)
    if space < len(extra):
        text_val = text_val[: (per * maxp) - len(extra) - 1].rstrip()
    return (text_val + extra).strip()

# --- SMS segmentation (URL-safe) ---------------------------------------------
_URL_RE   = re.compile(r"https?://\S+", re.I)
# --- keep SMS from ending on a bare URL (prevents weird previews/eating last line) ---
_URL_END_RE = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

def ensure_not_link_ending(text: str) -> str:
    """
    If an SMS ends on a bare URL, append a newline. No extra text.
    """
    if not text:
        return text
    if _URL_END_RE.search(text):
        return text.rstrip() + "\n"
    return text

# --- SMS/link sanitizers (to keep links clickable in SMS) ---
_LINK_MD_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_TRAIL_PUNCT_RE = re.compile(r"([)\].,!?;:]+)$")

def _unwrap_markdown_links(text: str) -> str:
    # Convert [label](url) to "label — url"
    return _LINK_MD_RE.sub(lambda m: f"{m.group(1)} — {m.group(2)}", text or "")

def _strip_styling(text: str) -> str:
    # Remove bullets/asterisks/emphasis chars that can cling to URLs
    return (text or "").replace("*", "").replace("_", "")

def _dedupe_spaces(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "")

def _tidy_urls_per_line(text: str) -> str:
    # Put each URL on its own line; strip trailing ) ] . , etc. from the token
    lines = []
    for raw in (text or "").splitlines():
        parts = []
        for tok in raw.split(" "):
            if tok.startswith("http"):
                tok = _TRAIL_PUNCT_RE.sub("", tok)
            parts.append(tok)
        lines.append(" ".join(parts).strip())
    return "\n".join(lines)
# --- Neutral tokenization & scoring for image candidates (no category hard-wiring) ---
from urllib.parse import urlparse

# Preferred affiliate-friendly hosts. This is only a hint for search;
# we DO NOT hard-wire product categories or force a specific merchant.
_AFFIL_HINT = "revolve shopbop nordstrom farfetch bloomingdales saks"

def _is_affiliate_url(url: str) -> bool:
    try:
        h = urlparse(url).netloc.lower()
        return h.endswith("shopmy.us") or ("amazon." in h)
    except Exception:
        return False

def _affiliate_upgrade(picks: list[dict], user_text: str) -> list[dict]:
    """
    For each pick, if it's not already affiliate-friendly, try to replace the URL with:
      1) a SYL search link biased by the user's wording + our affiliate hint
      2) if that fails, an Amazon search link for the label
    Never adds extra lines—just replaces the URL when we find a better monetizable target.
    """
    upgraded = []
    ut = (user_text or "").strip()
    for p in picks:
        title = (p.get("title") or "").strip()
        url   = (p.get("url")   or "").strip()

        if _is_affiliate_url(url):
            upgraded.append(p)
            continue

        alt = ""
        try:
            # let SYL route to a supported retailer using the user's own words
            alt = _syl_search_url(title or ut or "best match", f"{ut} {_AFFIL_HINT}".strip())
        except Exception:
            pass

        if not alt:
            try:
                alt = _amz_search_url(title or ut or "best match")
            except Exception:
                pass

        upgraded.append({"title": title, "url": (alt or url), "host": p.get("host","")})
    return upgraded

import re
from urllib.parse import urlparse

_STOP_WORDS = {
    "the","and","or","for","with","this","that","those","these","you","your",
    "me","mine","a","an","to","in","at","on","of","by","it","its","my","our",
    "size","sizes","xs","sm","small","medium","large","xl","xxl","xxx","fit",
    "please","send","link","links","photo","picture","image","pic","find",
    "want","need","like","some","any","budget","price","range","under","over"
}

def _tokenize_query(s: str) -> list[str]:
    s = (s or "").lower()
    toks = re.findall(r"[a-z0-9]+", s)
    # keep as LIST (order preserved) so we can slice; do not return a set
    return [t for t in toks if len(t) >= 3 and t not in _STOP_WORDS]


def _score_image_candidate(user_text: str, c: dict) -> int:
    """Score a lens candidate by overlap with user_text tokens. No category hard-wiring."""
    toks = _tokenize_query(user_text)
    if not toks:
        return 0
    title = (c.get("title") or "").lower()
    host  = (c.get("host")  or "").lower()
    path  = urlparse(c.get("url") or "").path.lower()
    hay   = " ".join((title, host, path))
    score = 0
    for t in toks:
        if t in title: score += 3
        if t in host:  score += 4
        if t in path:  score += 2
    return score

# --- Intent extraction (brand + category) for image queries ---
_BRAND_TOKENS = {
    "gucci","prada","celine","ray-ban","rayban","saint laurent","ysl",
    "balenciaga","burberry","chanel","versace","tom ford","maui jim",
    "oakley","dior","fendi","valentino"
}
_CATEGORY_SYNONYMS = {
    "sunglasses": {"sunglasses","sunnies","shades","sunglass"},
    "hat": {"hat","wide brim","sunhat","sun hat","bucket hat","visor","fedora"},
    "coverup": {"cover-up","coverup","beach cover","kaftan","pareo","sarong"},
    "sandals": {"sandal","sandals","slides","flip flops","flip-flops","pool shoes"},
    "necklace": {"necklace","pendant","chain","choker"},
    # add more categories here as you need
}

def _intent_from_text(t: str) -> tuple[str|None, str|None]:
    t = (t or "").lower()
    brand = next((b for b in _BRAND_TOKENS if b in t), None)
    category = None
    for cat, syns in _CATEGORY_SYNONYMS.items():
        if any(s in t for s in syns):
            category = cat
            break
    return brand, category

# --- Bulleted lines → ensure they have a link (multi-line, dash-robust) ---
import re

_DASH = r"[—–-]"  # em, en, hyphen
_BULLET_START = re.compile(r'^\s*(?:\d+[.)]\s*|\-\s+)')  # "1. " or "- "

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _looks_like_retailer(s: str) -> bool:
    return bool(re.match(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ&.' ]{1,40}$", s or ""))

def _extract_label_and_retailer(chunk: str) -> tuple[str, str]:
    lines = [ln.rstrip() for ln in (chunk or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    first = _BULLET_START.sub("", lines[0]).strip()
    joined = _normalize_spaces(" ".join(lines))

    # Simple: "Label — Retailer —"
    m = re.match(r'^(?P<label>.+?)\s+' + _DASH + r'\s+(?P<ret>[^-—–].+?)\s*(?:' + _DASH + r'\s*)?$', first)
    if m:
        label = m.group("label").strip()
        retailer = m.group("ret").strip()
        if _looks_like_retailer(retailer):
            return label, retailer

    # Fallback: "Label - description. Retailer -"
    lab = first
    mdash = re.search(r'\s' + _DASH + r'\s', first)
    if mdash:
        lab = first[:mdash.start()].strip()

    tail = re.split(r'\s' + _DASH + r'\s|\.', joined)[-1].strip(" -—–")
    if _looks_like_retailer(tail):
        return lab, tail

    last_line = lines[-1].strip(" -—–")
    if _looks_like_retailer(last_line):
        return lab, last_line

    return lab, ""
def _is_https_live(url: str) -> bool:
    """Return True if the URL resolves with 2xx/3xx; False on errors (SSL, 4xx/5xx, timeouts)."""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False
    try:
        import requests
        r = requests.head(url, allow_redirects=True, timeout=3)
        if 200 <= r.status_code < 400:
            return True
        # Some sites dislike HEAD; try one GET quickly
        r2 = requests.get(url, allow_redirects=True, timeout=4)
        return 200 <= r2.status_code < 400
    except Exception:
        return False
    
def _is_affiliate_hostname(host: str) -> bool:
    """Return True if the host is an affiliate-friendly domain we control/monetize."""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h.endswith("shopmy.us") or ("amazon." in h)

def _ensure_links_on_bullets(text: str, user_text: str) -> str:
    """
    Make every bullet produce 'Label — http...' (never '— —').
    - If the bullet chunk already has a link: upgrade to affiliate (SYL→Amazon), validate, rewrite first line, drop raw link lines.
    - If the bullet has no link: build one (Amazon if explicitly asked; else prefer affiliates).
    """
    lines = (text or "").splitlines()
    out: list[str] = []
    want_amazon = "amazon" in (user_text or "").lower()

    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]

        # Not a bullet OR already contains http → copy and advance
        if ("http://" in ln or "https://" in ln) or not _BULLET_START.match(ln or ""):
            out.append(ln.rstrip())
            i += 1
            continue

        # Collect the bullet "chunk": this bullet line + following non-bullet, non-link lines
        chunk_lines = [ln]
        j = i + 1
        while j < n and lines[j].strip() and not _BULLET_START.match(lines[j]) and ("http://" not in lines[j] and "https://" not in lines[j]):
            chunk_lines.append(lines[j])
            j += 1

        label, retailer = _extract_label_and_retailer("\n".join(chunk_lines))

        # CASE A: chunk already has a link → upgrade & validate, then rewrite
        if any("http://" in cl or "https://" in cl for cl in chunk_lines):
            # 1) first url in the chunk
            orig_url = ""
            for cl in chunk_lines:
                m = re.search(r"(https?://\S+)", cl)
                if m:
                    orig_url = m.group(1)
                    break

            # 2) affiliate candidate (SYL→Amazon), else keep brand url
            alt_url = _prefer_affiliate_url(label, user_text) or orig_url

            # 3) if still not affiliate, try again to monetize
            try:
                host = urlparse(alt_url).netloc.lower()
            except Exception:
                host = ""
            if not _is_affiliate_hostname(host):
                improved = _prefer_affiliate_url(label, user_text)
                if improved:
                    alt_url = improved

            # 4) validate; if unsafe, fall back to affiliate search again
            if not _is_https_live(alt_url):
                fallback = _prefer_affiliate_url(label, user_text)
                if fallback:
                    alt_url = fallback

            # 5) rewrite first bullet line and drop raw link-only lines
            first_line = chunk_lines[0].rstrip()
            first_line = re.sub(r"\s[—–-]\shttps?://\S+\s*$", "", first_line)  # remove trailing existing link
            first_line = re.sub(r"\s[—–-]\s*$", "", first_line)

            if alt_url:
                out.append(f"{first_line} — {alt_url}")
            else:
                out.append(first_line)

            for k in range(1, len(chunk_lines)):
                if ("http://" in chunk_lines[k]) or ("https://" in chunk_lines[k]):
                    continue
                out.append(chunk_lines[k].rstrip())

            i = j
            continue

        # CASE B: no link in the chunk → build one
        
        url = ""       # ensure url is always defined in this branch

        if label:
            try:
                if want_amazon or retailer.lower().startswith("amazon"):
                    # optional deep link if you added it
                    url = _amz_deep_link_if_obvious(label)
                    if not url:
                        url = _amz_search_url(label or (user_text or "best match"))
                else:
                    url = _prefer_affiliate_url(label, user_text) or _syl_search_url(label or (user_text or "best match"), user_text)
            except Exception:
                url = ""

        # FINAL GUARANTEE (this is where "if not url" was blowing up)
        if not url:
            try:
                url = _syl_search_url(label or (user_text or "best match"), user_text) or _amz_search_url(label or (user_text or "best match"))
            except Exception:
                url = ""

        if url:
            first_line = chunk_lines[0].rstrip()
            first_line = re.sub(r"\s[—–-]\s*$", "", first_line)
            out.append(f"{first_line} — {url}")
            for k in range(1, len(chunk_lines)):
                out.append(chunk_lines[k].rstrip())
        else:
            out.extend(cl.rstrip() for cl in chunk_lines)

        i = j

    return "\n".join(out)
    
# --- Prefer affiliate-friendly links for text bullets (no category rules) ---
_AFFIL_HINT = "sephora ulta nordstrom revolve shopbop target anthropologie free people amazon"

def _prefer_affiliate_url(label: str, user_text: str) -> str:
    """
    Try to return an affiliate-friendly URL for the given label:
      1) SYL search with an affiliate hint (retailer-agnostic)
      2) Amazon search
      3) "" (caller falls back)
    """
    label = (label or "").strip()
    ut = (user_text or "").strip()
    # 1) SYL first — let SYL pick a supported merchant
    try:
        hint = f"{ut} {_AFFIL_HINT}".strip()
        url = _syl_search_url(label or (ut or "best match"), hint)
        if url:
            return url
    except Exception:
        pass
    # 2) Amazon next
    try:
        url = _amz_search_url(label or (ut or "best match"))
        if url:
            return url
    except Exception:
        pass
    # 3) no suggestion
    return ""

def _amz_deep_link_if_obvious(label: str) -> str:
    """
    Try to form a known-good Amazon product deep link for very obvious labels.
    Only return a URL if it resolves live; otherwise return "".
    Keep this list tiny & conservative.
    """
    try:
        s = (label or "").lower()
        tag = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()

        # Example ASINs — include only ones you're comfortable with
        if "supergoop" in s and "scalp" in s and ("50" in s or "spf" in s):
            url = "https://www.amazon.com/dp/B07NC1QF5M"  # Supergoop! Poof (example)
            if tag:
                url = f"{url}?tag={tag}"
            return url if _is_https_live(url) else ""

        if "neutrogena" in s and ("cool dry sport" in s or "sport scalp" in s):
            url = "https://www.amazon.com/dp/B00NR1YQ1O"  # Neutrogena Sport (example)
            if tag:
                url = f"{url}?tag={tag}"
            return url if _is_https_live(url) else ""

        return ""
    except Exception:
        return ""

def _shorten_bullet_labels(text: str, max_len: int = 42) -> str:
    """
    Clamp long labels so two links fit in 2 parts and leave room for voice.
    Works on lines like: 'Label — https://...'
    """
    out = []
    for ln in (text or "").splitlines():
        if " — http" in ln:
            label, rest = ln.split(" — http", 1)
            label = label.strip()
            if len(label) > max_len:
                label = label[:max_len - 1].rstrip() + "…"
            out.append(f"{label} — http{rest}")
        else:
            out.append(ln)
    return "\n".join(out)

# --- Copy tidy: remove dangling "here" phrasings when links are above ---
_BAD_HERE = (
    "you can find it here:",
    "you can find it here",
    "find it here:",
    "find it here",
    "here's the link:",
    "here is the link:",
    "here's a link:",
    "here is a link:",
)

def _clean_here_phrases(text: str) -> str:
    t = text or ""
    low = t.lower()
    changed = False
    for p in _BAD_HERE:
        if p in low:
            idx = low.find(p)
            # cut the phrase + any trailing spaces
            t = t[:idx] + t[idx + len(p):]
            low = t.lower()
            changed = True
    return t.strip()

from urllib.parse import quote_plus

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
    # procedure guard...
    base = (os.getenv("SYL_WRAP_TEMPLATE") or "").strip()
    pub  = (os.getenv("SYL_PUBLISHER_ID") or "").strip()
    if not (base and pub): return ""

    q = quote_plus((name or "").strip())

    # --- Retailer routing map (pattern -> (merchant_key, search_url_format)) ---
    # Tip: order from most-common → less-common so the first match wins quickly.

    _RETAILER_ROUTES = [
        # Beauty
        (r"(?i)\bsephora\b",                   ("sephora",        "https://www.sephora.com/search?keyword={q}")),
        (r"(?i)\bulta\b|\bultra\b",            ("ulta",           "https://www.ulta.com/search?Ntt={q}")),
        (r"(?i)\bdermstore\b",                 ("dermstore",      "https://www.dermstore.com/search?search={q}")),
        (r"(?i)\bcredo\b|\bcredo beauty\b",    ("credo",          "https://credobeauty.com/search?q={q}")),
        (r"(?i)\bglossier\b",                  ("glossier",       "https://www.glossier.com/search?q={q}")),
        (r"(?i)\bfenty\b|\bfenty beauty\b",    ("fenty beauty",   "https://www.fentybeauty.com/search?q={q}")),
        (r"(?i)\btatcha\b",                    ("tatcha",         "https://www.tatcha.com/search?q={q}")),
        (r"(?i)\btarte\b",                     ("tarte cosmetics","https://tartecosmetics.com/search?q={q}")),
        (r"(?i)\bmac cosmetics\b|\bmac\b",     ("mac cosmetics",  "https://www.maccosmetics.com/search?q={q}")),
        (r"(?i)\bestee lauder\b",              ("estee lauder",   "https://www.esteelauder.com/search?Ntt={q}")),
        (r"(?i)\bcredobeauty\b",               ("credo",          "https://credobeauty.com/search?q={q}")),

        # Department / Designer
        (r"(?i)\bnordstrom rack\b",            ("nordstrom rack", "https://www.nordstromrack.com/sr?keyword={q}")),
        (r"(?i)\bbloomingdale'?s\b",           ("bloomingdale's", "https://www.bloomingdales.com/shop/keyword/{q}")),
        (r"(?i)\bneiman marcus\b",             ("neiman marcus",  "https://www.neimanmarcus.com/search.jsp?Ntt={q}")),
        (r"(?i)\bsaks off 5th\b",              ("saks off 5th",   "https://www.saksoff5th.com/search?q={q}")),
        (r"(?i)\bsaks\b",                      ("saks fifth avenue","https://www.saksfifthavenue.com/search?q={q}")),
        (r"(?i)\brevolve\b",                   ("revolve",        "https://www.revolve.com/r/search/?q={q}")),
        (r"(?i)\bssense\b",                    ("ssense",         "https://www.ssense.com/en-us/women/search?q={q}")),

        # Fashion – contemporary & mall
        (r"(?i)\banthropologie\b|\banthro\b",  ("anthropologie",  "https://www.anthropologie.com/search?q={q}")),
        (r"(?i)\bfree\s*people\b|\bfreepeople\b", ("freepeople",   "https://www.freepeople.com/s?query={q}")),
        (r"(?i)\basos\b",                      ("asos",           "https://www.asos.com/search/?q={q}")),
        (r"(?i)\babercrombie\b",               ("abercrombie & fitch","https://www.abercrombie.com/shop/us/search/{q}")),
        (r"(?i)\bamerican eagle\b|\bae\b",     ("american eagle outfitters","https://www.ae.com/us/en/search/{q}")),
        (r"(?i)\bh&m\b",                       ("h&m",            "https://www2.hm.com/en_us/search-results.html?q={q}")),
        (r"(?i)\bmango\b",                     ("mango",          "https://shop.mango.com/us/search?q={q}")),
        (r"(?i)\bj\.?crew\b",                  ("j.crew",         "https://www.jcrew.com/search2?N=&Nloc=en&Ntrm={q}")),
        (r"(?i)\bmadenwell\b|\bmadewell\b",    ("madewell",       "https://www.madewell.com/search?q={q}")),
        (r"(?i)\bgap factory\b",               ("gap factory",    "https://www.gapfactory.com/search?q={q}")),
        (r"(?i)\bgap\b(?!\s*factory)",         ("gap",            "https://www.gap.com/search?q={q}")),
        (r"(?i)\bold navy\b",                  ("old navy",       "https://oldnavy.gap.com/search?q={q}")),
        (r"(?i)\buniqlo\b",                    ("uniqlo",         "https://www.uniqlo.com/us/en/search/?q={q}")),
        (r"(?i)\burban outfitters\b",          ("urban outfitters","https://www.urbanoutfitters.com/search?q={q}")),
        (r"(?i)\blulus\b",                     ("lulus",          "https://www.lulus.com/search?q={q}")),
        (r"(?i)\bprincess polly\b",            ("princess polly", "https://us.princesspolly.com/search?q={q}")),
        (r"(?i)\bprettylittlething\b",         ("prettylittlething","https://www.prettylittlething.us/search/?q={q}")),
        (r"(?i)\bsteve madden\b",              ("steve madden",   "https://www.stevemadden.com/search?q={q}")),
        (r"(?i)\bsam edelman\b",               ("sam edelman",    "https://www.samedelman.com/search?q={q}")),

        # Sneakers & athleisure
        (r"(?i)\bnike\b",                      ("nike",           "https://www.nike.com/w?q={q}&vst={q}")),
        (r"(?i)\badidas\b",                    ("adidas",         "https://www.adidas.com/us/search?q={q}")),
        (r"(?i)\bnew balance\b",               ("new balance",    "https://www.newbalance.com/search?q={q}")),
        (r"(?i)\bhoka\b",                      ("hoka one",       "https://www.hoka.com/en/us/search?q={q}")),
        (r"(?i)\bconverse\b",                  ("converse",       "https://www.converse.com/search?q={q}")),
        (r"(?i)\bugg\b",                       ("ugg",            "https://www.ugg.com/search?q={q}")),
        (r"(?i)\bvans\b",                      ("vans",           "https://www.vans.com/search?q={q}")),
        (r"(?i)\bzappos\b",                    ("zappos",         "https://www.zappos.com/search?q={q}")),

        # Outdoor & gear
        (r"(?i)\brei\b",                       ("rei",            "https://www.rei.com/search?q={q}")),
        (r"(?i)\bbackcountry\b",               ("backcountry",    "https://www.backcountry.com/store/search?q={q}")),
        (r"(?i)\bdick'?s\b|\bdick.s\b",        ("dick's sporting goods","https://www.dickssportinggoods.com/search/SearchResults.jsp?searchTerm={q}")),

        # Home + big box
        (r"(?i)\btarget\b",                    ("target",         "https://www.target.com/s?searchTerm={q}")),
        (r"(?i)\bwalmart\b",                   ("walmart",        "https://www.walmart.com/search?q={q}")),
        (r"(?i)\bwayfair\b",                   ("wayfair",        "https://www.wayfair.com/keyword.php?keyword={q}")),
        (r"(?i)\bhome depot\b|\bhomedepot\b",  ("home depot",     "https://www.homedepot.com/s/{q}")),
        (r"(?i)\bcb2\b",                       ("cb2",            "https://www.cb2.com/search?query={q}")),
        (r"(?i)\bcontainer store\b",           ("the container store","https://www.containerstore.com/s/{q}")),
        (r"(?i)\bsur la table\b",              ("sur la table",   "https://www.surlatable.com/s?query={q}")),
        (r"(?i)\bruggable\b",                  ("ruggable",       "https://my.ruggable.com/search?q={q}")),

        # Tech / photo
        (r"(?i)\bb&h\b|\bbh photo\b",          ("b&h photo video","https://www.bhphotovideo.com/c/search?Ntt={q}")),
    ]

    if re.search(r"(?i)\bamazon\b", user_text or ""):
        return ""

    retailer_url = ""
    for pat, (merchant_key, fmt) in _RETAILER_ROUTES:
        if re.search(pat, user_text or ""):
            if _syl_allowed(merchant_key):
                retailer_url = fmt.format(q=q)
            break

    if not retailer_url:
        return ""  # skip alt to avoid 404

    return base.format(pub=pub, url=quote_plus(retailer_url))

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
    try:       
        with db.session() as s:
            s.execute(sqltext("""
                UPDATE public.user_profiles
                SET plan_status = COALESCE(plan_status, 'pending'),
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

    except Exception as e:
        logger.warning("[Gate][DB] defaults skipped (db unavailable): %s", e)
        return {}

    _, _, plan_status, trial_start, _, _, daily_used, daily_date = row

    # reset daily counters if date rolled
    # reset daily counters if date rolled
    if daily_date != datetime.now(timezone.utc).date():
        try:
            with db.session() as s:
                s.execute(sqltext("""
                    UPDATE public.user_profiles
                    SET daily_counter_date = CURRENT_DATE, daily_msgs_used = 0
                    WHERE user_id = :u
                """), {"u": user_id})
                s.commit()
        except Exception as e:
            logger.warning("[Gate][DB] counter reset skipped (db unavailable): %s", e)

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

def _segments_for_sms(
    text: str,
    *,
    per: int = 360,
    max_parts: int = 2,
    prefix_reserve: int = 8,
) -> list[str]:
    """
    Split text into <= max_parts SMS chunks of size <= per, never splitting inside a URL.
    Reserves 'prefix_reserve' chars for the '[i/n] ' prefix.
    """
    text = (text or "").rstrip()
    if not text:
        return []

    if len(text) <= per:
        return [text]

    out: list[str] = []
    i, n = 0, len(text)
    limit_per = max(10, per - prefix_reserve)

    def _in_url(pos: int) -> tuple[int, int] | None:
        for m in _URL_RE.finditer(text):
            s, e = m.span()
            if s <= pos < e:
                return (s, e)
            if s > pos:
                break
        return None

    while i < n and len(out) < max_parts - 1:
        remaining = n - i
        if remaining <= limit_per:
            break

        cut = i + limit_per

        # never break inside a URL
        span = _in_url(cut)
        if span:
            cut = span[0]                 # back up to URL start
        else:
            # otherwise back up to last space
            ws = text.rfind(" ", i, cut)
            if ws != -1 and ws > i:
                cut = ws

        out.append(text[i:cut].rstrip())
        i = cut
        while i < n and text[i] == " ":
            i += 1

    out.append(text[i:].rstrip())
    return [p for p in out if p]

# after
def _store_and_send(
    user_id: int,
    convo_id: int,
    text_val: str,
    send_phone: Optional[str] = None,
    user_text: Optional[str] = None,   
    media_urls: list[str] | None = None,
) -> None:
    """
    Store once, send once. No manual segmentation or [1/3] prefixes.
    Carriers will stitch if the payload segments on their side.
    """
    # --- outbound dedupe: skip if we just sent the exact same text in this convo ---
    # ---- dedupe (check now, set later after success) ----
    dedupe_key = None
    try:
        from hashlib import sha1
        sig = sha1(((str(convo_id) + ":" + (text_val or "")).encode("utf-8"))).hexdigest()
        dedupe_key = f"sent:{convo_id}:{sig}"
        if _rds and _rds.exists(dedupe_key):
            logger.info("[Send][Dedupe] Skipping duplicate send for convo %s", convo_id)
            return
    except Exception:
        dedupe_key = None

    image_mode = bool(media_urls)
        
    text_val = (text_val or "").strip()

    # Allow image-only messages: only abort when there's no text AND no media.
    if not text_val and not media_urls:
        logger.warning("[Send] Empty text_val and no media; aborting")
        return
    # else: continue — lens block will add picks for images


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

    # sanitizers
    text_val = _unwrap_markdown_links(text_val)
    text_val = _strip_styling(text_val)
    text_val = _dedupe_spaces(text_val)
    text_val = _tidy_urls_per_line(text_val)

    # wrappers
    text_val = wrap_all_affiliates(text_val)     # adds Amazon ?tag= / SYL redirect
    text_val = normalize_syl_links(text_val)     # legacy sylikes → shopmy.us
    text_val = ensure_not_link_ending(text_val)
   
    # Optional debug marker (visible once)
    DEBUG_MARKER = os.getenv("DEBUG_MARKER", "")
    if DEBUG_MARKER:
        text_val = text_val.rstrip() + f"\n{DEBUG_MARKER}"
    OFFER_EMAIL = (os.getenv("OFFER_EMAIL_ON_OVERFLOW") or "0").lower() in ("1","true","yes")
    per  = int(os.getenv("SMS_PER_PART", "360"))
    maxp = int(os.getenv("SMS_MAX_PARTS", "2"))

    if OFFER_EMAIL and len(text_val) > (per * maxp):
        text_val = (
            text_val[: (per * maxp) - 80].rstrip()
            + "\n\nToo long to fit by SMS. Want the full list by email? Reply EMAIL."
        )
    # Optional email-offer (rotating, only when helpful)
    per  = int(os.getenv("SMS_PER_PART", "320"))
    maxp = int(os.getenv("SMS_MAX_PARTS", "3"))
    text_val = _maybe_add_email_offer(text_val, per, maxp)

    # === IMAGE MATCHES (neutral, token-scored; affiliate upgrade; wrap + validate) ===
    try:
        if media_urls:
            ut = (user_text or "").strip()

            # Lens candidates
            cands = lens_products(media_urls[0], allowed_domains=None, topn=12)

            # Score by user words (no category rules). Keep >0; if none, we'll fall back.
            scored = [( _score_image_candidate(ut, c), c ) for c in cands]
            winners = [c for s, c in scored if s > 0]

            if not winners:
                # Fall back to two SYL searches from user text
                toks = _tokenize_query(ut)
                q1 = " ".join(toks[:4]) or (ut or "best match")
                q2 = " ".join(toks[4:8]) or q1
                links = []
                try: links.append((" ".join(t.capitalize() for t in q1.split()) or "Top picks", _syl_search_url(q1, ut)))
                except Exception: pass
                if q2 != q1:
                    try: links.append(("More picks", _syl_search_url(q2, ut)))
                    except Exception: pass

                intro = "Found close matches from your photo & request:"
                lines = [f"- {title} — {url}" for title, url in links if url]
                text_val = (intro + "\n" + "\n".join(lines)).strip()
                image_mode = True

            else:
                # Deduplicate by host for variety
                by_host = {}
                for c in winners:
                    h = (c.get("host") or "").lower()
                    if h and h not in by_host:
                        by_host[h] = c
                deduped = list(by_host.values())

                # Guarantee URL (synthesize if missing)
                fixed = []
                for c in deduped:
                    title = (c.get("title") or "").strip() or (c.get("host") or "").strip()
                    host  = (c.get("host") or "").lower()
                    url   = (c.get("url")  or "").strip()
                    if not (url.startswith("http://") or url.startswith("https://")):
                        try:
                            if "amazon." in host:
                                url = _amz_search_url(title or ut or "best match")
                            else:
                                url = _syl_search_url(title or (ut or "best match"), f"{ut} {host}".strip())
                        except Exception:
                            url = ""
                    if url:
                        fixed.append({"title": title, "url": url, "host": host})

                if not fixed:
                    # same fallback as above
                    toks = _tokenize_query(ut)
                    q1 = " ".join(toks[:4]) or (ut or "best match")
                    q2 = " ".join(toks[4:8]) or q1
                    fixed = []
                    try: fixed.append({"title": (" ".join(t.capitalize() for t in q1.split()) or "Top picks"), "url": _syl_search_url(q1, ut), "host":"search"})
                    except Exception: pass
                    if q2 != q1:
                        try: fixed.append({"title": "More picks", "url": _syl_search_url(q2, ut), "host":"search"})
                        except Exception: pass

                picks = fixed[:2]   # two links → fits 2 SMS parts

                if picks:
                    # affiliate upgrade (SYL preferred, Amazon next)
                    picks = _affiliate_upgrade(picks, ut)

                    # build body
                    intro = "Found close matches:"
                    lines = [f"- {p['title']} — {p['url']}" for p in picks]
                    text_val = (intro + "\n" + "\n".join(lines)).strip()

                    # wrap AFTER building body (ensures deep links become SYL/Amazon)
                    text_val = wrap_all_affiliates(text_val)
                    text_val = normalize_syl_links(text_val)
                    text_val = ensure_not_link_ending(text_val)

                    # validate wrapped links; fallback to SYL search on mismatch
                    validated_lines = []
                    for ln in text_val.splitlines():
                        if (" — http" not in ln) and ("http" not in ln):
                            validated_lines.append(ln)
                            continue
                        parts = ln.split(" — http", 1)
                        if len(parts) != 2:
                            validated_lines.append(ln)
                            continue
                        label = parts[0].strip()
                        url   = "http" + parts[1].strip()

                        # expected host from the pick whose title prefix appears in label
                        expect = ""
                        for p in picks:
                            if p["title"] and p["title"].split()[0] in label:
                                expect = p.get("host","")
                                break
                        if not expect:
                            try:
                                from urllib.parse import urlparse
                                expect = urlparse(url).netloc
                            except Exception:
                                pass

                        if _looks_live_and_same_host(url, expect):
                            validated_lines.append(f"{label} — {url}")
                        else:
                            try:
                                alt = _syl_search_url(label or (ut or "best match"), ut)
                                validated_lines.append(f"{label} — {alt}")
                            except Exception:
                                validated_lines.append(label)

                    text_val = "\n".join(validated_lines).strip()
                    image_mode = True
    except Exception as e:
        logger.warning("[Vision] image block error: {}", e)
    # === END IMAGE MATCHES ===

    # Final safety: ensure bullets have links, then shorten labels
    text_val = _ensure_links_on_bullets(text_val, user_text or "")
    text_val = _shorten_bullet_labels(text_val)

    # Link-first (only real URLs)
    lines = [ln for ln in (text_val or "").splitlines()]
    link_lines  = [ln for ln in lines if ("http://" in ln) or ("https://" in ln) or (" — http" in ln)]
    other_lines = [ln for ln in lines if ln not in link_lines]

    if image_mode:
        # PHOTO MODE: keep only the link lines (drops any stray narration)
        text_val = "\n".join(link_lines).strip()
    else:
        # TEXT MODE: links first, then remaining copy
        text_val = "\n".join(
            link_lines + ([""] if (link_lines and other_lines) else []) + other_lines
        ).strip()

    # tidy phrasing now that links are at the top
    text_val = _clean_here_phrases(text_val)
    text_val = ensure_not_link_ending(text_val)

    # Split into SMS parts (2 × 380 or 360 if that’s your env)
    parts = _segments_for_sms(
        text_val,
        per=int(os.getenv("SMS_PER_PART", "380")),
        max_parts=int(os.getenv("SMS_MAX_PARTS", "2")),
        prefix_reserve=8,   # room for "[1/2] "
    )
    # --- segmentation debug + hard guard ---
    try:
        logger.info("[Send][Seg] parts=%d body_len=%d", len(parts or []), len(text_val or ""))
    except Exception:
        pass

    if not parts:
        logger.warning("[Send] No parts produced; sending fallback single part")
        per = int(os.getenv("SMS_PER_PART", "380"))
        parts = [ (text_val or "").strip()[:per] ]

    GHL_WEBHOOK_URL = os.getenv("GHL_OUTBOUND_WEBHOOK_URL")
    delay_ms = int(os.getenv("SMS_PART_DELAY_MS", "1800"))   # 1600–2200 is a good sweet spot
    
    for idx, part in enumerate(parts, 1):
        prefix = f"[{idx}/{len(parts)}] " if len(parts) > 1 else ""
        full_text = prefix + part
        message_id = str(uuid.uuid4())

        # DB store each part
        try:
            with db.session() as s:
                models.insert_message(s, convo_id, "out", message_id, full_text)
                s.commit()
            logger.info(
                "[Worker][DB] Outbound stored: convo_id=%s user_id=%s msg_id=%s",
                convo_id, user_id, message_id
            )
        except Exception as e:
            # Don’t block sending if the DB is down
            logger.warning(
                "[Worker][DB] Outbound store FAILED (db unavailable): %s",
        e
    )

        # tiny pause so carriers keep order
        time.sleep(delay_ms / 1000.0)

    # mark this exact body as sent (for 60s) NOW that we succeeded
    try:
        if _rds and dedupe_key:
            _rds.set(dedupe_key, "1", ex=60)
    except Exception:
        pass   

    return
# --------------------------------------------------------------------- #
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

# --- simple shopping intent detector (retailer-agnostic) ---
import re

_SHOP_INTENT_RE = re.compile(
    r"(?i)\b("
    r"find this|where to buy|send.*link|buy.*(this|one)|"
    r"need.*(link|options|picks)|recommend.*(dress|shoes|top|hat|sunscreen|serum|oil)|"
    r"link.*please|can you link|shoot me the link|size\s?(xs|s|m|l|xl)"
    r")\b"
)

def _has_shop_intent(text: str) -> bool:
    return bool(_SHOP_INTENT_RE.search(text or ""))

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
    # normalize attachment strings like "url1, url2, url3" -> ["url1","url2","url3"]
    import re
    def _split_clean_urls(lst):
        out = []
        for v in (lst or []):
            if isinstance(v, str):
                for p in re.split(r"[,\s]+", v):
                    p = p.strip().strip(".,;:)]")
                    if p.startswith("http"):
                        out.append(p)
        return out

    media_urls = _split_clean_urls(media_urls)

    # 0) Plan gate ---------------------------------------------------------------
    try:
        try:
            gate_snapshot = _ensure_profile_defaults(user_id)
        except Exception as e:
            logger.error("[Gate] snapshot/build error: %s", e)
            gate_snapshot = {}
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
        first = (media_urls[-1] or "").strip()  # last image/audio is primary
        lower = first.lower()
        try:
            # If it's audio, transcribe immediately and return
            if any(lower.endswith(ext) for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
                logger.info("[Worker][Media] Attachment audio detected: %s", first)
                reply = ai.transcribe_and_respond(first, user_id=user_id)
                _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
                return
            # If it's an image, DO NOT call describe_image here.
            # Let the chat path handle the image(s) via context['media_urls'].
        except Exception as e:
            logger.warning("[Worker][Media] Attachment handling failed: %s", e)

    # If the user pasted a naked URL in text, only fast-path audio; let images fall through
    if "http" in user_text:
        if any(ext in normalized_text for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Audio URL detected, transcribing.")
            reply = ai.transcribe_and_respond(user_text.strip(), user_id=user_id)
            _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
            return
        
    # If a naked URL is in the text, quick media sniff
    if "http" in user_text:
        if any(ext in normalized_text for ext in [".mp3", ".m4a", ".wav", ".ogg"]):
            logger.info("[Worker][Media] Audio URL detected, transcribing.")
            reply = ai.transcribe_and_respond(user_text.strip(), user_id=user_id)
            _store_and_send(user_id, convo_id, reply, send_phone=user_phone)
            return
    # image URL in text? fall through to chat (no early describe)

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
            context={
                "has_completed_quiz": has_quiz,
                "media_urls": media_urls or [],
            }
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
        _STYLE_INTENT_RE = re.compile(
            r"(?i)\b(haircut|hair cut|hair style|hairstyle|bob|lob|bangs|fringe|layers|part|makeup|outfit|wardrobe|look|photo)\b"
        )

        def _looks_like_style_intent(text: str) -> bool:
            return bool(_STYLE_INTENT_RE.search(text or ""))

        # GPT pass-through links:
        # If user asked for links (or we auto-link product asks) AND GPT didn't include any URL,
        # add a minimal Amazon fallback; otherwise do nothing (we'll just wrap).
        make_links_now = (
            link_request or
            (auto_link_flag and _looks_like_product_intent(user_text) and not _looks_like_style_intent(user_text))
        )
        if make_links_now and not _URL_RE.search(reply or ""):
            names = _extract_pick_names(reply, maxn=3)           
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

                    if strategy == "syl-only":
                        # only SYL; if syl is empty (not a retailer), fall back to Amazon
                        link_lines.append(f"{n}: {syl or amz}")

                    elif strategy == "amazon-only":
                        # only Amazon
                        link_lines.append(f"{n}: {amz}")

                    elif strategy == "syl-first":
                        # prefer SYL when available; otherwise show Amazon
                        link_lines.append(f"{n}: {syl or amz}")
                        if syl and amz:
                            link_lines.append(f"{n} (alt): {amz}")

                    elif strategy == "amazon-first":
                        # prefer Amazon; include SYL only if we actually have one
                        link_lines.append(f"{n}: {amz}")
                        if syl:
                            link_lines.append(f"{n} (alt): {syl}")

                    else:  # dual
                        # show both; omit SYL if empty (procedures/techniques etc.)
                        link_lines.append(f"{n}: {amz}")
                        if syl:
                            link_lines.append(f"{n} (alt): {syl}")

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

    image_mode = bool(media_urls)

    # Only convert to shoppable bullets when it makes sense
    if (
        image_mode                               # image present (meant for picks)
        or _has_shop_intent(user_text)           # user asked for link/options/buy
        or _has_shop_intent(reply)               # model itself promised picks/links
        or (_ALLOW_AMZ_SEARCH_TOKEN in (text_val or ""))  # your allow token
    ):
        reply = _shorten_bullet_labels(
            _ensure_links_on_bullets(reply, user_text)
        )

    # else: leave reply purely conversational
    _store_and_send(
        user_id, convo_id, reply, user_phone,
        user_text=user_text, media_urls=media_urls
    )
    return

def _looks_live_and_same_host(url: str, expect_host: str) -> bool:
    try:
        import requests
        from urllib.parse import urlparse
        def _norm(h: str) -> str:
            h = (h or "").lower()
            return h[4:] if h.startswith("www.") else h

        r = requests.head(url, allow_redirects=True, timeout=3)
        if r.status_code >= 400:
            return False
        final = _norm(urlparse(r.url).netloc)
        if _norm(expect_host) == final:
            return True
        # Some sites don't love HEAD; try one GET
        r2 = requests.get(url, allow_redirects=True, timeout=4)
        if r2.status_code >= 400:
            return False
        final = _norm(urlparse(r2.url).netloc)
        return _norm(expect_host) == final
    except Exception:
        return False


def _ping_job():
    from loguru import logger
    logger.info("[Worker] Executed ping job")
    return "pong"

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

