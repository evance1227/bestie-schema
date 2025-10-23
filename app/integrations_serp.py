# app/integrations_serp.py
from __future__ import annotations

import os
import re
import requests
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from loguru import logger
from typing import List, Dict, Set

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
LENS_ENDPOINT = "https://serpapi.com/search.json"

# Retailers we prefer to show first
PREFERRED_ORDER: List[str] = [
    "anthropologie.com",  # NEW: high-priority
    "revolve.com",
    "shopbop.com",
    "nordstrom.com",
    "freepeople.com",
    "asos.com",
    "bloomingdales.com",
    "saksfifthavenue.com",
    "boohoo.com",
]

# Keep Amazon out of IMAGE results unless explicitly allowed upstream
EXCLUDE_DOMAINS_DEFAULT: Set[str] = {
    "amazon.com", "amzn.to", "amazon.co.uk", "amazon.ca", "amazon.de",
    "amazon.co", "amazon.com.au"
}

# Only check “sold out” for these hosts (fast, safe subset)
CHECK_STOCK_HOSTS: Set[str] = {
    "anthropologie.com", "revolve.com", "shopbop.com", "nordstrom.com",
    "freepeople.com", "asos.com", "bloomingdales.com", "saksfifthavenue.com",
}

_STOCK_TOKENS = (
    "sold out", "sold-out", "out of stock", "out-of-stock",
    "no longer available", "unavailable", "currently unavailable"
)

_UA = "BestieBot/1.0 (+https://bestie)"
_TIMEOUT = 3.5   # seconds
_MAX_STOCK_CHECKS = 6  # cap network calls for speed

import os, re, json, time, html, urllib.parse
import requests
from typing import List, Optional

RAINFOREST_API_KEY = os.getenv("RAINFOREST_API_KEY", "")

# --- Helpers ----
def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)

def _first_match(patterns: List[re.Pattern], html_text: str) -> Optional[str]:
    for pat in patterns:
        m = pat.search(html_text)
        if m:
            return html.unescape(m.group(1))
    return None

# --- Amazon PDP via Rainforest ------------------------------------------------
def _amazon_pdp(query: str) -> str:
    if not RAINFOREST_API_KEY or not query.strip():
        return ""
    try:
        r = requests.get(
            "https://api.rainforestapi.com/request",
            params={
                "api_key": RAINFOREST_API_KEY,
                "type": "search",
                "amazon_domain": "amazon.com",
                "search_term": query,
            },
            timeout=7,
        )
        data = r.json() if r.ok else {}
        for item in (data.get("search_results") or [])[:8]:
            asin = item.get("asin")
            if asin:
                return f"https://www.amazon.com/dp/{asin}"
    except Exception:
        pass
    return ""

# --- SYL merchants: fetch search HTML and grab first product card -------------
# We use your env SYL_SEARCH_TEMPLATES_JSON for the search URL pattern.
_SYL_TEMPLATES = {}
try:
    _SYL_TEMPLATES = json.loads(os.getenv("SYL_SEARCH_TEMPLATES_JSON", "{}"))
except Exception:
    _SYL_TEMPLATES = {}

_SELECTORS = {
    # domain -> list of (regex) patterns to capture first product HREF
    # We keep this regex-based to avoid extra parser deps.
    "revolve.com": [
        re.compile(r'<a[^>]+href="([^"]+/product[^"]+)"[^>]*>'),   # product.jsp or /product…
        re.compile(r'<a[^>]+href="(/r/[^"]+/[^"]+/[^"]+)"'),       # /r/brand/…/p/…
    ],
    "freepeople.com": [
        re.compile(r'<a[^>]+class="c-product-card__image-link"[^>]+href="([^"]+)"'),
        re.compile(r'<a[^>]+href="(/[^"]+/products/[^"]+)"'),
    ],
    "nordstrom.com": [
        re.compile(r'<a[^>]+data-testid="product-card-link"[^>]+href="([^"]+)"'),
        re.compile(r'<a[^>]+href="(/s/[^"]+/\d+)"'),
    ],
    "shopbop.com": [
        re.compile(r'<a[^>]+class="product-img-link"[^>]+href="([^"]+)"'),
        re.compile(r'<a[^>]+href="(/product_detail/[^"]+)"'),
    ],
}

def _syl_pdp(query: str, domains: Optional[List[str]]) -> str:
    if not query.strip():
        return ""
    doms = [d.lower() for d in (domains or []) if d]
    for dom in doms:
        tmpl = _SYL_TEMPLATES.get(dom)
        if not tmpl:
            continue
        try:
            url = tmpl.format(q=urllib.parse.quote_plus(query))
            resp = requests.get(url, timeout=7, headers={"User-Agent":"Mozilla/5.0"})
            if not resp.ok:
                continue
            text = resp.text
            for pat in _SELECTORS.get(dom, []):
                href = _first_match([pat], text)
                if href:
                    return _abs(f"https://{dom}", href)
        except Exception:
            continue
    return ""

# --- Public: find PDP url -----------------------------------------------------
def find_pdp_url(query: str, domains: Optional[List[str]] = None) -> str:
    """
    Return a BUY-NOW PDP URL for the given query.
    - If domains include amazon.com (or domains is None), try Amazon via Rainforest.
    - If specific SYL merchant domains are provided, try to resolve a PDP on those.
    Returns "" if nothing found quickly.
    """
    q = (query or "").strip()
    if not q:
        return ""

    doms = [d.lower() for d in (domains or []) if d] if domains else []
    # Try Amazon unless caller explicitly constrains to non-Amazon
    if not doms or any(d.endswith("amazon.com") for d in doms):
        pdp = _amazon_pdp(q)
        if pdp:
            return pdp

    if doms:
        pdp = _syl_pdp(q, doms)
        if pdp:
            return pdp

    return ""

