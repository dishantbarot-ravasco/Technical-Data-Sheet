"""
apps/api/routers/users_views.py — User management and authentication endpoints.

Ported from FastAPI routers/users.py.
Note: POST /api/auth/login is handled by Phase 3 (TDSLoginView in auth_views.py).
This file handles the remaining user endpoints.

Endpoints:
  GET  /api/auth/me
  POST /api/auth/request-otp
  POST /api/auth/verify-otp
  POST /api/auth/change-password
  POST /api/users/setup
  GET  /api/users
  GET  /api/users/{id}
  POST /api/users
  PATCH /api/users/{id}
"""
import logging

import bcrypt
from django.db import transaction, connection
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError, PermissionDenied
from rest_framework.throttling import AnonRateThrottle


class OTPRequestThrottle(AnonRateThrottle):
    """3 OTP emails per minute per IP — prevents email-spam abuse."""
    scope = 'otp_request'


class OTPVerifyThrottle(AnonRateThrottle):
    """10 OTP verify attempts per minute per IP — supplements per-code lockout."""
    scope = 'otp_verify'

from apps.core.models import TDSUser
from apps.api.permissions import IsAdmin, is_allowed_email_domain
from apps.services.otp_service import generate_otp, verify_otp, send_otp_email

logger = logging.getLogger(__name__)

