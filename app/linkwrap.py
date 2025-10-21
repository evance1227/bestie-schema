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
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, quote, quote_plus, unquote
import urllib.parse
import logging
logger = logging.getLogger(__name__)



# =========================
# ENV
# =========================

# --- ShopYourLikes / ShopMy ---
SYL_ENABLED       = (os.getenv("SYL_ENABLED") or "0").strip().lower() in ("1", "true", "yes")
SYL_PUBLISHER_ID  = (os.getenv("SYL_PUBLISHER_ID") or "").strip()

# One canonical redirect template (NO profile links). If your account uses a different
# base, set SYL_WRAP_TEMPLATE in Render to that pattern; keep {pub} and {url}.
SYL_WRAP_TEMPLATE = (
    os.getenv("SYL_WRAP_TEMPLATE")
    or "https://go.shopmy.us/p-{pub}?url={url}"
).strip()

SYL_RETAILERS = [
    d.strip().lower()
    for d in (os.getenv("SYL_RETAILERS") or "*").split(",")
    if d.strip()
]

SYL_DENYLIST = [
    d.strip().lower()
    for d in (
        os.getenv("SYL_DENYLIST")
        or "geni.us,gumroad.com,bit.ly,tinyurl.com,rstyle.me,ltk.app.link,like2know.it"
    ).split(",")
    if d.strip()
]

# Hosts that we will NEVER wrap via SYL (send raw retailer URL instead)
SYL_SKIP_HOSTS = {
    d.strip().lower()
    for d in (os.getenv("SYL_SKIP_HOSTS") or "").split(",")
    if d.strip()
}

logging.info(
    "[SYL] template=%s merchants=%s deny=%s",
    SYL_WRAP_TEMPLATE, SYL_RETAILERS, SYL_DENYLIST
)

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
# --- Legacy SYL link normalizer (hotfix) ---
# --- Legacy SYL link normalizer (hotfix) ---
import re
import json, os, urllib.parse, http.client

def _load_allowed():
    doms = os.getenv("SYL_ALLOWED_DOMAINS", "")
    allowed = [d.strip().lower() for d in doms.split(",") if d.strip()]
    tmpl = os.getenv("SYL_SEARCH_TEMPLATES_JSON", "{}")
    try:
        templates = {k.lower(): v for k, v in json.loads(tmpl).items()}
    except Exception:
        templates = {}
    return set(allowed), templates

ALLOWED_SYL_DOMAINS, SYL_TEMPLATES = _load_allowed()

def _head_ok(url: str, timeout=5) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(parts.netloc, timeout=timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("HEAD", path)
        r = conn.getresponse()
        return 200 <= r.status < 400
    except Exception:
        return False

def _amazon_search(query: str) -> str:
    return f"https://www.amazon.com/s?k={urllib.parse.quote_plus(query)}"

def _syl_search(domain: str, query: str) -> str | None:
    domain = domain.lower()
    if domain in ALLOWED_SYL_DOMAINS and domain in SYL_TEMPLATES:
        return SYL_TEMPLATES[domain].format(q=urllib.parse.quote_plus(query))
    return None

def _is_allowed(domain: str) -> bool:
    d = domain.lower()
    return d.endswith("amazon.com") or any(d.endswith(x) for x in ALLOWED_SYL_DOMAINS)

def _wrap(url: str, cfg) -> str:
    # Prefer your existing wrappers; no raw links.
    if getattr(cfg, "GENIUSLINK_ENABLED", False):
        return cfg.genius.wrap(url)          # your existing shortener
    if "amazon." in url and getattr(cfg, "AMAZON_ASSOCIATE_TAG", ""):
        parts = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        qs["tag"] = [cfg.AMAZON_ASSOCIATE_TAG]
        new_q = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))
    if getattr(cfg, "SYL_ENABLED", False):
    # TEMP: skip SYL for preferred merchant flows to avoid bad redirect
        skip_env = (os.getenv("SYL_TEMP_SKIP_DOMAINS", "") or "").lower().split(",")
        skip = {d.strip() for d in skip_env if d.strip()}
        try:
            host = urllib.parse.urlsplit(url).netloc.lower()
        except Exception:
            host = ""
        if host and host in skip:
            return url  # send direct merchant link until SYL is fixed
        return cfg.syl.wrap(url)
            # your existing SYL wrapper (shop-links.co etc.)
    return url  # last resort; but with our config this should rarely trigger.

