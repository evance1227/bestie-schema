from __future__ import annotations

import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

from loguru import logger
from app.amazon_api import search_amazon_products


def _normalize(items: List[Dict]) -> List[Dict]:
    """
    Normalize heterogeneous vendor rows into a single shape the rest of the app expects.
    Keys kept: title, name, url, review, merchant (+ meta passthrough).
    """
    out: List[Dict] = []
    for it in items or []:
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "name": (it.get("name") or title).strip(),
            "url": (it.get("url") or "").strip(),  # Do NOT wrap upstream. Leave clean.
            "review": (it.get("review") or "").strip(),
            "merchant": (it.get("merchant") or "amazon.com").strip(),
            "meta": it.get("meta") or {},
        })
    return out


def _fallback_from_query(query: str, max_items: int = 3) -> List[Dict]:
    """
    If APIs fail or env keys are missing, create name-only candidates.
    Link injection happens later in workers (_ensure_amazon_links) using the numbered format.
    """
    q = (query or "").strip()
    if not q:
        return []
    # Split simple multi-item asks: "A, B or C / D"
    seeds = [s.strip() for s in re.split(r"(?:,|/| and | or )", q) if s.strip()]
    if not seeds:
        seeds = [q]
    # Keep tidy names
    seeds = [s[:80] for s in seeds]
    return [{"title": s, "name": s, "url": "", "review": "", "merchant": "amazon.com"} for s in seeds[:max_items]]


def build_product_candidates(intent: Optional[Dict]) -> List[Dict]:
    """
    Main entry used by workers.py.
      - Accepts parsed intent dict (from ai_intent.extract_product_intent)
      - Calls Amazon search with constraints
      - Normalizes the output
      - Falls back to name-only candidates if needed (so Amazon search links can be injected later)
    """
    if not intent or intent.get("intent") != "find_products":
        return []

    query = (intent.get("query") or "").strip()
    constraints = intent.get("constraints") or {}
    if not query:
        logger.info("[ProductSearch] Missing query in intent; returning []")
        return []

    try:
        # Always try Amazon first for monetization coverage.
        rows = search_amazon_products(query, max_results=10, constraints=constraints)
        if rows:
            norm = _normalize(rows)
            logger.info("[ProductSearch] {} candidates from Amazon for '{}'", len(norm), query)
            return norm

        # Fallback: name-only candidates so the worker can inject safe Amazon search links.
        fallback = _fallback_from_query(query, max_items=int(constraints.get("count") or 3))
        logger.info("[ProductSearch] Amazon empty; using {} fallback seed(s) for '{}'", len(fallback), query)
        return fallback

    except Exception as e:
        logger.exception("[ProductSearch] Error building candidates: {}", e)
        return _fallback_from_query(query, max_items=int((intent.get("constraints") or {}).get("count") or 3))


def prefer_amazon_first(candidates: List[Dict]) -> List[Dict]:
    """
    Lightweight re-rank that brings Amazon DP links to the top and then sorts by URL presence and name length.
    """
    def _is_amazon(url: str) -> bool:
        try:
            return "amazon." in urlparse(url or "").netloc.lower()
        except Exception:
            return False

    def key(row: Dict) -> Tuple[int, int, int]:
        url = (row.get("url") or "").strip()
        name = (row.get("title") or row.get("name") or "").strip()
        is_amz = 0 if _is_amazon(url) else 1
        has_url = 0 if url else 1  # items with URLs first
        name_len = len(name)
        return (is_amz, has_url, name_len)

    return sorted(candidates or [], key=key)
