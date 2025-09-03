import os
from loguru import logger
from rq import Queue
from redis import Redis
from app import workers
from urllib.parse import urlparse

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

redis = Redis.from_url(REDIS_URL)
q = Queue("bestie_queue", connection=redis)

def enqueue_generate_reply(convo_id: int, user_id: int, text_val: str, *, user_phone: str | None = None):
    return q.enqueue(
        workers.generate_reply_job,
        args=(convo_id, user_id, text_val),
        kwargs={"user_phone": user_phone},
        job_timeout=600,
        result_ttl=500,
    )

# Boot visibility
host = urlparse(REDIS_URL).hostname
logger.info("[API][QueueBoot] Host={} Queue={}", host, q.name)
logger.info("[API][QueueBoot] Using REDIS_URL={} queue={}", REDIS_URL, q.name)
logger.info("[Gate][Config] DEV_BYPASS_PHONE={}", os.getenv("DEV_BYPASS_PHONE"))

def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    from app.workers import generate_reply_job
    job = q.enqueue(generate_reply_job, convo_id, user_id, text,
                    job_timeout=120, result_ttl=500)
    logger.info("[API][Queue] Enqueued job_id={} -> queue='{}' redis='{}'",
                job.id, q.name, REDIS_URL)
    return job

def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    """If you use a link wrapper job, import it lazily too."""
    try:
        from app.linkwrap import wrap_link_job
    except Exception:
        logger.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60)