# app/linkwrap.py

import urllib.parse, http.client, os

def _head_ok(url: str, timeout=6) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(p.netloc, timeout=timeout)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        conn.request("HEAD", path)
        r = conn.getresponse()
        return 200 <= r.status < 400
    except Exception:
        return False

def _amazon_search(query: str) -> str:
    return f"https://www.amazon.com/s?k={urllib.parse.quote_plus(query)}"

def _is_allowed_host(host: str) -> bool:
    host = host.lower()
    if host.endswith("amazon.com"):
        return True
    # SYL merchants are allowed via your wrapper; use the env list if present
    allowed = (os.getenv("SYL_MERCHANTS", "") or "")
    return allowed.strip() == "*"  # you run with "*" today

def _looks_like_pdp(host: str, path: str) -> bool:
    host = host.lower()
    path = path.lower()
    if host.endswith("amazon.com"):
        return ("/dp/" in path) or ("/gp/product/" in path)
    if host.endswith("sephora.com"):
        return "/product/" in path
    if host.endswith("ulta.com"):
        return "/p/" in path
    if host.endswith("nordstrom.com"):
        return "/s/" in path
    if host.endswith("dermstore.com"):
        return "/product_" in path
    return False

def _wrap(url: str, *, cfg) -> str:
    # Prefer your existing shorteners/wrappers
    if getattr(cfg, "GENIUSLINK_ENABLED", False):
        return cfg.genius.wrap(url)

    if "amazon." in url and getattr(cfg, "AMAZON_ASSOCIATE_TAG", ""):
        parts = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        qs["tag"] = [cfg.AMAZON_ASSOCIATE_TAG]
        new_q = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))

    if getattr(cfg, "SYL_ENABLED", False):
        # TEMP: skip SYL for specific merchants to avoid the bad redirect
        skip_env = (os.getenv("SYL_TEMP_SKIP_DOMAINS", "") or "").lower().split(",")
        skip = {d.strip() for d in skip_env if d.strip()}
        try:
            host = urllib.parse.urlsplit(url).netloc.lower()
        except Exception:
            host = ""

        # <<< PASTE THESE THREE LINES RIGHT HERE >>>
        if host and host in skip:
            logger.info("[Affil] SYL skip host=%s url=%s", host, url)
            return url  # send direct merchant link until SYL is fixed

        # otherwise use your SYL wrapper (shopmy)
        return cfg.syl.wrap(url)

    # Last resort (should rarely trigger with your config)
    return url

