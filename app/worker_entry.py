# app/worker_entry.py
"""
Compatibility entry point for older Procfiles or local runs.
Delegates to app.start_worker.main() so all config (heartbeat, timeouts,
env diagnostics, graceful shutdown) stays centralized.

If start_worker import fails, we fall back to a minimal RQ worker that reads:
  - REDIS_URL
  - QUEUE_NAME (default: bestie_queue)
"""

from __future__ import annotations

import os
from loguru import logger
import os
logger.info("[env] GL_REWRITE=%s GL_ALLOW_REDIRECT_TEMPLATE=%s GL_UNWRAP_REDIRECTS=%s AMAZON_CANONICALIZE=%s TAG=%s",
            os.getenv("GL_REWRITE"), os.getenv("GL_ALLOW_REDIRECT_TEMPLATE"),
            os.getenv("GL_UNWRAP_REDIRECTS"), os.getenv("AMAZON_CANONICALIZE"),
            os.getenv("AMAZON_ASSOC_TAG"))

def _fallback_worker() -> None:
    from redis import Redis
    from rq import Worker, Queue, Connection

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")

    queue_name = os.getenv("QUEUE_NAME", "bestie_queue")
    logger.info("[worker_entry][fallback] Connecting to Redis={} queue='{}'", redis_url, queue_name)

    conn = Redis.from_url(redis_url)
    with Connection(conn):
        worker = Worker([Queue(queue_name)])
        logger.info("🚀 bestie-worker is online (fallback), listening on '{}'", queue_name)
        worker.work(logging_level="INFO")

def main() -> None:
    try:
        # Preferred path: reuse the robust start_worker logic
        from app.start_worker import main as start_main
        start_main()
    except Exception as e:
        logger.exception("[worker_entry] Failed to import/use app.start_worker: {}", e)
        _fallback_worker()

if __name__ == "__main__":
    main()
