"""Fallback error handling for the DRF API views.

Bad client input should get a 4xx, never a 500. Views validate their own
parameters (see the _*_param helpers in api/views.py); this handler is the
safety net for what they miss: a lookup of a record that does not exist
becomes a 404, and a value that cannot be parsed becomes a 400.

Because the same exceptions can also come from a bug in our own code, every
conversion is logged with its traceback to the 'mehdie.errors' logger (the
gunicorn journal, not e-mail). If valid requests start getting 400/404s,
look there:  journalctl -u gunicorn-tools | grep 'API request rejected'
"""
import logging

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import DataError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger('mehdie.errors')


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)  # DRF's own: Http404, ParseError, ...
    if response is not None:
        return response

    if isinstance(exc, ObjectDoesNotExist):
        code, detail = status.HTTP_404_NOT_FOUND, 'Not found.'
    elif isinstance(exc, (ValueError, TypeError, DataError, DjangoValidationError)):
        code, detail = status.HTTP_400_BAD_REQUEST, 'Invalid request parameters.'
    else:
        return None  # a genuine server error: let Django return 500 and e-mail ADMINS

    request = context.get('request')
    where = f'{request.method} {request.get_full_path()}' if request is not None else '?'
    logger.error('API request rejected with %s: %r on %s', code, exc, where, exc_info=exc)
    return Response({'detail': detail}, status=code)