# ----------------- Merchant-first link chooser ----------------- #
def best_link(
    query: str,
    candidates: list[str] | None,
    *,
    cfg,
    preferred_domains: list[str] | None = None,
) -> str:
    """
    Pick the monetization-best link:
      1) If the user named retailers (preferred_domains), stay on those (SYL) – no Amazon fallback.
      2) Otherwise evaluate SYL merchant search vs Amazon and choose by AFFIL_STRATEGY:
         - syl-first | amazon-first | max-revenue
    PDP candidates always win when live.
    """
    preferred = [d.strip().lower() for d in (preferred_domains or [])]
    prefer_only = len(preferred) > 0

    strategy = (os.getenv("AFFIL_STRATEGY") or "max-revenue").strip().lower()
    # rough basis points; tune in env to reflect your real rev shares
    syl_bps = int(os.getenv("SYL_BPS", "1200"))       # 12%
    amz_bps = int(os.getenv("AMAZON_BPS", "300"))     # 3%

    def _host(u: str) -> str:
        try:
            return urllib.parse.urlsplit(u).netloc.lower()
        except Exception:
            return ""

    def _host_ok(h: str) -> bool:
        return _is_allowed_host(h) and (not prefer_only or any(h.endswith(d) for d in preferred))

    # (0) PDP candidate wins if it matches host policy and is live
    for cand in candidates or []:
        if not cand:
            continue
        u = cand.strip().strip("<>")
        h = _host(u)
        if _host_ok(h) and _looks_like_pdp(h, urllib.parse.urlsplit(u).path or "/") and _head_ok(u):
            return _wrap(u, cfg=cfg)

    # Build SYL retailer searches we could send (if any)
    syl_options: list[str] = []
    if prefer_only:
        for dom in preferred:
            srch = _retailer_search_url(dom, query)
            if srch:
                syl_options.append(srch)
    else:
        # When no preferred merchants were named, consider a few common SYL retailers from ENV
        try:
            import json
            tmpl_map = json.loads(os.getenv("SYL_SEARCH_TEMPLATES_JSON", "{}"))
            # light heuristic: try Revolve, Free People, Nordstrom first if present
            for key in ("revolve.com","freepeople.com","nordstrom.com","shopbop.com","sephora.com","ulta.com","dermstore.com"):
                if key in tmpl_map:
                    syl_options.append(tmpl_map[key].format(q=urllib.parse.quote_plus(query)))
        except Exception:
            pass

    # Any allowed/live candidate (non-PDP) that matches the host policy
    for cand in candidates or []:
        u = cand.strip().strip("<>")
        if _host_ok(_host(u)) and _head_ok(u):
            return _wrap(u, cfg=cfg)

    # If user constrained merchants: return first SYL option (or "")
    if prefer_only:
        if syl_options:
            return _wrap(syl_options[0], cfg=cfg)
        return ""  # do not leak to Amazon when merchant was explicitly requested

    # No constraint → pick by strategy among SYL vs Amazon search
    amz = _amazon_search(query)
    syl = syl_options[0] if syl_options else ""

    def _score(url: str) -> int:
        # crude scoring by expected rev share; PDP candidates were handled earlier
        if not url:
            return -1
        h = _host(url)
        return syl_bps if h and "amazon." not in h else amz_bps

    if strategy == "syl-first":
        return _wrap(syl or amz, cfg=cfg)
    if strategy == "amazon-first":
        return _wrap(amz or syl, cfg=cfg)
    # max-revenue (default)
    return _wrap((syl if _score(syl) >= _score(amz) else amz), cfg=cfg)

def _retailer_search_url(domain: str, query: str) -> str | None:
    """
    Build a retailer site-search URL using SYL search templates from ENV.
    Env: SYL_SEARCH_TEMPLATES_JSON = {"revolve.com":"https://www.revolve.com/r/Search.jsp?searchBy=All&searchQuery={q}", ...}
    """
    try:
        import json
        tmpl_map = json.loads(os.getenv("SYL_SEARCH_TEMPLATES_JSON", "{}"))
        for k, fmt in tmpl_map.items():
            if domain.endswith(k) or k.endswith(domain):
                return fmt.format(q=urllib.parse.quote_plus(query))
    except Exception:
        pass
    return None

def normalize_syl_links(text: str) -> str:
    """
    Rewrite old-style SYL links to the canonical go.shopmy.us pattern.
    Examples:
      https://go.sylikes.com/redirect?publisher_id=729877&url=https%3A%2F%2Fwww.freepeople.com%2F...
      https://go.sylikes.com/redirect?publisher_id=729877&url=https://www.freepeople.com/...
    Output:
      https://go.shopmy.us/p-<pub>?url=<raw retailer url>
    """
    pattern = re.compile(r"https?://go\.sylikes\.com/redirect\?publisher_id=(\d+)&url=([^\s\)\]]+)")
    def _repl(m):
        pub = m.group(1)
        url_param = m.group(2)
        try:
            parsed = urlparse("http://x/x?" + "url=" + url_param)
            q = parse_qs(parsed.query)
            retailer_url = q.get("url", [url_param])[0]
        except Exception:
            retailer_url = url_param
        retailer_url = unquote(retailer_url)
        return f"https://go.shopmy.us/p-{pub}?url={retailer_url}"
    return pattern.sub(_repl, text)

