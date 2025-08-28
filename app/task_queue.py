# app/task_queue.py
from dotenv import load_dotenv
load_dotenv()

import os
from loguru import logger
from rq import Queue
from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL")  # must match in BOTH services
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is not set")

redis = Redis.from_url(REDIS_URL)
q = Queue("bestie_queue", connection=redis)

# Boot visibility
logger.info("[API][QueueBoot] Using REDIS_URL={} queue={}", REDIS_URL, q.name)

def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    """
    Always enqueue the GPT reply job — no deduplication logic.
    """
    # IMPORTANT: use import-path string so the worker imports the callable itself
    job = q.enqueue(
        "app.workers.generate_reply_job",
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
