# app/task_queue.py
from __future__ import annotations
from typing import Optional, List

import os
import logging
import hashlib

from rq import Queue
from redis import Redis

logger = logging.getLogger(__name__)

# ---------- Env / defaults ----------
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

QUEUE_NAME = (os.getenv("QUEUE_NAME", "bestie_queue") or "").strip()

# Guard: ensure we didn't fat-finger the name (keeps API & worker in lockstep)
EXPECTED_QUEUE = "bestie_queue"
if QUEUE_NAME != EXPECTED_QUEUE:
    raise RuntimeError(f"Wrong QUEUE_NAME={QUEUE_NAME}, expected {EXPECTED_QUEUE}")

# One global Queue object for the API process
# Upstash/Render friendly: ssl_cert_reqs=None is OK for rediss
redis_conn: Redis = Redis.from_url(REDIS_URL, ssl_cert_reqs=None)
q: Queue = Queue(QUEUE_NAME, connection=redis_conn)

logger.info("[QueueBoot] queue=%s redis=%s", QUEUE_NAME, REDIS_URL)

# Keep aligned with worker defaults (override via Render env if needed)
JOB_TIMEOUT_SEC = int(os.getenv("WORKER_JOB_TIMEOUT", "240"))
RESULT_TTL_SEC  = int(os.getenv("WORKER_RESULT_TTL", "900"))

# Short enqueue de-dupe window to prevent accidental double-enqueues (double webhooks, retries)
ENQUEUE_DEDUPE_TTL_SEC = int(os.getenv("ENQUEUE_DEDUPE_TTL_SEC", "8"))

# ---------- De-dupe helpers ----------
def _enqueue_key(convo_id: int, user_id: int, text_val: str, user_phone: Optional[str]) -> str:
    """
    Compose a stable hash to de-dupe the same message that may arrive twice (e.g., webhook retry).
    """
    digest = hashlib.sha256(f"{convo_id}|{user_id}|{user_phone or ''}|{text_val or ''}".encode()).hexdigest()
    return f"bestie:enqueue:{digest}"

def _should_skip_enqueue(key: str) -> bool:
    """
    Return True if this payload was enqueued very recently.
    Uses SETNX + EXPIRE in Redis; fail-open on Redis hiccups.
    """
    if ENQUEUE_DEDUPE_TTL_SEC <= 0:
        return False
    try:
        r = q.connection  # reuse the same Redis connection as the Queue
        if r.setnx(key, "1"):
            r.expire(key, ENQUEUE_DEDUPE_TTL_SEC)
            return False
        return True
    except Exception:
        return False

# ---------- Public API ----------
def enqueue_generate_reply(
    q: Queue,                               # pass the canonical Queue (usually `task_queue.q`)
    user_id: int,
    convo_id: int,
    text_val: str,
    *,
    media_urls: Optional[List[str]] = None,
    user_phone: Optional[str] = None,
    msg_id: Optional[str] = None,
):
    """
    Enqueue the core reply job on the SAME queue the worker listens on.
    We pass a string task path so RQ can import lazily (avoids circulars).
    """
    key = msg_id or _enqueue_key(convo_id, user_id, text_val, user_phone)
    logger.info("[Queue] enqueue key=%s", key)

    if _should_skip_enqueue(key):
        logger.warning("[Queue] duplicate suppressed convo_id=%s user_id=%s", convo_id, user_id)
        return type("JobStub", (), {"id": f"dedup-{key[-8:]}"})()

    logger.info(
        "[Queue] enqueue_generate_reply → q=%s user_id=%s convo_id=%s media_cnt=%d",
        getattr(q, "name", "bestie_queue"), user_id, convo_id, len(media_urls or []),
    )

    job = q.enqueue(
        "app.workers.generate_reply_job",     # lazy import by string
        args=(user_id, convo_id, text_val),
        kwargs={
            "media_urls": (media_urls or []),
            "user_phone": user_phone,
        },
        job_timeout=JOB_TIMEOUT_SEC,
        result_ttl=RESULT_TTL_SEC,
    )
    logger.info("[Queue] ✅ enqueued job_id=%s queue=%s", getattr(job, "id", None), q.name)
    return job

def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    """
    Optional link-wrapper job. Safe no-op if wrap_link_job isn't present.
    """
    try:
        from app.linkwrap import wrap_link_job
    except Exception:
        logger.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60, result_ttl=300)
