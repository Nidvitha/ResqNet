"""
Reusable permission classes.

A DRF *permission* is a small object with one job: look at the incoming request
and answer "is this allowed?". Views declare which permissions they require, and
DRF refuses the request with 403 before your view code ever runs.

Two levels matter in ResQNet:

* **Role permissions** (`IsCitizen`, `IsFieldOfficer`, `IsAdminRole`) answer
  "what kind of user is this?" - checked once per request.
* **Object permissions** (`IsOwnerOrStaff`, `IsAssignedOfficer`) answer "may this
  user touch *this specific row*?" - checked per object.

Both are required. Section 32 is explicit: a citizen must never be able to read
another citizen's report, and an officer must never edit another officer's
inspection. Hiding a button in the frontend is not security.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


class IsCitizen(BasePermission):
    message = "Only citizens can perform this action."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_citizen)


class IsFieldOfficer(BasePermission):
    message = "Only field officers can perform this action."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_field_officer
        )


class IsAdminRole(BasePermission):
    message = "Only administrators can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_admin_role or user.is_superuser))


class IsFieldOfficerOrAdmin(BasePermission):
    message = "Only field officers or administrators can perform this action."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user.is_field_officer or user.is_admin_role or user.is_superuser


class IsOwnerOrStaff(BasePermission):
    """
    Object-level ownership check.

    The object must expose the owning user through one of the attribute names in
    `owner_fields`. Administrators and superusers bypass the check because
    reviewing every case is their job.
    """

    owner_fields = ("citizen", "user", "owner", "created_by")
    message = "You do not have access to this record."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_admin_role or user.is_superuser:
            return True
        for field in self.owner_fields:
            owner = getattr(obj, field, None)
            if owner is not None:
                return owner == user
        return False


class IsAssignedOfficer(BasePermission):
    """
    Only the officer the case was assigned to may modify it.

    Three outcomes:

    * **Administrators** - full access. Section 16 allows "the assigned officer
      or appropriately authorized personnel" to change an inspection.
    * **The assigned officer** - full access to their own work.
    * **The owning citizen** - read only. They are entitled to see the inspection
      of their own property (Section 4 lists tracking inspection progress as a
      citizen capability) but must never be able to alter the findings.

    Every other officer is refused outright, including on read, so one officer
    cannot browse another's field notes.
    """

    message = "This case is assigned to a different officer."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_admin_role or user.is_superuser:
            return True

        officer = getattr(obj, "officer", None)
        if officer is None:
            assignment = getattr(obj, "assignment", None)
            officer = getattr(assignment, "officer", None)
        if officer == user:
            return True

        # Read-only fallback for the citizen whose property this is.
        if request.method in SAFE_METHODS:
            report = getattr(obj, "report", None)
            if report is not None and getattr(report, "citizen_id", None) == user.id:
                return True
        return False


class ReadOnly(BasePermission):
    """Allows GET/HEAD/OPTIONS only. Combined with other classes using `|`."""

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


__all__ = [
    "Role",
    "IsCitizen",
    "IsFieldOfficer",
    "IsAdminRole",
    "IsFieldOfficerOrAdmin",
    "IsOwnerOrStaff",
    "IsAssignedOfficer",
    "ReadOnly",
]
