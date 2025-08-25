from dotenv import load_dotenv
load_dotenv()

import os
from rq import Queue
from redis import Redis

redis = Redis.from_url(os.environ.get("REDIS_URL"))
q = Queue("bestie_queue", connection=redis)

def enqueue_generate_reply(convo_id: int, user_id: int, text: str):
    from app.workers import generate_reply_job
    return q.enqueue(
        generate_reply_job,
        convo_id, user_id, text,
        job_timeout=120
    )


def enqueue_wrap_link(convo_id: int, raw_url: str, campaign: str = "default"):
    from app.workers import wrap_link_job
    return q.enqueue(
        wrap_link_job,
        convo_id, raw_url, campaign,
        job_timeout=60,      # removed Retry(...)
    )
