"""Report routes, mounted at /api/reports/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import sync, views

app_name = "reports"

# A router turns a ViewSet into the standard set of URLs automatically:
#   ''            -> list (GET) / create (POST)
#   '<pk>/'       -> retrieve / update / destroy
#   '<pk>/submit/'-> the custom @action methods
router = DefaultRouter()
router.register("", views.ReportViewSet, basename="report")

urlpatterns = [
    # Static paths must come before the router, or the router's '<pk>/' pattern
    # would swallow 'sync/' and try to look up a report with that id.
    path("sync/", sync.sync_reports, name="sync"),
    path("sync/status/", sync.sync_status, name="sync-status"),
    path("triage-preview/", views.triage_preview, name="triage-preview"),
    path("", include(router.urls)),
]
