from django.urls import path
from apps.api.routers.qap_views import generate_qap_pdf

urlpatterns = [
    path('tds/<int:tds_id>/qap/pdf', generate_qap_pdf, name='generate_qap_pdf'),
]
