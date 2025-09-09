# app/task_queue.py
from __future__ import annotations

from typing import Optional, List

import os
import hashlib
from urllib.parse import urlparse

from loguru import logger
from rq import Queue
from redis import Redis

# Worker entrypoints
from app import workers


# ---------- Env / defaults ----------
REDIS_URL = os.getenv("REDIS_URL", "").strip()
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

QUEUE_NAME = os.getenv("QUEUE_NAME", "bestie_queue").strip()

# Keep aligned with worker defaults (override via Render env if needed)
JOB_TIMEOUT_SEC = int(os.getenv("WORKER_JOB_TIMEOUT", "240"))
RESULT_TTL_SEC = int(os.getenv("WORKER_RESULT_TTL", "900"))

# Short enqueue de-dupe window to prevent accidental double-enqueues (double webhooks, retries)
ENQUEUE_DEDUPE_TTL_SEC = int(os.getenv("ENQUEUE_DEDUPE_TTL_SEC", "8"))


# ---------- Redis / Queue ----------
redis = Redis.from_url(REDIS_URL)
q = Queue(QUEUE_NAME, connection=redis, default_timeout=JOB_TIMEOUT_SEC)

# Boot visibility
host = urlparse(REDIS_URL).hostname
logger.info("[API][QueueBoot] Host={} Queue={} REDIS={}", host, q.name, REDIS_URL)
logger.info("[Gate][Config] DEV_BYPASS_PHONE={}", os.getenv("DEV_BYPASS_PHONE"))


# ---------- De-dupe helpers ----------
def _enqueue_key(convo_id: int, user_id: int, text_val: str, user_phone: Optional[str]) -> str:
    """
    Compose a stable hash to de-dupe the same message that may arrive twice (e.g., webhook retry).
    """
    digest = hashlib.sha256(
        f"{convo_id}|{user_id}|{user_phone or ''}|{text_val or ''}".encode()
    ).hexdigest()
    return f"bestie:enqueue:{digest}"


def _should_skip_enqueue(key: str) -> bool:
    """
    Return True if this payload was enqueued very recently.
    Uses SETNX + expire in Redis; fail-open on Redis hiccups.
    """
    if ENQUEUE_DEDUPE_TTL_SEC <= 0:
        return False
    try:
        # SETNX semantics: if key did not exist, set it and allow; otherwise, skip
        if redis.setnx(key, "1"):
            redis.expire(key, ENQUEUE_DEDUPE_TTL_SEC)
            return False
        return True
    except Exception:
        # If Redis hiccups, fail open and proceed
        return False


# ---------- Public API ----------
def enqueue_generate_reply(
    convo_id: int,
    user_id: int,
    text_val: str,
    user_phone: Optional[str] = None,
    media_urls: Optional[List[str]] = None,
):
    key = _enqueue_key(convo_id, user_id, text_val, user_phone)
    logger.info("[Queue] enqueue key=%s", key)

    if _should_skip_enqueue(key):
        logger.warning("[Queue] duplicate suppressed convo_id=%s user_id=%s", convo_id, user_id)
        return type("JobStub", (), {"id": f"dedup-{key[-8:]}"})()

    logger.info(
        "[Queue] enqueue_generate_reply → q=%s user_id=%s convo_id=%s media_cnt=%d",
        getattr(q, "name", "bestie_queue"), user_id, convo_id, len(media_urls or []),
    )
    job = q.enqueue(
        workers.generate_reply_job,
        convo_id,
        user_id,
        text_val,
        user_phone=user_phone,
        media_urls=media_urls,
        job_timeout=JOB_TIMEOUT_SEC,
        result_ttl=RESULT_TTL_SEC,
    )
    return job

def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    """
    If you use a link wrapper job, import it lazily and enqueue here.
    This is optional and safe to keep as a no-op if wrap_link_job is not present.
    """
    try:
        from app.linkwrap import wrap_link_job
    except Exception:
        logger.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60, result_ttl=300)
