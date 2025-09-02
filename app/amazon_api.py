# app/amazon_api.py

import os
import requests
import re
from typing import List, Dict, Optional
from urllib.parse import quote_plus

RAINFOREST_API_KEY = os.getenv("RAINFOREST_API_KEY")
GENIUSLINK_WRAP = os.getenv("GENIUSLINK_WRAP")
AFFILIATE_TAG = "schizobestie-20"


# ----------------------------------
# ✨ Personality-Powered Filtering & Vibe Matching
# ----------------------------------

STOPWORDS = set([
    "the", "a", "an", "with", "and", "or", "to", "for", "by", "in", "on",
    "my", "your", "their", "cheap", "best", "top", "under", "of", "me", "that"
])

ASTRO_TAGS = [
    "aries", "taurus", "gemini", "cancer", "leo", "virgo",
    "libra", "scorpio", "sagittarius", "capricorn", "aquarius", "pisces"
]

VIBE_KEYWORDS = [
    "glow", "boss", "baddie", "clean girl", "rich mom", "soft life", "witchy",
    "main character", "vampire skin", "minimalist", "matcha", "anti-aging",
    "bloat", "breakout", "hydration", "plumping", "glass skin", "filter skin",
    "divorce era", "revenge body", "tantric", "sexual wellness", "sleep hygiene",
    "astrology", "confidence", "self care", "anxiety", "menopause", "glazed"
]

BANNED_PHRASES = [
    "Vacation in a bottle", "Spa day in your pocket", "Sun-kissed glow",
    "Feel like a goddess", "Your skin will thank you", "Beauty arsenal",
    "Secret weapon for a quick refresh", "Say goodbye to", "Main character in every room",
    "Begging for a glow-up", "Strutting like you just stepped off a yacht", "Daily adventures",
    "Unsung hero", "Glowing from within", "Trust me, you need this"
]


# ----------------------------------
# ✨ Helpers for Scoring and Filtering
# ----------------------------------

def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOPWORDS}

def _dp_link(asin: str) -> str:
    base = f"https://www.amazon.com/dp/{asin}?tag={AFFILIATE_TAG}"
    return GENIUSLINK_WRAP.format(url=quote_plus(base)) if GENIUSLINK_WRAP else base

def _remove_banned_phrases(text: str) -> str:
    for phrase in BANNED_PHRASES:
        if phrase.lower() in text.lower():
            return ""  # Remove entire line if banned phrase appears
    return text

def _vibe_match(query: str, title: str, snippet: str = "") -> float:
    q = _tokens(query)
    d = _tokens(f"{title} {snippet}")
    match = len(q & d)
    return match / max(len(q), 1)

def _astro_score(query: str, title: str) -> float:
    q = query.lower()
    t = title.lower()
    if any(sign in q for sign in ASTRO_TAGS) and any(sign in t for sign in ASTRO_TAGS):
        return 0.1
    return 0.0

def _vibe_score(query: str, title: str) -> float:
    if not query: return 0
    match_count = sum(1 for word in VIBE_KEYWORDS if word in title.lower())
    return 0.02 * match_count

def _popularity_score(rating: float, reviews: int) -> float:
    from math import log
    r = rating / 5.0
    v = log(max(reviews, 1), 10) / 5.0
    return 0.7 * r + 0.3 * v

def _price_bias(price: Optional[float], query: str) -> float:
    if not price: return 0
    q = query.lower()
    if any(p in q for p in ["cheap", "budget", "under", "affordable"]):
        if price < 25: return 0.15
        if price < 50: return 0.05
    return 0

def _score(item: Dict, query: str) -> float:
    meta = item.get("meta") or {}
    title = item.get("title") or ""
    snippet = item.get("review") or ""
    rating = float(meta.get("rating") or 0)
    reviews = int(meta.get("ratings_total") or 0)
    price = None
    if meta.get("price") and isinstance(meta["price"], dict):
        price = meta["price"].get("value")
    return (
        0.50 * _vibe_match(query, title, snippet) +
        0.25 * _popularity_score(rating, reviews) +
        0.10 * _astro_score(query, title) +
        0.10 * _vibe_score(query, title) +
        0.05 * _price_bias(price, query)
    )


# ----------------------------------
# 🌍 Rainforest Search API Wrapper
# ----------------------------------

def search_amazon_products(query: str, max_results: int = 10) -> List[Dict]:
    if not RAINFOREST_API_KEY:
        print("[Rainforest API] Missing key")
        return []

    try:
        url = "https://api.rainforestapi.com/request"
        params = {
            "api_key": RAINFOREST_API_KEY,
            "type": "search",
            "amazon_domain": "amazon.com",
            "search_term": query,
            "page": 1,
            "number_of_results": max_results,
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

        results = []
        for item in data.get("search_results", []):
            asin = item.get("asin")
            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            rating = float(item.get("rating") or 0)
            reviews = int(item.get("ratings_total") or 0)
            price = item.get("price")

            if not asin or not title or rating < 3.8 or reviews < 50:
                continue

            clean_snippet = _remove_banned_phrases(snippet)

            results.append({
                "name": title,
                "title": title,
                "url": _dp_link(asin),
                "review": clean_snippet or "High rating + emotional relevance — a vibe match.",
                "merchant": "amazon.com",
                "meta": {
                    "rating": rating,
                    "ratings_total": reviews,
                    "price": price,
                    "buybox": item.get("product", {}).get("buybox_winner"),
                    "fulfillment": item.get("product", {}).get("fulfillment"),
                }
            })

        return sorted(results, key=lambda x: _score(x, query), reverse=True)[:3]

    except Exception as e:
        print(f"[Rainforest API] ❌ {e}")
        return []