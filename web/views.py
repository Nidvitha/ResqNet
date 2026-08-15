"""
Server-rendered pages.

Why a `web` app at all? Section 7's app list covers the *backend domains*, and
none of them owns "the public landing page" or "the citizen portal shell". Rather
than bolt page views onto `reports` (where they do not belong) or into `config`
(which should stay configuration), one small app owns the presentation layer.
That keeps every other app a clean API, exactly as Section 7 intends.

These views are deliberately thin. They render a template and nothing else - the
pages fetch their data from the JSON API with `fetch()`, which is what makes the
offline layer possible. A page that received its data server-side could not be
usefully cached and replayed without a network.
"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from analytics.services import public_statistics


def home(request):
    """
    The public landing page (Section 5). Not a dashboard.

    Reachable without authentication, and it explains what ResQNet is before
    asking anyone to sign in.
    """
    return render(request, "web/home.html", {"stats": public_statistics()})


def about(request):
    return render(request, "web/about.html")


def privacy(request):
    return render(request, "web/privacy.html")


def terms(request):
    return render(request, "web/terms.html")


def contact(request):
    return render(request, "web/contact.html")


@never_cache
def login_page(request):
    if request.user.is_authenticated:
        return redirect(portal_for(request.user))
    return render(request, "web/login.html")


@never_cache
def register_page(request):
    if request.user.is_authenticated:
        return redirect(portal_for(request.user))
    return render(request, "web/register.html")


def portal_for(user) -> str:
    """Send each role to its own portal after login."""
    if user.is_admin_role or user.is_superuser:
        return "web:admin_dashboard"
    if user.is_field_officer:
        return "web:officer_dashboard"
    return "web:citizen_dashboard"


@never_cache
def portal_redirect(request):
    if not request.user.is_authenticated:
        return redirect("web:login")
    return redirect(portal_for(request.user))


# --- Citizen portal --------------------------------------------------------

@login_required
def citizen_dashboard(request):
    return render(request, "web/citizen/dashboard.html")


@login_required
def citizen_report_form(request):
    return render(request, "web/citizen/report_form.html")


@login_required
def citizen_reports(request):
    return render(request, "web/citizen/reports.html")


@login_required
def citizen_report_detail(request, reference):
    return render(request, "web/citizen/report_detail.html", {"reference": reference})


# --- Field officer portal --------------------------------------------------

@login_required
def officer_dashboard(request):
    return render(request, "web/officer/dashboard.html")


@login_required
def officer_map(request):
    return render(request, "web/officer/map.html")


@login_required
def officer_inspection(request, assignment_id):
    return render(request, "web/officer/inspection.html", {"assignment_id": assignment_id})


# --- Admin command centre --------------------------------------------------

@login_required
def admin_dashboard(request):
    return render(request, "web/admin/dashboard.html")


@login_required
def admin_cases(request):
    return render(request, "web/admin/cases.html")


@login_required
def admin_case_detail(request, reference):
    return render(request, "web/admin/case_detail.html", {"reference": reference})


@login_required
def admin_officers(request):
    return render(request, "web/admin/officers.html")


@login_required
def admin_heatmap(request):
    return render(request, "web/admin/heatmap.html")


@login_required
def admin_audit(request):
    return render(request, "web/admin/audit.html")


def offline_page(request):
    """Shown by the service worker when a navigation fails with no cache entry."""
    return render(request, "web/offline.html")


@never_cache
def service_worker(request):
    """
    Serve `sw.js` from the site root.

    A service worker can only control URLs at or below its own path. Registered
    from `/static/js/sw.js` it would control only `/static/`, which is useless -
    the pages that must work offline live at `/citizen/` and `/officer/`. Serving
    the same file from `/sw.js` gives it scope over the whole site.
    """
    path = Path(settings.BASE_DIR) / "static" / "sw.js"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        raise Http404("Service worker not found.") from None

    response = HttpResponse(content, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response