def build_amazon_search_url(query: str) -> str:
    """
    Build an Amazon search URL that is ALWAYS tagged with our associate ID.
    """
    q = urllib.parse.quote_plus((query or "").strip())
    tag = os.getenv("AMAZON_ASSOCIATE_TAG", "").strip()
    if not tag:
        raise RuntimeError("AMAZON_ASSOCIATE_TAG is required for Amazon links")
    return f"https://www.amazon.com/s?k={q}&tag={tag}"

_AFFIL_PARAMS = {
    # generic
    "utm_source","utm_medium","utm_campaign","utm_content","utm_term","utm_id",
    "_ga","_gid","_gl","gclid","fbclid","mc_cid","mc_eid",
    # retailer/affiliate networks frequently seen in the wild
    "utm_channel","cm_mmc","cm_mmc1","cm_mmc2","rfsn","irgwc","aff","affid",
    "ranMID","ranEAID","cjevent","epik","mbid","ncid"
}

def _strip_affiliate_params(retailer_url: str) -> str:
    try:
        u = urlparse(retailer_url)
        q = parse_qs(u.query, keep_blank_values=True)
        q = {k: v for k, v in q.items() if k.lower() not in _AFFIL_PARAMS}
        new_q = urlencode([(k, vv) for k, vals in q.items() for vv in vals])
        # drop fragments; keep normalized path (handles Nordstrom /s/... nicely)
        return urlunparse((u.scheme, u.netloc, u.path, "", new_q, ""))
    except Exception:
        return retailer_url


def _amz_search_url(query: str) -> str:
    """
    Build a clean Amazon search link (not a dp/ASIN deep link).
    Example: https://www.amazon.com/s?k=sea+salt+spray&tag=YOURTAG-20
    """
    q = quote_plus((query or "").strip())
    if not q:
        q = "best match"
    tag = (os.getenv("AMAZON_ASSOCIATE_TAG") or "").strip()
    base = f"https://www.amazon.com/s?k={q}"
    return f"{base}&tag={tag}" if tag else base

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
# --- retailer routing map (pattern -> (merchant_key, search_url_format)) ---
_RETAILER_ROUTES = [
    (r"(?i)\bsephora\b",        ("sephora.com",        "https://www.sephora.com/search?keyword={q}")),
    (r"(?i)\bultra\b|\bulta\b", ("ulta.com",           "https://www.ulta.com/search?Ntt={q}")),
    (r"(?i)\bnordstrom\b",      ("nordstrom.com",      "https://www.nordstrom.com/sr?keyword={q}")),
    (r"(?i)\btarget\b",         ("target.com",         "https://www.target.com/s?searchTerm={q}")),
    # nice-to-haves you asked for:
    (r"(?i)\banthropologie\b|\banthro\b",
                                ("anthropologie.com",  "https://www.anthropologie.com/search?q={q}")),
    (r"(?i)\bfree\s*people\b|\bfreepeople\b",
                                ("freepeople.com",     "https://www.freepeople.com/s?query={q}")),
]

def _syl_search_url(name: str, user_text: str) -> str:
    """
    Build a ShopYourLikes redirect for a specific retailer implied by the user's text.
    Returns "" if no retailer was requested or SYL is disabled.
    """
    # SYL is off or not configured
    if not (SYL_ENABLED and SYL_PUBLISHER_ID and SYL_WRAP_TEMPLATE):
        return ""

    # Explicit "amazon" -> we don't SYL-wrap Amazon (we use _amz_search_url instead)
    if re.search(r"(?i)\bamazon\b", user_text or ""):
        return ""

    q = quote((name or "").strip(), safe="")

    retailer_url = ""
    for pat, (merchant_key, fmt) in _RETAILER_ROUTES:
        if re.search(pat, user_text or ""):
            # honor allowlist/denylist logic just like other wrappers
            domain = urlparse(fmt).netloc
            if not _should_syl(domain):
                return ""
            retailer_url = fmt.format(q=q)
            break

    if not retailer_url:
        return ""  # nothing matched -> skip SYL alt

    # Produce the redirect using your ENV template (or default)
    return SYL_WRAP_TEMPLATE.format(
        pub=SYL_PUBLISHER_ID,
        url=quote(retailer_url, safe="")
    )



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
        return GENIUSLINK_WRAP.format(url=quote(u, safe=''))


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

