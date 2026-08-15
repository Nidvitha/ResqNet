"""
Upload and coordinate validation.

Section 12 is emphatic: *"Never rely only on frontend validation."* The browser
compresses photos to save bandwidth, but a hostile client can post anything at
all to the same endpoint. Everything the frontend checks, the backend checks
again - and a few things the frontend cannot check, like whether the bytes are
genuinely an image rather than a renamed script.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, UnidentifiedImageError


def validate_image_upload(uploaded_file) -> None:
    """
    Four independent checks, because each catches something the others miss.

    1. **Size**       - a 4 GB upload is a denial-of-service, not a photo.
    2. **Extension**  - cheap first filter.
    3. **Real content** - Pillow actually decodes the header. A file named
       `roof.jpg` that contains PHP fails here, where the extension check passed.
    4. **Dimensions** - a 60000x60000 "decompression bomb" is small on disk but
       exhausts memory when opened.
    """
    max_bytes = getattr(settings, "MAX_UPLOAD_SIZE_BYTES", 5 * 1024 * 1024)
    if uploaded_file.size > max_bytes:
        raise ValidationError(
            f"File is too large ({uploaded_file.size / 1024 / 1024:.1f} MB). "
            f"Maximum allowed is {max_bytes / 1024 / 1024:.0f} MB."
        )
    if uploaded_file.size == 0:
        raise ValidationError("The uploaded file is empty.")

    extension = Path(uploaded_file.name).suffix.lower().lstrip(".")
    allowed_extensions = getattr(settings, "ALLOWED_IMAGE_EXTENSIONS", ["jpg", "jpeg", "png"])
    if extension not in allowed_extensions:
        raise ValidationError(
            f"Unsupported file type '.{extension}'. Allowed: {', '.join(allowed_extensions)}."
        )

    content_type = getattr(uploaded_file, "content_type", None)
    allowed_types = getattr(settings, "ALLOWED_IMAGE_CONTENT_TYPES", ["image/jpeg", "image/png"])
    if content_type and content_type not in allowed_types:
        raise ValidationError(f"Unsupported content type '{content_type}'.")

    try:
        uploaded_file.seek(0)
        image = Image.open(uploaded_file)
        image.verify()  # decodes the header only - cheap, and raises on rubbish
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as reopened:
            width, height = reopened.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError("This file is not a readable image.") from exc
    finally:
        uploaded_file.seek(0)

    max_dimension = getattr(settings, "MAX_IMAGE_DIMENSION", 6000)
    if width > max_dimension or height > max_dimension:
        raise ValidationError(
            f"Image is too large ({width}x{height}px). "
            f"Maximum {max_dimension}px on the longest side."
        )
    if width < 100 or height < 100:
        raise ValidationError("Image is too small to be useful as evidence (minimum 100x100px).")


def secure_upload_path(instance, filename: str) -> str:
    """
    Build the stored path for an upload.

    The client's filename is *never* used on disk. A name like
    `../../config/settings.py` would otherwise let an attacker write outside the
    media folder. We keep only the validated extension and generate a fresh
    random name, then bucket by date so no single directory grows unbounded.
    """
    extension = Path(filename).suffix.lower().lstrip(".")
    allowed = getattr(settings, "ALLOWED_IMAGE_EXTENSIONS", ["jpg", "jpeg", "png"])
    if extension not in allowed:
        extension = "jpg"

    folder = getattr(instance, "upload_subdirectory", "evidence")
    today = timezone.now()
    return f"{folder}/{today:%Y/%m/%d}/{uuid.uuid4().hex}.{extension}"


def validate_latitude(value) -> Decimal:
    """Coordinates arrive from a browser and are never trusted (Section 11)."""
    try:
        latitude = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError("Latitude must be a number.") from None
    if not (Decimal("-90") <= latitude <= Decimal("90")):
        raise ValidationError("Latitude must be between -90 and 90.")
    return latitude


def validate_longitude(value) -> Decimal:
    try:
        longitude = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError("Longitude must be a number.") from None
    if not (Decimal("-180") <= longitude <= Decimal("180")):
        raise ValidationError("Longitude must be between -180 and 180.")
    return longitude


def validate_coordinate_pair(latitude, longitude) -> tuple[Decimal, Decimal]:
    """
    Validate both coordinates together.

    Exactly (0, 0) is rejected: it is a point in the Atlantic Ocean and is almost
    always a browser returning an uninitialised value rather than a real location.
    """
    lat = validate_latitude(latitude)
    lon = validate_longitude(longitude)
    if lat == 0 and lon == 0:
        raise ValidationError(
            "Coordinates (0, 0) are not a valid location. "
            "Please allow GPS access or enter the address manually."
        )
    return lat, lon
