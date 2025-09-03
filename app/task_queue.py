import os
from urllib.parse import urlparse
from loguru import logger
from rq import Queue
from redis import Redis
from app import workers

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

redis = Redis.from_url(REDIS_URL)
q = Queue("bestie_queue", connection=redis)

# ✅ The ONLY enqueue_generate_reply function
def enqueue_generate_reply(convo_id: int, user_id: int, text_val: str, *, user_phone: str | None = None):
    logger.info("[Queue] enqueue_generate_reply user_phone={}", user_phone)
    return q.enqueue(
        workers.generate_reply_job,
        args=(convo_id, user_id, text_val),
        kwargs={"user_phone": user_phone},  # <-- pass phone via kwargs
        job_timeout=600,
        result_ttl=500,
    )

# Optional: boot visibility
host = urlparse(REDIS_URL).hostname
logger.info("[API][QueueBoot] Host={} Queue={} REDIS={}", host, q.name, REDIS_URL)
logger.info("[Gate][Config] DEV_BYPASS_PHONE={}", os.getenv("DEV_BYPASS_PHONE"))

# ✅ KEEP your affiliate link wrapper as-is
def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    """
    If you use a link wrapper job, import it lazily too.
    """
    try:
        from app.linkwrap import wrap_link_job
    except Exception:
        logger.warning("[API][Queue] wrap_link_job not found; skipping enqueue.")
        return None

    return q.enqueue(wrap_link_job, convo_id, raw_url, campaign, job_timeout=60)
