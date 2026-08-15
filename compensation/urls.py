"""Compensation routes, mounted at /api/compensation/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "compensation"

router = DefaultRouter()
router.register("rules", views.ReliefRuleViewSet, basename="rule")
router.register("allowances", views.ReliefAllowanceViewSet, basename="allowance")
router.register("claims", views.CompensationClaimViewSet, basename="claim")

urlpatterns = [path("", include(router.urls))]
