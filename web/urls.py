"""Server-rendered page routes, mounted at the site root."""

from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    # --- Public --------------------------------------------------------
    path("", views.home, name="home"),
    path("about/", views.about, name="about"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
    path("contact/", views.contact, name="contact"),
    path("offline/", views.offline_page, name="offline"),
    # Served from the root so the service worker's scope covers the whole site.
    path("sw.js", views.service_worker, name="service-worker"),

    # --- Authentication ------------------------------------------------
    path("login/", views.login_page, name="login"),
    path("register/", views.register_page, name="register"),
    path("portal/", views.portal_redirect, name="portal"),

    # --- Citizen portal ------------------------------------------------
    path("citizen/", views.citizen_dashboard, name="citizen_dashboard"),
    path("citizen/report/", views.citizen_report_form, name="citizen_report_form"),
    path("citizen/reports/", views.citizen_reports, name="citizen_reports"),
    path("citizen/reports/<str:reference>/", views.citizen_report_detail, name="citizen_report_detail"),

    # --- Field officer portal ------------------------------------------
    path("officer/", views.officer_dashboard, name="officer_dashboard"),
    path("officer/map/", views.officer_map, name="officer_map"),
    path("officer/inspection/<int:assignment_id>/", views.officer_inspection, name="officer_inspection"),

    # --- Admin command centre ------------------------------------------
    path("command/", views.admin_dashboard, name="admin_dashboard"),
    path("command/cases/", views.admin_cases, name="admin_cases"),
    path("command/cases/<str:reference>/", views.admin_case_detail, name="admin_case_detail"),
    path("command/officers/", views.admin_officers, name="admin_officers"),
    path("command/heatmap/", views.admin_heatmap, name="admin_heatmap"),
    path("command/audit/", views.admin_audit, name="admin_audit"),
]
