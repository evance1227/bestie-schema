# app/ai_intent.py
from __future__ import annotations

from typing import Dict, Optional, List, Tuple
import re
from loguru import logger

__all__ = ["extract_product_intent", "CATEGORY_KEYWORDS"]

# --------------------------- Lexicons --------------------------- #
# Broad, friendly categories that your product_search can map
CATEGORY_KEYWORDS = {
    # skincare
    "moisturizer": "skincare",
    "moisturiser": "skincare",
    "cream": "skincare",
    "serum": "skincare",
    "cleanser": "skincare",
    "toner": "skincare",  # guarded by printer rules below
    "sunscreen": "skincare",
    "spf": "skincare",
    "retinol": "skincare",
    "tretinoin": "skincare",
    "adapalene": "skincare",
    "mask": "skincare",
    "eye cream": "skincare",
    # hair
    "shampoo": "haircare",
    "conditioner": "haircare",
    "mask for hair": "haircare",
    "hair mask": "haircare",
    "hair oil": "haircare",
    "heat protectant": "haircare",
    # makeup
    "mascara": "makeup",
    "foundation": "makeup",
    "concealer": "makeup",
    "blush": "makeup",
    "lip": "makeup",
    "tint": "makeup",
    "skin tint": "makeup",
    "brow": "makeup",
    # body
    "body lotion": "bodycare",
    "body wash": "bodycare",
    "self tanner": "bodycare",
    "deodorant": "bodycare",
    # devices/tools
    "airwrap": "devices",
    "hair dryer": "devices",
    "straightener": "devices",
    "clarisonic": "devices",
    # pets
    "dog": "pets",
    "dogs": "pets",
    "puppy": "pets",
    "kibble": "pets",
    "treats": "pets",
    "leash": "pets",
    "harness": "pets",
    "toy": "pets",
    "fast fetch": "pets",
    # printers
    "printer": "printers",
    "inkjet": "printers",
    "laser printer": "printers",
}

# Words that imply price sensitivity or a budget alternative
LOWER_PRICE_WORDS = {
    "cheaper", "cheap", "cheapest", "less expensive", "budget", "affordable",
    "inexpensive", "under", "on a budget", "not expensive", "dupe", "alternative"
}

CHANNEL_HINTS = {
    "amazon": "amazon",
    "prime": "amazon",
    "sephora": "sephora",
    "ulta": "ulta",
    "target": "target",
    "walmart": "walmart",
}

BRAND_WHITELIST = {
    "medik8", "merit", "is clinical", "eltamd", "la roche-posay", "supergoop",
    "cerave", "cetaphil", "beauty of joseon", "k18", "olaplex", "tatcha",
    "tower 28", "rare beauty", "maybelline", "l’oreal", "loreal", "dior",
    "charlotte tilbury", "nyx", "saie", "kosas", "il makiage",
}

SHADE_WORDS = {
    "ivory", "fair", "light", "medium", "tan", "deep", "neutral", "warm", "cool",
    "linen", "sand", "honey", "bisque", "beige", "almond", "buff", "n", "w", "c"
}

SKIN_TYPES = {"oily", "dry", "combination", "combo", "normal", "sensitive", "acne-prone", "acne", "rosacea"}
HAIR_FLAGS = {
    "fine", "thick", "coarse", "curly", "wavy", "straight", "blonde", "color-treated",
    "bleach", "keratin", "extensions", "frizzy"
}
CONCERN_WORDS = {"melasma", "hyperpigmentation", "breakout", "breakouts", "acne", "wrinkles", "aging", "anti-aging"}

# Routine audit keys and ingredient tokens
ROUTINE_KEYS = {
    "routine", "am routine", "pm routine", "morning routine", "night routine",
    "overlap", "layer", "together", "alternate nights", "same night",
    "use with", "mix with", "stacking", "too much"
}
INGREDIENT_TOKENS = [
    "retinol","retinal","tretinoin","adapalene","aha","bha","pha","salicylic",
    "glycolic","lactic","mandelic","benzoyl peroxide","vitamin c","ascorbic",
    "niacinamide","azelaic","arbutin","kojic","peptide","copper peptide",
    "hyaluronic","ceramide","sunscreen","spf"
]

# --------------------------- Helpers --------------------------- #
def _norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.strip(" ?.!\"'()[]")
    return s

def _category_guess(text_lower: str) -> Optional[str]:
    """
    Prefer 'printers' when a printer-ish term is present,
    so 'toner' alone is not misread as skincare if 'printer' is in the text.
    """
    if (
        "printer" in text_lower
        or "inkjet" in text_lower
        or "laser printer" in text_lower
        or re.search(r"\bink\b", text_lower)
        or "cartridge" in text_lower
        or "toner cartridge" in text_lower
        or ("toner" in text_lower and "printer" in text_lower)
    ):
        return "printers"

    for kw, cat in CATEGORY_KEYWORDS.items():
        if kw in text_lower:
            return cat
    return None

