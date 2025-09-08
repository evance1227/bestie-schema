from __future__ import annotations

import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, quote_plus

from loguru import logger
from app.amazon_api import search_amazon_products

AMAZON_TAG = "schizobestie-20"

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

    # PATCH: if the query is low-signal, skip shopping entirely → let Bestie chat
    if _is_low_signal(query):
        logger.info("[ProductSearch] Low-signal query '{}'; returning []", query)
        return []

    try:
        # Always try Amazon first for monetization coverage.
        rows = search_amazon_products(query, max_results=10, constraints=constraints)
        if rows:
            norm = _normalize(rows)
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
    """
    Lightweight re-rank that brings Amazon DP links to the top and then sorts by URL presence and name length.
    """
    def _is_amazon(url: str) -> bool:
        try:
            return "amazon." in urlparse(url or "").netloc.lower()
        except Exception:
            return False

    def key(row: Dict) -> Tuple[int, int, int]:
        url = (row.get("url") or "").strip()
        name = (row.get("title") or row.get("name") or "").strip()
        is_amz = 0 if _is_amazon(url) else 1
        has_url = 0 if url else 1  # items with URLs first
        name_len = len(name)
        return (is_amz, has_url, name_len)

    return sorted(candidates or [], key=key)