def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _clean_url(url: str) -> str:
    """Normalize retailer URLs and strip tracking params."""
    try:
        # normalize some mobile URLs
        if "revolve.com/mobile/" in url:
            url = url.replace("revolve.com/mobile/", "revolve.com/")

        u = urlparse(url)
        disallowed = {
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "utm_id", "_ga", "_gid", "_gl", "gclid", "fbclid", "mc_cid", "mc_eid"
        }
        q = parse_qs(u.query, keep_blank_values=True)
        q = {k: v for k, v in q.items() if k not in disallowed}
        new_q = urlencode([(k, vv) for k, vals in q.items() for vv in vals])
        return urlunparse((u.scheme, u.netloc, u.path, "", new_q, ""))  # drop fragment
    except Exception:
        return url


def _score_host(host: str) -> int:
    try:
        return PREFERRED_ORDER.index(host)
    except ValueError:
        return 999


def _is_probably_sold_out(url: str, host: str) -> bool:
    """
    Cheap “sold out” detector:
    - Only run for known hosts (CHECK_STOCK_HOSTS)
    - Fetch small HTML and look for common phrases
    - Hard timeout + short read
    """
    if host not in CHECK_STOCK_HOSTS:
        return False
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, stream=True)
        # read up to ~80KB
        chunk = b""
        for i, part in enumerate(r.iter_content(8192)):
            chunk += part
            if len(chunk) > 80_000 or i > 9:
                break
        text = (chunk or b"").decode("utf-8", errors="ignore").lower()
        return any(tok in text for tok in _STOCK_TOKENS)
    except Exception:
        # if we can't tell, don't exclude
        return False
    
_AMAZON_DP_RE = re.compile(r"https?://(?:www\.)?amazon\.com/(?:gp/product|dp)/[A-Z0-9]{8,12}")

def find_pdp_url(name: str) -> str:
    """
    Minimal stub: if your sources already attach a candidate list on the product dict,
    prefer any Amazon dp/gp URL. Otherwise, return "" to let best_link fall back to search.
    Replace this with your real SERP lookup when ready.
    """
    return ""  # Implement with your SERP integration when available

def lens_products(
    image_url: str,
    allowed_domains: List[str] | None = None,
    topn: int = 8,
    exclude_domains: Set[str] | None = None,
    filter_sold_out: bool = True,
) -> List[Dict]:
    """
    Use SerpAPI Google Lens to fetch visually similar shopping links.
    Returns: [{ "title": str, "url": str, "host": str, "thumbnail": str }]
    """
    if not SERPAPI_KEY or not image_url:
        return []

    if exclude_domains is None:
        exclude_domains = EXCLUDE_DOMAINS_DEFAULT

    params = {"engine": "google_lens", "url": image_url, "api_key": SERPAPI_KEY}
    try:
        r = requests.get(LENS_ENDPOINT, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("SerpAPI error: {}", e)
        return []

    items = (
        data.get("visual_matches")
        or data.get("image_results")
        or data.get("similar_images")
        or []
    )

    results: List[Dict] = []
    seen: Set[str] = set()

    for it in items:
        url = it.get("link") or it.get("source") or it.get("original") or it.get("thumbnail")
        if not url:
            continue
        url = _clean_url(url)
        host = _host(url)
        if not host:
            continue

        if allowed_domains and host not in {d.lower() for d in allowed_domains}:
            continue
        if host in exclude_domains:
            continue
        if url in seen:
            continue

        seen.add(url)
        title = (it.get("title") or it.get("snippet") or host).strip()
        results.append({
            "title": title,
            "url": url,
            "host": host,
            "thumbnail": it.get("thumbnail") or "",
        })

    # Sort by retailer preference, then shorter title (usually cleaner)
    results.sort(key=lambda d: (_score_host(d["host"]), len(d["title"])))

    # Optional: remove "sold out" items (quick sniff, limited checks)
    if filter_sold_out and results:
        kept: List[Dict] = []
        checks = 0
        for c in results:
            if checks < _MAX_STOCK_CHECKS:
                if _is_probably_sold_out(c["url"], c["host"]):
                    checks += 1
                    continue
                checks += 1
            kept.append(c)
            if len(kept) >= topn:
                break
        # If filtering nuked everything, fall back to original
        if kept:
            results = kept

    return results[:max(1, topn)]

# --- PDP resolver (Amazon via Rainforest). If a non-Amazon domain is forced, skip Amazon. ---
def find_pdp_url(name: str, domains: list[str] | None = None) -> str:
    """
    Return a BUY-NOW product detail page (PDP) URL.
    - Amazon: use Rainforest API to get the first ASIN → https://www.amazon.com/dp/<ASIN>
    - If domains are provided and none is amazon.com, we skip the Amazon step.
    """
    name = (name or "").strip()
    doms = [d.lower() for d in (domains or []) if d]

    # If caller constrained to non-Amazon merchants, don't try Amazon.
    try_amazon = (not doms) or any(d.endswith("amazon.com") for d in doms)
    if not name:
        return ""

    if try_amazon:
        api_key = os.getenv("RAINFOREST_API_KEY", "")
        if not api_key:
            return ""
        try:
            resp = requests.get(
                "https://api.rainforestapi.com/request",
                params={
                    "api_key": api_key,
                    "type": "search",
                    "amazon_domain": "amazon.com",
                    "search_term": name,
                },
                timeout=7,
            )
            data = resp.json() if resp.ok else {}
            results = (data.get("search_results") or [])[:6]
            for r in results:
                asin = r.get("asin")
                if asin:
                    return f"https://www.amazon.com/dp/{asin}"
        except Exception:
            pass

    # Nothing resolved
    return ""

