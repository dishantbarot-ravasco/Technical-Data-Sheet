tds-app-codebase-knowledge-map.md


# TDS App — Full Codebase Knowledge Map
 
*Last updated: 2026-08-24. All files read including pdf_renderer.py, packing_service.py, splicing_service.py, exceptions.py, all management commands, and all three standalone frontend calculators.*
 
---
 
## 1. Tech Stack
 
| Layer | Technology |
|-------|-----------|
| Backend | Django 5.2 + Django REST Framework |
| Database | PostgreSQL (`technical_data_sheet` DB) |
| Auth | simplejwt with custom `TDSCookieJWTAuthentication` |
| Password hashing | bcrypt (direct, not passlib) — rounds=12 for passwords, rounds=10 for OTPs |
| PDF generation | WeasyPrint + Jinja2 (`pdf_renderer.py` + `apps/services/templates/tds.html`) |
| PDF merging | pypdf (for batch ZIPs) |
| Static file serving | WhiteNoise (serves the entire `frontend/` directory) |
| Frontend | Vanilla HTML/CSS/JS (ES modules), no framework |
| Deployment | Render.com free tier, free PostgreSQL |
| Time zone | `Asia/Kolkata` |
| Google OAuth | google-auth-oauthlib + PKCE |
| TOTP 2FA | pyotp |
| Email | SMTP (Django's send_mail); OTP fallback to stdout |
 
---
 
## 2. Repository Layout
 
```
tds_app/
├── django_backend/
│   ├── config/
│   │   ├── settings.py          ← Django settings (all env vars, JWT, CORS, throttling)
│   │   ├── urls.py              ← Root URL conf (API + catch-all frontend)
│   │   └── middleware.py        ← AdminOnlyCsrfMiddleware
│   └── apps/
│       ├── core/
│       │   ├── models.py        ← ALL 31 models (schema source of truth)
│       │   ├── admin.py         ← Django admin registration for all models
│       │   └── management/
│       │       └── commands/
│       │           ├── seed_dimensional_specs.py  ← Seeds tolerance data for 8 standards
│       │           ├── wipe_test_tds_data.py      ← Deletes all TDSInput/TDSBatch rows (dry-run safe)
│       │           ├── send_daily_tds_report.py   ← CLI wrapper for daily email report
│       │           └── check_cover_grade_usage.py ← Read-only diagnostic for cover grade FK usage
│       ├── api/
│       │   ├── auth_backend.py     ← TDSCookieJWTAuthentication + TDSUserBackend
│       │   ├── auth_views.py       ← Login, TOTP setup, Google OAuth (plain Django views)
│       │   ├── auth_serializers.py ← simplejwt token subclasses
│       │   ├── exceptions.py       ← Custom DRF exception handler (wraps all errors as {"detail": ...})
│       │   ├── permissions.py      ← IsAdmin, IsEditor
│       │   ├── views.py            ← GET /api/health/ only
│       │   ├── urls.py             ← API URL aggregator (batch+lookup BEFORE tds)
│       │   └── routers/
│       │       ├── tds_views.py / tds_urls.py
│       │       ├── batch_views.py / batch_urls.py
│       │       ├── lookup_views.py / lookup_urls.py
│       │       ├── master_views.py / master_urls.py
│       │       ├── users_views.py / users_urls.py
│       │       ├── pdf_views.py / pdf_urls.py
│       │       ├── packing_views.py / packing_urls.py
│       │       ├── reports_views.py / reports_urls.py
│       │       ├── device_views.py / device_urls.py
│       │       ├── google_oauth_views.py / google_oauth_urls.py
│       │       └── totp_views.py / totp_urls.py
│       └── services/
│           ├── calculations.py        ← All belt math formulas
│           ├── tds_number.py          ← Atomic TDS number generator
│           ├── sections.py            ← Parameter group order + customer-copy exclusions
│           ├── pdf_service.py         ← build_tds_doc_data() + dataclasses
│           ├── pdf_renderer.py        ← render_tds_html() / render_tds_pdf() via Jinja2+WeasyPrint
│           ├── packing_service.py     ← compute_packing() + PackingResult dataclass
│           ├── splicing_service.py    ← compute_splicing() + SplicingResult dataclass
│           ├── device_service.py      ← TrustedDevice management
│           ├── otp_service.py         ← OTP generate/verify/email
│           ├── totp_service.py        ← TOTP enrollment/verify + JWT helpers
│           └── tds_report_service.py  ← Daily email report
│           # Static assets for PDF:
│           # apps/services/templates/tds.html   ← Jinja2 PDF template (A4, #F5A623 orange)
│           # apps/services/static/indus_logo.png
│           # apps/services/static/tuv_logo.png
├── frontend/
│   ├── css/style.css
│   ├── img/
│   ├── js/
│   │   ├── api.js                    ← All API fetch helpers
│   │   ├── auth.js                   ← Session management, login flow, nav user
│   │   ├── generate-tds.js           ← TDS creation form logic (v7, ~2089 lines)
│   │   └── search-tds.js             ← TDS search/filter/modal/delete logic
│   ├── index.html                    ← Login page (Step 1: creds, Step 2: device verify)
│   ├── home.html                     ← Dashboard (stats + recent TDS + quick-action cards)
│   ├── generate-tds.html             ← TDS form (single-belt + multi-belt queue)
│   ├── search-tds.html               ← Search + detail modal + delete confirm
│   ├── tds-preview.html              ← PDF preview with checkbox exclusion injection
│   ├── tds-multi-preview.html        ← Batch preview (summary chips + all-belt table + ZIP/merge actions)
│   ├── packing-calculator.html       ← Standalone packing calculator (no TDS creation)
│   ├── splicing-calculator.html      ← Standalone splicing calculator (fully client-side, no API calls)
│   └── admin.html                    ← Admin panel (users/analytics/all-TDS/system info)
├── start.py                          ← Render build runner (collectstatic + migrate + gunicorn)
└── run_django.py                     ← Local dev runner
```
 
---
 
## 3. Database Schema (31 Models in `apps/core/models.py`)
 
### 3a. Managed Models (Django creates/migrates these tables)
 
| Model | Table | Purpose |
|-------|-------|---------|
| `TDSUser` | `users` | App users; NOT AbstractBaseUser; has `is_authenticated=True` class attr for DRF |
| `OTPCode` | `otp_codes` | bcrypt-hashed 6-digit codes, 10-min TTL, max 5 attempts |
| `TrustedDevice` | `trusted_devices` | 64-char hex device token, httpOnly `tds_device` cookie (1yr) |
| `UserTOTP` | `user_totp` | pyotp TOTP secrets (confirmed flag) |
| `TDSInput` | `tds_inputs` | Core TDS record — ALL fields of a belt spec |
| `TDSBatch` | `tds_batches` | Groups of TDS records from a single batch-import |
| `TDSSequence` | `tds_sequence` | Single row (year=0) for atomic TDS number generation |
 
### 3b. Unmanaged Models (master data, `managed=False`)
 
All read from pre-existing DB tables:
 
| Model | Notes |
|-------|-------|
| `Standard` | Testing standards (IS 1891, ISO 14890, DIN 22102, etc.) |
| `Purpose` | Domestic / International |
| `BeltType` | Flat Belt, Chevron, etc. |
| `Brand` | INDUS SUPER BRUTE, etc. |
| `FabricType` | EP, NN, EE, etc. |
| `FabricStyle` | Weave variants per fabric type |
| `CoverGrade` | Grade code + specific gravity (M, H, etc.) |
| `BeltRating` | e.g. EP 1000/5 → kN/m + num_plies |
| `BrandParameter` | Junction: brand → parameter |
| `TDSParameter` | Parameter definitions (name, group, test method, reference) |
| `CoverGradeValue` | EAV: cover grade → parameter value pairs |
| `FabricTypeParameterValue` | EAV: fabric type → parameter value pairs |
| `FabricStyleParameterValue` | EAV: fabric style → parameter value pairs (overrides) |
| `BeltRatingValue` | EAV: belt rating → parameter value pairs |
| `DimensionalParameterSpec` | Width-band tolerance strings per standard |
| `StandardTestMethod` | Test method text per parameter per standard |
| `Customer` | Customer master (name, contact, application, location) |
| `ReelType` | Reel geometry (formula_key, core_diameter_m, center_to_center_m, max_roll_diameter_m, num_rolls_base) |
| `PackingType` | Packing options (name, is_available) |
| `ContainerType` | Container specs |
| `RegionContainerWeightLimit` | Max gross weight per container+region combo |
| `SpliceMethodConfig` | Hot/cold splice buffer mm (DB override of hardcoded fallbacks) |
| `SpliceStepLookup` | fabric_rating → step_length_mm lookup rows (ascending order) |
| `HotSpliceCuringLookup` | Thickness-band → curing time/temp table |
| `SamplingPlanLookup` | Belt length → sampling count (IS 1891 table) |
 
### 3c. Key TDSInput Fields
 
- **Identity**: `tds_id`, `tds_number` (zero-padded 4-digit), `tds_date`, `tds_doc_number`, `status` (draft/approved/declined)
- **References**: `purpose_id`, `belt_type_id`, `brand_id`, `standard_id`, `customer_id`, `created_by_id`, `approved_by_id`
- **Belt spec**: `belt_description`, `construction_type`, `belt_width_mm`, `belt_length_m`, `edge_construction`
- **Cover/fabric**: `cover_grade_id`, `fabric_type_id`, `fabric_style_id`, `make_of_fabric`, `belt_rating_id`
- **Dimensions**: `num_plies`, `top_cover_mm`, `bottom_cover_mm`, `carcass_thickness_mm`, `carcass_from_rating`, `total_thickness_mm`
- **Breakers**: `breaker_top`, `breaker_top_plies`, `breaker_bottom`, `breaker_bottom_plies`
- **Weight**: `belt_weight_per_m_kg`, `belt_gross_weight_per_m_kg`
- **Packing**: `reel_type_id`, `packing_type_id`, `num_rolls`, `length_per_roll_m`, `roll_dimensions`, `net_weight_kg`, `gross_weight_kg`, `gross_weight_per_roll_kg`
- **International**: `shipping_region`, `container_type_id`
- **Splicing**: `splicing_required`, `vulcanization_method`, `num_joints`, `step_length_mm`, `splice_length_mm`, `total_extra_length_m`, `interply_skim_mm`
---
 
## 4. Authentication & Security
 
### Login Flow (Two-Factor Device-Trust)
 
```
POST /api/auth/login
  │
  ├── credentials wrong → 401
  ├── too many attempts → 429 (throttle: 5/min/IP)
  ├── device cookie valid (TrustedDevice lookup) → issue JWT, return {status: "ok"}
  └── new device:
        ├── generate_otp() → send_otp_email() (bcrypt-hashed OTP in DB)
        ├── store pending_user_id in Django session
        └── return {status: "device_verify"}
            │
            POST /api/auth/device-verify (code)
              ├── verify_otp() — 10-min TTL, 5-attempt max
              ├── make_full_jwt() → access + refresh tokens
              ├── register_device() → creates TrustedDevice + sets tds_device cookie (1yr httpOnly)
              ├── send_new_device_notification() + notify_admins_new_device_login()
              └── return JWT + set cookie
```
 
### Google OAuth (PKCE)
 
```
GET /api/auth/google/login/
  → stores state + code_verifier in Django session → redirect to Google
 
GET /api/auth/google/callback/
  → verify state (CSRF), pop code_verifier, fetch token, get userinfo
  → user must already exist (no auto-registration)
  → trusted device? → redirect ?oauth_token=<jwt>
  → new device? → send OTP, redirect ?step=device_verify
```
 
### JWT Cookie Setup
 
- Cookie name: `TDS_COOKIE_NAME` setting (e.g. `tds_auth`)
- Max age: `TDS_COOKIE_MAX_AGE` (default 12hr)
- httpOnly, Secure (prod), SameSite=Lax
- `TDSCookieJWTAuthentication`: reads httpOnly cookie first, falls back to `Authorization: Bearer` header
- Token contains: `sub` (user_id), `role`, `email`, `full_name`
### Roles
 
- `admin` — full access, user management, admin panel
- `user` — create/edit/approve/decline/delete TDS
- `viewer` — search + view + download only
### Rate Limiting (DRF Throttle)
 
- Login: 5/min/IP (anonymous)
- OTP request: 3/min/IP
- OTP verify: 10/min/IP
---
 
## 5. API Endpoints (Complete List)
 
### Auth
 
| Method | Path | Handler |
|--------|------|---------|
| POST | `/api/auth/login` | `TDSLoginView` |
| POST | `/api/auth/logout` | `logout_view` |
| GET | `/api/auth/me` | `me` |
| POST | `/api/auth/request-otp` | `request_otp` |
| POST | `/api/auth/verify-otp` | `verify_otp_and_change` |
| POST | `/api/auth/change-password` | `change_own_password` |
| POST | `/api/auth/device-verify` | `device_verify` |
| GET | `/api/auth/google/login/` | `google_login` (plain Django) |
| GET | `/api/auth/google/callback/` | `google_callback` (plain Django) |
| POST | `/api/auth/2fa/verify` | `verify_totp` ← actual path (NOT `/api/auth/totp/verify`) |
| POST | `/api/auth/2fa/enroll-confirm` | `confirm_totp_enrollment` ← actual path (NOT `/api/auth/totp/confirm`) |
| POST | `/api/auth/totp/enroll` | TOTP enrollment — backend implemented but NOT yet in urls.py (partially orphaned) |
 
### Health
 
| Method | Path | Handler |
|--------|------|---------|
| GET | `/api/health/` | `health_check` (AllowAny, always 200) |
 
Response: `{"status": "ok"|"degraded", "service": "...", "django": "<version>", "database": "ok"|"error: ..."}`. Always returns 200 so uptime monitors distinguish app-down from DB-problem.
 
### Users
 
| Method | Path | Handler |
|--------|------|---------|
| POST | `/api/users/setup` | `setup_first_user` (AllowAny, only if 0 users) |
| GET | `/api/users` | `list_users` (admin+user) |
| POST | `/api/users/` | `create_user` (IsAdmin) |
| GET | `/api/users/<id>` | `get_user` |
| PATCH | `/api/users/<id>/` | `update_user` (IsAdmin) |
 
### Master / Reference Data
 
| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/bootstrap` | One-shot: all dropdowns |
| GET | `/api/standards` | |
| GET | `/api/purposes` | |
| GET | `/api/belt-types` | |
| GET | `/api/brands` | |
| GET | `/api/fabric-types` | |
| GET | `/api/fabric-styles?fabric_type_id=` | |
| GET | `/api/cover-grades?standard_id=` | |
| GET | `/api/belt-ratings?fabric_type_id=` | |
| GET | `/api/reel-types` | |
| GET | `/api/packing-types` | |
| GET | `/api/container-types` | |
| GET | `/api/parameters?brand_id=` | Grouped by parameter_group |
| GET | `/api/customers?search=&limit=` | |
| POST | `/api/customers` | Create new |
| PATCH | `/api/customers/<id>` | IsEditor |
| GET | `/api/shipping-constraints?container_type_id=&region=` | Live from DB, never hardcoded |
 
### TDS (CRITICAL: lookup+batch URLs registered BEFORE tds_urls)
 
| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/tds/lookup` | EAV data preview + fabric style auto-select |
| GET | `/api/tds/dimensional-specs?standard_id=&belt_width_mm=` | Tolerance bands |
| POST | `/api/tds/batch/` | Create multiple TDS (shared fields + belt list) |
| POST | `/api/tds/batch/text-import/` | Bulk paste import |
| GET | `/api/tds/batch/` | List batches |
| GET | `/api/tds/batch/<id>` | Batch detail |
| GET | `/api/tds/batch/<id>/pdf` | Batch PDF (ZIP or merged) |
| GET | `/api/tds/batch/<id>/print-all/` | Merged PDF for printing (used by tds-multi-preview.html) |
| GET | `/api/tds/batch/<id>/download-zip/` | ZIP of individual PDFs |
| GET | `/api/tds` | List all TDS (TDSBriefOut) |
| POST | `/api/tds` | Create single TDS |
| GET | `/api/tds/<id>` | Get full TDSOut |
| PATCH | `/api/tds/<id>` | Update (draft only) |
| DELETE | `/api/tds/<id>` | Delete (admin/user) |
| POST | `/api/tds/<id>/approve` | Approve (admin) |
| POST | `/api/tds/<id>/decline` | Decline (admin) |
| GET | `/api/tds/<id>/pdf` | PDF generation (WeasyPrint) |
| POST | `/api/tds/<id>/packing` (no trailing slash) | Compute + save packing |
| PATCH | `/api/tds/<id>/packing/` (trailing slash) | Recompute packing |
| GET/POST | `/api/internal/send-daily-report/` | REPORT_CRON_SECRET protected |
 
---
 
## 6. Key Services & Formulas
 
### calculations.py
 
- **`belt_weight_per_metre(SG, width_mm, thickness_mm)`**: `SG × T × (W/1000)`
- **`belt_gross_weight_per_metre(...)`**: `SG × (T + 0.5) × (W/1000)` (+0.5 for packaging)
- **Reel diameter formulas** (3 variants: circular, twin, elliptical):
  - circular: `D = sqrt(4/π × d_m × L + k²)`
  - twin: `D = sqrt(4/π × d_m × L/2 + k²)` (belt split across 2 drums)
  - elliptical: `D = sqrt(4/π × d_m × L + (k + 2l/π)²) − 2l/π`
- **`parse_belt_rating(name)`**: regex `(\d+(?:\.\d+)?)\/(\d+)` → (kN float, plies int)
- **`auto_select_fabric_style(fabric_type_id, kn, plies)`**: per_ply = kn/plies; lowest style_name ≥ per_ply
- **`step_length_mm(kn, plies)`**: IS 14206 lookup table (9 entries, max 400 mm) — fallback used by `splicing_service.py`
- **`splice_length_mm(width, kn, plies, type, buffer)`**: `round(0.3×W + step×(N-1) + buffer)`
  - buffer: 50mm hot, 75mm cold (DB `SpliceMethodConfig` override, fallback hardcoded)
- **`total_extra_length_m(num_joints, splice_len_mm)`**: `joints × splice_len / 1000`
- **`get_sampling_count(belt_length_m)`**: IS 1891 table (DB first, hardcoded fallback)
- **`ENDLESS_MAX_BELT_LENGTH_M = 100`**: enforced both backend and frontend
- **Container constraints**: always from `get_container_constraints()` → DB, never hardcoded
### packing_service.py — `compute_packing()`
 
**`PackingResult` dataclass fields**: `num_rolls` (int), `length_per_roll_m`, `roll_height_m`, `roll_width_m`, `roll_dimensions` (str), `net_weight_kg`, `gross_weight_kg`, `gross_weight_per_roll_kg` (all float)
 
**Internal helpers**:
- `_round_up_half(v)`: `math.ceil(v * 2) / 2` — rounds up to nearest 0.5
- `_max_length_circular(max_D, k, d_m)`: `((max_D² - k²) × π) / (4 × d_m)`
- `_max_length_elliptical(max_D, k, l, d_m)`: adjusts via `offset = 2l/π`, then same formula on shifted D and k
**`compute_packing()` flow**:
1. Fetches `ReelType` from DB; raises `ValueError` if not found
2. Reads `k=core_diameter_m`, `l=center_to_center_m`, `max_D=max_roll_diameter_m`, `base_rolls=num_rolls_base`; `d_m = total_thickness_mm / 1000`
3. Calls `reel_diameter()` from calculations.py to get outer diameter `D`
4. If `D > max_D` → back-calculates:
   - circular: `L_max = _max_length_circular()`; `num_rolls = ceil(belt_length / L_max)`
   - twin: `L_per_single = _max_length_circular()`; `num_pairs = ceil(belt_length / (2×L_per_single))`; `num_rolls = num_pairs × 2` (always even)
   - elliptical: `L_max = _max_length_elliptical()`; same ceil pattern
   - `D` capped at `max_D` for dimension display
5. If `D ≤ max_D`: `num_rolls = base_rolls`; `L_per_roll = belt_length / base_rolls`
6. `roll_height_m = _round_up_half(D)`; `roll_width_m = _round_up_half((belt_width_mm + 100) / 1000)`
7. `roll_dimensions = f"H: {roll_height_m:.2f} m × W: {roll_width_m:.2f} m"`
8. `net_weight_kg = _round_up_half(belt_weight_per_m_kg × belt_length_m)`
9. Gross: `gross_per_m = belt_weight_per_m_kg × (total_thickness_mm + 0.5) / total_thickness_mm`; `gross_weight_kg = _round_up_half(gross_per_m × belt_length_m)`
10. `gross_weight_per_roll_kg = _round_up_half(gross_weight_kg / num_rolls)` (None if num_rolls=0)
**Note**: The gross formula scales existing net/m by `(T+0.5)/T` rather than re-multiplying by SG. Equivalent but avoids needing SG as a parameter.
 
### splicing_service.py — `compute_splicing()`
 
**`SplicingResult` frozen dataclass fields**: `step_length_mm` (int), `splice_length_mm` (int), `total_extra_length_m` (float)
 
**Internal helpers**:
- `_get_buffer_from_db(method)`: queries `SpliceMethodConfig`; fallback hot=50/cold=75mm
- `_get_step_from_db(fabric_rating)`: queries `SpliceStepLookup` table ascending; returns step for first row where `fabric_rating ≤ row.fabric_rating`; if fabric_rating exceeds all rows, returns step of the highest row; returns `None` if table is empty
**`compute_splicing(belt_rating_kn_m, num_plies, belt_width_mm, num_joints, vulcanization_method)` flow**:
1. `fabric_rating = belt_rating_kn_m / num_plies`
2. Tries `_get_step_from_db(fabric_rating)`; falls back to `_fallback_step()` from calculations.py if DB returns None
3. Gets buffer from `_get_buffer_from_db(vulcanization_method)`
4. `splice_len = round(0.3 × belt_width_mm + step × (num_plies - 1) + buffer)`
5. `total_extra = num_joints × splice_len / 1000`
6. Returns `SplicingResult(step_length_mm=step, splice_length_mm=splice_len, total_extra_length_m=total_extra)`
### exceptions.py — Custom DRF Exception Handler
 
Wired via `REST_FRAMEWORK['EXCEPTION_HANDLER']`. Two cases:
- **Unhandled exceptions** (response is None): logs full traceback with `logging.exception()`, returns `{'detail': 'An unexpected server error occurred.'}` with HTTP 500
- **Validation errors**: if `response.data` is a bare dict (not already `{'detail': ...}`), wraps it as `{'detail': response.data}` so frontend always sees a consistent error shape
### pdf_renderer.py
 
- **Template location**: `apps/services/templates/tds.html` (Jinja2)
- **Static assets**: `apps/services/static/indus_logo.png`, `tuv_logo.png` — embedded as base64 data URIs via `_logo_data_uri()`
- **`render_tds_html(doc, exclude_groups, exclude_gi_fields, show_test_method, show_reference)`**: renders Jinja2 template to HTML string; passes logos, `TDS_NOTES`, and exclusion sets
- **`render_tds_pdf(...)`**: lazy-imports WeasyPrint (`from weasyprint import HTML`), calls `render_tds_html()`, then `HTML(string=html_str, base_url=static_dir).write_pdf(optimize_images=True)`
- **History note**: template previously pointed at `tds_app/backend/` (now `backend_archived/`) — caused `TemplateNotFound` on every render; fixed by moving template+logos inside `apps/services/`
### apps/services/templates/tds.html — PDF Jinja2 Template
 
- A4 portrait, 8pt Inter font, `#F5A623` orange headers throughout
- Header row: 3-cell table — Indus logo | company info | TUV logo; inline SVG TUV fallback
- Two table layouts: 5-column for standard parameter groups (param name, test method, reference, spec value, Indus value); 2-column for Packing/Splicing groups
- `data-group` attribute on `.param-table` and `data-param-id` on `<tr>` — targeted by `injectCheckboxes()` in `tds-preview.html`
- `exclude_groups` and `exclude_gi_fields` are Jinja2 sets passed from `render_tds_html()`
- Signature block: Prepared By + Customer Acceptance; `page-break-inside: avoid`
### tds_number.py
 
- `next_tds_number()`: `SELECT FOR UPDATE` on `TDSSequence(year=0)`, increments, returns `f"{n:04d}"`
- Must be called inside `transaction.atomic()`
- Creates the sentinel row if it doesn't exist
### sections.py
 
- `PARAMETER_GROUP_ORDER`: 18 groups in exact PDF rendering order
- `CUSTOMER_COPY_EXCLUDE_GROUPS`: 4 groups hidden on customer copy
**CRITICAL — 4 places that must stay in sync for customer-copy exclusions**:
1. `apps/services/sections.py` → `CUSTOMER_COPY_EXCLUDE_GROUPS`
2. `frontend/js/search-tds.js` → local constant
3. `frontend/tds-preview.html` → local constant
4. `frontend/tds-multi-preview.html` → local constant
Groups: `['Fabric Parameters', 'Sampling and Testing', 'Packing and Logistics', 'Splicing Parameters']`
 
### pdf_service.py — `build_tds_doc_data(tds_id, ...)`
 
EAV assembly priority:
1. `CoverGradeValue` (rubber compound specs)
2. `FabricTypeParameterValue` if not already set
3. `FabricStyleParameterValue` overrides (step 3)
4. `BeltRatingValue` (except param_id=4, excluded)
Key computed fields in the doc:
- Belt Breaking Strength: Weft % = `(weft_kn/warp_kn) × 100`
- Elastic Modulus rounded to 2dp
- Hot splice curing: `HotSpliceCuringLookup.filter(thickness_mm__gte=...).first()` — uses highest tier if all below
- `_DIRECT_MAP`: dict of parameter_name → lambda(tds) for non-EAV parameters (dimensional, construction, packing, etc.)
### otp_service.py
 
- TTL: 10 minutes; Max attempts: 5
- `generate_otp`: `secrets.randbelow(1_000_000)` zero-padded to 6 digits, bcrypt rounds=10
- `verify_otp`: increments attempts BEFORE checking (timing oracle prevention); deletes OTPCode row on success
### device_service.py
 
- Token: `secrets.token_hex(32)` = 64-char hex
- Cookie: `tds_device`, 1-year max_age, httpOnly, SameSite=Lax
- `is_trusted_device`: reads cookie, DB lookup, bumps `last_used_at`
- On new login: notifies user + all active admin emails (fail_silently)
### totp_service.py
 
- `PreAuthToken`: 5-min lifetime, `token_type='pre_auth'`
- `EnrollToken`: 10-min lifetime, `token_type='enroll'`
- ISSUER: `'Ravasco TDS'`
- `make_full_jwt(user)`: Returns `{access_token, refresh, user_id, role, full_name, email}`
---
 
## 7. Management Commands
 
All run via `python run_django.py <command>` locally or `python manage.py <command>` with settings.
 
### `seed_dimensional_specs`
 
Seeds `dimensional_parameter_specs` tolerance data for parameter IDs 1 (belt_width), 2 (top_cover), 3 (bottom_cover), 4 (carcass), 6 (total_thickness) across 8 standard classifications: IS1891, ISO14890, DIN22102, ARPM, ASTMD378, AS1332, SANS1173, INHOUSE.
 
```
python run_django.py seed_dimensional_specs          # dry run (safe default)
python run_django.py seed_dimensional_specs --replace  # overwrite existing rows
```
 
Uses raw SQL with `ON CONFLICT DO NOTHING` or `DO UPDATE` depending on `--replace`.
 
### `wipe_test_tds_data`
 
Deletes every `TDSInput` + `TDSBatch` row. Does NOT touch any master/reference data (standards, customers, users, etc.). Nothing else has FK pointing at these tables so no cascade issues.
 
```
python run_django.py wipe_test_tds_data                          # dry run (safe default)
python run_django.py wipe_test_tds_data --confirm                # actually delete
python run_django.py wipe_test_tds_data --confirm --reset-sequence  # also reset TDS counter to 0
```
 
`--reset-sequence` sets `TDSSequence(year=0).last_number = 0` so the next TDS will be `'0001'`.
 
### `send_daily_tds_report`
 
CLI wrapper for `send_daily_tds_report()` from `tds_report_service.py`. The same function is also called by `GET /api/internal/send-daily-report/` endpoint (REPORT_CRON_SECRET protected), so both paths produce identical output.
 
```
python run_django.py send_daily_tds_report
python run_django.py send_daily_tds_report --date 2026-08-07   # backfill a specific day
```
 
Emails every active admin (role='admin') a daily TDS activity summary.
 
### `check_cover_grade_usage`
 
Read-only diagnostic. Checks whether a given cover grade is referenced by any `TDSInput` row (which would block deletion due to `PROTECT`) vs. only by `CoverGradeValue` (which would cascade on delete).
 
Default checks: grade_code containing 'HAR' under DIN, and 'F' under SANS. No writes, always safe to run.
 
---
 
## 8. Frontend Architecture
 
### Module Structure
 
All pages use ES modules (`<script type="module">`). No framework, no bundler.
 
```
auth.js         — getSession/setSession, requireAuth, login, verifyDevice, logout,
                  populateNavUser, openChangePasswordModal, showToast, getAuthHeaders()
api.js          — apiFetch wrapper, all typed API helpers (getBootstrap, createTDS,
                  listTDS, getTDS, downloadPdf, getShippingConstraints, etc.)
generate-tds.js — TDS form (~2089 lines); single-belt + multi-belt queue
search-tds.js   — Search/list/filter/modal/delete
```
 
### Session Storage
 
- `SESSION_KEY = 'tds_session'` in `sessionStorage` (NOT `localStorage['access']`)
- Contains: `{token, user_id, role, full_name, email}`
- `getAuthHeaders()`: returns `{Authorization: 'Bearer <token>'}` or `{}` (httpOnly cookie is automatic)
- `requireAuth()`: checks `s?.user_id || s?.token`
### generate-tds.js Key Behaviors
 
**Searchable dropdowns**: All 16 `<select>` elements wrapped by `makeSearchable()` — renders a live-filter text input, appends list to `<body>` to escape `overflow:hidden`, syncs via `_searchableSyncs[id]()`
 
**EAV cascade**:
```
brand → standards → cover grades (per standard)
fabric type → belt ratings + fabric styles (parallel Promise.all)
cover grade + belt rating → tdsLookup() → EAV data (plies, carcass, skim, sg)
                                        → autoSelectFabricStyle() (trusts server answer)
                                        → recalcTotal() → recalcWeight() → recalcPacking()
```
 
**Belt description auto-assembly**: `{width} × {fabric} × {rating} × {top} × {bottom} × {grade} × {edge} × {construction type} {belt type} [× BOT-N X BOB-M]`
 
**Weight formulas** (client-side mirror of backend):
- Net/m: `SG × T × (W/1000)` rounded up to nearest 0.5
- Gross/m: `SG × (T+0.5) × (W/1000)` rounded up to nearest 0.5
**Packing preview**: mirrors `packing_service.py`; handles circular/twin/elliptical reel formulas; back-calculates `num_rolls` when `D > maxD`; twin reels always multiples of 2; international: caps by container height + weight limits from live DB fetch
 
**Splicing preview**: mirrors backend IS 14206 formula exactly; uses `Math.round()` (not roundUpHalf), matching Python's `round()` for splice_length_mm and total_extra_length_m
 
**Multi-belt queue**: `beltQueue[]` stores captured belt specs; shared customer/document fields sent separately; loops `createTDS()` per belt; navigates to `tds-multi-preview.html`
 
**Manual packing override**: `#packing-override-fields` hidden by default; pre-fills from auto-computed values when opened; override fields take priority in payload
 
**Submission flow** (submitTDS):
1. `validateForm()` — 11 required checks
2. Create/patch customer if needed
3. Build TDS payload (matches TDSCreateIn schema)
4. POST `/api/tds` → on success: navigate to `tds-preview.html` (or `tds-multi-preview.html` for queue)
5. Draft mode: stays on form, shows toast
### search-tds.js Key Behaviors
 
- All records loaded once from `listTDS()`, filtered client-side (no per-filter API calls)
- Filters: text search (tds_number + customer + standard + rating), standard_id, date range
- Table: 7 columns; "View" → modal (always fetches full `getTDS(id)`, not brief cache); "⬇ PDF" → `downloadPdf()` with Customer/Internal Copy toggle
- Detail modal: 4 tabs (Overview, Belt Specs, Packing & Logistics, Splicing Parameters); all fields populated from one `getTDS()` call
- Delete: confirm overlay → `deleteTDS()` → reload records
- `CUSTOMER_COPY_EXCLUDE_GROUPS`: mirrors sections.py and tds-preview.html — keep all 4 in sync
### tds-preview.html Key Behaviors
 
- Loads PDF in an `<iframe>` via `/api/tds/{id}/pdf?format=html` (not a separate layout)
- `injectCheckboxes()`: after iframe loads, injects checkboxes into the iframe's DOM for per-group and per-row exclusion
- `snapshotExclusions()` / `restoreExclusions()`: preserve checkbox state across iframe refreshes
- `getOptsFromIframe()`: reads checkbox state back out to build `excludeGroups`/`excludeParams`/`excludeGiFields` arrays for final PDF URL
### tds-multi-preview.html Key Behaviors
 
- Reachable after multi-belt queue submission; URL param: `?batch_id=<id>`
- Fetches `GET /api/tds/batch/{id}/` and renders: summary chips (total belts, date, created-by) + 8-column records table
- Three actions:
  - **Print All Merged**: navigates to `/api/tds/batch/{id}/print-all/?copy=<internal|customer>`
  - **Print All Separate Tabs**: opens one `window.open(tds-preview.html?tds_id=...)` per row in the click handler synchronously (avoids popup blocker)
  - **Download ZIP**: navigates to `/api/tds/batch/{id}/download-zip/?copy=<internal|customer>`
- Preview link per row → `tds-preview.html?tds_id=&tds_number=` (reuses full single-belt preview)
- Uses `getAuthHeaders()` from `auth.js` (NOT a local `getToken()`)
- **Historical bug fixed**: old local `getToken()` read `localStorage/sessionStorage['access']` — a key never set by the app. Auth.js stores the session as a JSON object at `sessionStorage['tds_session']`. Fixed by importing `getAuthHeaders()` from `auth.js`.
- `CUSTOMER_COPY_EXCLUDE_GROUPS` defined locally — must stay in sync with sections.py, search-tds.js, tds-preview.html
### packing-calculator.html Key Behaviors
 
- Standalone calculator linked from home dashboard "Packing Calculator" quick-action card
- **No TDS creation** — purely a calculation tool
- Reads reel types + container types from `GET /api/bootstrap` on load
- Fetches live shipping constraints from `GET /api/shipping-constraints?container_type_id=&region=` when region/container selection changes
- Mirrors `packing_service.py` formulas: `reelDiam()` (circular/twin/elliptical), net weight = `SG × T × (W/1000)`, gross = `SG × (T+0.5) × (W/1000)`
- **Sync requirement**: if packing math changes, must update here AND `generate-tds.js`'s `recalcPacking()`
### splicing-calculator.html Key Behaviors
 
- Standalone calculator linked from home dashboard "Splicing Calculator" quick-action card
- **Fully client-side — no API calls at all** (unlike packing-calculator.html which fetches bootstrap)
- Computes per IS 14206 Part I : 1995
- Inputs: Belt Rating (kN/m), Number of Plies, Belt Width (mm), Number of Joints, Vulcanisation Method (hot/cold toggle)
- `STEP_TABLE` hardcoded in page: `[[100,150],[125,200],[160,200],[200,250],[250,300],[300,350],[315,350],[350,400],[400,400]]`
- Formula: `spliceLen = Math.round(0.3 × width + step × (plies-1) + buffer)`; buffer = 50 (hot) / 75 (cold)
- Shows derived values (rating-per-ply, step length, buffer), splice length highlight, total extra belt length, and step lookup table with the active row highlighted in gold
- Joints=0 hides the total extra row
- Uses `requireAuth()` from auth.js — redirects unauthenticated users
- **Sync requirement**: if splicing formula, buffer values, or STEP_TABLE thresholds change, must update here, `js/generate-tds.js` (`getSpliceStep()/recalcSplicing()`), AND `splicing_service.py` together
---
 
## 9. Config & Settings Highlights
 
| Setting | Value/Behaviour |
|---------|----------------|
| `DATABASES` | PostgreSQL (DATABASE_URL from env) |
| `APPEND_SLASH` | `False` |
| `TIME_ZONE` | `'Asia/Kolkata'` |
| `CORS_ALLOW_CREDENTIALS` | `True` (for httpOnly cookies) |
| `CORS_ALLOWED_ORIGINS` | Dev: localhost; Prod: Render frontend URL |
| `MIDDLEWARE` | AdminOnlyCsrfMiddleware + WhiteNoise + Django standard |
| `AdminOnlyCsrfMiddleware` | CSRF only on `/internal-mgmt-rvsc/` (admin panel) |
| `STATICFILES_DIRS` | `[BASE_DIR.parent / 'frontend']` |
| `AUTH_USER_MODEL` | Not set — TDSUser is NOT AbstractBaseUser |
| `REST_FRAMEWORK` | `DEFAULT_AUTHENTICATION_CLASSES`: TDSCookieJWTAuthentication; `DEFAULT_PERMISSION_CLASSES`: IsAuthenticated; `EXCEPTION_HANDLER`: custom handler in `exceptions.py` |
| `SIMPLE_JWT` | Access token 12hr, Refresh 7 days |
| `THROTTLE_RATES` | login=5/min, otp_request=3/min, otp_verify=10/min |
| `REPORT_CRON_SECRET` | hmac.compare_digest() protected daily-report endpoint |
| `OAUTHLIB_RELAX_TOKEN_SCOPE` | `'1'` (handles Google's full-URI scopes) |
 
### Root URL Conf (config/urls.py)
 
```
/api/                → apps.api.urls
/internal-mgmt-rvsc/ → Django admin (CSRF-protected)
/                    → catch-all → WhiteNoise frontend (index.html)
```
 
### API URL Registration Order (apps/api/urls.py) — CRITICAL
 
```python
include(batch_urls)   # /api/tds/batch/...  — MUST be before tds_urls
include(lookup_urls)  # /api/tds/lookup, /api/tds/dimensional-specs — MUST be before tds_urls
include(tds_urls)     # /api/tds/<int:tds_id> — would swallow "batch" and "lookup" if first
```
 
---
 
## 10. Known Issues & Gaps
 
| Issue | Details |
|-------|---------|
| "Remember me" security | Stores plaintext email+password in `localStorage` under `tds_saved_credentials` — flagged in `index.html` comments as known issue. |
| TOTP enrollment not wired | TOTP backend fully implemented (verify + enroll-confirm in `totp_urls.py`); enrollment initiation endpoint (`totp/enroll`) implemented in `totp_views.py` but NOT registered in `totp_urls.py` — half-orphaned. |
| `passlib` incompatibility | Passlib 1.7.4 incompatible with bcrypt ≥ 4.0; fixed by calling `bcrypt.checkpw()` directly in `auth_backend.py`. |
 
---
 
## 11. Deployment (Render.com)
 
### `start.py` / `build.sh` sequence
 
```
1. pip install -r requirements.txt
2. python manage.py collectstatic --noinput
3. python manage.py migrate
4. gunicorn config.wsgi:application
```
 
### Environment Variables Required
 
```
DATABASE_URL, SECRET_KEY, DJANGO_SETTINGS_MODULE,
TDS_COOKIE_NAME, TDS_COOKIE_MAX_AGE, TDS_COOKIE_SECURE, TDS_COOKIE_SAMESITE,
TDS_DEVICE_COOKIE_SECURE,
GOOGLE_CLIENT_SECRETS_JSON (or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET),
GOOGLE_REDIRECT_URI,
EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL,
REPORT_CRON_SECRET,
ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS (or CORS_ALLOW_ALL_ORIGINS=True for dev)
```
 
---
 
## 12. Data Flow Summary — Creating a TDS
 
```
Frontend (generate-tds.js)
  1. loadAllDropdowns() → GET /api/bootstrap → one DB round-trip for all dropdowns
  2. User selects: brand → standard → cover grade + fabric type → belt rating
  3. runLookup() → POST /api/tds/lookup → EAV assembly server-side
       → returns: plies, carcass, skim, SG, fabric_style (auto-selected)
  4. Client computes weight/packing/splicing (mirrors backend formulas exactly)
  5. User fills: width, length, covers, customer, packing, etc.
  6. validateForm() → submitTDS('preview')
  7. Optional: POST /api/customers (new) or PATCH /api/customers/{id} (update contact info)
  8. POST /api/tds → TDSCreateIn schema
       Backend:
         a. tds_number = next_tds_number() inside transaction.atomic()
         b. Server computes: total_thickness_mm, belt_weight_per_m_kg, belt_gross_weight_per_m_kg
         c. If packing fields not supplied: compute_packing() (packing_service.py)
         d. If splicing_required: compute_splicing() (splicing_service.py) for step/splice/total lengths
         e. interply_skim_mm fetched from BeltRatingValue (param_id=5)
         f. record.save()
       Returns: TDSOut with all computed fields
  9. Navigate → tds-preview.html?tds_id=X&tds_number=0042&splicing=true
       → GET /api/tds/{id}/pdf?format=html → WeasyPrint renders tds.html via pdf_renderer.py
       → injectCheckboxes() → user can hide rows/groups
     10. User downloads → GET /api/tds/{id}/pdf?exclude_groups=...&exclude_params=...
           → pdf_renderer.render_tds_pdf() → WeasyPrint → inline attachment in response
```
 
---
 
## 13. Quick-Reference: Where Things Live
 
| I need to... | Go to... |
|--------------|---------|
| Change how TDS numbers are generated | `services/tds_number.py` |
| Change belt math (weight, reel, splice) | `services/calculations.py` AND `frontend/js/generate-tds.js` (keep in sync) |
| Change packing computation | `services/packing_service.py` AND `frontend/js/generate-tds.js` → `recalcPacking()` AND `frontend/packing-calculator.html` |
| Change splicing computation | `services/splicing_service.py` AND `services/calculations.py` (fallback table) AND `frontend/js/generate-tds.js` → `recalcSplicing()` AND `frontend/splicing-calculator.html` |
| Change PDF layout/template | `services/pdf_renderer.py` + `services/templates/tds.html` |
| Change PDF data assembly (EAV) | `services/pdf_service.py` → `build_tds_doc_data()` |
| Change which groups appear on customer PDFs | `services/sections.py` AND `frontend/js/search-tds.js` AND `frontend/tds-preview.html` AND `frontend/tds-multi-preview.html` |
| Add a new API endpoint | Add view + url file + include in `apps/api/urls.py` |
| Change error response format | `apps/api/exceptions.py` → custom DRF exception handler |
| Change user roles | `apps/core/models.py` (TDSUser) + `apps/api/permissions.py` + `frontend/admin.html` |
| Change OTP TTL or attempt limit | `services/otp_service.py` → `_OTP_TTL_MINUTES`, `_MAX_ATTEMPTS` |
| Change trusted device token length | `services/device_service.py` → `secrets.token_hex(32)` |
| Change JWT cookie settings | `settings.py` → `TDS_COOKIE_*` env vars |
| Change throttling rates | `settings.py` → `REST_FRAMEWORK.DEFAULT_THROTTLE_RATES` |
| Change the daily report schedule | External scheduler hitting `/api/internal/send-daily-report/` |
| Wipe test data locally | `python run_django.py wipe_test_tds_data --confirm [--reset-sequence]` |
| Seed dimensional tolerance data | `python run_django.py seed_dimensional_specs [--replace]` |
| Check if a cover grade is safe to delete | `python run_django.py check_cover_grade_usage` |
| Add a new master data type | Add unmanaged model to `models.py` + admin registration + endpoint in `master_views.py` + `master_urls.py` |
