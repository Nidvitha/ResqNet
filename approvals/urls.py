"""Approval and payout routes, mounted at /api/approvals/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "approvals"

router = DefaultRouter()
router.register("decisions", views.ApprovalViewSet, basename="approval")
router.register("payouts", views.PayoutViewSet, basename="payout")

urlpatterns = [
    path("queue/", views.review_queue, name="queue"),
    path("<int:report_id>/approve/", views.approve, name="approve"),
    path("<int:report_id>/reject/", views.reject, name="reject"),
    path("<int:report_id>/review/", views.review, name="review"),
    path("<int:report_id>/payout/initiate/", views.payout_initiate, name="payout-initiate"),
    path("<int:report_id>/payout/complete/", views.payout_complete, name="payout-complete"),
    path("", include(router.urls)),
]
