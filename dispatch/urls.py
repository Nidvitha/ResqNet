"""Dispatch routes, mounted at /api/dispatch/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "dispatch"

router = DefaultRouter()
router.register("assignments", views.AssignmentViewSet, basename="assignment")

urlpatterns = [
    path("my-assignments/", views.my_assignments, name="my-assignments"),
    path("offline-package/", views.offline_package, name="offline-package"),
    path("assignments/<int:assignment_id>/accept/", views.accept, name="accept"),
    path("candidates/<int:report_id>/", views.candidates, name="candidates"),
    path("assign/", views.assign_manually, name="assign-manually"),
    path("auto-assign/<int:report_id>/", views.retry_auto_assign, name="auto-assign"),
    path("", include(router.urls)),
]
