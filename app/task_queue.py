from dotenv import load_dotenv
load_dotenv()

import os
from urllib.parse import urlparse
from loguru import logger
from rq import Queue
from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

redis = Redis.from_url(REDIS_URL)
q = Queue("bestie_queue", connection=redis)

host = urlparse(REDIS_URL).hostname
logger.info("[API][QueueBoot] Host={} Queue={}", host, q.name)
logger.info("[API][QueueBoot] Using REDIS_URL={} queue={}", REDIS_URL, q.name)


def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    # Lazy import to avoid startup/circular import issues
    from app.workers import generate_reply_job
    job = q.enqueue(
        generate_reply_job,   # function object
        convo_id,
        user_id,
        text,
        job_timeout=120,
        result_ttl=500,
    )
    logger.info("[API][Queue] Enqueued job_id={} -> queue='{}' redis='{}'", job.id, q.name, REDIS_URL)
    return job


def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    # Import from the module that actually defines it (adjust if yours differs)
    try:
        from app.linkwrap import wrap_link_job   # if you have linkwrap.py
    except Exception:
        from app.workers import wrap_link_job    # fallback if it lives in workers.py
    job = q.enqueue(
        wrap_link_job,
        convo_id,
        raw_url,
        campaign,
        job_timeout=60,
    )
    logger.info("[API][Queue] Enqueued wrap_link job_id={} -> queue='{}' redis='{}'", job.id, q.name, REDIS_URL)
    return job
