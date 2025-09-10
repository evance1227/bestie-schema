from __future__ import annotations

import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, quote_plus
from urllib.parse import urlparse

from loguru import logger
from app.amazon_api import search_amazon_products
import os
RF_ENABLED = (os.getenv("RAINFOREST_ENABLED") or "1").lower() in ("1", "true", "yes")

AMAZON_TAG = "schizobestie-20"
RETAILER_SEARCH = {
    # retailer domain -> search URL format
    "freepeople.com": "https://www.freepeople.com/s/?q={q}",
    "sephora.com":    "https://www.sephora.com/search?keyword={q}",
    "ulta.com":       "https://www.ulta.com/shop/SearchDisplay?searchTerm={q}",
    "nordstrom.com":  "https://www.nordstrom.com/sr?keyword={q}",
}

RETAILER_KEYWORDS = {
    # user keywords -> retailer domain
    "free people": "freepeople.com",
    "freepeople":  "freepeople.com",
    "sephora":     "sephora.com",
    "ulta":        "ulta.com",
    "nordstrom":   "nordstrom.com",
}

def _dp_link(asin: str) -> str:
    """Build a clean Amazon DP link with affiliate tag."""
    asin = (asin or "").strip().upper()
    if not asin:
        return ""
    return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_TAG}"

def _normalize(items: List[Dict]) -> List[Dict]:
    """
    Normalize heterogeneous vendor rows into a single shape the rest of the app expects.
    Keys kept: title, name, url, review, merchant (+ meta passthrough).
    Prefer clean DP links using ASIN; only fall back to Amazon search when no ASIN is available.
    """
    out: List[Dict] = []
    for it in items or []:
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            continue

        raw_url = (it.get("url") or it.get("link") or "").strip()
        merchant = (it.get("merchant") or "amazon.com").strip()
        meta = it.get("meta") or {}

        # Try to find an ASIN in common places from different providers
        asin = (
            it.get("asin") or it.get("ASIN") or
            (it.get("product") or {}).get("asin") or
            meta.get("asin")
        )
        # If none, try to extract from any raw Amazon URL
        if not asin and "amazon." in (raw_url or ""):
            m = re.search(r"/dp/([A-Z0-9]{10})(?:[/?]|$)", raw_url, flags=re.I)
            if m:
                asin = m.group(1).upper()

        # Determine final URL
        if asin:
            final_url = _dp_link(asin)
        else:
            # No ASIN — prefer the given URL if it's already amazon search/product; else build a clean search
            if raw_url and "amazon." in raw_url:
                final_url = raw_url
                # append tag if missing
                if "tag=" not in final_url:
                    sep = "&" if "?" in final_url else "?"
                    final_url = f"{final_url}{sep}tag={AMAZON_TAG}"
            else:
                q = quote_plus(title)
                final_url = f"https://www.amazon.com/s?k={q}&tag={AMAZON_TAG}"

        out.append({
            "title": title,
            "name": (it.get("name") or title).strip(),
            "url": final_url,             # now always a DP link (when ASIN exists) or clean search
            "review": (it.get("review") or "").strip(),
            "merchant": merchant,
            "meta": meta,
        })
    return out
def dedupe_products(products: List[Dict]) -> List[Dict]:
    """
    Remove duplicate products by ASIN or name.
    Keeps the first unique product and drops the rest.
    """
    seen = set()
    unique: List[Dict] = []
    for p in products or []:
        key = p.get("asin") or p.get("name") or p.get("title")
        if not key:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique

def _fallback_from_query(query: str, max_items: int = 3) -> List[Dict]:
    """
    If APIs fail or env keys are missing, create name-only candidates.
    Link injection happens later in workers (_ensure_amazon_links) using the numbered format.
    """
    q = (query or "").strip()
    if not q:
        return []
    # Split simple multi-item asks: "A, B or C / D"
    seeds = [s.strip() for s in re.split(r"(?:,|/| and | or )", q) if s.strip()]
    if not seeds:
        seeds = [q]
    # Keep tidy names
    seeds = [s[:80] for s in seeds]
    return [{
    "title": s,
    "name": s,
    "url": f"https://www.amazon.com/s?k={s.replace(' ', '+')}&tag=schizobestie-20",
    "review": "",
    "merchant": "amazon.com"
} for s in seeds[:max_items]]
def _retailer_candidates(query: str, max_items: int = 3) -> List[Dict]:
    q = (query or "").strip()
    low = q.lower()
    # pick first retailer mentioned in the query
    for kw, host in RETAILER_KEYWORDS.items():
        if kw in low:
            url_tpl = RETAILER_SEARCH.get(host)
            if not url_tpl:
                break
            url = url_tpl.format(q=quote_plus(q))
            # shape must match _normalize() output keys
            return [{
                "title": q,
                "name": q,
                "url": url,
                "review": "",
                "merchant": host,
                "meta": {"retailer": host},
            }]
    label = host.split(".")[0].title()  # e.g. "Nordstrom"
    q_clean = re.sub(
        r"\$?\d{2,4}(\s*[-–]\s*\$?\s*\d{2,4})?|\b(quality|cheap|not|amazon|please)\b",
        "",
        low,
        flags=re.I,
    )
    q_clean = re.sub(r"\s{2,}", " ", q_clean).strip() or q
    title = f"{label} — {q_clean}"

    return [{
        "title": title,
        "name": title,
        "url": url,
        "review": "",
        "merchant": host,
        "meta": {"retailer": host, "source": "retailer_search"},
    }]


