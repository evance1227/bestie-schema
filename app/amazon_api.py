# app/amazon_api.py
import os
import re
import requests
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote_plus

# Env + Affiliate
RAINFOREST_API_KEY = os.getenv("RAINFOREST_API_KEY")
GENIUSLINK_WRAP = os.getenv("GENIUSLINK_WRAP")
AFFILIATE_TAG = os.getenv("AMAZON_ASSOCIATE_TAG", "schizobestie-20")

# ---------- Personality / vibe helpers (lightweight) ----------
STOPWORDS = {
    "the","a","an","with","and","or","to","for","by","in","on",
    "my","your","their","of","me","that","set","pack"
}
VIBE_KEYWORDS = [
    "glow","boss","baddie","clean girl","rich mom","soft life","witchy",
    "main character","minimalist","hydration","plumping","glass skin",
    "confidence","self care","anxiety","menopause","glazed","extension","prime"
]
BANNED_PHRASES = [
    "Vacation in a bottle","Spa day in your pocket","Sun-kissed glow",
    "Feel like a goddess","Your skin will thank you","Beauty arsenal",
    "Secret weapon for a quick refresh","Say goodbye to","Main character in every room",
    "Begging for a glow-up","Strutting like you just stepped off a yacht","Daily adventures",
    "Unsung hero","Glowing from within","Trust me, you need this",
]

# ---------- Tokenization / scoring ----------
def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOPWORDS}

def _remove_banned_phrases(text: str) -> str:
    for phrase in BANNED_PHRASES:
        if phrase.lower() in (text or "").lower():
            return ""  # drop marketing fluff
    return text or ""

def _price_value(meta_price) -> Optional[float]:
    # Rainforest formats price as dict: {"symbol":"$","value":12.99,...}
    try:
        if isinstance(meta_price, dict):
            v = meta_price.get("value")
            return float(v) if v is not None else None
        if isinstance(meta_price, (int, float, str)):
            return float(meta_price)
    except Exception:
        return None
    return None

def _prime_flag(item: Dict) -> bool:
    ful = (item.get("product") or {}).get("fulfillment")
    # Examples: "Amazon", "Amazon + Prime", "Prime"
    return bool(ful and "prime" in str(ful).lower())

def _brand_in(title: str, names: List[str]) -> bool:
    tl = (title or "").lower()
    return any(b.lower() in tl for b in names or [])

def _brand_absent(title: str, names: List[str]) -> bool:
    tl = (title or "").lower()
    return not any(b.lower() in tl for b in names or [])

def _dp_link(asin: str) -> str:
    base = f"https://www.amazon.com/dp/{asin}?tag={AFFILIATE_TAG}"
    return GENIUSLINK_WRAP.format(url=quote_plus(base)) if GENIUSLINK_WRAP else base

def _vibe_match(query: str, title: str, snippet: str = "") -> float:
    q = _tokens(query)
    d = _tokens(f"{title} {snippet}")
    if not q:
        return 0.0
    return len(q & d) / max(len(q), 1)

def _popularity_score(rating: float, reviews: int) -> float:
    from math import log
    r = max(min(rating / 5.0, 1.0), 0.0)
    v = log(max(reviews, 1), 10) / 5.0
    return 0.7 * r + 0.3 * v

def _brand_score(title: str, include: List[str]) -> float:
    if not include:
        return 0.0
    t = title.lower()
    hits = sum(1 for b in include if b.lower() in t)
    return min(0.25, 0.10 * hits)  # small boost per brand hit

def _price_target_score(price: Optional[float], constraints: Dict) -> float:
    if price is None:
        return 0.0
    bonus = 0.0
    # Under hard cap
    max_price = constraints.get("max_price")
    if isinstance(max_price, int) and price <= max_price:
        bonus += 0.15
    # Inside range closeness
    lo, hi = None, None
    pr = constraints.get("price_range")
    if isinstance(pr, (list, tuple)) and len(pr) == 2:
        lo, hi = pr
        if lo is not None and hi is not None and lo <= price <= hi:
            # closer to middle is better
            mid = (lo + hi) / 2
            dist = abs(price - mid) / max(hi - lo, 1)
            bonus += (0.20 * (1 - dist))
    # “Cheaper/budget” vibe
    if constraints.get("price") == "lower":
        if price < 25:
            bonus += 0.15
        elif price < 50:
            bonus += 0.05
    return min(bonus, 0.35)

def _prime_score(constraints: Dict, item: Dict) -> float:
    if constraints.get("speed") == "fast":
        return 0.12 if _prime_flag(item) else 0.0
    return 0.0

