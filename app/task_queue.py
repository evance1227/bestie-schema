# app/task_queue.py
from __future__ import annotations

import os
import hashlib
from urllib.parse import urlparse
from typing import Optional

from loguru import logger
from rq import Queue
from redis import Redis

# Import worker functions (you use this style already)
from app import workers

# ---------- Env ----------
REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

QUEUE_NAME = os.getenv("QUEUE_NAME", "bestie_queue")

# Keep timeouts/result TTL aligned with worker defaults (override in Render envs)
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
    h = hashlib.sha256(f"{convo_id}|{user_id}|{user_phone or ''}|{text_val or ''}".encode()).hexdigest()
    return f"bestie:enqueue:{h}"

def _should_skip_enqueue(key: str) -> bool:
    """Return True if this payload was enqueued very recently."""
    if ENQUEUE_DEDUPE_TTL_SEC <= 0:
        return False
    try:
        # SETNX: if key did not exist, set it and allow; otherwise skip
        if redis.setnx(key, "1"):
            redis.expire(key, ENQUEUE_DEDUPE_TTL_SEC)
            return False
        return True
    except Exception:
        # If Redis hiccups, fail open and proceed
        return False

# ---------- Public API ----------
def enqueue_generate_reply(convo_id: int, user_id: int, text_val: str, *, user_phone: str | None = None):
    """
    Enqueue the reply job. We pass user_phone so workers can apply
    usage gates and per-tier reply-length ceilings correctly.
    """
    # De-dupe guard for rapid duplicate webhooks/retries
    key = _enqueue_key(convo_id, user_id, text_val, user_phone)
    if _should_skip_enqueue(key):
        logger.warning("[Queue] duplicate suppressed convo_id={} user_id={}", convo_id, user_id)
        # Return a stub with an id so caller logs don't break
        return type("JobStub", (), {"id": f"dedup-{key[-8:]}"})()

    logger.info("[Queue] enqueue_generate_reply phone={} chars={}", (user_phone or "")[-4:], len(text_val or ""))

    return q.enqueue(
        workers.generate_reply_job,
        args=(convo_id, user_id, text_val),
        kwargs={"user_phone": user_phone},  # keep passing phone
        job_timeout=JOB_TIMEOUT_SEC,
        result_ttl=RESULT_TTL_SEC,
        ttl=RESULT_TTL_SEC + 60,
        failure_ttl=RESULT_TTL_SEC,
        description=f"reply:{convo_id}:{user_id}",
    )

# Optional: keep your affiliate link wrapper as-is
def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    """
    If you use a link wrapper job, import it lazily too.
    """
    try:
        from app.linkwrap import wrap_link_job
    except Exception:
        logger.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60, result_ttl=300)
