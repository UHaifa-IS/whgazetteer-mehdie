"""Report errors that the app catches and carries on from.

Replaces sentry_sdk.capture_exception (Sentry was removed in 2026-11): the
exception and its traceback go to the 'mehdie.errors' logger, which settings.py
writes to the journal (journalctl -u gunicorn-tools / -u celery@whg). These are
not e-mailed, because some are raised once per row of an upload; uncaught
server errors (HTTP 500) are e-mailed to ADMINS (see settings.LOGGING).
"""
import logging
import sys

logger = logging.getLogger('mehdie.errors')


def capture_exception(error=None):
    """Log `error` (or the exception being handled) with its traceback."""
    if error is None:
        exc_info = sys.exc_info()
    else:
        exc_info = (type(error), error, error.__traceback__)
    logger.error('Handled exception: %r', exc_info[1], exc_info=exc_info)
