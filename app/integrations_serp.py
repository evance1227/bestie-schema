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
# app/integrations_serp.py
import os, requests

def find_pdp_url(name: str, domains: list[str] | None = None) -> str:
    """
    Try to return a BUY-NOW PDP URL.
    - For Amazon: use Rainforest API search → first ASIN → https://www.amazon.com/dp/<asin>
    - For non-Amazon merchants (Revolve/FP/Nordstrom): return "" for now (we’ll keep SYL search)
    """
    name = (name or "").strip()
    doms = [d.lower() for d in (domains or [])]

    # If the user asked for specific merchants and none is amazon.com, skip Amazon.
    if doms and not any(d.endswith("amazon.com") for d in doms):
        return ""

    api_key = os.getenv("RAINFOREST_API_KEY", "")
    if not api_key or not name:
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
    return ""
