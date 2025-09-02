# app/product_search.py
from typing import List, Dict, Optional
from urllib.parse import urlparse
from math import log
import re

from loguru import logger
from app.amazon_api import search_amazon_products

STOPWORDS = {
    "the","a","an","for","with","and","or","to","of","on","in","by","at","from",
    "you","your","my","me","that","this","these","those","is","are","be","need",
    "cheap","cheaper","budget","under","less","expensive","not","insanely","please"
}

def _tokens(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in STOPWORDS}

def _overlap_score(query: str, title: str, snippet: str = "") -> float:
    q = _tokens(query)
    d = _tokens(f"{title} {snippet}")
    if not q or not d:
        return 0.0
    inter = len(q & d)
    # Jaccard-ish + slight boost for exact phrase presence
    base = inter / max(len(q), 1)
    if " ".join(sorted(q)) in " ".join(sorted(d)):
        base += 0.1
    return min(base, 1.0)

def _popularity_score(rating: float, ratings_total: int) -> float:
    # Normalize rating to 0..1 and give diminishing returns to review count
    r = max(0.0, min(5.0, rating)) / 5.0
    n = log(max(ratings_total, 1), 10) / 5.0  # ~0..1 across 1..100k+
    return 0.7 * r + 0.3 * min(n, 1.0)

def _price_bias(price: Optional[float], wants_lower: bool) -> float:
    if not wants_lower or price is None:
        return 0.0
    # Favor lower absolute price; map cheaper to + up to 0.2
    try:
        p = float(price)
        if p <= 25:   return 0.2
        if p <= 50:   return 0.12
        if p <= 100:  return 0.05
        return 0.0
    except Exception:
        return 0.0

def _intent_to_query(intent_data: Optional[Dict]) -> Optional[str]:
    if not intent_data:
        return None
    if isinstance(intent_data, dict):
        q = intent_data.get("query")
        if q:
            return str(q).strip()
        maybe = intent_data.get("intent")
        if maybe and str(maybe).strip().lower() not in {"find_products", "search", "product_search"}:
            return str(maybe).strip()
        parts: List[str] = []
        for k in ("brand", "product", "category", "goal", "need", "skin_type", "budget", "notes"):
            v = intent_data.get(k)
            if v:
                parts.append(str(v))
        if parts:
            return " ".join(parts)
    try:
        return str(intent_data).strip()
    except Exception:
        return None

def _wants_lower_price(intent_data: Optional[Dict]) -> bool:
    if not intent_data:
        return False
    constraints = (intent_data.get("constraints") or {})
    if str(constraints.get("price", "")).lower() == "lower":
        return True
    q = (intent_data.get("query") or "").lower()
    return any(w in q for w in ("cheap", "cheaper", "budget", "under", "less expensive", "affordable"))

def _score_candidate(query: str, item: Dict, wants_lower: bool) -> float:
    meta = item.get("meta") or {}
    rating = float(meta.get("rating") or 0)
    ratings_total = int(meta.get("ratings_total") or 0)
    snippet = ""
    # Rainforest may put short text in our "review" field; use it as snippet if present
    if item.get("review"):
        snippet = str(item["review"])
    rel = _overlap_score(query, item.get("title") or item.get("name") or "", snippet)
    pop = _popularity_score(rating, ratings_total)
    price = None
    price_obj = meta.get("price") or {}
    if isinstance(price_obj, dict):
        price = price_obj.get("value")
    score = 0.55 * rel + 0.35 * pop + 0.10 * _price_bias(price, wants_lower)
    return score

def build_product_candidates(intent_data: Optional[Dict]) -> List[Dict]:
    """
    Use Rainforest search (with optional embedded product data), keep only direct DP links,
    and rank generically by relevance + rating + review count (+ optional low-price bias).
    """
    q = _intent_to_query(intent_data)
    if not q:
        logger.info("[ProductSearch] No intent → no candidates")
        return []

    wants_lower = _wants_lower_price(intent_data)

    try:
        # Ask for a bit more than we need; we’ll rank and cut to 3 later.
        raw = search_amazon_products(q, max_results=10, min_stars=3.8, with_buybox=True)
        filtered: List[Dict] = []
        for p in raw:
            url = p.get("url")
            title = p.get("title") or p.get("name") or ""
            if not url or "/dp/" not in url or not title:
                continue
            filtered.append(p)

        if not filtered:
            logger.info("[ProductSearch] 0 DP candidates after filtering for q='{}'", q)
            return []

        # Rank generically
        ranked = sorted(
            filtered,
            key=lambda it: _score_candidate(q, it, wants_lower),
            reverse=True
        )

        norm: List[Dict] = []
        for p in ranked[:3]:
            url = p.get("url")
            if "?tag=" not in url:
                url += "?tag=schizobestie-20"
            norm.append({
                "title": p.get("title") or p.get("name") or "",
                "url": url,
                "merchant": p.get("merchant") or "amazon.com",
                "review": p.get("review") or "",
            })

        logger.info("[ProductSearch] Built {} candidates for query='{}'", len(norm), q)
        return norm

    except Exception as e:
        logger.exception("[ProductSearch] search failed: {}", e)
        return []

def prefer_amazon_first(candidates: List[Dict]) -> List[Dict]:
    def is_amazon(u: str) -> bool:
        try:
            return "amazon." in urlparse(u).netloc.lower()
        except Exception:
            return False
    return sorted(candidates, key=lambda c: (not is_amazon(c.get("url", ""))))
