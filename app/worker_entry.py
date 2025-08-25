import os
from redis import Redis
from rq import Worker, Queue, Connection
from loguru import logger

# ✅ Use relative import
from . import workers

redis_url = os.getenv("REDIS_URL", "rediss://default:...@clever-lion-42257.upstash.io:6379")
logger.info("🔗 Connecting to Redis at {}", redis_url)

conn = Redis.from_url(redis_url)

if __name__ == "__main__":
    with Connection(conn):
        worker = Worker([Queue("bestie_queue")])
        logger.info("🚀 bestie-worker is online, listening on 'bestie_queue'")
        worker.work()
