# app/product_search.py
from typing import List, Dict, Optional
from urllib.parse import urlparse
from loguru import logger

# app/product_search.py
import re, urllib.parse
from loguru import logger

def _amz_search_url(q: str) -> str:
    # Use Amazon search URLs (they never 404); your monetization layer will rewrite them
    return "https://www.amazon.com/s?k=" + urllib.parse.quote_plus(q)

def fetch_products(query: str, category: str | None = None, constraints: dict | None = None):
    """
    Minimal curated results so the LLM doesn't invent products or dead dp links.
    Returns a list of dicts the builder can consume. Keys are generic and safe.
    """
    q = (query or "").lower()
    wants_lower = False
    if constraints and str(constraints.get("price", "")).lower() == "lower":
        wants_lower = True
    if any(w in q for w in ("cheap", "cheaper", "less expensive", "budget", "under", "dupe", "alternative")):
        wants_lower = True

    # Specific curated case: iS Clinical Youth Intensive Cream → cheaper alternatives
    if re.search(r"\bis\s*clinical\b.*youth\s+intensive\s+cream", q, re.I) or "is clinical youth intensive cream" in q:
        products = [
            {
                "title": "Naturium Multi-Peptide Moisturizer",
                "url": _amz_search_url("Naturium Multi-Peptide Moisturizer"),
                "one_liner": "Peptide-rich hydrator for firmness at a budget price.",
                "price_hint": "$20–$30",
            },
            {
                "title": "Olay Regenerist Micro-Sculpting Cream (Fragrance-Free)",
                "url": _amz_search_url("Olay Regenerist Micro-Sculpting Cream fragrance free"),
                "one_liner": "Amino-peptides + niacinamide for smoothing and bounce.",
                "price_hint": "$25–$35",
            },
            {
                "title": "La Roche-Posay Hyaluronic Acid B5 Moisturizer",
                "url": _amz_search_url("La Roche-Posay Hyaluronic Acid B5 moisturizer"),
                "one_liner": "Triple-HA plumping hydration with a luxe feel.",
                "price_hint": "$25–$35",
            },
        ]
        return products

    # Unknown query → let the LLM handle it (but we logged it)
    logger.warning("[ProductSearch] No curated match for query=%r; returning empty list", query)
    return []

def build_product_candidates(query: str, category: str | None = None, constraints: dict | None = None):
    # Pass-through to keep your worker code unchanged
    return fetch_products(query, category, constraints)

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
        # Prefer explicit query from the extractor
        q = intent_data.get("query")
        if q:
            return str(q).strip()

        # Sometimes the phrase is stuffed into "intent" (but ignore control values)
        maybe = intent_data.get("intent")
        if maybe and str(maybe).strip().lower() not in {"find_products", "search", "product_search"}:
            return str(maybe).strip()

        # Otherwise assemble from common hints
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
