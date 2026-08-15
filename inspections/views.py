"""
Inspection API views.

Section 32's second ownership rule - "Officer A must not modify Officer B's
inspection" - is enforced on three independent layers here:

1. `get_queryset()` never returns another officer's inspection.
2. `IsAssignedOfficer` re-checks at the object level.
3. `_assert_can_modify()` inside the service checks again before any write.

That is not redundancy for its own sake. Each layer protects a different way in:
the queryset covers the API, the permission covers direct object access, and the
service covers every non-view caller.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAssignedOfficer, IsFieldOfficerOrAdmin
from compensation.services import preview_compensation
from dispatch.models import Assignment
from reports.states import InvalidTransition

from .models import Inspection
from .serializers import (
    InspectionPhotoSerializer,
    InspectionSerializer,
    InspectionUpdateSerializer,
    SignOffSerializer,
    StartInspectionSerializer,
)
from .services import (
    InspectionPermissionError,
    attach_inspection_photo,
    complete_inspection,
    sign_off_inspection,
    start_inspection,
    update_inspection,
)


class InspectionViewSet(viewsets.ModelViewSet):
    """Field inspections. Creation goes through `start/`, never a bare POST."""

    permission_classes = [IsAuthenticated, IsAssignedOfficer]
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        queryset = Inspection.objects.select_related(
            "report", "report__citizen", "officer", "assignment"
        ).prefetch_related("photos")

        if user.is_admin_role or user.is_superuser:
            return queryset
        if user.is_field_officer:
            return queryset.filter(officer=user)
        return queryset.filter(report__citizen=user)   # citizens read their own, read-only

    def get_serializer_class(self):
        if self.action in {"update", "partial_update"}:
            return InspectionUpdateSerializer
        return InspectionSerializer

    def partial_update(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = InspectionUpdateSerializer(inspection, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            update_inspection(
                inspection=inspection,
                data=serializer.validated_data,
                user=request.user,
                request=request,
            )
        except InspectionPermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InspectionSerializer(inspection, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Finish the checklist. Still editable until signed."""
        inspection = self.get_object()
        try:
            complete_inspection(inspection=inspection, user=request.user, request=request)
        except InspectionPermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InspectionSerializer(inspection, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="sign-off")
    def sign_off(self, request, pk=None):
        """Sign off, freezing the record and triggering compensation."""
        inspection = self.get_object()
        serializer = SignOffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Capture the officer's own position at sign-off, if the device supplied it.
        latitude = serializer.validated_data.get("inspection_latitude")
        longitude = serializer.validated_data.get("inspection_longitude")
        if latitude is not None and longitude is not None:
            inspection.inspection_latitude = latitude
            inspection.inspection_longitude = longitude
            inspection.save(update_fields=["inspection_latitude", "inspection_longitude"])

        try:
            sign_off_inspection(
                inspection=inspection,
                signature_name=serializer.validated_data["signature_name"],
                user=request.user,
                request=request,
            )
        except InspectionPermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        inspection.refresh_from_db()
        return Response(InspectionSerializer(inspection, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="photos",
            parser_classes=[MultiPartParser, FormParser])
    def upload_photo(self, request, pk=None):
        inspection = self.get_object()
        serializer = InspectionPhotoSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            photo = attach_inspection_photo(
                inspection=inspection,
                image=serializer.validated_data["image"],
                caption=serializer.validated_data.get("caption", ""),
                damage_area=serializer.validated_data.get("damage_area", ""),
                latitude=serializer.validated_data.get("captured_latitude"),
                longitude=serializer.validated_data.get("captured_longitude"),
                user=request.user,
                request=request,
            )
        except InspectionPermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            InspectionPhotoSerializer(photo, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="compensation-preview")
    def compensation_preview(self, request, pk=None):
        """
        Show the officer what their assessment is worth before they sign.

        Not a promise of payment - an administrator still reviews it - but it
        makes the consequence of each severity grade visible at the moment it is
        being chosen.
        """
        inspection = self.get_object()
        return Response(preview_compensation(inspection))


@api_view(["POST"])
@permission_classes([IsFieldOfficerOrAdmin])
def start(request):
    """Open an inspection for one of the officer's assigned cases."""
    serializer = StartInspectionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        assignment = Assignment.objects.select_related("report").get(
            pk=serializer.validated_data["assignment_id"]
        )
    except Assignment.DoesNotExist:
        return Response({"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        inspection = start_inspection(assignment=assignment, user=request.user, request=request)
    except InspectionPermissionError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        InspectionSerializer(inspection, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )
