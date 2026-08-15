"""
Health check endpoint.

A health check answers one question: "is this service actually alive?" It is the
first thing a deployment platform, load balancer, or uptime monitor calls. For
ResQNet it also proves the database connection works, because a disaster-relief
API that returns 200 while its database is unreachable is worse than one that
honestly reports failure.

This endpoint is deliberately public - it must be reachable without credentials.
It exposes no data beyond liveness.
"""

from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health_check(request):
    """Return service and database liveness."""
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any DB failure means "not healthy"
        database_ok = False

    payload = {
        "service": "ResQNet",
        "status": "ok" if database_ok else "degraded",
        "database": "connected" if database_ok else "unavailable",
    }
    http_status = status.HTTP_200_OK if database_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return Response(payload, status=http_status)
