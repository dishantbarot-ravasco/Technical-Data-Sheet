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
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError, ObjectDoesNotExist
from django.db.utils import IntegrityError
import logging

logger = logging.getLogger(__name__)

# Exception types this codebase's own service layer raises deliberately with
# human-readable messages (audited — none embed secrets, file paths, or raw
# SQL), so it's safe to pass str(exc) straight through to the client instead
# of flattening it into the generic 500 message below.
_DESCRIBABLE_EXCEPTIONS = (ValueError, KeyError, DjangoValidationError, ObjectDoesNotExist)


def _describe_integrity_error(exc):
    """
    Turn a raw django.db.utils.IntegrityError into a human-readable message
    without leaking the underlying SQL. Falls back to a generic message if the
    driver's diagnostic info isn't available (e.g. sqlite in some test setups).
    """
    diag = getattr(getattr(exc, '__cause__', None), 'diag', None)
    constraint = getattr(diag, 'constraint_name', None) or ''
    message_detail = (getattr(diag, 'message_detail', None) or str(exc)).lower()

    if 'unique' in constraint or 'duplicate key' in message_detail:
        return 'A record with this value already exists.'
    if 'fkey' in constraint or 'foreign key' in message_detail:
        return 'Referenced record no longer exists.'
    if 'not null' in message_detail or 'not-null' in message_detail:
        return 'A required field was left empty.'
    return 'Database constraint violation.'


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
        # DRF didn't recognize this exception type at all. Log the full
        # traceback either way (logger.exception captures sys.exc_info()
        # automatically) so server logs always have the full picture.
        logger.exception('Unhandled exception in %s', context.get('view'))

        if isinstance(exc, _DESCRIBABLE_EXCEPTIONS):
            # A deliberate application-level error (bad input/state) that
            # happens to not be a DRF APIException subclass — describe it.
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(exc, IntegrityError):
            return Response({'detail': _describe_integrity_error(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Truly unexpected (AttributeError, TypeError, ...) — most likely a
        # real bug, and the least predictable in content, so keep this one
        # generic rather than risk echoing something sensitive to the client.
        detail = 'An unexpected server error occurred.'
        if settings.DEBUG:
            detail = f'{detail} ({type(exc).__name__}: {exc})'
        return Response({'detail': detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # DRF's default serializer-validation error shape is a bare dict of
    # {field: [errors]} with no top-level "detail" key. Wrap it so every
    # error response — validation or otherwise — has the same {"detail": ...}
    # envelope the frontend expects.
    if isinstance(response.data, dict) and 'detail' not in response.data:
        response.data = {'detail': response.data}

    return response
