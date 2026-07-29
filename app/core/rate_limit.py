"""
Rate limiting — the single shared Limiter
==========================================
Every ``@limiter.limit(...)`` decorator and ``SlowAPIMiddleware`` must use the
*same* ``Limiter`` instance.

There used to be two. ``app/main.py`` built one with ``default_limits`` and
assigned it to ``app.state.limiter``; ``app/routes/auth.py`` built a second one
that the route decorators registered against. Two consequences:

  - ``SlowAPIMiddleware`` was never added at all, so ``default_limits`` never
    applied to anything. Every route outside ``auth`` was completely unthrottled
    and ``RATE_LIMIT_PER_MINUTE`` was a no-op setting.

  - Had the middleware simply been added, the decorated auth routes would have
    been limited twice — once by their decorator's instance and once by the
    middleware's — which is confusing to debug and not what either limit meant.

Storage is in-memory, so counters are per worker process: with N uvicorn workers
the effective limit is N times what is configured here. That is another reason
the API runs a single worker. Moving to a shared Redis backend would fix it.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"],
)
