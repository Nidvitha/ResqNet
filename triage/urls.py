"""Triage routes, mounted at /api/triage/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "triage"

router = DefaultRouter()
router.register("rules", views.TriageRuleViewSet, basename="rule")
router.register("thresholds", views.TriageThresholdViewSet, basename="threshold")
router.register("results", views.TriageResultViewSet, basename="result")
router.register("infrastructure", views.CriticalInfrastructureSiteViewSet, basename="infrastructure")

urlpatterns = [
    path("preview/", views.preview, name="preview"),
    path("recalculate/<int:report_id>/", views.recalculate, name="recalculate"),
    path("", include(router.urls)),
]
