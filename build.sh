#!/usr/bin/env bash
# build.sh — Render build script for TDS Automation App
# Runs from the repo root (tds_app/) during Render's build phase.
set -e

echo "==> Installing Python packages..."
pip install -r requirements.txt

echo "==> Collecting static files (Django admin)..."
cd django_backend
python manage.py collectstatic --noinput

echo "==> Running database migrations..."
python manage.py migrate

echo "==> Ensuring cache table exists..."
python manage.py createcachetable

echo "==> Build complete."
