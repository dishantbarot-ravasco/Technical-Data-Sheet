"""
apps/api/permissions.py — Custom DRF permission classes.

Mirrors FastAPI auth dependencies:
  IsEditor  → require_editor  (role in ('admin', 'tds_creator'))
  IsAdmin   → require_admin   (role == 'admin')
  IsCreator → require_creator (role in ('admin', 'tds_creator'))

Note: the role name is 'tds_creator' (matches the live DB's chk_user_role
constraint) — not 'user'. 'viewer'
is intentionally excluded from every class below — a viewer can only
search/view/download TDS (those endpoints use plain IsAuthenticated), never
create, edit, approve, decline, delete, or manage users.
"""

from django.conf import settings
from rest_framework.permissions import BasePermission


def is_allowed_email_domain(email: str) -> bool:
    """
    True only for an email address ending in "@<settings.ALLOWED_EMAIL_DOMAIN>"
    (case-insensitive). Used to gate account creation and login so no address
    outside the company domain can ever have or use a TDSUser account.
    """
    return (email or '').strip().lower().endswith('@' + settings.ALLOWED_EMAIL_DOMAIN.lower())


class IsEditor(BasePermission):
    """
    Allows access only to users with role 'admin' or 'tds_creator'.
    Used for approve/decline/delete/status operations — viewer excluded.
    """
    message = 'Editor (admin or user) role required.'

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, 'is_active', False))
            and getattr(user, 'role', None) in ('admin', 'tds_creator')
        )


class IsCreator(IsEditor):
    """
    Allows access to users with role 'admin' or 'tds_creator'.
    Used for create_tds — viewer can search/view/download but cannot create,
    approve, decline, delete, or manage users.

    MAINTAINABILITY (fixed): this used to duplicate IsEditor.has_permission
    verbatim as a second, independent copy -- harmless today since both
    currently gate the same role set, but a future change to one without
    the other would silently make them drift apart. Inheriting from
    IsEditor keeps the actual check in exactly one place while keeping the
    two names (and their distinct `message`) for call-site clarity, per
    the module docstring above on why they are kept as separate concepts.
    """
    message = 'Creator (admin or user) role required.'


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
