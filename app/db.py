# app/db.py
import os
from dotenv import load_dotenv
load_dotenv()

from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# --- Required ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# --- Optional tuning (safe defaults) ---
DB_POOL_SIZE         = int(os.getenv("DB_POOL_SIZE", "5"))       # base pool
DB_MAX_OVERFLOW      = int(os.getenv("DB_MAX_OVERFLOW", "10"))   # burst capacity
DB_POOL_RECYCLE      = int(os.getenv("DB_POOL_RECYCLE", "280"))  # seconds; < Render idle kill
DB_POOL_TIMEOUT      = int(os.getenv("DB_POOL_TIMEOUT", "10"))   # seconds to wait for a conn
SQLALCHEMY_ECHO      = os.getenv("SQLALCHEMY_ECHO", "0") == "1"  # SQL debug logs
DB_STATEMENT_TIMEOUT = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "60000"))  # 60s
DB_APP_NAME          = os.getenv("DB_APP_NAME", "bestie-backend")

# Psycopg2 / Postgres-only connect args
connect_args = {}
if DATABASE_URL.lower().startswith(("postgres://", "postgresql://")):
    # Statement timeout + application_name help with runaway queries & observability
    connect_args = {
        "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT}",
        "application_name": DB_APP_NAME,
    }

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,                 # kill stale conns automatically
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=DB_POOL_RECYCLE,
    pool_timeout=DB_POOL_TIMEOUT,
    echo=SQLALCHEMY_ECHO,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

@contextmanager
def session():
    """Yield a session with safe commit/rollback semantics."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

# Optional helpers (non-breaking)
def dispose_engine():
    """Close all pooled connections (useful on shutdown hooks)."""
    try:
        engine.dispose()
    except Exception:
        pass