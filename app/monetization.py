# app/monetization.py
from __future__ import annotations

"""
Revenue-first product ranking with sponsor support, Amazon bias on near-ties,
and link hygiene prep (via linkwrap). Backward-compatible with choose(candidates).

Expected candidate shape (extra fields optional):
{
  "name": "...",
  "title": "...",
  "url": "https://www.amazon.com/dp/ASIN...",
  "merchant": "amazon.com",
  "review": "...",
  "commission_pct": 3.0,                # percent, not decimal
  "sponsor_bid_cents": 12,              # cents per click paid
  "last_ctr": 0.12,                     # 12%
  "last_conv_rate": 0.08,               # 8%
  "returns_rate": 0.06,                 # 6%
  "brand": "Merit",
  "meta": {
      "price": {"value": 28.00},        # Rainforest style
      "rating": 4.6,
      "ratings_total": 874,
      "availability": "In Stock"
  }
}
"""

import os
import re
from typing import Dict, List, Optional, Tuple
from math import log

from loguru import logger
from urllib.parse import urlparse

from app.linkwrap import convert_to_geniuslink

# ---------------- Env-configurable weights ---------------- #
W_SPONSOR   = float(os.getenv("MONETIZE_SPONSOR_WEIGHT",   "0.35"))
W_COMMISSION= float(os.getenv("MONETIZE_COMMISSION_WEIGHT","0.35"))
W_EPC       = float(os.getenv("MONETIZE_EPC_WEIGHT",       "0.20"))
W_QUALITY   = float(os.getenv("MONETIZE_QUALITY_WEIGHT",   "0.06"))  # rating/volume
W_BRAND_FIT = float(os.getenv("MONETIZE_BRAND_WEIGHT",     "0.02"))
W_RETURN_P  = float(os.getenv("MONETIZE_RETURNS_WEIGHT",   "0.02"))

AMAZON_TIE_BONUS = float(os.getenv("MONETIZE_AMAZON_TIE_BONUS", "0.03"))
AMAZON_HOST_RE   = re.compile(r"(?:^|\.)amazon\.", re.I)

# ---------------- Helpers ---------------- #
def _is_amazon(url: str) -> bool:
    try:
        return bool(AMAZON_HOST_RE.search(urlparse(url or "").netloc))
    except Exception:
        return False

def _price_value(meta_price) -> Optional[float]:
    try:
        if isinstance(meta_price, dict):
            v = meta_price.get("value")
            return float(v) if v is not None else None
        if isinstance(meta_price, (int, float, str)):
            return float(meta_price)
    except Exception:
        return None
    return None

def _quality_score(meta: dict) -> float:
    """Small boost for highly rated, well-reviewed items."""
    try:
        rating = float((meta or {}).get("rating") or 0)
        votes  = int((meta or {}).get("ratings_total") or 0)
        r = max(min(rating / 5.0, 1.0), 0.0)
        v = 0.0 if votes <= 0 else min(1.0, log(votes + 1, 10) / 3.0)  # log scale to ~1.0 around 1k reviews
        return 0.7 * r + 0.3 * v
    except Exception:
        return 0.0

def _commission_component(c: dict) -> float:
    # Use commission% and price if present; otherwise commission% alone.
    pct = float(c.get("commission_pct") or 0.0) / 100.0
    price = _price_value(((c.get("meta") or {}).get("price")))
    if price:
        return pct * min(price, 300.0) / 300.0  # normalize by a cap
    return pct

def _sponsor_component(c: dict) -> float:
    cents = float(c.get("sponsor_bid_cents") or 0.0)
    return min(1.0, cents / 50.0)  # treat 50¢ CPC as a strong bid (cap)

def _epc_component(c: dict) -> float:
    # Heuristic EPC proxy: ctr * conv * commission_value
    ctr  = max(0.0, float(c.get("last_ctr") or 0.0))          # already 0..1
    conv = max(0.0, float(c.get("last_conv_rate") or 0.0))    # already 0..1
    pct  = float(c.get("commission_pct") or 0.0) / 100.0
    price= _price_value(((c.get("meta") or {}).get("price"))) or 25.0
    epc  = ctr * conv * pct * price
    # Normalize epc by a loose upper bound so it contributes in 0..1
    return min(1.0, epc / 1.0)  # $1 EPC treated as strong

