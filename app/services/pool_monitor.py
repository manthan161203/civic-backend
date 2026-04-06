"""
Connection Pool Monitoring Service
===================================
Provides real-time monitoring of database connection pool health.

Monitors:
  - Active connections
  - Connection timeout rates
  - Failed connections
  - Pool exhaustion events
  - Connection recycling
"""

import asyncio
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.core.logger import get_logger
from app.database import get_pool_stats, engine

logger = get_logger("pool_monitor")


class PoolHealthMetrics:
    """Track connection pool health metrics over time."""

    def __init__(self, window_minutes: int = 10):
        self.window_minutes = window_minutes
        self.lock = threading.Lock()
        self.errors: List[Dict] = []
        self.warnings: List[Dict] = []

    def record_error(self, error_type: str, message: str):
        """Record a connection pool error."""
        with self.lock:
            self.errors.append({
                'timestamp': datetime.utcnow(),
                'type': error_type,
                'message': message,
            })
            self._cleanup_old_entries()

    def record_warning(self, message: str):
        """Record a connection pool warning."""
        with self.lock:
            self.warnings.append({
                'timestamp': datetime.utcnow(),
                'message': message,
            })
            self._cleanup_old_entries()

    def _cleanup_old_entries(self):
        """Remove entries older than the window period."""
        cutoff = datetime.utcnow() - timedelta(minutes=self.window_minutes)
        self.errors = [e for e in self.errors if e['timestamp'] > cutoff]
        self.warnings = [w for w in self.warnings if w['timestamp'] > cutoff]

    def get_summary(self) -> Dict:
        """Get a summary of recent errors and warnings."""
        with self.lock:
            return {
                'window_minutes': self.window_minutes,
                'recent_errors': len(self.errors),
                'recent_warnings': len(self.warnings),
                'latest_errors': self.errors[-5:],  # Last 5 errors
                'latest_warnings': self.warnings[-5:],  # Last 5 warnings
            }


# Global metrics instance
metrics = PoolHealthMetrics()


def get_pool_health() -> Dict:
    """Get comprehensive pool health information."""
    try:
        stats = get_pool_stats()
        metrics_summary = metrics.get_summary()

        # Assess health status
        health_status = assess_pool_health(stats)

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'status': health_status['status'],
            'message': health_status['message'],
            'pool_stats': stats,
            'metrics': metrics_summary,
            'recommendations': health_status.get('recommendations', []),
        }
    except Exception as e:
        logger.error(f"Error getting pool health: {e}", exc_info=True)
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'status': 'unknown',
            'message': f"Could not assess pool health: {str(e)}",
            'pool_stats': {},
            'metrics': metrics.get_summary(),
        }


def assess_pool_health(stats: Dict) -> Dict:
    """Assess the health of the connection pool based on statistics."""
    recommendations = []

    try:
        checked_out = stats.get('checked_out', 0)
        pool_size = stats.get('pool_size', 5)
        overflow = stats.get('overflow', 0)

        # Healthy: less than 80% utilization
        if checked_out < (pool_size * 0.8):
            return {
                'status': 'healthy',
                'message': f'Pool is healthy. {checked_out}/{pool_size} connections in use.',
            }

        # Warning: 80-95% utilization
        elif checked_out < (pool_size * 0.95):
            return {
                'status': 'warning',
                'message': f'Pool utilization is high. {checked_out}/{pool_size} connections in use.',
                'recommendations': [
                    'Monitor for connection leaks',
                    'Consider increasing pool_size in database.py',
                    'Review long-running database operations',
                ]
            }

        # Critical: >95% utilization or overflow connections
        else:
            recommendations = [
                'Pool is near exhaustion!',
                'Increase pool_size in database.py',
                'Investigate connection leaks',
                'Check for deadlocks or long-running queries',
                'Consider implementing connection pooling middleware',
            ]

            if overflow > 0:
                recommendations.insert(0, f'Overflow connections active: {overflow}')

            return {
                'status': 'critical',
                'message': f'Pool exhaustion detected! {checked_out}/{pool_size} + {overflow} overflow connections.',
                'recommendations': recommendations,
            }

    except Exception as e:
        logger.warning(f"Error assessing pool health: {e}")
        return {
            'status': 'unknown',
            'message': 'Could not assess pool health',
            'recommendations': ['Check pool statistics manually'],
        }


async def monitor_pool_health(interval_seconds: int = 30):
    """Continuously monitor pool health and log warnings.

    This should be run as a background task.

    Args:
        interval_seconds: Check interval in seconds
    """
    logger.info(f"Starting connection pool monitoring (interval: {interval_seconds}s)")

    while True:
        try:
            health = get_pool_health()
            status = health.get('status', 'unknown')

            if status == 'healthy':
                logger.debug(health.get('message', 'Pool is healthy'))
            elif status == 'warning':
                logger.warning(f"Pool warning: {health.get('message', 'Unknown warning')}")
                metrics.record_warning(health.get('message', 'High pool utilization'))
            elif status == 'critical':
                logger.error(f"Pool critical: {health.get('message', 'Pool exhaustion')}")
                metrics.record_error('pool_exhaustion', health.get('message', 'Pool exhaustion detected'))

                # Log recommendations
                for rec in health.get('recommendations', []):
                    logger.error(f"  - {rec}")

            await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            logger.info("Connection pool monitoring stopped")
            break
        except Exception as e:
            logger.error(f"Error in pool monitoring: {e}", exc_info=True)
            await asyncio.sleep(interval_seconds)


def log_pool_stats():
    """Log current pool statistics (useful for debugging)."""
    try:
        stats = get_pool_stats()
        logger.info("Connection Pool Statistics:")
        for key, value in stats.items():
            logger.info(f"  {key}: {value}")
    except Exception as e:
        logger.error(f"Error logging pool stats: {e}")


class PoolHealthCheckThread(threading.Thread):
    """Background thread for continuous pool health monitoring."""

    def __init__(self, interval_seconds: int = 30, daemon: bool = True):
        super().__init__(daemon=daemon)
        self.interval_seconds = interval_seconds
        self.running = True
        self.name = "PoolHealthCheckThread"

    def run(self):
        """Run the monitoring loop."""
        logger.info(f"Pool health check thread started (interval: {self.interval_seconds}s)")

        while self.running:
            try:
                health = get_pool_health()
                status = health.get('status', 'unknown')

                if status == 'critical':
                    logger.error(f"CRITICAL: {health.get('message')}")
                    for rec in health.get('recommendations', []):
                        logger.error(f"  - {rec}")
                elif status == 'warning':
                    logger.warning(f"WARNING: {health.get('message')}")

                threading.Event().wait(self.interval_seconds)

            except Exception as e:
                logger.error(f"Error in pool health check: {e}", exc_info=True)
                threading.Event().wait(self.interval_seconds)

    def stop(self):
        """Stop the monitoring thread."""
        self.running = False
        logger.info("Stopping pool health check thread")
