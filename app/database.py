"""
Database — SQLAlchemy engine, session factory, and Base with Connection Pool Monitoring
========================================================================================
Exposes:
  - ``Base``                    : Declarative base for all ORM models.
  - ``SessionLocal``            : Session factory (use via ``get_db()`` dependency).
  - ``get_db()``                : FastAPI dependency that yields a DB session per request.
  - ``check_db_connection()``   : Startup health check — logs and returns True/False.
  - ``get_pool_stats()``        : Returns current connection pool statistics.
  - ``PoolMonitor``             : Background task for continuous pool monitoring.
"""

import threading
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("database")


class PoolStats:
    """Track connection pool statistics for monitoring."""
    def __init__(self):
        self.lock = threading.Lock()
        self.total_connections_created = 0
        self.total_checkins = 0
        self.total_checkouts = 0
        self.current_overflow = 0
        self.checkin_failures = 0
        self.checkout_failures = 0

    def record_connect(self, dbapi_conn, connection_record):
        with self.lock:
            self.total_connections_created += 1

    def record_checkin(self, dbapi_conn, connection_record):
        with self.lock:
            self.total_checkins += 1

    def record_checkout(self, dbapi_conn, connection_record, connection_proxy):
        with self.lock:
            self.total_checkouts += 1

    def to_dict(self):
        with self.lock:
            return {
                'total_connections_created': self.total_connections_created,
                'total_checkins': self.total_checkins,
                'total_checkouts': self.total_checkouts,
                'checkin_failures': self.checkin_failures,
                'checkout_failures': self.checkout_failures,
            }


pool_stats = PoolStats()

engine = create_engine(
    settings.DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,                # Increased from 2 to 5 for better concurrency
    max_overflow=2,             # Allow 2 temporary connections during spikes
    pool_pre_ping=True,         # Test connections before use (critical for stability)
    pool_recycle=300,           # Recycle connections every 5 minutes
    pool_timeout=15,            # Increased from 10 to 15 seconds timeout
    echo=False,
    connect_args={
        "application_name": "civic_app",
        "connect_timeout": 10,      # Increased connection timeout
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,  # Send keepalive every 10 seconds
        "keepalives_count": 3,      # Retry 3 times before giving up
    },
)

# Attach pool event listeners for monitoring
@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """Log and track new connections."""
    pool_stats.record_connect(dbapi_conn, connection_record)
    logger.debug("Database connection created")

@event.listens_for(engine, "checkin")
def receive_checkin(dbapi_conn, connection_record):
    """Log and track connection checkins."""
    pool_stats.record_checkin(dbapi_conn, connection_record)

@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_conn, connection_record, connection_proxy):
    """Log and track connection checkouts."""
    pool_stats.record_checkout(dbapi_conn, connection_record, connection_proxy)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency that provides a DB session per request with error handling."""
    from fastapi import HTTPException
    db = SessionLocal()
    try:
        yield db
    except HTTPException:
        # HTTPExceptions are normal application flow (404, 401, etc.) — not DB errors.
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Database session error: {e}", exc_info=True)
        raise
    finally:
        try:
            db.close()
        except Exception as e:
            logger.warning(f"Error closing database session: {e}")


def get_pool_stats() -> dict:
    """Return current connection pool statistics."""
    pool = engine.pool
    return {
        'pool_size': pool.size() if hasattr(pool, 'size') else 'N/A',
        'checked_out': pool.checkedout() if hasattr(pool, 'checkedout') else 'N/A',
        'overflow': pool.overflow() if hasattr(pool, 'overflow') else 'N/A',
        'total_connections_created': pool_stats.total_connections_created,
        'total_checkins': pool_stats.total_checkins,
        'total_checkouts': pool_stats.total_checkouts,
    }


def check_db_connection() -> bool:
    """Run once at startup to verify DB is reachable with retry logic."""
    import time
    max_retries = 3
    retry_delay = 2

    for attempt in range(max_retries):
        conn = None
        try:
            conn = engine.raw_connection()
            # Execute a simple query to ensure the connection works
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            logger.info("Database connection verified successfully")

            # Log initial pool stats
            stats = get_pool_stats()
            logger.info(f"Connection pool stats: {stats}")
            return True
        except Exception as e:
            logger.warning(f"Database connection attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as close_err:
                    logger.error(f"Failed to close connection: {close_err}")

    logger.error(f"Database connection failed after {max_retries} attempts")
    return False


def dispose_pool():
    """Dispose of all connections in the pool (useful for graceful shutdown)."""
    try:
        engine.dispose()
        logger.info("Connection pool disposed")
    except Exception as e:
        logger.error(f"Error disposing connection pool: {e}")