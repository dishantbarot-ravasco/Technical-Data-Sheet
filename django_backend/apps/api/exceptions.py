"""
apps/api/exceptions.py — Custom DRF exception handler.

Wired in via REST_FRAMEWORK['EXCEPTION_HANDLER'] in config/settings.py, so
every DRF view in the app (all routers under apps/api/routers/) funnels its
raised exceptions through custom_exception_handler() below instead of DRF's
default. The goal is one consistent JSON error shape everywhere, so the
frontend's error-handling code never has to special-case different endpoints:

  { "detail": "human-readable message" }
or, for serializer validation errors:
  { "detail": { "field_name": ["error message", ...] } }
"""
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    """
    Called by DRF whenever a view raises an exception.

    context['view'] is the view instance that raised it — used only for the
    log message so a 500 in the logs can be traced back to which endpoint
    caused it.
    """
    # Let DRF build its default response first (handles APIException subclasses,
    # ValidationError, PermissionDenied, NotAuthenticated, Http404, etc.)
    response = exception_handler(exc, context)

    if response is None:
        # DRF didn't recognize this exception type at all — i.e. an actual
        # unhandled bug in view code. Log the full traceback (logger.exception
        # captures sys.exc_info() automatically) and return a generic 500
        # rather than leaking a raw Python traceback/exception message to the client.
        logger.exception('Unhandled exception in %s', context.get('view'))
        return Response(
            {'detail': 'An unexpected server error occurred.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # DRF's default serializer-validation error shape is a bare dict of
    # {field: [errors]} with no top-level "detail" key. Wrap it so every
    # error response — validation or otherwise — has the same {"detail": ...}
    # envelope the frontend expects.
    if isinstance(response.data, dict) and 'detail' not in response.data:
        response.data = {'detail': response.data}

    return response
