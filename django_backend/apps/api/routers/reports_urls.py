"""
apps/api/routers/reports_urls.py — URLs for reports_views.
"""
from django.urls import path

from .reports_views import trigger_daily_report

urlpatterns = [
    path('internal/send-daily-report/', trigger_daily_report, name='send-daily-report'),
]
