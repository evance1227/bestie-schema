# app/product_search.py
from typing import List, Dict, Optional
from urllib.parse import urlparse
from loguru import logger
from app.amazon_api import search_amazon_products  # ✅ Make sure amazon_api.py has the Rainforest integration

def build_product_candidates(intent_data: Optional[Dict]) -> List[Dict]:
    """
    Build a normalized list of product candidates using Rainforest API.
    Returns: [{"title": str, "url": str, "merchant": str}, ...]
    """
    q = _intent_to_query(intent_data)
    if not q:
        logger.info("[ProductSearch] No intent → no candidates")
        return []

    try:
        raw_results = search_amazon_products(q)
        norm: List[Dict] = []

        for p in raw_results:
            url = p.get("url")
            if not url or "/dp/" not in url:
                continue  # Skip search pages or broken links

            title = p.get("name") or p.get("title") or ""
            review = p.get("review") or "Top-rated and worth the glow-up."
            merchant = "amazon.com"

            # Monetize the link
            if "?tag=" not in url:
                url += "?tag=schizobestie-20"

            norm.append({
                "title": title,
                "url": url,
                "merchant": merchant,
                "review": review
            })

        logger.info("[ProductSearch] Built {} candidates for query='{}'", len(norm), q)
        return norm

    except Exception as e:
        logger.exception("[ProductSearch] fetch_products failed: {}", e)
        return []


def _intent_to_query(intent_data: Optional[Dict]) -> Optional[str]:
    """Turn whatever shape your intent extractor returns into a search query."""
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


def prefer_amazon_first(candidates: List[Dict]) -> List[Dict]:
    """Sort so any amazon.* URLs come first, keep relative order otherwise."""
    def is_amazon(u: str) -> bool:
        try:
            return "amazon." in urlparse(u).netloc.lower()
        except Exception:
            return False

    return sorted(candidates, key=lambda c: (not is_amazon(c.get("url", ""))))
