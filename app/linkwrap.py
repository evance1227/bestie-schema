# app/linkwrap.py
from __future__ import annotations

import os
import time
import random
from typing import Optional

import httpx
from loguru import logger

# Config
GENIUSLINK_API_KEY = os.getenv("GENIUSLINK_API_KEY")
GENIUSLINK_API_BASE = os.getenv("GENIUSLINK_API_BASE", "https://api.geni.us/v1/shorten")


def _should_convert(url: str) -> bool:
    """
    Only try to convert real http(s) links that aren't already geni.us.
    """
    if not url:
        return False
    u = url.strip()
    if "geni.us" in u:
        return False
    return u.startswith("http://") or u.startswith("https://")


def convert_to_geniuslink(raw_url: str, retries: int = 3, backoff: float = 1.5) -> str:
    """
    Convert a raw product URL into a Geniuslink short/affiliated URL.
    Retries on transient failures and falls back to the original URL.
    """
    if not _should_convert(raw_url):
        return raw_url or ""

    if not GENIUSLINK_API_KEY:
        logger.warning("⚠️ GENIUSLINK_API_KEY missing; skipping conversion for {}", raw_url)
        return raw_url  # graceful fallback

    headers = {"Authorization": f"Bearer {GENIUSLINK_API_KEY}"}
    payload = {"url": raw_url}

    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.post(GENIUSLINK_API_BASE, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                short = (data or {}).get("shortLink")
                if short and "geni.us" in short:
                    logger.success("🔗 Geniuslink created: {} -> {}", raw_url, short)
                    return short
                logger.warning("⚠️ Geniuslink response missing shortLink (attempt {}) for {}", attempt, raw_url)
            elif resp.status_code in (429, 500, 502, 503, 504):
                # Retry on transient/server errors
                logger.error("❌ Geniuslink transient error (attempt {}): {} {}", attempt, resp.status_code, resp.text)
            else:
                # Non-retryable
                logger.error("❌ Geniuslink API failed: status={} body={}", resp.status_code, resp.text)
                break

        except Exception as e:
            logger.exception("💥 Exception in convert_to_geniuslink (attempt {}) for {}", attempt, raw_url)

        # Exponential backoff with a little jitter
        if attempt < retries:
            sleep_s = backoff ** attempt + random.uniform(0, 0.25)
            time.sleep(sleep_s)

    logger.error("❌ All Geniuslink attempts failed for {}, falling back to raw", raw_url)
    return raw_url


# RQ job wrapper – used by enqueue_wrap_link()
def wrap_link_job(convo_id: int, raw_url: str, campaign: str = "default") -> str:
    """
    Background job: turn a raw URL into a Geniuslink short link.
    Returns the short URL (or the original if conversion fails).
    """
    logger.info("[Linkwrap][Job] convo_id={} campaign={} url={}", convo_id, campaign, raw_url)
    short = convert_to_geniuslink(raw_url)
    logger.info("[Linkwrap][Job] result={}", short)
    return short
