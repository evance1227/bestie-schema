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

GENIUSLINK_WRAP = (os.getenv("GENIUSLINK_WRAP") or "").strip()
GENIUSLINK_DOMAIN = (os.getenv("GENIUSLINK_DOMAIN") or "").strip()
GL_REWRITE = (os.getenv("GL_REWRITE") or "0").lower() in ("1", "true", "yes")
GL_ALLOW_REDIRECT_TEMPLATE = (os.getenv("GL_ALLOW_REDIRECT_TEMPLATE") or "0").lower() in ("1","true","yes")

GL_UNWRAP_REDIRECTS = (os.getenv("GL_UNWRAP_REDIRECTS") or "1").lower() not in ("0", "false", "")
AMAZON_CANONICALIZE = (os.getenv("AMAZON_CANONICALIZE") or "1").lower() not in ("0", "false", "")

# ShopYourLikes
SYL_ENABLED = (os.getenv("SYL_ENABLED") or "0").lower() in ("1","true","yes")
SYL_WRAP_TEMPLATE = (os.getenv("SYL_WRAP_TEMPLATE") or "").strip()
SYL_PUBLISHER_ID = (os.getenv("SYL_PUBLISHER_ID") or "").strip()
SYL_API_KEY = (os.getenv("SYL_API_KEY") or "").strip()
_SYL_RETAILERS = tuple([d.strip().lower() for d in (os.getenv("SYL_RETAILERS") or "").split(",") if d.strip()])
_SYL_DENY = tuple([d.strip().lower() for d in (os.getenv("SYL_DENYLIST") or "").split(",") if d.strip()])
_GO_SHOPMY_PREFIX = "https://go.shopmy.us/p-"

# -------------------- Regex ------------------ #
_URL = re.compile(r"https?://[^\s)\]]+", re.I)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_AMZN_HOST = re.compile(r"(?:^|\.)amazon\.", re.I)
_GENIUS_HOST = re.compile(r"(?:^|\.)geni\.us$", re.I)
_SYL_HOSTS = {"shop-links.co", "goto.shopyourlikes.com"}

# ASIN extractors: /dp/ASIN, /gp/product/ASIN, /ASIN/
_ASIN_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|gp/offer-listing|[^/]+/dp)/([A-Z0-9]{10})(?:[/?]|$)",
    re.I,
)

_AMAZON_SHORTNERS = {"a.co", "amzn.to"}
SHOPYOURLIKES_PREFIX = "https://shop-links.co/"

def wrap_shopurl(url: str) -> str:
    if not url:
        return url
    # TODO: you’ll pull your ShopYourLikes retailer ID/handle
    return f"{SHOPYOURLIKES_PREFIX}?url={url}"

# add below other SYL env reads
_SYL_DENY = tuple([d.strip().lower() for d in (os.getenv("SYL_DENYLIST") or "").split(",") if d.strip()])

def _is_syl_retailer(host: str) -> bool:
    """
    True if host should be wrapped by ShopYourLikes.
    - If SYL_RETAILERS="*", wrap all non-Amazon/non-denylisted hosts.
    - Else wrap if host endswith any domain in SYL_RETAILERS.
    """
    host = (host or "").lower()
    # NEW: never treat SYL hosts as retailers
    if host in _SYL_HOSTS:
        return False
    # never wrap denylisted or Amazon/Genius
    if any(host.endswith(d) for d in _SYL_DENY) or "amazon." in host or _GENIUS_HOST.search(host):
        return False

    # wrap-all mode
    if "*" in _SYL_RETAILERS:
        return True

    # whitelist mode
    return any(host.endswith(dom) for dom in _SYL_RETAILERS)

def _wrap_with_shopyourlikes(url: str) -> str:
    """Return ShopYourLikes redirect if enabled and template is configured, else original url."""
    if not (SYL_ENABLED and SYL_WRAP_TEMPLATE and url):
        return url
    try:
        # NEW: don't wrap an already-SYL link
        if urlparse(url).netloc.lower() in _SYL_HOSTS:
            return url
        return SYL_WRAP_TEMPLATE.format(url=quote_plus(url))
    except Exception:
        return url
    
def _wrap_with_shopyourlikes(url: str) -> str:
    """
    Build a short go.shopmy.us redirect when possible; otherwise fall back
    to your template. Never re-wrap SYL, and trim noisy search queries first.
    """
    if not (SYL_ENABLED and url):
        return url
    try:
        # never re-wrap SYL
        host = urlparse(url).netloc.lower()
        if host in _SYL_HOSTS:
            return url

        # trim retailer search URLs so SYL resolves reliably
        url = _trim_retailer_search(url)

        # Prefer go.shopmy.us/p-<publisherId>?url=<...> when publisher id exists
        if SYL_PUBLISHER_ID:
            return f"{_GO_SHOPMY_PREFIX}{SYL_PUBLISHER_ID}?url={quote_plus(url)}"

        # Otherwise use your SYL_WRAP_TEMPLATE if provided
        if SYL_WRAP_TEMPLATE:
            return SYL_WRAP_TEMPLATE.format(url=quote_plus(url))

        # If neither is set, return original url untouched
        return url
    except Exception:
        return url

def _trim_retailer_search(url: str) -> str:
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if "amazon." in host:
            return url  # do not touch Amazon here

        # allow only 'keyword' or 'q' for most retailers
        qs = dict(parse_qsl(u.query, keep_blank_values=False))
        allowed = {}
        for k in ("keyword", "q"):
            if k in qs and qs[k]:
                allowed[k] = qs[k]
                break

        clean = urlunparse(u._replace(query=urlencode(allowed, doseq=True)))
        # hard cap length for SMS aesthetics
        return clean if len(clean) <= 200 else clean[:200]
    except Exception:
        return url
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
    Wrap Amazon URLs for Geniuslink when GL_REWRITE is enabled; otherwise just ensure tag.
    """
    if not GL_REWRITE:
        # When GL is off, just guarantee the Amazon tag is present
        return _ensure_amazon_tag(url, AMAZON_TAG) if _is_amazon(url) else url

    try:
        u = urlparse(url)
        host = u.netloc.lower()

        # Already a geni.us link or not amazon → leave it
        if _GENIUS_HOST.search(host):
            return url
        if "amazon." not in host or host in _AMAZON_SHORTNERS:
            return url

        # Option A: redirect template
        if GENIUSLINK_WRAP and GL_ALLOW_REDIRECT_TEMPLATE:
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
    # Non-Amazon path: ShopYourLikes for supported retailers
    try:
        u = urlparse(clean)
        if _is_syl_retailer(u.netloc):
            return _wrap_with_shopyourlikes(clean)
    except Exception:
        pass

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
# at top with other env flags
CLOSER_MODE = (os.getenv("CLOSER_MODE") or "ai").lower()   # 'ai' | 'static' | 'off'

_URL_END_RE = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

def ensure_not_link_ending(text: str) -> str:
    """
    If CLOSER_MODE=static, append a neutral closer when a message ends with a URL.
    Otherwise return text untouched (AI will decide closers).
    """
    if not text or CLOSER_MODE != "static":
        return text

    if _URL_END_RE.search(text):
        closers = [
            "Want me to tweak brand, budget, or color?",
            "Prefer 1 luxe and 1 budget option?",
            "Need sizing/fit details or a comparable alt?",
        ]
        return text.rstrip() + "\n" + closers[0]
    return text