log = logging.getLogger("linkwrap")

def _syl_should_wrap_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host in SYL_DENYLIST:
            return False
        if host in SYL_SKIP_HOSTS:
            return False
        return True
    except Exception:
        return True

def _wrap_syl(url: str) -> str:
    """
    Wrap non-Amazon retailer URLs with SYL using the canonical template.
    If wrapping appears unsafe (shortener/denylist, broken response, or the
    wrapped link resolves to a different retailer host), fall back to the
    raw retailer URL. Amazon is handled elsewhere (never SYL-wrapped here).
    """
    if not (SYL_ENABLED and SYL_PUBLISHER_ID):
        return url

    # 1) Normalize the retailer URL first (strip UTM/affiliate junk)
    url = _strip_affiliate_params(url)

    # 2) Basic guards: empty host, Amazon, or denylisted shorteners → send raw
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        host = ""
    if not host:
        return url
    if "amazon." in host:               # Amazon is wrapped by the Amazon helper elsewhere
        return url
    if host in SYL_DENYLIST:            # e.g., geni.us, bit.ly, rstyle.me, ltk.app.link, like2know.it
        return url

    # 3) Canonical SYL template (fix any legacy/redirect templates)
    tpl = (SYL_WRAP_TEMPLATE or "https://go.shopmy.us/p-{pub}?url={url}").strip()
    if "sylikes.com" in tpl or "redirect?publisher_id" in tpl or "/p-" not in tpl:
        tpl = "https://go.shopmy.us/p-{pub}?url={url}"
    syl_url = tpl.format(pub=SYL_PUBLISHER_ID, url=url)   # raw url; SYL will encode

    # 4) Fast validation (optional; default ON via SYL_VALIDATE=1):
    #    accept SYL only if the wrapped link resolves to the same retailer host.
    if (os.getenv("SYL_VALIDATE", "1").lower() in ("1", "true", "yes")):
        def _norm(h: str) -> str:
            h = (h or "").lower()
            return h[4:] if h.startswith("www.") else h

        try:
            import requests  # local import to avoid a hard module dep at import time

            # HEAD first (cheap); follow redirects
            r = requests.head(syl_url, allow_redirects=True, timeout=3)
            if r.status_code >= 400:
                return url
            final_host = _norm(urlparse(r.url).netloc)

            if _norm(host) != final_host:
                # Some retailers don’t implement HEAD well; try one GET as a fallback
                r2 = requests.get(syl_url, allow_redirects=True, timeout=4)
                if r2.status_code >= 400:
                    return url
                final_host = _norm(urlparse(r2.url).netloc)
                if _norm(host) != final_host:
                    return url  # mismatched retailer → send raw
        except Exception:
            return url  # any network hiccup → send raw

    # 5) Looks good → use SYL
    try:
        log.info("[SYL] tpl=%s dest=%s => %s", tpl, url, syl_url)
    except Exception:
        pass
    return syl_url

def _wrap_url(url: str) -> str:
    """
    Choose the right wrapper for a single URL:

      - empty / denylist          -> raw
      - Google/Maps/YouTube      -> raw
      - Amazon                   -> _wrap_amazon (handled elsewhere)
      - everything else          -> try _wrap_syl (it validates and falls back to raw)

    The new _wrap_syl() will:
      * strip affiliate/UTM params
      * skip shorteners/denylist
      * optionally validate the wrapped link (SYL_VALIDATE=1)
      * return the raw retailer URL on any risk
    """
    if not url:
        return url
    if _is_denied(url):
        return url

    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]

        # NEW: don't re-wrap links that are already SYL
        if host.endswith("shopmy.us"):
            return url

        # skip obvious non-retail destinations
        if ("google." in host) or ("maps.google." in host) or ("youtu" in host):
            return url

        # Amazon uses its own wrapper (never SYL)
        if _AMAZON_HOST.search(host):
            return _wrap_amazon(url)

        # Non-Amazon retailers: let _wrap_syl decide; it will fall back to raw if unsafe
        return _wrap_syl(url)

    except Exception:
        return url