def _detect_channel(text_lower: str) -> Optional[str]:
    for k, v in CHANNEL_HINTS.items():
        if k in text_lower:
            return v
    return None

def _detect_count(text_lower: str) -> Optional[int]:
    m = re.search(r"\b(?:send|show|give)\s+me\s+(?:the\s+)?(\d+)\s+(?:options|links|picks|products)\b", text_lower)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m = re.search(r"\btop\s+(\d+)\b", text_lower)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    if any(p in text_lower for p in ["one pick", "one option", "single pick"]):
        return 1
    return None

def _parse_price_qualifiers(text_lower: str) -> Dict[str, object]:
    c: Dict[str, object] = {}
    if any(w in text_lower for w in LOWER_PRICE_WORDS):
        c["price"] = "lower"
        c["need_budget_alt"] = True
    m = re.search(r"\bunder\s*\$?\s*(\d{1,4})\b", text_lower)
    if m:
        c["max_price"] = int(m.group(1))
    m = re.search(r"\$\s*(\d{1,4})\s*-\s*\$\s*(\d{1,4})", text_lower)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        c["price_range"] = [min(lo, hi), max(lo, hi)]
    m = re.search(r"\baround\s*\$?\s*(\d{1,4})\b", text_lower)
    if m and "price_range" not in c and "max_price" not in c:
        c["target_price"] = int(m.group(1))
    return c

def _parse_sunscreen_filters(text_lower: str) -> Dict[str, object]:
    c: Dict[str, object] = {}
    m = re.search(r"\bspf\s*(\d{2,3})\b", text_lower)
    if m:
        c["spf_exact"] = int(m.group(1))
    if "mineral" in text_lower or "zinc" in text_lower or "titanium" in text_lower:
        c["mineral_only"] = True
    if "chemical" in text_lower:
        c["chemical_ok"] = True
    if "tinted" in text_lower:
        c["tinted"] = True
    if "water resistant" in text_lower or "water-resistant" in text_lower:
        c["water_resistant"] = True
        mwr = re.search(r"\b(40|80)\s*min", text_lower)
        if mwr:
            c["water_resistant_min"] = int(mwr.group(1))
    if "pa++++" in text_lower:
        c["pa_rating"] = "PA++++"
    if "fragrance-free" in text_lower or "fragrance free" in text_lower:
        c["fragrance_free"] = True
    if "non-comedogenic" in text_lower or "non comedogenic" in text_lower:
        c["non_comedogenic"] = True
    if "oil-free" in text_lower or "oil free" in text_lower:
        c["oil_free"] = True
    if "extensions" in text_lower and "pink" in text_lower:
        c["mineral_only"] = True
    return c

def _parse_retinoid_strength(text_lower: str) -> Dict[str, object]:
    c: Dict[str, object] = {}
    m = re.search(r"\b(\d(?:\.\d+)?)\s*%\s*(?:retinol|retinal|retinaldehyde)\b", text_lower)
    if m:
        try:
            c["retinoid_percent"] = float(m.group(1))
        except Exception:
            pass
    if "starter" in text_lower or "beginner" in text_lower:
        c["retinoid_level"] = "starter"
    if "strong" in text_lower or "max" in text_lower or "intense" in text_lower:
        c["retinoid_level"] = "strong"
    return c

def _extract_brands(text_lower: str) -> Tuple[List[str], List[str]]:
    include, exclude = [], []
    for b in BRAND_WHITELIST:
        if b in text_lower:
            include.append(b)
    m = re.findall(r"(?:not|no|avoid)\s+([A-Za-z][A-Za-z' \-]{1,30})", text_lower)
    for raw in m:
        name = _norm(raw).lower()
        if name and name not in exclude:
            exclude.append(name)
    return include, exclude

