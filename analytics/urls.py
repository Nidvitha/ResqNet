"""Analytics routes, mounted at /api/analytics/."""

from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("public/", views.public_stats, name="public"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("overview/", views.overview, name="overview"),
    path("by-status/", views.by_status, name="by-status"),
    path("by-disaster-type/", views.by_disaster_type, name="by-disaster-type"),
    path("geographic/", views.geographic, name="geographic"),
    path("heatmap/", views.heatmap, name="heatmap"),
    path("officer-workload/", views.officer_workload, name="officer-workload"),
    path("inspections/", views.inspections, name="inspections"),
    path("compensation/", views.compensation, name="compensation"),
    path("payouts/", views.payouts, name="payouts"),
    path("trend/", views.trend, name="trend"),
    path("satellite/", views.satellite, name="satellite"),
    path("zones/", views.zones, name="zones"),
]
