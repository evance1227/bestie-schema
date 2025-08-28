from dotenv import load_dotenv
load_dotenv()

import os
from urllib.parse import urlparse
from loguru import logger
from rq import Queue
from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL")  # must match in BOTH services
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

redis = Redis.from_url(REDIS_URL)
q = Queue("bestie_queue", connection=redis)

# Boot visibility (safe now that REDIS_URL & q exist)
host = urlparse(REDIS_URL).hostname
logger.info("[API][QueueBoot] Host={} Queue={}", host, q.name)
logger.info("[API][QueueBoot] Using REDIS_URL={} queue={}", REDIS_URL, q.name)

def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    job = q.enqueue(
        "app.workers.generate_reply_job",   # import-path string
        convo_id,
        user_id,
        text,
        job_timeout=120,
        result_ttl=500,
    )
    logger.info("[API][Queue] Enqueued job_id={} -> queue='{}' redis='{}'",
                job.id, q.name, REDIS_URL)
    return job

def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    job = q.enqueue(
        "app.workers.wrap_link_job",
        convo_id,
        raw_url,
        campaign,
        job_timeout=60,
    )
    logger.info("[API][Queue] Enqueued wrap_link job_id={} -> queue='{}' redis='{}'",
                job.id, q.name, REDIS_URL)
    return job
