# app/linkwrap.py
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from loguru import logger

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

# 1) Flatten Markdown links to plain URLs for SMS
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

def sms_ready_links(text: str) -> str:
    """
    Convert [label](url) to bare url so SMS apps auto-link it.
    Leaves non-markdown URLs as-is.
    """
    if not text:
        return ""
    text = _MD_LINK.sub(r"\2", text)
    # remove stray empty parentheses and collapse whitespace
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()

# 2) Ensure Amazon affiliate tag is present once, with correct separator
_AMAZON_HOSTS = {
    "amazon.com", "www.amazon.com", "smile.amazon.com",
    "m.amazon.com", "a.co"  # a.co expands server-side; we cannot rewrite it reliably
}
def _ensure_amazon_tag(url: str, tag: str) -> str:
    """
    If url is an amazon.com product URL and lacks ?tag=, append tag safely.
    If tag exists, do not duplicate it. Works with existing query strings.
    """
    try:
        u = urlparse(url)
        if u.netloc.lower() not in _AMAZON_HOSTS:
            return url
        # do not touch a.co short links
        if u.netloc.lower() == "a.co":
            return url
        qs = dict(parse_qsl(u.query, keep_blank_values=True))
        if "tag" not in qs and tag:
            qs["tag"] = tag
            new_q = urlencode(qs, doseq=True)
            return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
        return url
    except Exception:
        return url
# 3) Walk all URLs in a text blob and apply _ensure_amazon_tag
_URL = re.compile(r"https?://[^\s)]+", re.I)

def enforce_affiliate_tags(text: str, amazon_tag: str) -> str:
    """
    Finds URLs in the text and returns text with Amazon URLs tagged.
    """
    def repl(m):
        url = m.group(0)
        return _ensure_amazon_tag(url, amazon_tag)
    return _URL.sub(repl, text or "")
# 4) One composer for SMS outputs
def make_sms_reply(reply: str, amazon_tag: str = "schizobestie-20") -> str:
    """
    Ensure reply is SMS-safe and append Amazon affiliate tag when missing.
    Called from workers.py during _finalize_and_send.
    """
    try:
        t = (reply or "")

        # 1) strip basic markdown emphasis that renders ugly in SMS
        t = t.replace("*", "").replace("_", "")

        # 2) flatten markdown links -> bare URLs (if helper exists)
        if "sms_ready_links" in globals():
            t = sms_ready_links(t)

        # 3) enforce Amazon tag on any amazon.com URLs (if helper exists)
        if "enforce_affiliate_tags" in globals():
            t = enforce_affiliate_tags(t, amazon_tag)

        # 4) collapse multiple spaces/newlines
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t
    except Exception as e:
        logger.warning("[Linkwrap] make_sms_reply failed: {}", e)
        return reply

