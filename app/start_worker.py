import os
import time
from urllib.parse import urlparse
from loguru import logger
from redis import Redis
from rq import Worker, Queue, Connection

QUEUE_NAME = "bestie_queue"
REDIS_URL  = os.getenv("REDIS_URL")

def main():
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not set. Configure it in Render on BOTH services.")

    # 🔊 prove the right file is running
    logger.info("[Worker][BOOTMARK] start_worker v3 running pid={} cwd={}", os.getpid(), os.getcwd())

    host = urlparse(REDIS_URL).hostname
    logger.info("[Worker][Boot] Host={} Queue={}", host, QUEUE_NAME)

    redis_conn = Redis.from_url(REDIS_URL)
    with Connection(redis_conn):
        # bind to the same connection everywhere
        q = Queue(QUEUE_NAME, connection=redis_conn)
        logger.info("[Worker][Boot] Using REDIS_URL={} queue='{}'", REDIS_URL, q.name)

        # make sure RQ can resolve "app.workers.generate_reply_job"
        from app import workers  # noqa: F401

        # heartbeat: log queue depth every 5s
        def heartbeat():
            while True:
                try:
                    logger.info("[Worker][HB] Queue '{}' depth={}", q.name, len(q.jobs))
                except Exception as e:
                    logger.error("[Worker][HB] Error: {}", e)
                time.sleep(5)

        import threading
        threading.Thread(target=heartbeat, daemon=True).start()

        worker = Worker(
    [q],
    name=f"bestie-worker-{os.getenv('RENDER_INSTANCE_ID', os.getpid())}"
)
        logger.info("🚀 bestie-worker is online, listening on '{}'…", q.name)
        worker.work(logging_level="INFO")

if __name__ == "__main__":
    main()
