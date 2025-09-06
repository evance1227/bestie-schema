# app/linkwrap.py
from __future__ import annotations

import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote_plus
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

        # Already geni.us? leave it
        if _GENIUS_HOST.search(host):
            return url

        if "amazon." not in host:
            return url

        # Shorteners: leave as-is
        if host in _AMAZON_SHORTNERS:
            return url

        # Option A: {url}-template redirect
        if GENIUSLINK_WRAP:
            return GENIUSLINK_WRAP.format(url=quote_plus(url))

        # Option B: domain + ASIN path
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
    Preferred transform: clean the URL, ensure Amazon tag, then wrap for Geniuslink if configured.
    """
    if not url:
        return url
    clean, trail = _strip_trailing_punct(url)
    clean = _strip_utm(clean)

    # Amazon path: tag, then wrap
    if _is_amazon(clean):
        if AMAZON_TAG:
            clean = _ensure_amazon_tag(clean, AMAZON_TAG)
        clean = _wrap_with_geniuslink(clean)

    return clean + trail

# Back-compat alias used elsewhere
convert_to_affiliate_link = convert_to_geniuslink

def rewrite_affiliate_links_in_text(text: str) -> str:
    """
    Rewrites BOTH markdown and bare URLs:
      - cleans UTMs
      - ensures amazon tag
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
        if _is_amazon(url) and AMAZON_TAG:
            return _ensure_amazon_tag(url, amazon_tag) + trail
        return m.group(0)

    return _URL.sub(_repl, text)

def make_sms_reply(reply: str, amazon_tag: str = "schizobestie-20") -> str:
    """
    Final SMS-safe pass:
      1) flatten markdown links
      2) enforce amazon affiliate tag
      3) collapse whitespace
    Do NOT add emojis or rewrite content here — workers handle tone and CTA.
    """
    try:
        t = (reply or "")

        # 1) flatten markdown
        t = sms_ready_links(t)

        # 2) rewrite/clean any URLs, then ensure tag for amazon (in case SMS copy joined lines)
        t = rewrite_affiliate_links_in_text(t)
        t = enforce_affiliate_tags(t, amazon_tag)

        # 3) collapse
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t
    except Exception as e:
        logger.warning("[Linkwrap] make_sms_reply failed: {}", e)
        return reply
