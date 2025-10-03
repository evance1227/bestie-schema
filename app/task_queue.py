# app/task_queue.py
from __future__ import annotations
from typing import Optional, List

import os
import logging
import hashlib

from rq import Queue
from redis import Redis

# job module
from app import workers


# ---------- Env / defaults ----------
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

QUEUE_NAME = os.getenv("QUEUE_NAME", "bestie_queue").strip()

# Guard: ensure we didn't fat-finger the name
EXPECTED_QUEUE = "bestie_queue"
if QUEUE_NAME != EXPECTED_QUEUE:
    raise RuntimeError(f"Wrong QUEUE_NAME={QUEUE_NAME}, expected {EXPECTED_QUEUE}")

# One global Queue object for the API process
q = Queue(QUEUE_NAME, connection=Redis.from_url(REDIS_URL))

logging.info("[QueueBoot] queue=%s redis=%s", QUEUE_NAME, REDIS_URL)

# Keep aligned with worker defaults (override via Render env if needed)
JOB_TIMEOUT_SEC  = int(os.getenv("WORKER_JOB_TIMEOUT", "240"))
RESULT_TTL_SEC   = int(os.getenv("WORKER_RESULT_TTL", "900"))

# Short enqueue de-dupe window to prevent accidental double-enqueues (double webhooks, retries)
ENQUEUE_DEDUPE_TTL_SEC = int(os.getenv("ENQUEUE_DEDUPE_TTL_SEC", "8"))

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
        r = q.connection  # reuse the same Redis connection as the Queue
        if r.setnx(key, "1"):
            r.expire(key, ENQUEUE_DEDUPE_TTL_SEC)
            return False
        return True
    except Exception:
        return False
    
# ---------- Public API ----------
def enqueue_generate_reply(
    q,                                  # ← expects a Queue instance
    convo_id: int,
    user_id: int,
    text_val: str,
    user_phone: Optional[str] = None,
    media_urls: Optional[List[str]] = None,
    msg_id: Optional[str] = None,
):
    key = msg_id or _enqueue_key(convo_id, user_id, text_val, user_phone)
    logging.info("[Queue] enqueue key=%s", key)

    if _should_skip_enqueue(key):
        logging.warning("[Queue] duplicate suppressed convo_id=%s user_id=%s", convo_id, user_id)
        return type("JobStub", (), {"id": f"dedup-{key[-8:]}"})()

    logging.info(
        "[Queue] enqueue_generate_reply → q=%s user_id=%s convo_id=%s media_cnt=%d",
        getattr(q, "name", "bestie_queue"), user_id, convo_id, len(media_urls or [])
    )

    # IMPORTANT: string task path + primitive args → RQ can pickle
    job = q.enqueue(
        "app.workers.generate_reply_job",
        args=(user_id, convo_id, text_val),
        kwargs={
            "media_urls": (media_urls or []),
            "user_phone": user_phone,
        },
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
        logging.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60, result_ttl=300)
