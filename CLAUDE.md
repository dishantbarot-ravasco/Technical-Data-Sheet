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

**Belt description format has no separate fabric-code field** — `_belt_description()`
(`apps/api/routers/batch_views.py`) and the frontend's `updateBeltDescription()` /
`liveParseBeltDescription()` (`frontend/js/generate-tds.js`) no longer take a `fabric_code`
argument. `BeltRating.rating_name` already starts with the fabric code (e.g. `"EP 1000/5"`), so
including both used to render as a duplicated `"EP X EP 1000/5"`. The belt-line paste format
(single-belt box, Multiple Belts box, and the `text_import_batch` endpoint) dropped from 10–13
fields to 9–12 accordingly — width X rating X top X bottom X grade X edge X end X type X length
[X bot_plies X bob_plies [X carcass_mm]]. When parsing a fabric code out of that format, derive
it from the rating's leading word (`rating.split(/\s+/)[0]`) rather than expecting its own field.

**Net/gross weight is a precise decimal, not rounded up to the nearest 0.5** —
`packing_service.compute_packing()`'s `net_weight_kg`/`gross_weight_kg`/
`gross_weight_per_roll_kg`, and the matching client-side previews in `recalcWeight()` /
`recalcPacking()` (`frontend/js/generate-tds.js`), now use plain `round(x, 2)` instead of
`_round_up_half()`. `_round_up_half()` is still correct and still used for physical roll
dimensions (reel height/width/length per roll) — it's specifically weight that changed, because
rounding weight up to 0.5 kg increments made the Belt Specs panel's per-metre weight × length
not reconcile with the displayed total, and made Belt Specs disagree with Packing & Logistics for
the same belt. Any new weight calculation should default to precise rounding unless it's a
physical roll dimension.

**Packing override fields are all-or-nothing on the frontend, not fallback-to-preview** —
`captureBeltSpec()` and `submitTDS()` in `frontend/js/generate-tds.js` only send
`num_rolls` / `length_per_roll_m` / `net_weight_kg` / `gross_weight_kg` when the user explicitly
typed into the corresponding `-override` input; they no longer fall back to the readonly
auto-computed display field's value. `roll_dimensions` is now always sent as `null` from the
frontend. This means the server's own `compute_packing()` is authoritative whenever no override
is present, rather than silently trusting a client-side preview value that could drift from the
server's own math (this was the root cause of the Belt Specs vs. Packing & Logistics mismatch
above).

**Global error-handling pass (backend + frontend)**:
- `apps/api/exceptions.py`'s `custom_exception_handler` now describes more exception types
  instead of collapsing everything DRF doesn't recognize into a generic 500. `ValueError` /
  `KeyError` / Django's `ValidationError` / `ObjectDoesNotExist` are treated as deliberate
  application-level errors and returned as `400` with `str(exc)` as the detail (this list —
  `_DESCRIBABLE_EXCEPTIONS` — is meant to only contain exception types whose message is always
  safe to show a client; audit any new addition for that before including it).
  `django.db.utils.IntegrityError` gets a dedicated `_describe_integrity_error()` that inspects
  the DB driver's `diag` info to say "already exists" / "referenced record no longer exists" /
  "required field was left empty" without leaking raw SQL. Everything else (e.g. `AttributeError`,
  `TypeError` — likely real bugs) still returns a generic 500, except in `DEBUG` mode where the
  exception type/message is appended for local debugging.
- `frontend/js/api.js`'s `apiFetch()` now flattens DRF's serializer-validation error shape
  (`{detail: {field: [msg, ...]}}`) into readable `"field: msg"` text instead of showing a raw
  JSON blob in the toast. `downloadPdf()` now reads the JSON error body on a failed PDF request
  instead of only reporting the HTTP status.
- `frontend/js/auth.js` (loaded first on every authenticated page) now has a global
  `window.addEventListener('unhandledrejection', ...)` that shows a toast for any promise
  rejection that never reached a try/catch — a backstop, not a replacement for the existing
  try/catch + `showToast` pattern used throughout the app.

