"""
apps/api/permissions.py — Custom DRF permission classes.

Mirrors FastAPI auth dependencies:
  IsEditor  → require_editor  (role in ('admin', 'user'))
  IsAdmin   → require_admin   (role == 'admin')
  IsCreator → require_creator (role in ('admin', 'user'))

Note: the 'tds_creator' role has been removed from the system (it was never
assignable from the admin UI and added a third overlapping tier). 'viewer'
is intentionally excluded from every class below — a viewer can only
search/view/download TDS (those endpoints use plain IsAuthenticated), never
create, edit, approve, decline, delete, or manage users.
"""

from rest_framework.permissions import BasePermission


class IsEditor(BasePermission):
    """
    Allows access only to users with role 'admin' or 'user'.
    Used for approve/decline/delete/status operations — viewer excluded.
    """
    message = 'Editor (admin or user) role required.'

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, 'is_active', False))
            and getattr(user, 'role', None) in ('admin', 'user')
        )


class IsCreator(BasePermission):
    """
    Allows access to users with role 'admin' or 'user'.
    Used for create_tds — viewer can search/view/download but cannot create,
    approve, decline, delete, or manage users.
    """
    message = 'Creator (admin or user) role required.'

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, 'is_active', False))
            and getattr(user, 'role', None) in ('admin', 'user')
        )


class IsAdmin(BasePermission):
    """
    Allows access only to users with role 'admin'.
    Equivalent to FastAPI's require_admin dependency.
    """
    message = 'Admin role required.'

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, 'is_active', False))
            and getattr(user, 'role', None) == 'admin'
        )
