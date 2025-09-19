# app/linkwrap.py
"""
Link wrapping & SMS hygiene for Bestie.
- wrap_all_affiliates(text): rewrite every link with affiliate logic (SYL + Geniuslink)
- ensure_not_link_ending(text): optional closer if reply ends on a URL (CLOSER_MODE=static)
"""

import os
import re
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs, urlencode


# ---------- ENV ----------
SYL_ENABLED       = (os.getenv("SYL_ENABLED") or "0").strip().lower() in ("1","true","yes")
SYL_PUBLISHER_ID  = (os.getenv("SYL_PUBLISHER_ID") or "").strip()
SYL_WRAP_TEMPLATE = (os.getenv("SYL_WRAP_TEMPLATE") or "https://go.shopmy.us/p-{pub}?url={url}").strip()
SYL_RETAILERS     = [d.strip().lower() for d in (os.getenv("SYL_RETAILERS") or "").split(",") if d.strip()]
SYL_DENYLIST      = [d.strip().lower() for d in (os.getenv("SYL_DENYLIST") or "geni.us,gumroad.com").split(",") if d.strip()]

GENIUSLINK_WRAP   = (os.getenv("GENIUSLINK_WRAP") or "").strip()
GENIUSLINK_DOMAIN = (os.getenv("GENIUSLINK_DOMAIN") or "").strip()   # optional DOMAIN mode
AMAZON_ASSOCIATE_TAG = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()

CLOSER_MODE       = (os.getenv("CLOSER_MODE") or "off").strip().lower()  # 'ai' | 'static' | 'off'

# ---------- REGEX ----------
_URL_RE       = re.compile(r"https?://[^\s)]+", re.I)
_MD_LINK_RE   = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_URL_END_RE   = re.compile(r"(https?://[^\s)]+)\s*$", re.I)
_AMAZON_HOST  = re.compile(r"amazon\.[^/]+$", re.I)

# ---------- HELPERS ----------
def _wrap_syl(url: str) -> str:
    """
    Wrap a retailer URL with ShopYourLikes redirect.
    """
    if not (SYL_ENABLED and SYL_PUBLISHER_ID):
        return url
    return f"https://goto.shopyourlikes.com/redirect?publisher_id={SYL_PUBLISHER_ID}&url={quote_plus(url)}"

def _append_amz_tag(u: str) -> str:
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
    u = _amazon_dp(url)  # returns DP if present; otherwise leaves search path
    if GENIUSLINK_WRAP:
        return GENIUSLINK_WRAP.format(url=quote_plus(u))
    if GENIUSLINK_DOMAIN:
        m = re.search(r"/dp/([A-Z0-9]{10})", u)
        if m:
            return f"https://{GENIUSLINK_DOMAIN.rstrip('/')}/{m.group(1)}"
    return _append_amz_tag(u)

def _is_denied(url: str) -> bool:
    low = url.lower()
    return any(d in low for d in SYL_DENYLIST)

def _amazon_dp(url: str) -> str:
    """
    Try to canonicalize Amazon URLs to /dp/ASIN. If we can't, return the original.
    """
    try:
        parsed = urlparse(url)
        if not _AMAZON_HOST.search(parsed.netloc):
            return url
        path = parsed.path or ""

        # /dp/ASIN
        m = re.search(r"/dp/([A-Z0-9]{10})", path, re.I)
        if not m:
            # /gp/product/ASIN
            m = re.search(r"/gp/product/([A-Z0-9]{10})", path, re.I)
        if not m:
            # sometimes ASIN is a standalone segment
            m = re.search(r"/([A-Z0-9]{10})(?:[/?]|$)", path)
        if not m:
            return url

        asin = m.group(1)
        return urlunparse((parsed.scheme, parsed.netloc, f"/dp/{asin}", "", "", ""))
    except Exception:
        return url

def _should_syl(domain: str) -> bool:
    """
    Decide if a retailer should be SYL-wrapped.
    - '*' in SYL_RETAILERS means all domains are allowed (except denylist & Amazon host).
    - If not '*', only wrap those explicitly listed.
    """
    if not SYL_ENABLED or not SYL_PUBLISHER_ID:
        return False
    if _AMAZON_HOST.search(domain):
        return False  # Amazon handled by Geniuslink
    if SYL_RETAILERS and SYL_RETAILERS[0] != "*":
        return any(domain.endswith(r) for r in SYL_RETAILERS)
    return True

def _wrap_syl(url: str) -> str:
    """
    Wrap a retailer URL via ShopYourLikes.
    """
    if not SYL_ENABLED or not SYL_PUBLISHER_ID:
        return url
    low = url.lower()
    if "go.shopmy.us" in low or "goto.shopyourlikes.com" in low:
        return url  # already wrapped
    return SYL_WRAP_TEMPLATE.format(pub=SYL_PUBLISHER_ID, url=quote_plus(url))

def _wrap_url(url: str) -> str:
    """
    Choose the right wrapper for a single URL:
      - denylist → return original
      - Amazon  → Geniuslink
      - Retail  → SYL
      - else    → original
    """
    if not url:
        return url
    if _is_denied(url):
        return url
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if _AMAZON_HOST.search(host):
            return _wrap_amazon(url)
        if _should_syl(host):
            return _wrap_syl(url)
        return url
    except Exception:
        return url

# ---------- PUBLIC ----------
def wrap_all_affiliates(text: str) -> str:
    """
    Rewrite every link in the text with affiliate logic (Markdown + plain).
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
        # Avoid double-working links we just normalized in markdown
        if f"]({url})" in out:
            return url
        return _wrap_url(url)
    out = _URL_RE.sub(_plain_repl, out)

    return out

def ensure_not_link_ending(text: str) -> str:
    """
    If CLOSER_MODE=static, append a neutral closer when a message ends with a URL.
    Otherwise do nothing — let AI decide.
    """
    if not text or CLOSER_MODE != "static":
        return text

    if _URL_END_RE.search(text):
        closers = [
            "Want me to tweak brand, budget, or color?",
            "Prefer one luxe and one budget option?",
            "Need sizing, fit, or a comparable alt?",
        ]
        return text.rstrip() + "\n" + closers[0]
    return text

# Back-compat shim for existing code paths in workers.py
def convert_to_geniuslink(url: str) -> str:
    """
    Legacy name used by workers.py. Keep it as a thin wrapper that applies
    affiliate logic as wrap_all_affiliates() but for a single URL.
    """
    return _append_amz_tag(url)


