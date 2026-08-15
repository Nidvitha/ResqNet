"""
Root URL configuration for ResQNet.

Django matches an incoming URL against this list top to bottom and hands the
request to the first matching view. Each app owns its own `urls.py`, and this
file simply mounts those under a prefix - so the API layout stays exactly as
described in Section 31.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from .health import health_check

urlpatterns = [
    # Django's built-in admin site - staff/superuser only.
    path("admin/", admin.site.urls),

    # --- API ---------------------------------------------------------------
    path("api/health/", health_check, name="health-check"),
    path("api/auth/", include("accounts.urls")),
    path("api/reports/", include("reports.urls")),
    path("api/triage/", include("triage.urls")),
    path("api/dispatch/", include("dispatch.urls")),
    path("api/inspections/", include("inspections.urls")),
    path("api/compensation/", include("compensation.urls")),
    path("api/approvals/", include("approvals.urls")),
    path("api/analytics/", include("analytics.urls")),
    path("api/audit/", include("audit.urls")),

    # --- Server-rendered site (public home page + role portals) ------------
    path("", include("web.urls")),
]

# During development Django serves uploaded media itself. In production this is
# handled by the web server instead - never by Django.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