# -------------------- PATCH: small guards only -------------------- #
_LOW_SIGNAL_FILLERS = {"hi", "hey", "hello", "help", "blah", "ok", "okay", "yo", "hey there"}

def _is_low_signal(q: str) -> bool:
    """
    Returns True when query is too weak to safely drive shopping.
    - less than 3 alnum chars after cleaning
    - or filler words only
    """
    if not q:
        return True
    clean = re.sub(r"[^A-Za-z0-9 ]+", "", q).strip().lower()
    if not clean:
        return True
    if clean in _LOW_SIGNAL_FILLERS:
        return True
    # fewer than 3 alphanumeric characters (e.g., "y", "go", "idk")
    alnum = re.sub(r"[^A-Za-z0-9]", "", clean)
    return len(alnum) < 3

def _looks_itemized(q: str) -> bool:
    """
    True if user listed multiple items (comma, slash, 'and', 'or'):
    only then do we use name-only fallback to keep some utility.
    """
    return bool(re.search(r"(,|/| and | or )", q))
# ----------------------------------------------------------------- #


def build_product_candidates(intent: Optional[Dict]) -> List[Dict]:
    """
    Main entry used by workers.py.
      - Accepts parsed intent dict (from ai_intent.extract_product_intent)
      - Calls Amazon search with constraints
      - Normalizes the output
      - Falls back to name-only candidates if needed (so Amazon search links can be injected later)
    """
    if not intent or intent.get("intent") != "find_products":
        return []

    query = (intent.get("query") or "").strip()
    constraints = intent.get("constraints") or {}
# NEW: if user asked for a specific retailer, build a retailer search link
    retail = _retailer_candidates(query, max_items=int(constraints.get("count") or 3))
    if retail:
        logger.info("[ProductSearch] Retailer-hint for '{}': {}", query, retail[0].get("merchant"))
        # Let link layer wrap with SYL; keep only a few to avoid spam
        return retail[:3]

    # PATCH: if the query is low-signal, skip shopping entirely → let Bestie chat
    if _is_low_signal(query):
        logger.info("[ProductSearch] Low-signal query '{}'; returning []", query)
        return []
    if not RF_ENABLED:
        logger.info("[ProductSearch] RF disabled by env; returning [] for '{}'", query)
        return []

    try:
        # Always try Amazon first for monetization coverage.
        rows = search_amazon_products(query, max_results=10, constraints=constraints)
        if rows:
            norm = _normalize(rows)
            norm = dedupe_products(norm)  # ✅ dedupe here
            logger.info("[ProductSearch] {} candidates from Amazon for '{}'", len(norm), query)
            return norm

        
        # PATCH: only use fallback when the query looks itemized (A, B or C).
        if _looks_itemized(query):
            fallback = _fallback_from_query(query, max_items=int(constraints.get("count") or 3))
            logger.info("[ProductSearch] Amazon empty; using {} fallback seed(s) for '{}'", len(fallback), query)
            return fallback

        logger.info("[ProductSearch] Amazon empty; returning [] for non-itemized query '{}'", query)
        return []

    except Exception as e:
        # PATCH: on API errors (e.g., 500 / 402), avoid junk fallbacks for non-itemized single-word junk.
        msg = str(e)
        if any(code in msg for code in ("402", "500", "503", "Client Error", "Server Error")) and not _looks_itemized(query):
            logger.warning("[ProductSearch] RF/HTTP error for '{}'; no fallback due to non-itemized query. Err={}", query, msg)
            return []
        logger.exception("[ProductSearch] Error building candidates: {}", e)
        return _fallback_from_query(query, max_items=int((intent.get("constraints") or {}).get("count") or 3))

def prefer_amazon_first(candidates: List[Dict]) -> List[Dict]:
    if not candidates:
        return []

    def _is_amz(u: str) -> bool:
        try: return "amazon." in urlparse(u or "").netloc.lower()
        except Exception: return False

    if all(_is_amz(c.get("url") or "") for c in candidates):
        return candidates  # preserve RF ranking

    with_idx = list(enumerate(candidates))
    with_idx.sort(key=lambda t: (0 if not _is_amz(t[1].get("url") or "") else 1, t[0]))
    return [row for _, row in with_idx]


