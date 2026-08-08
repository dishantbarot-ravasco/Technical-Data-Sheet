"""
apps/api/views.py — Top-level, standalone API views.

This module holds only the health-check endpoint. Every feature endpoint
(TDS, batch, users, PDF, auth, etc.) instead lives in its own
apps/api/routers/<feature>_views.py + <feature>_urls.py pair — see
apps/api/urls.py for the full list of routers and how they're wired together.
This file exists separately because a health check isn't really "a feature",
just an operational endpoint used by uptime monitors / Render's health checks.
"""
import django
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['GET'])
@permission_classes([AllowAny])   # no auth required — monitoring tools need to hit this unauthenticated
def health_check(request):
    """
    GET /api/health/

    Returns 200 with status='ok' if Django is up and can reach the database,
    or status='degraded' (still 200, not 5xx) if the DB connection fails —
    deliberately not a 500 so uptime monitors can distinguish "app is down"
    from "app is up but DB has a problem" by reading the body.
    """
    from django.db import connection
    try:
        connection.ensure_connection()
        db_status = 'ok'
    except Exception as e:
        db_status = f'error: {e}'

    return Response({
        'status':   'ok' if db_status == 'ok' else 'degraded',
        'service':  'TDS Automation — Django Backend',
        'django':   django.get_version(),
        'database': db_status,
    })
