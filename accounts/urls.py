"""Authentication and user-management routes, mounted at /api/auth/."""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("firebase/config/", views.FirebaseConfigView.as_view(), name="firebase-config"),
    path("firebase/login/", views.FirebaseLoginView.as_view(), name="firebase-login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
    path("me/availability/", views.MyAvailabilityView.as_view(), name="my-availability"),
    path("officers/", views.OfficerListCreateView.as_view(), name="officer-list"),
    path("officers/<int:pk>/", views.OfficerDetailView.as_view(), name="officer-detail"),
]