def _constraint_score(item: Dict, query: str, constraints: Dict) -> float:
    title = item.get("title") or item.get("name") or ""
    snippet = item.get("review") or ""
    meta = item.get("meta") or {}
    rating = float(meta.get("rating") or 0)
    reviews = int(meta.get("ratings_total") or 0)
    price = _price_value(meta.get("price"))

    score = 0.50 * _vibe_match(query, title, snippet)
    score += 0.25 * _popularity_score(rating, reviews)
    score += 0.15 * _brand_score(title, constraints.get("include_brands") or [])
    score += _price_target_score(price, constraints)
    score += _prime_score(constraints, item)

    # Very mild signal for VIBE_KEYWORDS
    vk_hits = sum(1 for w in VIBE_KEYWORDS if w in title.lower())
    score += min(0.10, 0.02 * vk_hits)
    return score

# ---------- Query refinement ----------
def _refine_query(query: str, constraints: Optional[Dict]) -> str:
    q = (query or "").strip()
    if not constraints:
        return q

    # Shade / color
    shade = constraints.get("shade")
    if shade:
        q += f" {shade}"

    # Sunscreen specifics
    if constraints.get("mineral_only"):
        q += " mineral"
    spf = constraints.get("spf_exact")
    if isinstance(spf, int):
        q += f" SPF {spf}"

    # Brand focus (append one or two)
    inc = constraints.get("include_brands") or []
    if inc:
        q += " " + " ".join(inc[:2])

    # Channel hint is already 'amazon' here by design
    return q.strip()

def _passes_filters(item: Dict, constraints: Dict) -> bool:
    title = item.get("title") or ""
    meta = item.get("meta") or {}
    price = _price_value(meta.get("price"))
    # Exclude brands
    exc = constraints.get("exclude_brands") or []
    if exc and not _brand_absent(title, exc):
        return False
    # Include brand strict mode? keep soft — do not require, just score
    # Price cap
    max_price = constraints.get("max_price")
    if isinstance(max_price, int) and price is not None and price > max_price:
        return False
    # Range
    pr = constraints.get("price_range")
    if isinstance(pr, (list, tuple)) and len(pr) == 2 and price is not None:
        lo, hi = pr
        if lo is not None and price < lo:
            return False
        if hi is not None and price > hi:
            return False
    # Fast shipping preference (do not hard filter, we score it), unless caller insists later
    return True

# ---------- Rainforest API wrapper ----------
def search_amazon_products(query: str,
                           max_results: int = 10,
                           constraints: Optional[Dict] = None) -> List[Dict]:
    """
    Returns a ranked list of up to `max_results` products with:
      - name/title
      - url (DP link, affiliate-tagged, optionally Geniuslink-wrapped)
      - review snippet (sanitized)
      - merchant 'amazon.com'
      - meta: rating, ratings_total, price, buybox, fulfillment
    """
    if not RAINFOREST_API_KEY:
        # Fail quiet: caller will inject an Amazon search link later if needed
        return []

    constraints = constraints or {}
    refined = _refine_query(query, constraints)

    try:
        url = "https://api.rainforestapi.com/request"
        params = {
            "api_key": RAINFOREST_API_KEY,
            "type": "search",
            "amazon_domain": "amazon.com",
            "search_term": refined,
            "page": 1,
            "number_of_results": max(10, max_results),  # fetch slightly more for filtering
            "include_sponsored": "false",
            "include_products_count": 5,
            "fields": ",".join([
                "search_results.title",
                "search_results.asin",
                "search_results.snippet",
                "search_results.rating",
                "search_results.ratings_total",
                "search_results.price",
                "search_results.product.buybox_winner",
                "search_results.product.fulfillment"
            ])
        }

        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Rainforest API] ❌ {e}")
        return []

    results: List[Dict] = []
    for item in data.get("search_results", []) or []:
        asin = item.get("asin")
        title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        rating = float(item.get("rating") or 0)
        reviews = int(item.get("ratings_total") or 0)
        price = item.get("price")

        # Quality gate
        if not asin or not title:
            continue
        if rating < 3.8 or reviews < 50:
            continue

        clean_snippet = _remove_banned_phrases(snippet)

        row = {
            "name": title,
            "title": title,
            "url": _dp_link(asin),
            "review": clean_snippet or "High rating + relevance. Good vibes here.",
            "merchant": "amazon.com",
            "meta": {
                "rating": rating,
                "ratings_total": reviews,
                "price": price,
                "buybox": (item.get("product") or {}).get("buybox_winner"),
                "fulfillment": (item.get("product") or {}).get("fulfillment"),
            }
        }

        if _passes_filters(row, constraints):
            results.append(row)

    # Score + slice
    results.sort(key=lambda x: _constraint_score(x, refined or query, constraints), reverse=True)

    # Respect explicit count if provided by intent
    explicit_count = constraints.get("count")
    cap = explicit_count if isinstance(explicit_count, int) and explicit_count > 0 else 3
    return results[:cap]