# =========================
# PUBLIC
# =========================

def wrap_all_affiliates(text: str) -> str:
    """
    Normalize outgoing links:
      - prefer SYL for premium retailers (sephora/ulta/revolve/etc.)
      - ensure Amazon links/searches are ALWAYS tagged with our associate ID
      - leave all other domains unchanged
    Works for both [markdown](url) and bare URLs in the text.
    """
    # Normalize any old SYL pattern to the canonical ShopMy URL first
    try:
        text = normalize_syl_links(text)  # rewrites go.sylikes.com → go.shopmy.us/p-<pub>?url=...
    except Exception:
        pass

    # Strip inline HTML attributes like target="_blank"> or target='_blank'>
    text = re.sub(r'\s*target\s*=\s*([\'"])_blank\1\s*>?', "", text, flags=re.I)


    PREMIUM_SYL_DOMAINS = (
        "sephora.com",
        "ulta.com",
        "revolve.com",
        "glossier.com",
        "anthropologie.com",
        "net-a-porter.com",
        "nordstrom.com",
    )

    amz_tag = os.getenv("AMAZON_ASSOCIATE_TAG", "").strip()

    def _domain(u: str) -> str:
        try:
            return urllib.parse.urlparse(u).netloc.lower()
        except Exception:
            return ""

    def _is_amazon(u: str) -> bool:
        d = _domain(u)
        return d.endswith("amazon.com") or d.endswith("amazon.co.uk") or d.endswith("amazon.ca")

    def _ensure_amazon_tag(u: str) -> str:
        # Add &tag= if missing (and if we have a tag configured)
        if not amz_tag:
            return u  # we won't mutate if tag isn't configured
        if "tag=" in u:
            return u
        sep = "&" if ("?" in u) else "?"
        return f"{u}{sep}tag={amz_tag}"

    def _should_syl(u: str) -> bool:
        d = _domain(u)
        return any(p in d for p in PREMIUM_SYL_DOMAINS)

    def _wrap_url(u: str) -> str:
        if not u or not u.startswith("http"):
            return u
        # premium retailers → wrap with SYL
        if _should_syl(u):
            return build_syl_redirect("premium", u)
        # Amazon → ensure tag on any link (search or product)
        if _is_amazon(u):
            return _ensure_amazon_tag(u)
        # otherwise unchanged
        return u

    # --- rewrite markdown links [label](url) ---
    def _md_repl(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return f"[{label}]({_wrap_url(url)})"

    out = _MD_LINK_RE.sub(_md_repl, text)

    # --- rewrite plain URLs ---
    def _plain_repl(m: re.Match) -> str:
        url = m.group(0)
        return _wrap_url(url)

    out = _URL_RE.sub(_plain_repl, out)
    return out

def build_syl_redirect(retailer: str, url: str) -> str:
    """
    Canonical ShopMy redirect: https://go.shopmy.us/p-<pub>?url=<encoded>
    """
    pub = os.getenv("SYL_PUBLISHER_ID", "").strip()
    if not pub:
        return url
    if "go.shopmy.us" in url:
        return url
    encoded = urllib.parse.quote_plus(url)
    return f"https://go.shopmy.us/p-{pub}?url={encoded}"

_URL_END_RE = re.compile(r"(https?://[^\s)]+)\s*$", re.I)

def ensure_not_link_ending(text: str) -> str:
    """
    If an SMS ends on a bare URL, add a neutral closer so carriers don't
    munge previews or eat the message.  No affiliate or AI call here.
    """
    if not text:
        return text
    if _URL_END_RE.search(text):
        return text.rstrip() + "\n"
    return text

# Legacy shim: single URL path (used by older call sites)
def convert_to_geniuslink(url: str) -> str:
    """
    Apply the same Amazon-tag logic that wrap_all_affiliates() uses, but for a single URL.
    (Kept for compatibility with older modules.)
    """
    return _append_amz_tag(url)
