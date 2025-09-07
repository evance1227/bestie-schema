# app/linkwrap.py
from __future__ import annotations

import os
import re
import html  # NEW: for unescaping redirect params
from urllib.parse import (
    urlparse, parse_qsl, parse_qs, urlencode, urlunparse, quote_plus
)
from loguru import logger

# -------------------- Env -------------------- #
AMAZON_TAG = (
    os.getenv("AMAZON_ASSOCIATE_TAG")
    or os.getenv("AMAZON_ASSOC_TAG")
    or ""
).strip()

# Option A: full redirect template, e.g. https://geni.us/redirect?url={url}
GENIUSLINK_WRAP = (os.getenv("GENIUSLINK_WRAP") or "").strip()
# Option B: domain style, e.g. geni.us   (we'll use https://{domain}/{ASIN})
GENIUSLINK_DOMAIN = (os.getenv("GENIUSLINK_DOMAIN") or "").strip()
GL_REWRITE = (os.getenv("GL_REWRITE") or "1").lower() not in ("0", "false", "")
GL_ALLOW_REDIRECT_TEMPLATE = (os.getenv("GL_ALLOW_REDIRECT_TEMPLATE") or "0").lower() in ("1","true","yes")

# NEW: make unwrapping and canonicalizing explicit toggles
GL_UNWRAP_REDIRECTS = (os.getenv("GL_UNWRAP_REDIRECTS") or "1").lower() not in ("0", "false", "")
AMAZON_CANONICALIZE = (os.getenv("AMAZON_CANONICALIZE") or "1").lower() not in ("0", "false", "")

# -------------------- Regex ------------------ #
_URL = re.compile(r"https?://[^\s)\]]+", re.I)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_AMZN_HOST = re.compile(r"(?:^|\.)amazon\.", re.I)
_GENIUS_HOST = re.compile(r"(?:^|\.)geni\.us$", re.I)

# ASIN extractors: /dp/ASIN, /gp/product/ASIN, /ASIN/
_ASIN_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|gp/offer-listing|[^/]+/dp)/([A-Z0-9]{10})(?:[/?]|$)",
    re.I,
)

_AMAZON_SHORTNERS = {"a.co", "amzn.to"}

# -------------------- Helpers ---------------- #
def _strip_trailing_punct(url: str) -> tuple[str, str]:
    """
    Return (clean_url, trailing_punct) so we can re-attach punctuation like ')' or '.'
    """
    trail = ""
    while url and url[-1] in ").,;!?":
        trail = url[-1] + trail
        url = url[:-1]
    return url, trail

