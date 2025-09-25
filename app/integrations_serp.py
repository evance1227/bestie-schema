# app/integrations_serp.py
from __future__ import annotations

import os
import requests
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from loguru import logger

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
LENS_ENDPOINT = "https://serpapi.com/search.json"

# Preferred retailers first
PREFERRED_ORDER = [
    "revolve.com", "shopbop.com", "nordstrom.com", "freepeople.com",
    "asos.com", "bloomingdales.com", "saksfifthavenue.com", "boohoo.com",
    "shein.com",
]

# Domains to exclude entirely (prevents random Amazon links)
EXCLUDE_DOMAINS_DEFAULT = {
    "amazon.com", "amzn.to", "amazon.co.uk", "amazon.ca", "amazon.de",
    "amazon.co", "amazon.com.au"
}


def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _clean_url(url: str) -> str:
    """Strip common tracking params; keep canonical path/query."""
    try:
        u = urlparse(url)
        disallowed = {
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "utm_id", "_ga", "_gid", "_gl", "gclid", "fbclid", "mc_cid", "mc_eid"
        }
        q = parse_qs(u.query, keep_blank_values=True)
        q = {k: v for k, v in q.items() if k not in disallowed}
        new_q = urlencode([(k, vv) for k, vals in q.items() for vv in vals])
        return urlunparse((u.scheme, u.netloc, u.path, "", new_q, ""))
    except Exception:
        return url


def _score_host(host: str) -> int:
    try:
        return PREFERRED_ORDER.index(host)
    except ValueError:
        return 999


def lens_products(
    image_url: str,
    allowed_domains: list[str] | None = None,
    topn: int = 8,
    exclude_domains: set[str] | None = None,
) -> list[dict]:
    """
    Use SerpAPI's Google Lens to fetch visually similar shopping links.
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

    # Different payloads use different keys; try the common ones
    items = (
        data.get("visual_matches")
        or data.get("image_results")
        or data.get("similar_images")
        or []
    )

    results: list[dict] = []
    seen: set[str] = set()

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

    # Prefer known retailers, then shorter titles (usually cleaner matches)
    results.sort(key=lambda d: (_score_host(d["host"]), len(d["title"])))
    return results[:max(1, topn)]
