# app/start_worker.py
"""
Bestie Worker bootstrap (add-only changes, reliability-focused).

Flow:
  1) Boot logs + masked env snapshot (sanity)
  2) Connect + ping Redis
  3) Import app.workers so import errors surface at boot
  4) Build Queue (env-driven) and log initial depth
  5) Start a heartbeat that logs current depth regularly
  6) Run a single RQ Worker with env-driven timeouts/TTLs
"""

import os
import sys
import time
import threading
from urllib.parse import urlparse

from loguru import logger
from redis import Redis
from rq import Worker, Queue, Connection

# Optional scheduler (off unless ENABLE_RQ_SCHEDULER=1)
try:
    from rq.scheduler import Scheduler  # noqa: F401
except Exception:  # pragma: no cover
    Scheduler = None

# ---------------------- Config (env) ---------------------- #
REDIS_URL = os.getenv("REDIS_URL")                             # required
QUEUE_NAME = os.getenv("QUEUE_NAME", "bestie_queue")           # same queue the web enqueues to

# tune via Render envs; safe defaults
WORKER_HEARTBEAT_SEC = int(os.getenv("WORKER_HEARTBEAT_SEC", "10"))
WORKER_JOB_TIMEOUT   = int(os.getenv("WORKER_JOB_TIMEOUT", "240"))
WORKER_RESULT_TTL    = int(os.getenv("WORKER_RESULT_TTL", "900"))

# ---------------------- Helpers --------------------------- #
def _mask(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    return s[:keep] + "…" + s[-keep:] if len(s) > keep * 2 else "***"

def _env_snap() -> None:
    """Mask-sensitive snapshot so we can verify the worker sees the right envs at boot."""
    snap = {
        "QUEUE_NAME": QUEUE_NAME,
        "REDIS_URL": _mask(os.getenv("REDIS_URL") or ""),
        "DATABASE_URL": _mask(os.getenv("DATABASE_URL") or ""),
        "OPENAI_API_KEY": _mask(os.getenv("OPENAI_API_KEY") or ""),
        "GHL_OUTBOUND_WEBHOOK_URL": _mask(os.getenv("GHL_OUTBOUND_WEBHOOK_URL") or ""),
        "JOB_TIMEOUT": WORKER_JOB_TIMEOUT,
        "RESULT_TTL": WORKER_RESULT_TTL,
        "HB_SEC": WORKER_HEARTBEAT_SEC,
    }
    logger.info("[Worker][ENV] {}", snap)

def _heartbeat(q: Queue, r: Redis, stop_flag: threading.Event) -> None:
    """
    Emit queue depth regularly so we can see liveness in logs and a simple key in Redis.
    (Non-invasive; if it fails, the worker still works.)
    """
    key = f"bestie:worker:hb:{os.getpid()}"
    while not stop_flag.is_set():
        try:
            depth = q.count
            r.setex(key, WORKER_HEARTBEAT_SEC * 3, str(time.time()))
            logger.info("[Worker][HB] Queue '{}' depth={}", q.name, depth)
        except Exception as e:
            logger.error("[Worker][HB] error: {}", e)
        stop_flag.wait(WORKER_HEARTBEAT_SEC)

# ---------------------- Main ------------------------------ #
def main() -> None:
    logger.info("[Worker][BOOT] start_worker online pid={} cwd={}", os.getpid(), os.getcwd())

    if not REDIS_URL:
        logger.error("[Worker][BOOT] REDIS_URL is not set; cannot start worker.")
        sys.exit(1)

    _env_snap()

    # Connect Redis and fail fast if unreachable
    try:
        redis_conn = Redis.from_url(REDIS_URL)
        redis_conn.ping()
    except Exception as e:
        logger.exception("[Worker][BOOT] Redis ping failed: {}", e)
        sys.exit(1)

    # Import job module up front so any error is visible at boot
    try:
        import app.workers as _w  # noqa: F401
        logger.info("[Worker][BOOT] Imported app.workers successfully")
    except Exception as e:
        logger.exception("[Worker][BOOT] Failed to import app.workers: {}", e)
        sys.exit(1)

    host = urlparse(REDIS_URL).hostname or "unknown-host"

    with Connection(redis_conn):
        # Queue (env-driven) + initial depth
        q = Queue(QUEUE_NAME, connection=redis_conn, default_timeout=WORKER_JOB_TIMEOUT)
        try:
            logger.info("[Worker][BOOT] Redis host={} queue='{}' initial depth={}", host, q.name, q.count)
        except Exception as e:
            logger.warning("[Worker][BOOT] Could not read initial depth: {}", e)

        # Optional: RQ scheduler (off unless ENABLE_RQ_SCHEDULER=1)
        if os.getenv("ENABLE_RQ_SCHEDULER") == "1" and Scheduler is not None:
            try:
                scheduler = Scheduler(queue=q, connection=redis_conn)
                threading.Thread(target=scheduler.run, daemon=True).start()
                logger.info("[Worker][BOOT] RQ Scheduler thread started")
            except Exception as e:
                logger.warning("[Worker][BOOT] Scheduler not running: {}", e)

        # Heartbeat thread (keeps your existing log style)
        stop_flag = threading.Event()
        threading.Thread(target=_heartbeat, args=(q, redis_conn, stop_flag), daemon=True).start()

        # RQ worker (single queue; env-driven TTL)
        worker = Worker([q], connection=redis_conn, default_worker_ttl=WORKER_RESULT_TTL)
        logger.info("🚀 bestie-worker is listening on '{}' (job_timeout={}s, result_ttl={}s)",
                    q.name, WORKER_JOB_TIMEOUT, WORKER_RESULT_TTL)

        # Block here; ctrl-C / SIGTERM handled by RQ
        worker.work(logging_level="INFO")

if __name__ == "__main__":
    main()
