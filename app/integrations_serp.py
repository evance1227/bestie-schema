# app/integrations_serp.py
from __future__ import annotations
import os
import requests
from urllib.parse import urlparse

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
LENS_ENDPOINT = "https://serpapi.com/search.json"

def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""

def lens_products(image_url: str, allowed_domains: list[str] | None = None, topn: int = 8) -> list[dict]:
    """
    Use SerpAPI's Google Lens engine to get visually similar products.
    Returns: [{title, url, host, thumbnail}]
    """
    if not SERPAPI_KEY:
        return []
    params = {
        "engine": "google_lens",
        "url": image_url,
        "api_key": SERPAPI_KEY,
    }
    r = requests.get(LENS_ENDPOINT, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = data.get("visual_matches") or []
    results: list[dict] = []
    for it in items:
        url = it.get("link") or it.get("source")
        if not url:
            continue
        domain = _host(url)
        if allowed_domains and domain not in allowed_domains:
            continue
        results.append({
            "title": it.get("title") or domain,
            "url": url,
            "host": domain,
            "thumbnail": it.get("thumbnail") or "",
        })
        if len(results) >= topn:
            break
    return results
