"""
apps/api/routers/users_urls.py — URL patterns for user management endpoints.
Included at /api/ by apps/api/urls.py.

Note: POST /api/auth/login is handled by Phase 3 (auth_views.py).
"""
from django.urls import path
from . import users_views as v

urlpatterns = [
    # Auth
    path('auth/me',               v.me,                   name='auth-me'),
    path('auth/request-otp',      v.request_otp,          name='auth-request-otp'),
    path('auth/verify-otp',       v.verify_otp_and_change, name='auth-verify-otp'),
    path('auth/change-password',  v.change_own_password,  name='auth-change-password'),
    # Users
    path('users/setup',           v.setup_first_user,     name='users-setup'),
    path('users',                 v.list_users,           name='users-list'),
    path('users/',                v.create_user,          name='users-create'),
    path('users/<int:user_id>',   v.get_user,             name='user-detail'),
    path('users/<int:user_id>/',  v.update_user,          name='user-update'),
    path('users/<int:user_id>/signature', v.user_signature, name='user-signature'),
]