def _returns_penalty(c: dict) -> float:
    rr = float(c.get("returns_rate") or (c.get("meta") or {}).get("returns_rate") or 0.0)
    # Penalize above 10% returns; 30%+ gets near full penalty
    if rr <= 0.10: return 0.0
    return min(1.0, (rr - 0.10) / 0.20)

def _availability_bad(c: dict) -> bool:
    avail = str((c.get("meta") or {}).get("availability") or "").lower()
    return "out of stock" in avail or "unavailable" in avail

def _brand_fit(c: dict, include_brands: Optional[List[str]]) -> float:
    if not include_brands: return 0.0
    title = (c.get("title") or c.get("name") or "").lower()
    return 1.0 if any(b.lower() in title for b in include_brands) else 0.0

def _score(c: dict, *, include_brands: Optional[List[str]] = None) -> Tuple[float, dict]:
    # Core components
    s_sponsor = _sponsor_component(c)
    s_comm   = _commission_component(c)
    s_epc    = _epc_component(c)
    s_quality= _quality_score(c.get("meta") or {})
    s_brand  = _brand_fit(c, include_brands)
    p_return = _returns_penalty(c)

    # Weighted sum minus penalty
    raw = (W_SPONSOR   * s_sponsor +
           W_COMMISSION* s_comm   +
           W_EPC       * s_epc    +
           W_QUALITY   * s_quality+
           W_BRAND_FIT * s_brand  -
           W_RETURN_P  * p_return)

    # Nudge for Amazon in a much-weighted marketplace (monetization + availability)
    if _is_amazon(c.get("url") or ""):
        raw += 0.0  # base — tie bonus handled at re-rank stage

    explain = {
        "sponsor": s_sponsor, "commission": s_comm, "epc": s_epc,
        "quality": s_quality, "brand_fit": s_brand, "returns_penalty": p_return,
        "weighted": raw
    }
    return raw, explain

def _prep_url(c: dict) -> str:
    """Convert to affiliate-safe / Geniuslink-wrapped URL once."""
    url = str(c.get("url") or "")
    try:
        return convert_to_geniuslink(url) if url else url
    except Exception as e:
        logger.debug("[Monetize] linkwrap failed for {}: {}", url, e)
        return url

def _asin(url: str) -> Optional[str]:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url, re.I)
    return m.group(1).upper() if m else None

def _dedupe_near_dupes(rows: List[dict]) -> List[dict]:
    """Deduplicate obvious near-duplicates by ASIN or normalized title."""
    seen = set()
    out: List[dict] = []
    for r in rows:
        url = r.get("final_url") or r.get("url") or ""
        key = _asin(url) or (r.get("title") or r.get("name") or "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out

def rank(candidates: List[dict], *, include_brands: Optional[List[str]] = None) -> List[dict]:
    """
    Return a new list of candidates with: { ..., final_url, score, explain } sorted best→worst.
    """
    rows: List[dict] = []
    for c in candidates or []:
        url = _prep_url(c)
        score, explain = _score(c, include_brands=include_brands)
        rows.append({**c, "final_url": url, "score": score, "explain": explain})

    # Sort by score
    rows.sort(key=lambda r: r["score"], reverse=True)

    # Amazon near-tie preference: if second place within AMAZON_TIE_BONUS and is Amazon, swap
    if len(rows) >= 2:
        a, b = rows[0], rows[1]
        if a["score"] - b["score"] <= AMAZON_TIE_BONUS:
            if _is_amazon(b.get("final_url") or b.get("url") or "") and not _is_amazon(a.get("final_url") or a.get("url") or ""):
                rows[0], rows[1] = b, a

    # Remove obvious duplicates by ASIN/title
    rows = _dedupe_near_dupes(rows)

    return rows

def top_k(candidates: List[dict], k: int = 3, *, include_brands: Optional[List[str]] = None) -> List[dict]:
    return rank(candidates, include_brands=include_brands)[:max(0, k)]

# ---------- Back-compat API ----------
def choose(candidates: List[dict]) -> dict:
    """
    Backward-compatible: return the single best candidate.
    """
    top = top_k(candidates, k=1)
    return top[0] if top else {}
