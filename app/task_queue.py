# app/task_queue.py

from dotenv import load_dotenv
load_dotenv()

import os
from rq import Queue
from redis import Redis

from app import workers

redis = Redis.from_url(os.environ.get("REDIS_URL"))
q = Queue("bestie_queue", connection=redis)

def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    """
    Always enqueue the GPT reply job — no deduplication logic.
    Even repeated or similar messages will generate new replies.
    """
    from app.workers import generate_reply_job
    return q.enqueue(
        generate_reply_job,
        convo_id,
        user_id,
        text,
        job_timeout=120,
        result_ttl=500
    )

def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    from app.workers import wrap_link_job
    return q.enqueue(
        wrap_link_job,
        convo_id,
        raw_url,
        campaign,
        job_timeout=60
    )
