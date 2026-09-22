from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DamageAssessmentViewSet, SatelliteAnalysisViewSet, SatelliteDetectionViewSet

app_name = "satellite"

router = DefaultRouter()
router.register("analyses", SatelliteAnalysisViewSet, basename="satellite-analysis")
router.register("detections", SatelliteDetectionViewSet, basename="satellite-detection")
router.register("assessments", DamageAssessmentViewSet, basename="damage-assessment")

urlpatterns = [
    path("", include(router.urls)),
]
