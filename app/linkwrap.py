# app/linkwrap.py
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

AMAZON_TAG = os.getenv("AMAZON_ASSOC_TAG", "").strip()

_URL_RE = re.compile(r'https?://[^\s)]+')

def _ensure_amazon_tag(url: str) -> str:
    """
    If the URL is an Amazon product/search page, ensure the Associates
    tag is present. For non-Amazon URLs, leave untouched (raw URL).
    """
    if not url:
        return url
    try:
        u = urlparse(url)
        host = u.netloc.lower()

        # Skip Amazon shortener; we can't append query to amzn.to
        if "amzn.to" in host:
            return url

        if "amazon." not in host:
            return url

        if not AMAZON_TAG:
            return url

        q = dict(parse_qsl(u.query, keep_blank_values=True))
        if q.get("tag") != AMAZON_TAG:
            q["tag"] = AMAZON_TAG

        new_u = u._replace(query=urlencode(q, doseq=True))
        return urlunparse(new_u)
    except Exception:
        return url

def rewrite_affiliate_links_in_text(text: str) -> str:
    """
    Finds all URLs in free-form text and applies _ensure_amazon_tag to each.
    Non-Amazon links remain raw. Returns the updated text.
    """
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        return _ensure_amazon_tag(m.group(0))

    return _URL_RE.sub(_repl, text)

# Back-compat for existing imports
def convert_to_geniuslink(url: str) -> str:
    """
    Temporary shim: until Skimlinks/other programs are live, we only
    affiliate Amazon links (via tag). Everything else is returned raw.
    """
    return _ensure_amazon_tag(url)

# New, clearer name if you want to use it:
convert_to_affiliate_link = convert_to_geniuslink
