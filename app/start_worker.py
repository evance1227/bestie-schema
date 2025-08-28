from redis import Redis
from rq import Worker, Queue, Connection
import os
import logging

# ✅ Use relative import
from . import workers

logging.basicConfig(level=logging.DEBUG)

redis_url = os.getenv("REDIS_URL")
print(f"🔗 Using Redis URL: {redis_url}")
conn = Redis.from_url(redis_url)

if __name__ == "__main__":
    with Connection(conn):
        q = Queue("bestie_queue")
        worker = Worker([q])
        print("🚀 bestie-worker is starting up and will process jobs...")
        worker.work(with_scheduler=True, logging_level="DEBUG")