def _is_amazon(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return bool(_AMZN_HOST.search(host))
    except Exception:
        return False

def _asin_from_url(url: str) -> str | None:
    try:
        m = _ASIN_RE.search(url)
        return m.group(1).upper() if m else None
    except Exception:
        return None

def _strip_utm(url: str) -> str:
    try:
        u = urlparse(url)
        qs = dict(parse_qsl(u.query, keep_blank_values=True))
        for k in list(qs.keys()):
            if k.lower().startswith("utm_") or k.lower() in {"psc"}:
                qs.pop(k, None)
        return urlunparse(u._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url

def _ensure_amazon_tag(url: str, tag: str) -> str:
    """
    If url is amazon.* and not a shortener, ensure tag= is present once.
    """
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if host in _AMAZON_SHORTNERS:
            return url  # cannot alter shorteners safely
        if "amazon." not in host or not tag:
            return url
        qs = dict(parse_qsl(u.query, keep_blank_values=True))
        if qs.get("tag") != tag:
            qs["tag"] = tag
        return urlunparse(u._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url

# NEW: unwrap a geni.us/redirect?url=<target> into the true target
def _unwrap_genius_redirect(url: str) -> str:
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if not GL_UNWRAP_REDIRECTS:
            return url
        if not _GENIUS_HOST.search(host):
            return url
        if not u.path.lower().startswith("/redirect"):
            return url
        q = parse_qs(u.query or "")
        target = q.get("url") or q.get("target") or q.get("to")
        if target and target[0]:
            real = html.unescape(target[0])
            logger.debug("[Linkwrap] Unwrapped geni.us redirect to {}", real)
            return real
    except Exception as e:
        logger.debug("[Linkwrap] unwrap redirect failed: {}", e)
    return url

# NEW: optional canonical Amazon DP link builder
def _canonicalize_amazon(url: str, tag: str) -> str:
    if not AMAZON_CANONICALIZE:
        return _ensure_amazon_tag(url, tag) if tag else url
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if host in _AMAZON_SHORTNERS or "amazon." not in host:
            return url  # do not touch shorteners or non-amazon
        asin = _asin_from_url(url)
        if not asin:
            return _ensure_amazon_tag(url, tag) if tag else url
        # Default to US store for canon DP. If you need intl later, expose env.
        dp_host = os.getenv("AMAZON_DP_HOST", "www.amazon.com")
        path = f"/dp/{asin}"
        query = urlencode({"tag": tag}) if tag else ""
        return urlunparse(("https", dp_host, path, "", query, ""))
    except Exception as e:
        logger.debug("[Linkwrap] canonicalize failed: {}", e)
        return _ensure_amazon_tag(url, tag) if tag else url

def _wrap_with_geniuslink(url: str) -> str:
    """
    Wrap Amazon URLs for Geniuslink in one of two ways:
      - GENIUSLINK_WRAP template -> {url} gets percent-encoded original
      - GENIUSLINK_DOMAIN + ASIN -> https://{domain}/{ASIN}
    If neither is set, return url unchanged.
    """
    if not GL_REWRITE:
        return url

    try:
        u = urlparse(url)
        host = u.netloc.lower()

        if _GENIUS_HOST.search(host):
            return url

        if "amazon." not in host or host in _AMAZON_SHORTNERS:
            return url

        if GENIUSLINK_WRAP and GL_ALLOW_REDIRECT_TEMPLATE:
            return GENIUSLINK_WRAP.format(url=quote_plus(url))

        if GENIUSLINK_DOMAIN:
            asin = _asin_from_url(url)
            if asin:
                domain = GENIUSLINK_DOMAIN.strip().lstrip("https://").lstrip("http://").rstrip("/")
                return f"https://{domain}/{asin}"
    except Exception as e:
        logger.debug("[Linkwrap] geniuslink wrap failed: {}", e)

    return url

# -------------------- Public API ---------------- #
def convert_to_geniuslink(url: str) -> str:
    """
    Preferred transform: unwrap broken redirects, clean, tag or canonicalize,
    then wrap for Geniuslink if configured.
    """
    if not url:
        return url

    clean, trail = _strip_trailing_punct(url)

    # NEW: unwrap any geni.us/redirect first to the real target
    try:
        clean = _unwrap_genius_redirect(clean)
    except Exception:
        pass

    clean = _strip_utm(clean)

    # Amazon path: canonicalize or tag, then optional wrap
    if _is_amazon(clean):
        if AMAZON_TAG or AMAZON_CANONICALIZE:
            clean = _canonicalize_amazon(clean, AMAZON_TAG)
        if GL_REWRITE:
            clean = _wrap_with_geniuslink(clean)


    return clean + trail

# Back-compat alias used elsewhere
convert_to_affiliate_link = convert_to_geniuslink

def rewrite_affiliate_links_in_text(text: str) -> str:
    """
    Rewrites BOTH markdown and bare URLs:
      - unwraps geni.us/redirect
      - cleans UTMs
      - ensures amazon tag or canonical DP
      - geniuslink wrap when configured
    """
    if not text:
        return text

    # 1) Markdown links first (preserve labels)
    def _md_repl(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return f"[{label}]({convert_to_geniuslink(url)})"

    out = _MD_LINK.sub(_md_repl, text)

    # 2) Bare links next
    def _bare_repl(m: re.Match) -> str:
        return convert_to_geniuslink(m.group(0))

    out = _URL.sub(_bare_repl, out)
    return out

def sms_ready_links(text: str) -> str:
    """
    Convert [label](url) → url for SMS autolinking and collapse whitespace.
    """
    if not text:
        return ""
    t = _MD_LINK.sub(lambda m: m.group(2), text)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\s{2,}", " ", t).strip()

def enforce_affiliate_tags(text: str, amazon_tag: str) -> str:
    """
    Tag any amazon.* URLs that may have slipped through without a tag (does not re-wrap).
    """
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        url, trail = _strip_trailing_punct(m.group(0))
        if _is_amazon(url) and amazon_tag:
            return _canonicalize_amazon(url, amazon_tag) + trail  # NEW: respect canonicalize flag
        return m.group(0)

    return _URL.sub(_repl, text)

def make_sms_reply(reply: str, amazon_tag: str = "schizobestie-20") -> str:
    """
    Final SMS-safe pass:
      1) flatten markdown links
      2) rewrite/clean any URLs (unwrap, tag/canon, optional wrap)
      3) ensure amazon tag if anything slipped
      4) collapse whitespace
    Do NOT add emojis or rewrite content here — workers handle tone and CTA.
    """
    try:
        t = (reply or "")

        # Flatten markdown like [label](url) → url
        t = sms_ready_links(t)

        # Rewrite links (unwrap geni.us, canonicalize/tag amazon, wrap if configured)
        t = rewrite_affiliate_links_in_text(t)

        # Enforce final Amazon tag (in case any got skipped)
        t = enforce_affiliate_tags(t, amazon_tag)

        # Final whitespace cleanup
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t
    except Exception as e:
        logger.warning("[Linkwrap] make_sms_reply failed: {}", e)
        return reply

# ==== Never end replies on a link ====
_URL_END_RE = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

def ensure_not_link_ending(text: str) -> str:
    """
    If the message ends with a URL, append a short conversational closer
    so SMS doesn't look ugly and the user is invited to reply.
    """
    if not text:
        return text
    if _URL_END_RE.search(text):
        closers = [
            "Want the lighter gel or a richer cream?",
            "Under $30 or go-for-gold?",
            "Do you want speed or maximum results? I can tune it."
        ]
        # simple deterministic pick
        return text.rstrip() + "\n" + closers[0]
    return text
