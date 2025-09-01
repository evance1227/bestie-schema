# app/ai_intent.py
from typing import Dict, Optional
from loguru import logger
import re

# very light category hints (expand anytime)
CATEGORY_KEYWORDS = {
    "moisturizer": "skincare",
    "cream": "skincare",
    "serum": "skincare",
    "cleanser": "skincare",
    "toner": "skincare",
    "sunscreen": "skincare",
    "retinol": "skincare",
    "mask": "skincare",
    "shampoo": "haircare",
    "conditioner": "haircare",
    "mascara": "makeup",
    "foundation": "makeup",
}

LOWER_PRICE_WORDS = {
    "cheaper", "less expensive", "budget", "affordable", "under",
    "dupe", "dupes", "alternative", "alt"
}

SIMILAR_PATTERNS = [
    r"(?:similar|like|alternative|alt|dupe|dupes?)\s+(?:to|for)\s+([^,.;\n]+)",
    r"(?:cheaper|less\s+expensive|budget|affordable)\s+(?:version|option)\s+of\s+([^,.;\n]+)",
    r"(?:like)\s+([^,.;\n]+)\s+(?:but|only)\s+(?:cheaper|less\s+expensive|more\s+affordable)",
]

def _category_guess(text_lower: str) -> Optional[str]:
    for kw, cat in CATEGORY_KEYWORDS.items():
        if kw in text_lower:
            return cat
    return None

def _norm(s: str) -> str:
    # normalize simple punctuation/whitespace
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" ?.!\"'()")
    return s

def extract_product_intent(user_text: str) -> Dict:
    """
    Return a dict understood by product_search.build_product_candidates:

        {
          "intent": "find_products",
          "query": <string>,           # canonical product name or need phrase
          "category": <optional str>,  # broad category if we can infer
          "constraints": { "price": "lower" | None }
        }

    If no product intent detected, returns {}.
    """
    if not user_text:
        return {}

    t = _norm(user_text)
    low = t.lower()
    constraints: Dict[str, str] = {}

    # price constraint keywords
    if any(w in low for w in LOWER_PRICE_WORDS):
        constraints["price"] = "lower"

    # explicit “similar/dupe of X”
    for pat in SIMILAR_PATTERNS:
        m = re.search(pat, low)
        if m:
            target_raw = m.group(1)
            target = _norm(target_raw)
            logger.info("[Intent] Parsed 'similar-to' target: {}", target)
            return {
                "intent": "find_products",
                "query": target,
                "category": _category_guess(low),
                "constraints": constraints,
            }

    # generic shopping phrasing
    if any(g in low for g in ["recommend", "product", "buy", "find", "need", "looking for"]):
        return {
            "intent": "find_products",
            "query": t,
            "category": _category_guess(low),
            "constraints": constraints,
        }

    # bare category noun (e.g., “moisturizer”)
    cat = _category_guess(low)
    if cat:
        return {
            "intent": "find_products",
            "query": t,
            "category": cat,
            "constraints": constraints,
        }

    return {}
