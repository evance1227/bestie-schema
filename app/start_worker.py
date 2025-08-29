# app/start_worker.py
import os
import time
from urllib.parse import urlparse
from loguru import logger
from redis import Redis
from rq import Worker, Queue, Connection

QUEUE_NAME = "bestie_queue"
REDIS_URL  = os.getenv("REDIS_URL")

def main():
    logger.info("[Worker][BOOTMARK] start_worker v3 running pid={} cwd={}", os.getpid(), os.getcwd())
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not set. Configure it in Render on BOTH services.")

    host = urlparse(REDIS_URL).hostname
    logger.info("[Worker][Boot] Host={} Queue={}", host, QUEUE_NAME)

    redis_conn = Redis.from_url(REDIS_URL)
    with Connection(redis_conn):
        q = Queue(QUEUE_NAME)
        logger.info("[Worker][Boot] Using REDIS_URL={} queue='{}'", REDIS_URL, q.name)

        # heartbeat
        def heartbeat():
            while True:
                try:
                    logger.info("[Worker][HB] Queue '{}' depth={}", q.name, len(q.jobs))
                except Exception as e:
                    logger.error("[Worker][HB] Error: {}", e)
                time.sleep(5)

        import threading
        threading.Thread(target=heartbeat, daemon=True).start()

        # Let RQ generate a unique worker name (prevents “active worker already”)
        worker = Worker([q])
        logger.info("🚀 bestie-worker is online, listening on '{}'…", q.name)
        worker.work(logging_level="INFO")

if __name__ == "__main__":
    main()
