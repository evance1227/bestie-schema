# app/linkwrap.py
import os
import json
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote
import logging
import requests

log = logging.getLogger(__name__)

AMAZON_TAG = os.getenv("AMAZON_ASSOC_TAG", "").strip()
GENIUS_API = os.getenv("GENIUSLINK_API_KEY", "").strip()
SKIMLINKS_SITE_ID = os.getenv("SKIMLINKS_SITE_ID", "").strip()  # for later

_AMAZON_HOST_PARTS = (
    "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de", "amazon.fr",
    "amazon.es", "amazon.it", "amazon.com.au", "amazon.co.jp", "amazon.in",
    "amazon.com.mx", "amazon.com.br", "amazon.nl", "amazon.se", "amazon.sg",
    "amazon.ae", "amazon.sa", "amazon.pl", "amazon.com.tr",
)

def _host(h: str) -> str:
    return (h or "").lower()

def is_amazon(url: str) -> bool:
    try:
        h = _host(urlparse(url).hostname)
        if not h:
            return False
        if h == "amzn.to":
            return True
        return any(h.endswith(dom) for dom in _AMAZON_HOST_PARTS)
    except Exception:
        return False

def _ensure_amazon_tag(url: str, tag: str) -> str:
    """Add ?tag= to Amazon URLs if missing. Leave amzn.to alone."""
    try:
        p = urlparse(url)
        if p.hostname and p.hostname.lower() == "amzn.to":
            return url  # can't modify short link; it already carries your tag via redirect rules
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if tag and not q.get("tag"):
            q["tag"] = tag
        new_q = urlencode(q, doseq=True)
        return urlunparse(p._replace(query=new_q))
    except Exception as e:
        log.warning("linkwrap: failed to tag amazon url: %s (%s)", url, e)
        return url

def _try_genius_shorten(dest_url: str) -> str | None:
    """Optional nicety: if GENIUSLINK_API_KEY is set, try to shorten.
    If anything fails, return None and the caller will fall back."""
    if not GENIUS_API:
        return None
    try:
        # Geniuslink API shape can vary by account; this endpoint works for most accounts.
        # If your account uses a different path, no worries—we’ll just fall back.
        r = requests.post(
            "https://api.geni.us/v1/shorten",
            headers={"X-API-KEY": GENIUS_API, "Content-Type": "application/json"},
            data=json.dumps({"url": dest_url}),
            timeout=5,
        )
        if r.ok:
            data = r.json()
            # common shapes: {"shortUrl": "..."} or {"result":{"shortUrl":"..."}}
            return data.get("shortUrl") or (data.get("result") or {}).get("shortUrl")
    except Exception as e:
        log.warning("linkwrap: genius shorten failed: %s", e)
    return None

def _try_skimlinks(url: str) -> str | None:
    """(For later) Create a Skimlinks go-skim URL. If not configured or denied, return None."""
    if not SKIMLINKS_SITE_ID:
        return None
    try:
        # This format works for standard Skimlinks “go” links once your site is approved.
        return f"https://go.skimresources.com/?id={SKIMLINKS_SITE_ID}&xs=1&url={quote(url, safe='')}"
    except Exception as e:
        log.warning("linkwrap: skimlinks wrap failed: %s", e)
        return None

def wrap_affiliate(url: str) -> str:
    """Best-effort affiliate wrapper with safe fallbacks.
    Priority today:
      1) Amazon → add tag → (optional) Geniuslink shorten
      2) (later) Non-Amazon → Skimlinks if available
      3) Otherwise → return the raw URL
    """
    if not url:
        return url

    # 1) Amazon first
    if is_amazon(url):
        tagged = _ensure_amazon_tag(url, AMAZON_TAG) if AMAZON_TAG else url
        short = _try_genius_shorten(tagged)
        return short or tagged

    # 2) (Later) non-Amazon via Skimlinks if configured
    wrapped = _try_skimlinks(url)
    if wrapped:
        return wrapped

    # 3) Always fall back to raw URL
    return url