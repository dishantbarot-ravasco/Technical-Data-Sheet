# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All Django commands run from `django_backend/` (not the repo root).

```bash
# Run the dev server
cd django_backend && python manage.py runserver

# Run the full test suite
cd django_backend && python manage.py test

# Run one app's tests / one test class / one test method
python manage.py test apps.services
python manage.py test apps.services.tests.test_calculations.PureMathTests
python manage.py test apps.services.tests.test_calculations.PureMathTests.test_round_half_up_rounds_half_away_from_bankers_rounding

# Migrations
python manage.py makemigrations --check --dry-run   # detect model/migration drift (also run in CI)
python manage.py migrate
python manage.py check                              # Django system check
python manage.py check --deploy                     # production security checks

# Dependency vulnerability scan (also run in CI)
pip install pip-audit && pip-audit -r requirements.txt
```

CI (`.github/workflows/ci.yml`) runs on every push/PR: pip-audit, `makemigrations --check`,
`check`, a full `migrate` against a **brand-new** Postgres container, `createcachetable`, then
the test suite. The "migrate against a brand-new database" step is not a formality — see
"Migration history" below for why it used to fail.

There is no separate FastAPI app anymore; comments referencing "the FastAPI app" or "ported
from FastAPI" throughout the codebase describe the previous implementation this Django app
replaced. `run_django.py` / `start.py` at the repo root are Render-entrypoint shims, not a
second live app.

## Architecture

**Two Django "apps" with very different roles**, both under `django_backend/apps/`:
- `apps.core` — models and migrations only. No views.
- `apps.api` — every HTTP-facing view, under `apps/api/routers/*_views.py`, wired up in
  `apps/api/urls.py`. Business logic that isn't pure HTTP plumbing lives in `apps/services/`
  (calculations, splicing, packing, PDF rendering, QAP assembly, OTP/device-trust, TDS
  numbering) and is imported by the views — keep that separation when adding features: routers
  parse/validate the request and call a service function, they don't embed the domain math.

**Frontend is static HTML+vanilla JS**, served by WhiteNoise directly from `frontend/` at the
site root (`frontend/index.html` → `/index.html`, etc. — see `WHITENOISE_ROOT` in
`config/settings.py`). There is no build step, bundler, or frontend framework. Every page links
the shared `frontend/css/style.css` (cross-page building blocks only) and layers page-specific
CSS in its own `<style>` block. `frontend/js/auth.js` has no imports and must load first on any
authenticated page — it also injects the mobile nav toggle as a side effect.

**Auth is device-aware 2FA, not a plain JWT login**: `POST /api/auth/login` returns either a full
JWT (trusted device) or `{status: 'device_verify'}` (new device → 6-digit email OTP →
`POST /api/auth/device-verify`). The JWT lives in an httpOnly `tds_access` cookie
(`apps/services/device_service.py`), with a `tds_refresh` cookie (scoped to `/api/auth/`) backing
a 30-day "remember me". `TDSCookieJWTAuthentication` (`apps/api/auth_backend.py`) tries the
cookie first, then falls back to a `Bearer` header for non-browser API clients. Roles are
`admin` / `tds_creator` / `viewer`, gated by `apps/api/permissions.py`'s `IsAdmin` /
`IsEditor` / `IsCreator` (viewer is search/view/download only).

**Migration history has real, historical gaps — don't assume `manage.py migrate` on a fresh DB
"just works" without checking.** ~30 reference/lookup tables (`purposes`, `standards`,
`tds_inputs`, `cover_grades`, `belt_ratings`, etc.) were declared `managed=False` in
`0001_initial.py` because the schema was originally provisioned outside Django; migrations
0008/0009/0014 later flipped them to `managed=True` (some flip-flopped False→True→False→True
across those three migrations — check the *current* state before assuming a table's DDL history
is clean). `0001_initial.py` now actually creates these tables for real (see the comment at its
top) so CI's from-scratch `migrate` works, but a few things follow from this:
- A handful of tables have **no DB-side auto-increment on their PK** — they were always
  populated with explicit IDs (`purposes`, `belt_types`, `brands`, `standards`,
  `tds_parameters`, `brand_parameters`). Creating a row via the ORM without an explicit PK on
  one of these raises `IntegrityError: null value in column ... violates not-null constraint`.
  Test factories that create these (`apps/api/tests/factories.py`) must pass an explicit PK.
- `tds_inputs.edge_construction`'s DB `CHECK` constraint only allows `'Cut Edge'` /
  `'Moulded Edge'` — not `'Moulded'`. `users.role`'s `CHECK` constraint (fixed in migration
  `0021_fix_chk_user_role`) allows `'admin' | 'tds_creator' | 'viewer'`.
- When adding a migration that touches one of these tables, prefer `SeparateDatabaseAndState`
  if the physical column might already exist from an earlier raw-SQL migration (see `0015`'s
  handling of `tds_inputs.batch_id`, which `0006_tds_batch`'s `RunSQL` already added) — a plain
  `AddField` will fail with "column already exists" on a from-scratch replay otherwise.

**Tests always create a real Postgres test database** (no sqlite, no mocking the DB) — `CACHES`
is forced to `LocMemCache` when `'test' in sys.argv` specifically because `DatabaseCache`'s table
isn't created by a migration (see `createcachetable` above) and the throwaway test DB never gets
it otherwise. `apps/api/tests/factories.py` has the shared fixture builders (`make_user`,
`make_tds_lookup_set`) — use them instead of re-deriving the `TDSInput` FK graph
(Purpose → BeltType → Brand → Standard → CoverGrade → FabricType → BeltRating) in every test.

**Caching**: reference/lookup endpoints in `apps/api/routers/master_views.py` and
`lookup_views.py` are wrapped in `@cache_page(CACHE_TTL_SECONDS)` (1 hour), backed by
`DatabaseCache` in production (shared across gunicorn's worker *processes* — `LocMemCache`
would not be). `cache_page` must be the outermost decorator, above `@api_view`, or it caches
DRF's un-rendered `Response` and errors. `GET /api/bootstrap` is deliberately **not** cached —
it embeds a live `customers` slice that changes whenever a user adds one via the generate-TDS
form.

**Dependency policy**: don't adopt a new release of an open-source library until it's been
public for at least 7 days (supply-chain risk mitigation — enforced by `renovate.json`'s
`minimumReleaseAge`). Check a package's PyPI release date before manually bumping
`requirements.txt`.

**Accessibility / contrast**: `--gold` / `--gold-light` (in `frontend/css/style.css`'s `:root`)
are brand accent colors that fail WCAG AA as *text* color on light backgrounds — use
`--gold-text` for text, keep `--gold`/`--gold-light` for backgrounds/borders/accents only. The
same applies to white text on a `--gold-light`/`#F5A623` background (use `--navy` instead) — this
pattern recurs across `admin.html`, `generate-tds.html`, `search-tds.html`, `home.html`, and the
calculator pages, so check both directions (color-as-text and text-on-that-background) when
touching gold-family styling anywhere.
