# app/linkwrap.py
"""
Link wrapping & SMS hygiene for Bestie.

Responsibilities
- wrap_all_affiliates(text): rewrite every URL in a reply with affiliate logic
    * Amazon  -> add Associates ?tag=... (or Geniuslink if configured)
    * Retail  -> ShopYourLikes/ShopMy redirect (single template; never profile page)
    * Google/Maps/YouTube and denylisted hosts are left as-is
- ensure_not_link_ending(text): optional static closer if a reply ends with a URL
- convert_to_geniuslink(url): legacy shim (returns Amazon URL with ?tag=...)
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs, urlencode


# =========================
# ENV
# =========================

# --- ShopYourLikes / ShopMy ---
SYL_ENABLED       = (os.getenv("SYL_ENABLED") or "0").strip().lower() in ("1", "true", "yes")
SYL_PUBLISHER_ID  = (os.getenv("SYL_PUBLISHER_ID") or "").strip()

# One canonical redirect template (NO profile links). If your account uses a different
# base, set SYL_WRAP_TEMPLATE in Render to that pattern; keep {pub} and {url}.
SYL_WRAP_TEMPLATE = (os.getenv("SYL_WRAP_TEMPLATE") or
                     "https://go.shopmy.us/p-{pub}?url={url}").strip()

# '*' means allow all retailers (except denylist & Amazon). Otherwise comma-separated list.
SYL_RETAILERS     = [d.strip().lower() for d in (os.getenv("SYL_RETAILERS") or "*").split(",") if d.strip()]

# Never SYL-wrap these (already-affiliate or our own assets)
SYL_DENYLIST      = [d.strip().lower() for d in (os.getenv("SYL_DENYLIST") or
                     "geni.us,gumroad.com,bit.ly,tinyurl.com").split(",") if d.strip()]

# --- Amazon / Geniuslink ---
GENIUSLINK_WRAP   = (os.getenv("GENIUSLINK_WRAP") or "").strip()           # e.g. "https://geni.us/redirect?url={url}"
GENIUSLINK_DOMAIN = (os.getenv("GENIUSLINK_DOMAIN") or "").strip()         # e.g. "yourid.geni.us"
AMAZON_ASSOCIATE_TAG = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()   # e.g. "schizobestie-20"

# --- Optional static closer ---
CLOSER_MODE       = (os.getenv("CLOSER_MODE") or "off").strip().lower()    # 'ai' | 'static' | 'off'


# =========================
# REGEX
# =========================
_URL_RE       = re.compile(r"https?://[^\s)>\]]+", re.I)
_MD_LINK_RE   = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_URL_END_RE   = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

# Amazon host detector and simple DP normalizer
_AMAZON_HOST  = re.compile(r"(^|\.)(amazon\.[^/]+)$", re.I)


# =========================
# HELPERS
# =========================

def _is_denied(url: str) -> bool:
    low = (url or "").lower()
    return any(d in low for d in SYL_DENYLIST)

def _amazon_dp(url: str) -> str:
    """
    Try to canonicalize Amazon URLs to /dp/ASIN. If not possible, return original.
    Works for:
      /dp/ASIN
      /gp/product/ASIN
      .../ASIN...
    """
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if not _AMAZON_HOST.search(host):
            return url

        path = parsed.path or ""
        m = re.search(r"/dp/([A-Z0-9]{10})", path, re.I)
        if not m:
            m = re.search(r"/gp/product/([A-Z0-9]{10})", path, re.I)
        if not m:
            # sometimes ASIN is a standalone segment in the path
            m = re.search(r"/([A-Z0-9]{10})(?:[/?]|$)", path)

        if not m:
            return url

        asin = m.group(1)
        return urlunparse((parsed.scheme, parsed.netloc, f"/dp/{asin}", "", "", ""))
    except Exception:
        return url

def _append_amz_tag(u: str) -> str:
    """
    Append Associates ?tag=... to ANY Amazon URL (DP or search) unless already present.
    """
    if not AMAZON_ASSOCIATE_TAG:
        return u
    try:
        p = urlparse(u)
        q = parse_qs(p.query)
        if "tag" not in q or not q["tag"]:
            q["tag"] = [AMAZON_ASSOCIATE_TAG]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))
    except Exception:
        return u


def _wrap_amazon(url: str) -> str:
    """
    Amazon affiliate: try DP canonicalization, then:
      - Geniuslink 'wrap' mode if GENIUSLINK_WRAP is set,
      - Geniuslink 'domain' mode if GENIUSLINK_DOMAIN is set and /dp/ASIN present,
      - else append ?tag=... as fallback (works for DP and search).
    """
    # If you prefer canonical DP; otherwise comment the next line.
    u = _amazon_dp(url)

    if GENIUSLINK_WRAP:
        return GENIUSLINK_WRAP.format(url=quote_plus(u))

    if GENIUSLINK_DOMAIN:
        m = re.search(r"/dp/([A-Z0-9]{10})", u)
        if m:
            return f"https://{GENIUSLINK_DOMAIN.rstrip('/')}/{m.group(1)}"

    return _append_amz_tag(u)

def _should_syl(domain: str) -> bool:
    """
    Decide if a retailer should be SYL-wrapped.
    - '*' in SYL_RETAILERS -> allow all domains (except denylist & Amazon host).
    - otherwise only wrap those explicitly listed.
    """
    if not (SYL_ENABLED and SYL_PUBLISHER_ID):
        return False

    if _AMAZON_HOST.search(domain):
        return False  # Amazon handled separately

    if SYL_RETAILERS and SYL_RETAILERS[0] != "*":
        return any(domain.endswith(r) for r in SYL_RETAILERS)

    return True

def _wrap_syl(url: str) -> str:
    """
    Wrap a retailer URL via ShopYourLikes/ShopMy redirect.
    NOTE: never profile links; always the redirect template.
    """
    if not (SYL_ENABLED and SYL_PUBLISHER_ID):
        return url

    low = (url or "").lower()
    if "go.shopmy.us" in low or "goto.shopyourlikes.com" in low or "go.sylikes.com" in low:
        return url


    return SYL_WRAP_TEMPLATE.format(pub=SYL_PUBLISHER_ID, url=quote_plus(url))

def _wrap_url(url: str) -> str:
    """
    Choose the right wrapper for a single URL:
      - denylist  -> original
      - Amazon    -> _wrap_amazon
      - Retailers -> _wrap_syl (if allowed)
      - Google/Maps/YouTube -> original
    """
    if not url:
        return url
    if _is_denied(url):
        return url

    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()

        # Skip common non-retail destinations
        if "google." in host or "maps.google." in host or "youtu" in host:
            return url

        if _AMAZON_HOST.search(host):
            return _wrap_amazon(url)

        if _should_syl(host):
            return _wrap_syl(url)

        return url
    except Exception:
        return url

# =========================
# PUBLIC
# =========================

def wrap_all_affiliates(text: str) -> str:
    """
    Rewrite every link in the text with affiliate logic.
    Order:
      1) rewrite markdown links first
      2) rewrite remaining plain URLs
    """
    if not text:
        return text

    # 1) Markdown links [label](url)
    def _md_repl(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return f"[{label}]({_wrap_url(url)})"

    out = _MD_LINK_RE.sub(_md_repl, text)

    # 2) Plain URLs
    def _plain_repl(m: re.Match) -> str:
        url = m.group(0)
        # If this exact URL already appears inside a markdown link we just rewrote,
        # leave it as-is to avoid double work.
        if f"]({url})" in out:
            return url
        return _wrap_url(url)

    out = _URL_RE.sub(_plain_repl, out)
    return out

def ensure_not_link_ending(text: str) -> str:
    """
    If CLOSER_MODE=static, append a short closer when a message ends with a URL.
    Otherwise do nothing (let AI decide).
    """
    if not text or CLOSER_MODE != "static":
        return text

    if _URL_END_RE.search(text):
        closer = "Want the direct page or a cheaper alt?"
        return text.rstrip() + "\n" + closer

    return text

# Legacy shim: single URL path (used by older call sites)
def convert_to_geniuslink(url: str) -> str:
    """
    Apply the same Amazon-tag logic that wrap_all_affiliates() uses, but for a single URL.
    (Kept for compatibility with older modules.)
    """
    return _append_amz_tag(url)
