"""Inspection routes, mounted at /api/inspections/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import sync, views

app_name = "inspections"

router = DefaultRouter()
router.register("", views.InspectionViewSet, basename="inspection")

urlpatterns = [
    path("start/", views.start, name="start"),
    path("sync/", sync.sync_inspections, name="sync"),
    path("", include(router.urls)),
]
