# TDS Automation App

Internal tool for **Ravasco Transmission & Packing Limited** that generates, manages, and
distributes conveyor-belt Technical Data Sheets (TDS) and Quality Assurance Plans (QAP),
compliant with IS 1891 / IS 14206 and related standards (ISO 14890, DIN 22102, AS 1332,
SANS 1173, ASTM D378).

## What it does

- Generates single-belt or multi-belt TDS documents as PDF, with belt weight, reel diameter,
  splice length, and packing dimensions computed server-side from standards-based formulas.
- Generates matching QAP (Quality Assurance Plan) PDFs, template-resolved by standard.
- Role-based access: **admin** (full access), **tds_creator** (create/edit TDS),
  **viewer** (search/view/download only).
- Device-aware two-factor login (email OTP on first sign-in from a new browser, remembered
  after that) — see [CLAUDE.md](CLAUDE.md) for the auth flow details.
- Admin panel with usage analytics, user management, and a daily email report.

## Stack

- **Backend**: Django 5.2 + Django REST Framework, PostgreSQL, WeasyPrint (PDF rendering from
  Jinja2 templates), simplejwt (JWT auth).
- **Frontend**: static HTML + vanilla JS, no build step — served directly by WhiteNoise from
  `frontend/`.
- **Deployment**: Render (`render.yaml`), gunicorn, a single free-tier Postgres instance shared
  by the app and its cache table.

## Local setup

1. Install dependencies (Python 3.12+):
   ```bash
   pip install -r requirements.txt
   ```
2. Create `.env` in the repo root (same file the app reads at runtime) with at least:
   ```
   TDS_SECRET_KEY=...
   DATABASE_URL=postgresql://user:pass@localhost:5432/technical_data_sheet
   ```
   See `django_backend/config/settings.py` for every setting read from the environment
   (SMTP, Google OAuth, `ALLOWED_EMAIL_DOMAIN`, etc. are all optional for local dev).
3. Run migrations and start the server:
   ```bash
   cd django_backend
   python manage.py migrate
   python manage.py createcachetable
   python manage.py runserver
   ```
4. Create an admin user via `python manage.py createsuperuser` or Django Admin, then sign in at
   `/index.html`.

## Testing

```bash
cd django_backend
python manage.py test
```

The suite (`apps/services/tests/`, `apps/api/tests/`) covers the calculation/service layer and
the auth, TDS-creation, packing-recompute, and QAP-generation write paths against a real
Postgres test database — see [CLAUDE.md](CLAUDE.md) for how to run a single test and for
migration-history gotchas that matter when writing new tests.

CI (GitHub Actions, `.github/workflows/ci.yml`) runs a dependency vulnerability scan
(`pip-audit`), a migration-drift check, and the full test suite against a from-scratch
Postgres database on every push and pull request.

## Deployment

Render reads `render.yaml`. `build.sh` installs dependencies, collects static files, runs
migrations, and ensures the cache table exists. See `render.yaml`'s comments for which
environment variables need to be set manually in the Render dashboard (Google OAuth
credentials, SMTP credentials, the daily-report cron secret).
