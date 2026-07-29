"""
Request context — carries the request ID into every log line.
=============================================================

``app/main.py`` generates an ``X-Request-ID`` per request and echoes it in the
response header, and ``ai_service`` tells users to "quote the X-Request-ID if it
keeps happening". But the ID lived only inside the middleware's local scope: the
one line the middleware itself logged carried it, and nothing else did. An error
logged from a service layer had no request ID on it, so the correlation the
header promised did not exist — you could not take an ID from a user and find
the traceback it belonged to.

A ``ContextVar`` fixes that without threading an argument through every function.
It is set once per request and read by the logging filter below, which attaches
it to every record emitted while handling that request — including from code
running in a thread pool, because ``contextvars`` propagate into
``run_in_threadpool``.
"""

import logging
from contextvars import ContextVar

# Empty outside a request (startup, the jobs service, tests).
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIdFilter(logging.Filter):
    """Attach the current request ID to every log record.

    A ``Filter`` rather than a ``Formatter`` so it works for both the text and
    JSON formatters without duplicating the logic, and so ``JSONFormatter``
    picks it up automatically via its unknown-attribute sweep.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


def set_request_id(request_id: str) -> None:
    """Bind a request ID for the current context."""
    request_id_var.set(request_id)


def get_request_id() -> str:
    """Return the current request ID, or an empty string outside a request."""
    return request_id_var.get()
