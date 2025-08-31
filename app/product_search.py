# app/product_search.py
from typing import List, Dict, Optional
from urllib.parse import urlparse
from loguru import logger

############################################################
# Legacy hook (optional): if you already had a real search
# implemented, keep/replace this function with your own.
############################################################
def fetch_products(query: str) -> List[Dict]:
    """
    Legacy/stub product search.

    Replace this with your actual search implementation if you have one.
    Must return a list of dicts like:
      {"title": "Name", "url": "https://...", "merchant": "amazon.com"}
    """
    logger.warning("[ProductSearch] fetch_products not implemented; returning []. query={}", query)
    return []

def _intent_to_query(intent_data: Optional[Dict]) -> Optional[str]:
    """Turn whatever shape your intent extractor returns into a search query."""
    if not intent_data:
        return None
    if isinstance(intent_data, dict):
        # common shapes your extractor might emit
        if intent_data.get("intent"):
            return str(intent_data["intent"]).strip()

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

def build_product_candidates(intent_data: Optional[Dict]) -> List[Dict]:
    """
    Build a normalized list of product candidates:
    [{"title": str, "url": str, "merchant": str}, ...]
    """
    q = _intent_to_query(intent_data)
    if not q:
        logger.info("[ProductSearch] No intent → no candidates")
        return []

    try:
        raw = fetch_products(q) or []
        norm: List[Dict] = []
        for p in raw:
            url = p.get("url") or p.get("link") or p.get("href")
            if not url:
                continue
            title = p.get("title") or p.get("name") or p.get("product") or ""
            merchant = p.get("merchant")
            if not merchant:
                try:
                    merchant = urlparse(url).netloc
                except Exception:
                    merchant = None
            norm.append({"title": title, "url": url, "merchant": merchant})
        logger.info("[ProductSearch] Built {} candidates for query='{}'", len(norm), q)
        return norm
    except Exception as e:
        logger.exception("[ProductSearch] fetch_products failed: {}", e)
        return []

def prefer_amazon_first(candidates: List[Dict]) -> List[Dict]:
    """Sort so any amazon.* URLs come first, keep relative order otherwise."""
    def is_amazon(u: str) -> bool:
        try:
            return "amazon." in urlparse(u).netloc.lower()
        except Exception:
            return False

    # stable sort by a boolean key
    return sorted(candidates, key=lambda c: (not is_amazon(c.get("url", ""))))
