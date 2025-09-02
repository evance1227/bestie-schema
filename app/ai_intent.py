# app/ai_intent.py
from typing import Dict, Optional
import re
from loguru import logger

__all__ = ["extract_product_intent"]

# Very light keyword hints — expand anytime
CATEGORY_KEYWORDS = {
    # skincare
    "moisturizer": "skincare",
    "moisturiser": "skincare",
    "cream": "skincare",
    "serum": "skincare",
    "cleanser": "skincare",
    "toner": "skincare",  # (printer toner handled via explicit "printer"/"ink" rule)
    "sunscreen": "skincare",
    "retinol": "skincare",
    "mask": "skincare",
    # hair
    "shampoo": "haircare",
    "conditioner": "haircare",
    # makeup
    "mascara": "makeup",
    "foundation": "makeup",
    # printers (kept here too, but we also have an explicit early rule)
    "printer": "printers",
    "inkjet": "printers",
    "laser printer": "printers",
}

LOWER_PRICE_WORDS = {
    "cheaper", "cheap", "cheapest", "less expensive", "budget", "affordable",
    "inexpensive", "under", "on a budget", "not expensive"
}

# Patterns that try to pull a canonical "target" after phrasing like
# "dupe of X", "similar to X", "like X but cheaper", or "X dupe"
SIMILAR_PATTERNS = [
    r"(?:dupe|alternative|similar|like).*(?:\bfor\b|\bof\b|\bis\b)?\s*([^,.;\n]+)",
    r"(?:similar|like|alternative|alt|dupe|dupes?)\s+(?:to|for)\s+([^,.;\n]+)",
    r"(?:cheaper|less\s+expensive|budget|affordable)\s+(?:version|option)\s+of\s+([^,.;\n]+)",
    r"(?:like)\s+([^,.;\n]+)\s+(?:but|only)\s+(?:cheaper|less\s+expensive|more\s+affordable)",
    r"([^,.;\n]+?)\s+(?:dupes?|alternative)s?$",  # "IS Clinical Youth Intensive Cream dupe"
]


def _norm(s: str) -> str:
    """Normalize simple punctuation/whitespace to get a cleaner query string."""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.strip(" ?.!\"'()")
    return s


def _category_guess(text_lower: str) -> Optional[str]:
    """
    Broad category inference. Prefer 'printers' when a printer-ish term is present,
    so 'toner' alone doesn't get misread as skincare if 'printer' is in the text.
    """
    if (
        "printer" in text_lower
        or "inkjet" in text_lower
        or "laser printer" in text_lower
        or re.search(r"\bink\b", text_lower)
        or "cartridge" in text_lower
        or "toner cartridge" in text_lower
    ):
        return "printers"

    for kw, cat in CATEGORY_KEYWORDS.items():
        if kw in text_lower:
            return cat
    return None


def extract_product_intent(user_text: str) -> Dict:
    """
    Return a dict understood by product_search.build_product_candidates:
        {
          "intent": "find_products",
          "query": <string>,           # canonical product name or need phrase
          "category": <optional str>,  # broad category if we can infer
          "constraints": { "price": "lower" | ... }
        }
    If no product intent detected, returns {}.
    """
    if not user_text:
        return {}

    t = _norm(user_text)
    low = t.lower()
    constraints: Dict[str, str] = {}

    # --- price sensitivity hints ---
    if any(w in low for w in LOWER_PRICE_WORDS):
        constraints["price"] = "lower"

    # --- explicit printer/ink intent (so it doesn't hit a generic clarifier) ---
    if (
        re.search(r"\bprinter(s)?\b", low)
        or re.search(r"\bink(jet)?\b", low)
        or "toner cartridge" in low
        or ("toner" in low and "printer" in low)
    ):
        return {
            "intent": "find_products",
            "query": user_text,  # keep the user's phrasing
            "category": "printers",
            "constraints": constraints,
        }

    # --- explicit “similar/dupe of X” targets ---
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

    # --- generic shopping phrasing ---
    if any(g in low for g in ["recommend", "recommendation", "suggest", "which", "buy", "find", "product", "looking for", "need"]):
        return {
            "intent": "find_products",
            "query": user_text,
            "category": _category_guess(low),
            "constraints": constraints,
        }

    # --- bare category noun (e.g., “moisturizer”) ---
    cat = _category_guess(low)
    if cat:
        return {
            "intent": "find_products",
            "query": user_text,
            "category": cat,
            "constraints": constraints,
        }

    return {}