**PDF breaker rows are omitted entirely when not selected, not printed as "No"** —
`build_tds_doc_data()` in `apps/services/pdf_service.py` now skips the
`"Breaker on Top | Number of Plies"` / `"Breaker on Bottom | Number of Plies"` GI rows outright
when `tds.breaker_top`/`tds.breaker_bottom` is falsy, mirroring the existing group-level skip for
Splicing Parameters (when splicing isn't required) but at the single-parameter level, since these
two rows live inside Belt Construction Parameters alongside other always-shown fields.

## Known future work / deferred proposals (v2 candidates)

These were discussed across past sessions but deliberately not built yet — either genuinely
deferred, or blocking on something outside the codebase (a source file, a third-party account,
factory data). Verify current status before assuming any of these is still open — some may have
been resolved in a session not captured here.

**PDF snapshot at approval time** — every TDS PDF (single download, batch ZIP/print-all, revision
history) is currently rendered live from the DB on every request; nothing is ever stored. Over a
2-5 year horizon this means a template/lookup-table edit silently changes how already-approved,
"issued" documents look on next download — a real gap for an ISO 9001 document-control system.
Full design (new `TDSPdfSnapshot` model storing PDF bytes in Postgres, captured once at
`approve_tds()`/`update_status()`, consumed by `pdf_views.py`'s `generate_pdf`/
`render_tds_pdf_bytes`) is written up at `C:\Users\Admin\.claude\plans\velvety-munching-honey.md`.

**Point-in-time EAV snapshotting for revision PDFs** — `TDSRevision.snapshot` only captures
directly-changed fields (e.g. belt width), not the *resolved* EAV spec values (cover grade specs,
fabric parameters, test methods) the PDF renderer joins live from current master data. A later
correction to a lookup table therefore changes how old revision PDFs render, same root cause as
the approval-snapshot gap above. The "overlay" approach (`build_tds_doc_data`'s `overrides=`
param in `revisions_views.py`) shipped as a stopgap; the deferred follow-up is expanding
`_update_tds()` in `apps/api/routers/tds_views.py` to also snapshot resolved EAV values at edit
time, for a true point-in-time reconstruction.

**Revision number on the PDF itself** — adding "Revision" to the PDF's General Information
section was scoped but deferred: GI rows are entirely DB-driven (not hardcoded), so this needs a
data migration seeding a new `TDSParameter`/`BrandParameter` row per brand, which was judged a
different/riskier risk class than the rest of the versioning work.

**~~Dedicated `noreply@ravasco.com` mailbox~~ — resolved.** Decided against a separate alias/mailbox;
all app email (OTP, notifications, daily report) sends from `dishant.barot@ravasco.com` directly —
a real, already-verified Workspace mailbox — which sidesteps the Gmail From-header-override issue
entirely since the authenticated SMTP account and the From address are the same. `SMTP_USER` /
`SMTP_FROM` in `render.yaml` and `.env` are both set to that address.

**Security/testing roadmap leftovers** (from a 5-tier hardening pass — confirm each is still
outstanding before treating as open):
- A real external pentest / OWASP ZAP scan against a staging deployment (no staging environment
  exists yet).
- Activating Sentry: scaffolding is already in `settings.py` (inert until `SENTRY_DSN` is set) —
  needs a Sentry project created and the DSN set in Render's env vars.
- Deploying migration `0021_fix_chk_user_role` to production (was applied to local dev only as of
  that session).
- A manual screen-reader walkthrough of the generate-tds flow end-to-end — only automated
  axe-core static scanning across pages was done, not the manual walkthrough originally scoped.

**QAP template gaps**:
- `SAMPLE_QAP.xlsx` (the QAP seed source, not in the repo) has a numeric `1.1` cell where the SN
  should read text `"1.10"` — patched via a one-off DB migration, but re-running
  `seed_qap_templates --replace` from that same spreadsheet will reintroduce the bug for all three
  templates. Needs the source file corrected directly.
- `OR` and `FR_CAN` QAP categories exist as placeholders in `qap_service.py`'s
  `STANDARD_TO_QAP_CATEGORY` mapping with no seeded template data — awaiting factory-supplied
  source data before `seed_qap_templates` can be extended to cover them.

**Daily report trend context** — floated but never decided: showing month-to-date or
week-over-week comparison (e.g. "23 today vs. 15/day average this month") in the daily TDS admin
email, for context beyond a raw daily count.

**Task queue infrastructure (Celery/RQ)** — considered and explicitly deemed unnecessary at
current volume (a handful of emails/day, one daily report); the background-thread fix applied to
login/device-verify emails (`apps/services/device_service.py`) is the proportional tool for now.
Revisit only if job volume grows enough to need retries/guaranteed delivery, or async work beyond
simple emails (e.g. bulk PDF generation) is added.
