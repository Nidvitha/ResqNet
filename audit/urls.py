"""Audit routes, mounted at /api/audit/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "audit"

router = DefaultRouter()
router.register("events", views.AuditEventViewSet, basename="event")

urlpatterns = [
    path("actions/", views.action_types, name="actions"),
    path("case/<str:reference>/", views.case_history, name="case-history"),
    path("", include(router.urls)),
]
