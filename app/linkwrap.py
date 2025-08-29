# app/linkwrap.py
import os
import httpx
import time
from loguru import logger

GENIUSLINK_API_KEY = os.getenv("GENIUSLINK_API_KEY")
GENIUSLINK_API_BASE = "https://api.geni.us/v1/shorten"

def convert_to_geniuslink(raw_url: str, retries: int = 3, backoff: float = 1.5) -> str:
    """
    Convert a raw product URL into a Geniuslink affiliate-safe URL.
    Retries on failure, and falls back to returning raw_url if Geniuslink fails.
    """
    if not raw_url:
        return ""

    if not GENIUSLINK_API_KEY:
        logger.warning("⚠️ Geniuslink API key missing, cannot convert {}", raw_url)
        return raw_url  # fallback to raw

    headers = {"Authorization": f"Bearer {GENIUSLINK_API_KEY}"}
    payload = {"url": raw_url}

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(GENIUSLINK_API_BASE, json=payload, headers=headers)
                if resp.status_code >= 400:
                    logger.error("❌ Geniuslink API failed (attempt {}): status={} body={}",
                                 attempt, resp.status_code, resp.text)
                else:
                    data = resp.json()
                    geni_url = data.get("shortLink")
                    if geni_url and "geni.us" in geni_url:
                        logger.success("🔗 Geniuslink created: {} -> {}", raw_url, geni_url)
                        return geni_url
                    else:
                        logger.warning("⚠️ Geniuslink response missing shortLink (attempt {}) for {}",
                                       attempt, raw_url)
        except Exception as e:
            logger.exception("💥 Exception in convert_to_geniuslink (attempt {}) for {}",
                             attempt, raw_url)

        # Backoff before retry
        if attempt < retries:
            time.sleep(backoff * attempt)

    # Fallback: all retries failed → return raw URL so Bestie still sends something
    logger.error("❌ All Geniuslink attempts failed for {}, falling back to raw", raw_url)
    return raw_url
# RQ job wrapper – safe to enqueue from task_queue
def wrap_link_job(convo_id: int, raw_url: str, campaign: str = "default") -> str:
    """
    Background job: turn a raw URL into a Geniuslink short link.
    Returns the short URL (or the original if conversion fails).
    """
    logger.info("[Linkwrap][Job] convo_id={} campaign={} url={}", convo_id, campaign, raw_url)
    short = convert_to_geniuslink(raw_url)
    logger.success("[Linkwrap][Job] wrapped -> {}", short)
    return short
