from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdminRole
from reports.models import DisasterReport

from .models import DamageAssessment, SatelliteAnalysis, SatelliteDetection
from .serializers import (
    DamageAssessmentSerializer,
    SatelliteAnalysisCreateSerializer,
    SatelliteAnalysisDetailSerializer,
    SatelliteDetectionSerializer,
)
from .services import refresh_damage_assessment, run_satellite_analysis


class SatelliteAnalysisViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def get_queryset(self):
        queryset = SatelliteAnalysis.objects.select_related("report", "requested_by").prefetch_related("detections")
        report_ref = self.request.query_params.get("report")
        if report_ref:
            queryset = queryset.filter(report__reference__iexact=report_ref)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return SatelliteAnalysisCreateSerializer
        return SatelliteAnalysisDetailSerializer

    def perform_create(self, serializer):
        self._run_now = bool(serializer.validated_data.pop("run_now", True))
        self.analysis = serializer.save(requested_by=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        if self._run_now:
            run_satellite_analysis(self.analysis, user=request.user, request=request)

        output = SatelliteAnalysisDetailSerializer(self.analysis, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, pk=None):
        analysis = self.get_object()
        run_satellite_analysis(analysis, user=request.user, request=request)
        return Response(SatelliteAnalysisDetailSerializer(analysis, context={"request": request}).data)


class SatelliteDetectionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminRole]
    serializer_class = SatelliteDetectionSerializer

    def get_queryset(self):
        queryset = SatelliteDetection.objects.select_related("analysis", "analysis__report")
        detection_type = self.request.query_params.get("type")
        if detection_type:
            queryset = queryset.filter(detection_type=detection_type)
        severity = self.request.query_params.get("severity")
        if severity:
            queryset = queryset.filter(severity=severity)
        report_ref = self.request.query_params.get("report")
        if report_ref:
            queryset = queryset.filter(analysis__report__reference__iexact=report_ref)
        return queryset

    @action(detail=False, methods=["get"], url_path="geojson")
    def geojson(self, request):
        features = []
        for detection in self.get_queryset()[:2000]:
            if detection.geometry_geojson:
                geometry = detection.geometry_geojson
            elif detection.latitude is not None and detection.longitude is not None:
                geometry = {
                    "type": "Point",
                    "coordinates": [float(detection.longitude), float(detection.latitude)],
                }
            else:
                continue

            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "id": detection.pk,
                        "analysis_id": detection.analysis_id,
                        "report_reference": getattr(detection.analysis.report, "reference", ""),
                        "detection_type": detection.detection_type,
                        "severity": detection.severity,
                        "confidence": float(detection.confidence),
                        "affected_area_hectares": float(detection.affected_area_hectares),
                    },
                }
            )

        return Response({"type": "FeatureCollection", "features": features})


class DamageAssessmentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminRole]
    serializer_class = DamageAssessmentSerializer

    def get_queryset(self):
        queryset = DamageAssessment.objects.select_related("report")
        report_ref = self.request.query_params.get("report")
        if report_ref:
            queryset = queryset.filter(report__reference__iexact=report_ref)
        return queryset

    @action(detail=False, methods=["post"], url_path="refresh")
    def refresh(self, request):
        report_ref = request.data.get("report_reference", "").strip()
        if not report_ref:
            return Response({"detail": "report_reference is required."}, status=status.HTTP_400_BAD_REQUEST)

        report = DisasterReport.objects.filter(reference__iexact=report_ref).first()
        if report is None:
            return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)

        assessment = refresh_damage_assessment(report, user=request.user, request=request)
        return Response(DamageAssessmentSerializer(assessment).data)