_VALID_ROLES = {"admin", "tds_creator", "viewer"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(plain.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def _verify_password(plain: str, hashed: str) -> bool:
    """Verify a bcrypt password."""
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def _user_out(u):
    return {
        "user_id":     u.user_id,
        "email":       u.email,
        "full_name":   u.full_name or '',
        "role":        u.role,
        "designation": u.designation or '',
        "is_active":   u.is_active,
        "created_at":  u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


# ── Authentication ─────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the profile of the currently authenticated user."""
    return Response(_user_out(request.user))


# ── OTP password-change (no existing auth required) ────────────────────────────

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([OTPRequestThrottle])
def request_otp(request):
    """
    Generate a 6-digit OTP and email it.
    Always returns 200 — even if email is not registered (prevents enumeration).
    """
    email = (request.data.get('email') or '').strip().lower()
    if not email:
        raise ValidationError({'detail': 'email is required.'})

    user = TDSUser.objects.filter(email=email).first()
    if user and user.is_active:
        otp = generate_otp(email)
        try:
            send_otp_email(email, otp, user.full_name or "")
        except RuntimeError as exc:
            # BUG FIX: this used to return a 503 here, which broke two things
            # at once, discovered by actually running the "forgot password"
            # flow end-to-end with SMTP unreachable (the same situation the
            # login/device-OTP flow was already fixed for earlier):
            #   1. This function's own docstring promises "Always returns
            #      200 - even if email is not registered (prevents
            #      enumeration)". A 503 only ever happens for an email that
            #      IS registered (otp is only generated inside this `if`
            #      branch) - so the status code itself leaked account
            #      existence, the exact thing the 200-always contract exists
            #      to prevent.
            #   2. generate_otp(email) above already committed a valid,
            #      checkable OTP before this send attempt, and
            #      send_otp_email() already prints it to the console as a
            #      DEBUG-mode fallback (see otp_service.py) - so the code was
            #      real and usable, but the 503 response stopped the
            #      frontend's Change Password modal from ever advancing to
            #      the "enter OTP" step, blocking password resets entirely
            #      whenever the mail server hiccups.
            # Log the failure for operators and fall through to the same 200
            # every other path returns - never surface email-transport
            # failures to the caller.
            logger.error("request_otp: failed to send OTP email to %s: %s", email, exc)
    return Response({'message': 'If that email is registered, an OTP has been sent.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([OTPVerifyThrottle])
def verify_otp_and_change(request):
    """Verify a previously requested OTP and update the user's password."""
    data = request.data
    email        = (data.get('email') or '').strip().lower()
    otp_code     = data.get('otp', '')
    new_password = data.get('new_password', '')

    if not new_password or len(new_password) < 8:
        raise ValidationError({'detail': 'Password must be at least 8 characters.'})
    if not verify_otp(email, otp_code):
        return Response(
            {'detail': 'Invalid or expired OTP. Please request a new one.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    user = TDSUser.objects.filter(email=email).first()
    if not user or not user.is_active:
        raise NotFound('User not found or inactive.')
    user.password_hash = _hash_password(new_password)
    user.save()
    return Response({'message': 'Password updated successfully.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_own_password(request):
    """
    Change the current user's own password.
    Body: { "current_password": "...", "new_password": "..." }
    """
    data       = request.data
    current_pw = data.get('current_password', '')
    new_pw     = data.get('new_password', '')
    user       = request.user

    if not _verify_password(current_pw, user.password_hash):
        return Response(
            {'detail': 'Current password is incorrect.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    if not new_pw or len(new_pw) < 8:
        raise ValidationError({'detail': 'New password must be at least 8 characters.'})

    user.password_hash = _hash_password(new_pw)
    user.save()
    return Response({'message': 'Password changed successfully.'})


# ── Bootstrap (no auth required — only works when zero users exist) ────────────

# Arbitrary fixed key for the Postgres advisory lock below — any 64-bit int
# works, it just needs to be constant and not collide with another lock use.
_SETUP_FIRST_USER_LOCK_KEY = 727501001


@api_view(['POST'])
@permission_classes([AllowAny])
def setup_first_user(request):
    """Create the very first admin user when no users exist in the database."""
    data     = request.data
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email:
        raise ValidationError({'detail': 'Email is required.'})
    if not is_allowed_email_domain(email):
        raise ValidationError({'detail': 'Only @ravasco.com email addresses are allowed.'})
    # SECURITY (fixed): this endpoint previously accepted any password —
    # including empty — for the very first (admin) account, since it skipped
    # the length/strength check that create_user() below already enforces
    # for every other user. Apply the same rule here.
    if not password or len(password) < 8:
        raise ValidationError({'detail': 'Password must be at least 8 characters.'})

    # SECURITY (fixed): the exists()-check-then-save() below used to run with
    # no locking, so two concurrent POSTs made before the real admin finishes
    # setup could both pass the "no users yet" check and both create an admin
    # account — a first-boot admin-hijack race. A Postgres advisory lock
    # scoped to this transaction serializes concurrent calls to this endpoint
    # so only one can ever win, without needing a row to lock (there isn't one
    # yet — that's exactly the case this endpoint handles).
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [_SETUP_FIRST_USER_LOCK_KEY])
        if TDSUser.objects.exists():
            return Response(
                {'detail': 'Users already exist. Use POST /api/users.'},
                status=status.HTTP_409_CONFLICT
            )
        user = TDSUser(
            email         = email,
            password_hash = _hash_password(password),
            full_name     = data.get('full_name'),
            designation   = data.get('designation'),
            role          = 'admin',
            is_active     = True,
        )
        user.save()
    return Response(_user_out(user), status=status.HTTP_201_CREATED)


# ── User management ────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_users(request):
    """List all application users. Admin and user roles only."""
    if request.user.role not in ('admin', 'tds_creator'):
        raise PermissionDenied('Admin or User role required')
    users = TDSUser.objects.order_by('user_id')
    return Response([_user_out(u) for u in users])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_user(request, user_id):
    """Get a user's profile. Any user can view their own; admin can view all."""
    if request.user.role != 'admin' and request.user.user_id != user_id:
        raise PermissionDenied('You can only view your own profile')
    user = TDSUser.objects.filter(pk=user_id).first()
    if not user:
        raise NotFound(f"User {user_id} not found")
    return Response(_user_out(user))


@api_view(['POST'])
@permission_classes([IsAdmin])
def create_user(request):
    """Create a new application user. Admin only."""
    data = request.data
    role = data.get('role') or 'tds_creator'
    if role not in _VALID_ROLES:
        raise ValidationError({'detail': f"role must be one of {_VALID_ROLES}"})
    email = (data.get('email') or '').strip().lower()
    if not is_allowed_email_domain(email):
        raise ValidationError({'detail': 'Only @ravasco.com email addresses are allowed.'})
    if TDSUser.objects.filter(email=email).exists():
        return Response(
            {'detail': 'Email already registered'},
            status=status.HTTP_409_CONFLICT
        )
    password = data.get('password', '')
    if not password or len(password) < 8:
        raise ValidationError({'detail': 'Password must be at least 8 characters.'})
    user = TDSUser(
        email         = email,
        password_hash = _hash_password(password),
        full_name     = data.get('full_name'),
        designation   = data.get('designation'),
        role          = role,
        is_active     = True,
    )
    user.save()
    return Response(_user_out(user), status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsAdmin])
def update_user(request, user_id):
    """Update a user's role, active status, name, or designation. Admin only."""
    user = TDSUser.objects.filter(pk=user_id).first()
    if not user:
        raise NotFound(f"User {user_id} not found")
    data = request.data
    if 'role' in data and data['role'] is not None:
        if data['role'] not in _VALID_ROLES:
            raise ValidationError({'detail': f"role must be one of {_VALID_ROLES}"})
        user.role = data['role']
    if 'is_active' in data and data['is_active'] is not None:
        user.is_active = bool(data['is_active'])
    if 'full_name' in data and data['full_name'] is not None:
        user.full_name = data['full_name']
    if 'designation' in data and data['designation'] is not None:
        user.designation = data['designation']
    user.save()
    return Response(_user_out(user))
