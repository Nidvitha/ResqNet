"""
Analytics API views.

One endpoint is public: `/api/analytics/public/`. Section 5 wants headline
statistics on the landing page before anyone logs in, so this returns aggregate
counts only - never a name, a coordinate, or a case reference. Everything else
requires an administrator.
"""

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.permissions import IsAdminRole

from . import services


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def public_stats(request):
    """Aggregate figures for the public home page. No personal data."""
    return Response(services.public_statistics())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def dashboard(request):
    """Everything the admin command centre needs in a single request."""
    return Response(services.full_dashboard())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def overview(request):
    return Response(services.overview())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def by_status(request):
    return Response(services.reports_by_status())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def by_disaster_type(request):
    return Response(services.reports_by_disaster_type())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def geographic(request):
    return Response(services.geographic_distribution())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def heatmap(request):
    """Weighted coordinates for the Leaflet heat layer."""
    try:
        limit = min(int(request.query_params.get("limit", 1000)), 5000)
    except (TypeError, ValueError):
        limit = 1000
    return Response(services.heatmap_points(limit=limit))


@api_view(["GET"])
@permission_classes([IsAdminRole])
def officer_workload(request):
    return Response(services.officer_workload())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def inspections(request):
    return Response(services.inspection_metrics())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def compensation(request):
    return Response(services.compensation_metrics())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def payouts(request):
    return Response(services.payout_metrics())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def trend(request):
    try:
        days = min(int(request.query_params.get("days", 30)), 365)
    except (TypeError, ValueError):
        days = 30
    return Response(services.daily_trend(days=days))


@api_view(["GET"])
@permission_classes([IsAdminRole])
def satellite(request):
    return Response(services.satellite_overview())


@api_view(["GET"])
@permission_classes([IsAdminRole])
def zones(request):
    return Response(services.zone_intelligence())