def _extract_shade(text: str) -> Optional[str]:
    m = re.search(r"\b(?:shade|color)\s*[:\-]?\s*([A-Za-z0-9\.\-]+)\b", text, flags=re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b\d(\.\d)?[NCW]\b", text, flags=re.I)
    if m:
        return m.group(0).strip()
    for w in SHADE_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", text, flags=re.I):
            return w
    return None

def _skin_hair_flags(text_lower: str) -> Dict[str, object]:
    c: Dict[str, object] = {}
    skins = [t for t in SKIN_TYPES if t in text_lower]
    hairs = [t for t in HAIR_FLAGS if t in text_lower]
    concerns = [t for t in CONCERN_WORDS if t in text_lower]
    if skins:
        c["skin_types"] = list(sorted(set(skins)))
    if hairs:
        c["hair_flags"] = list(sorted(set(hairs)))
    if concerns:
        c["concerns"] = list(sorted(set(concerns)))
    if "sensitive" in text_lower:
        c["fragrance_free"] = True
        c["non_comedogenic"] = True
    if "pregnant" in text_lower or "pregnancy" in text_lower:
        c["pregnancy_safe"] = True
    if "puppy" in text_lower:
        c["pet_age"] = "puppy"
    if "dachshund" in text_lower:
        c["dog_breed"] = "dachshund"
    return c

def _speed_coupons(text_lower: str) -> Dict[str, object]:
    c: Dict[str, object] = {}
    if "prime" in text_lower or "same day" in text_lower or "today" in text_lower:
        c["speed"] = "fast"
    if "coupon" in text_lower or "promo" in text_lower or "discount" in text_lower or "code" in text_lower:
        c["coupon_search"] = True
    return c

# --------------------------- Core --------------------------- #
def extract_product_intent(user_text: str) -> Dict:
    """
    Return a dict understood by product_search.build_product_candidates:
        {
          "intent": "find_products" | "routine_audit",
          "query": <string>,
          "category": <optional str>,
          "constraints": { ... }
        }
    If no product intent detected, returns {}.
    """
    if not user_text:
        return {}

    t = _norm(user_text)
    low = t.lower()
    constraints: Dict[str, object] = {}

    # price and general qualifiers
    constraints.update(_parse_price_qualifiers(low))
    constraints.update(_speed_coupons(low))

    # Routine / overlap audit first
    if any(k in low for k in ROUTINE_KEYS) or re.search(r"can i (use|layer|mix).+ with ", low):
        found = [tok for tok in INGREDIENT_TOKENS if tok in low]
        return {
            "intent": "routine_audit",
            "query": user_text,
            "category": "skincare",
            "constraints": {"ingredients": sorted(set(found))}
        }

    # explicit printer/ink intent
    if (
        re.search(r"\bprinter(s)?\b", low)
        or re.search(r"\bink(jet)?\b", low)
        or "toner cartridge" in low
        or ("toner" in low and "printer" in low)
    ):
        return {
            "intent": "find_products",
            "query": user_text,
            "category": "printers",
            "constraints": constraints,
        }

    # sunscreen and retinoid specific filters
    if "sunscreen" in low or "spf" in low:
        constraints.update(_parse_sunscreen_filters(low))
    if "retinol" in low or "retinal" in low or "retinaldehyde" in low:
        constraints.update(_parse_retinoid_strength(low))

    # channel and count
    channel = _detect_channel(low)
    if channel:
        constraints["channel"] = channel

    count = _detect_count(low)
    if count:
        constraints["count"] = int(count)

    # brand includes and excludes
    inc_brands, exc_brands = _extract_brands(low)
    if inc_brands:
        constraints["include_brands"] = sorted(set(inc_brands))
    if exc_brands:
        constraints["exclude_brands"] = sorted(set(exc_brands))

    # skin, hair, and concerns
    constraints.update(_skin_hair_flags(low))

    # shade or color selection
    shade = _extract_shade(user_text)
    if shade:
        constraints["shade"] = shade

    # explicit “similar/dupe of X” targets
    similar_patterns = [
        r"(?:dupe|alternative|similar|like).*(?:\bfor\b|\bof\b|\bis\b)?\s*([^,.;\n]+)",
        r"(?:similar|like|alternative|alt|dupe|dupes?)\s+(?:to|for)\s+([^,.;\n]+)",
        r"(?:cheaper|less\s+expensive|budget|affordable)\s+(?:version|option)\s+of\s+([^,.;\n]+)",
        r"(?:like)\s+([^,.;\n]+)\s+(?:but|only)\s+(?:cheaper|less\s+expensive|more\s+affordable)",
        r"([^,.;\n]+?)\s+(?:dupes?|alternative)s?$",
    ]
    for pat in similar_patterns:
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
    if any(g in low for g in ["recommend", "recommendation", "suggest", "which", "buy", "find", "product", "looking for", "need", "link", "send me"]):
        return {
            "intent": "find_products",
            "query": user_text,
            "category": _category_guess(low),
            "constraints": constraints,
        }

    # bare category nouns like “moisturizer”, “shampoo”, “mascara”, or “fast fetch”
    cat = _category_guess(low)
    if cat:
        return {
            "intent": "find_products",
            "query": user_text,
            "category": cat,
            "constraints": constraints,
        }

    return {}
