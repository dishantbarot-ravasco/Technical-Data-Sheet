/**
 * api.js - Central API layer for the INDUS TDS Automation App.
 *
 * This module is the single source of truth for all HTTP calls to the Django backend.
 * Every page imports from here instead of calling `fetch()` directly, so that:
 *   - Auth headers are always included automatically.
 *   - Error handling is consistent (non-2xx responses throw a real Error).
 *   - The server URL is configured in one place (API_BASE).
 *
 * Exports:
 *   apiFetch          - low-level authenticated fetch wrapper (used internally)
 *   getBootstrap      - load all master-data dropdowns in one call
 *   getStandards / getPurposes / getBeltTypes / getBrands / getFabricTypes
 *   getReelTypes / getPackingTypes / getCoverGrades / getFabricStyles / getBeltRatings
 *   resolveBeltRatings
 *   getCustomers / createCustomer / updateCustomer
 *   tdsLookup / getDimensionalSpecs
 *   listTDS / getTDS / createTDS / createBatch / approveTDS / declineTDS / deleteTDS
 *   getParameters
 *   listUsers / createUser / updateUser
 *   getUserSignatureUrl / uploadUserSignature / deleteUserSignature
 *   requestOTP / verifyOTP / changePassword
 *   getPdfUrl / downloadPdf
 *
 * Imports:
 *   getAuthHeaders (from ./auth.js) - supplies the Bearer JWT token on every request.
 */

import { getAuthHeaders } from './auth.js';

// Relative URL - works because the frontend is served by Django at the same origin.
// Change to 'http://localhost:8000/api' only if running the frontend as a separate dev server.
const API_BASE = '/api';

/**
 * Core authenticated fetch wrapper used by all API functions in this module.
 *
 * It:
 *  1. Prepends API_BASE to the path so callers only write e.g. '/tds'.
 *  2. Injects the JWT Authorization header from sessionStorage via getAuthHeaders().
 *  3. Merges any extra headers/options supplied by the caller.
 *  4. Throws a descriptive Error for any non-2xx HTTP response.
 *  5. Returns null for 204 No Content (delete/patch with no body).
 *  6. Otherwise parses and returns the JSON response body.
 *
 * @param {string} path    - API path starting with '/', e.g. '/tds/42'
 * @param {RequestInit} [options={}] - Optional fetch options (method, body, headers, etc.)
 * @returns {Promise<any>} Parsed JSON response, or null for 204 responses.
 * @throws {Error} If the server returns a non-2xx status code.
 */
let _refreshInFlight = null;

/**
 * Silently exchange the httpOnly tds_refresh cookie for a fresh tds_access
 * cookie. Coalesced into a single in-flight request so a burst of parallel
 * apiFetch() calls that all hit a stale access token don't each fire their
 * own refresh — they share one outcome.
 * @returns {Promise<boolean>} true if refresh succeeded
 */
function _refreshAccessToken() {
  if (!_refreshInFlight) {
    _refreshInFlight = fetch(`${API_BASE}/auth/token/refresh`, {
      method:      'POST',
      credentials: 'include',
      headers:     { 'Content-Type': 'application/json' },
      body:        '{}',
    }).then(r => r.ok).catch(() => false)
      .finally(() => { _refreshInFlight = null; });
  }
  return _refreshInFlight;
}

async function apiFetch(path, options = {}, _retried = false) {
  const url = `${API_BASE}${path}`;

  // Build default headers: JSON content type + JWT Bearer token.
  // Caller-supplied headers are merged last so they can override if needed.
  const defaults = {
    headers: {
      'Content-Type': 'application/json',
      ...getAuthHeaders(),       // inject Bearer token on every request
      ...(options.headers || {}),
    },
  };

  // Spread options so callers can pass method, body, signal, etc.
  const res = await fetch(url, { ...defaults, ...options, credentials: 'include', headers: { ...defaults.headers, ...(options.headers || {}) } });

  // A previously-valid session's access token can expire mid-use (12h
  // lifetime) without the tab ever having been closed. Rather than surface
  // that as a hard "session expired" error, try the cookie-backed refresh
  // once and silently replay the request — the user never notices, as long
  // as the 30-day tds_refresh cookie is still valid. Only retried once, so
  // a genuinely dead session still fails through to the normal error path.
  if (res.status === 401 && !_retried) {
    const refreshed = await _refreshAccessToken();
    if (refreshed) return apiFetch(path, options, true);
  }

  if (!res.ok) {
    // Try to extract a human-readable message from the Django/DRF error body.
    // DRF typically returns { "detail": "some message" } on errors.
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail && typeof body.detail === 'object') {
        // Serializer-validation shape: { detail: { field: [msg, ...], ... } }
        // Flatten into readable text instead of a raw JSON blob in the toast.
        msg = Object.entries(body.detail)
          .map(([field, errs]) => `${field}: ${Array.isArray(errs) ? errs.join(' ') : errs}`)
          .join('; ');
      } else {
        msg = body.detail || JSON.stringify(body);
      }
    } catch (_) {}
    throw new Error(msg);
  }

  // 204 No Content - server processed the request but returned nothing (e.g. DELETE).
  if (res.status === 204) return null;

  return res.json();
}

/* ══════════════════════════════════════════════════════════
   SECTION: Master / Lookup Data
   These endpoints return reference tables used to populate <select> dropdowns.
   All are loaded during page init via getBootstrap() or individually as needed.
══════════════════════════════════════════════════════════ */

/**
 * Load all master-data tables in one request (bootstrap endpoint).
 * Returns an object with: customers, purposes, belt_types, brands,
 * standards, fabric_types, reel_types, packing_types, container_types.
 * Use this on form pages to fill all dropdowns with a single API call.
 * @returns {Promise<Object>}
 */
export const getBootstrap    = () => apiFetch('/bootstrap');

/** Fetch all available testing standards (e.g. IS 1891, DIN 22102). @returns {Promise<Array>} */
export const getStandards    = () => apiFetch('/standards');

/** Fetch all TDS purpose options (e.g. Domestic, International). @returns {Promise<Array>} */
export const getPurposes     = () => apiFetch('/purposes');

/** Fetch all belt type options (e.g. Flat Belt, Pipe Belt). @returns {Promise<Array>} */
export const getBeltTypes    = () => apiFetch('/belt-types');

/** Fetch all INDUS belt brands. @returns {Promise<Array>} */
export const getBrands       = () => apiFetch('/brands');

/** Fetch all fabric types (e.g. EP = Polyester-Nylon, NN = Nylon-Nylon). @returns {Promise<Array>} */
export const getFabricTypes  = () => apiFetch('/fabric-types');

/** Fetch all reel types (circular, twin, elliptical). @returns {Promise<Array>} */
export const getReelTypes    = () => apiFetch('/reel-types');

/**
 * Fetch packing types that are currently available (e.g. wooden crate, steel band).
 * The `available_only=true` query param filters out discontinued types.
 * @returns {Promise<Array>}
 */
export const getPackingTypes = () => apiFetch('/packing-types?available_only=true');

/** Fetch all shipping container types (e.g. 20ft, 40ft, 40ft High Cube). @returns {Promise<Array>} */
export const getContainerTypes = () => apiFetch('/container-types');

/**
 * Fetch the live max height/width/gross-weight limits for one container type +
 * shipping region, straight from the database — never hardcode these values on
 * the frontend, since an admin can change them without touching this code.
 * @param {number} containerTypeId
 * @param {string} region - e.g. 'USA' or 'Rest of World'
 * @returns {Promise<{max_height_m:number, max_width_m:number, max_gross_weight_kg:number}>}
 */
export const getShippingConstraints = (containerTypeId, region) =>
  apiFetch(`/shipping-constraints?container_type_id=${encodeURIComponent(containerTypeId)}&region=${encodeURIComponent(region)}`);

/**
 * Fetch cover grades for a specific testing standard.
 * Cover grade determines the rubber compound (e.g. M = general purpose, H = heat resistant).
 * Must be re-fetched whenever the user changes the Standard dropdown.
 * @param {number} standardId - ID of the standard (e.g. IS 1891)
 * @returns {Promise<Array>} Array of { id, grade_code, description } objects
 */
export const getCoverGrades = (standardId) =>
  apiFetch(`/standards/${standardId}/cover-grades`);

/**
 * Fetch fabric styles for a specific fabric type.
 * A fabric style describes the weave pattern and strength per ply
 * (e.g. "EP 160/3" = Polyester-Nylon, 160 kN/m total for 3 plies = 53.3 kN/ply).
 * Must be re-fetched whenever the user changes the Fabric Type dropdown.
 * @param {number} fabricTypeId - ID of the fabric type (e.g. EP)
 * @returns {Promise<Array>}
 */
export const getFabricStyles = (fabricTypeId) =>
  apiFetch(`/fabric-types/${fabricTypeId}/styles`);

/**
 * Fetch belt ratings for a specific fabric type.
 * A belt rating combines the total tensile strength (kN/m) and number of plies.
 * Example: "EP 630/4" = EP fabric, 630 kN/m, 4 plies → 157.5 kN/m per ply.
 * Must be re-fetched whenever the user changes the Fabric Type dropdown.
 * @param {number} fabricTypeId - ID of the fabric type
 * @returns {Promise<Array>}
 */
export const getBeltRatings = (fabricTypeId) =>
  apiFetch(`/fabric-types/${fabricTypeId}/belt-ratings`);

/**
 * Find every belt rating across ALL fabric types matching a bare
 * "<kN>/<plies>" number (e.g. "1000/5") -- used by the belt-description
 * paste-parser (liveParseBeltDescription() in generate-tds.js) to figure
 * out which Fabric Type a pasted rating belongs to when Fabric Type hasn't
 * been selected yet (the displayed/pasted rating text no longer carries its
 * own fabric-code prefix -- see stripFabricPrefix()).
 * @param {string} ratingText - Bare rating, e.g. "1000/5"
 * @returns {Promise<Array>} [{id, fabric_type_id, rating_name}, ...]
 */
export const resolveBeltRatings = (ratingText) =>
  apiFetch(`/belt-ratings/resolve?rating=${encodeURIComponent(ratingText)}`);

/* ══════════════════════════════════════════════════════════
   SECTION: Customers
══════════════════════════════════════════════════════════ */

/**
 * Fetch all customers for the autocomplete search on the TDS form.
 * @returns {Promise<Array>} Array of customer objects
 */
export const getCustomers = () => apiFetch('/customers');

/**
 * Search customers by name on the server (used by the TDS form autocomplete).
 * Unlike getCustomers(), this isn't limited to the first 100 alphabetically -
 * it queries the full table with an icontains match on customer_name.
 * @param {string} query - Search text (matched anywhere in the customer name)
 * @param {number} [limit=20] - Max results to return
 * @returns {Promise<Array>}
 */
export const searchCustomers = (query, limit = 20) =>
  apiFetch(`/customers?search=${encodeURIComponent(query)}&limit=${limit}`);

/**
 * Create a new customer record in the database.
 * @param {Object} payload - { customer_name, contact_person, application, plant_location }
 * @returns {Promise<Object>} The newly created customer, including its customer_id
 */
export const createCustomer = (payload) =>
  apiFetch('/customers', { method: 'POST', body: JSON.stringify(payload) });

/**
 * Update an existing customer's details (contact, application, location).
 * Uses PATCH so only the supplied fields are changed - others are untouched.
 * @param {number} id      - The customer's ID
 * @param {Object} payload - Partial customer object with fields to update
 * @returns {Promise<Object>} The updated customer record
 */
export const updateCustomer = (id, payload) =>
  apiFetch(`/customers/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

/* ══════════════════════════════════════════════════════════
   SECTION: EAV Lookup and Dimensional Specs
   EAV = Entity-Attribute-Value - the database stores belt parameter
   values keyed by (standard_id, cover_grade_id, belt_rating_id).
   The lookup endpoint resolves these to actual spec values (plies,
   carcass thickness, skim thickness, specific gravity, etc.).
══════════════════════════════════════════════════════════ */

/**
 * Perform an EAV lookup to retrieve belt construction parameters
 * for a given combination of standard + cover grade + belt rating.
 * Returns data such as: num_plies, carcass_thickness_mm, interply_skim_mm,
 * and specific_gravity (used for weight calculations).
 *
 * This is called automatically whenever the user selects all three dropdowns.
 *
 * @param {Object} payload - { standard_id, cover_grade_id, belt_rating_id }
 * @returns {Promise<Object>} Lookup result with belt_rating and cover_grade sub-objects
 */
export const tdsLookup = (payload) =>
  apiFetch('/tds/lookup', { method: 'POST', body: JSON.stringify(payload) });

/**
 * Fetch dimensional specification values (tolerance ranges) for a belt
 * given its standard, width, cover thicknesses, and total thickness.
 * These values populate the "Dimensional Parameters" group on the TDS PDF.
 *
 * @param {number} standardId - The testing standard ID
 * @param {Object} params     - Optional filters: belt_width_mm, top_cover_mm,
 *                              bottom_cover_mm, carcass_thickness_mm, total_thickness_mm
 * @returns {Promise<Object>} Map of parameter_id → { parameter_name, spec_value }
 */
export const getDimensionalSpecs = (standardId, params) => {
  // Build query string - only include params that have a defined value
  const qs = new URLSearchParams({ standard_id: standardId });
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined) qs.append(k, v); });
  return apiFetch(`/tds/dimensional-specs?${qs}`);
};

/* ══════════════════════════════════════════════════════════
   SECTION: TDS CRUD
   TDS = Technical Data Sheet - the main document produced by this app.
══════════════════════════════════════════════════════════ */

/**
 * List all TDS records, with optional server-side filtering.
 * @param {Object} [params={}] - Query params: standard_id, from_date, to_date, etc.
 * @returns {Promise<Array>} Array of TDS summary objects
 */
export const listTDS = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/tds/${qs ? '?' + qs : ''}`);
};

/**
 * Fetch full details of a single TDS by its database ID.
 * @param {number} id - The TDS database ID (tds_id)
 * @returns {Promise<Object>} Full TDS record including nested relations
 */
export const getTDS = (id) => apiFetch(`/tds/${id}`);

/**
 * List past revisions for a TDS (lightweight — no snapshot data).
 * @param {number} id - The TDS database ID (tds_id)
 * @returns {Promise<Array>} [{revision_number, edited_by, edited_at, change_summary}, ...]
 */
export const getTdsRevisions = (id) => apiFetch(`/tds/${id}/revisions`);

/**
 * Get one past revision's full field snapshot.
 * @param {number} id  - The TDS database ID (tds_id)
 * @param {number} rev - The revision number
 * @returns {Promise<Object>} {revision_number, edited_by, edited_at, change_summary, snapshot}
 */
export const getTdsRevisionDetail = (id, rev) => apiFetch(`/tds/${id}/revisions/${rev}`);

/**
 * Download the spec-sheet PDF for one past revision of a TDS.
 * Mirrors downloadPdf() above but hits the revision-specific endpoint,
 * which overlays that revision's snapshot onto the current record before
 * rendering (see revisions_views.generate_revision_pdf) - the resulting
 * PDF carries a "HISTORICAL REVISION" header banner.
 *
 * @param {number} id           - The TDS database ID (tds_id)
 * @param {number} revisionNum  - The revision number to download
 * @param {string} tdsNumber    - Used for the downloaded filename
 * @param {Object} [opts={}]    - { excludeGroups?: string[] }
 * @returns {Promise<void>}
 * @throws {Error} If the server returns a non-2xx status
 */
export async function downloadRevisionPdf(id, revisionNum, tdsNumber, opts = {}) {
  const qs = new URLSearchParams();
  if (opts.excludeGroups?.length) {
    opts.excludeGroups.forEach(g => qs.append('exclude_groups', g));
  }
  const query = qs.toString();
  const res = await fetch(`${API_BASE}/tds/${id}/revisions/${revisionNum}/pdf${query ? '?' + query : ''}`, { headers: getAuthHeaders() });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Revision PDF export failed: HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const url  = URL.createObjectURL(blob);

  // BUG FIX: this used to always build its own filename client-side,
  // ignoring whatever the server actually named it (revisions_views.py::
  // generate_revision_pdf) -- the two had already drifted out of sync once
  // (this used "-revNN", the server used "_rev_NN") and would again the next
  // time either changed independently. Same fix as downloadPdf() above:
  // trust the server's Content-Disposition, and use this template only if
  // the header is somehow missing. revision_number 0 (the original,
  // pre-first-edit snapshot) has no suffix, matching
  // pdf_service.revision_pdf_filename()'s same rule.
  const cd = res.headers.get('Content-Disposition') || '';
  const match = /filename="?([^";]+)"?/i.exec(cd);
  const revNum = Number(revisionNum);
  const fallbackName = revNum === 0
    ? `TDS-${tdsNumber}.pdf`
    : `TDS-${tdsNumber}_rev_${String(revNum).padStart(2, '0')}.pdf`;
  const filename = match ? match[1] : fallbackName;

  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/**
 * Create a new TDS record from the form payload.
 * The server also computes derived values (total_thickness, splice lengths, etc.)
 * and assigns a sequential TDS number (e.g. "TDS-2024-0042").
 * @param {Object} payload - TDSCreateIn schema (see schemas.py)
 * @returns {Promise<Object>} Created TDS with tds_id and tds_number
 */
export const createTDS = (payload) =>
  apiFetch('/tds', { method: 'POST', body: JSON.stringify(payload) });

/**
 * Edit an existing TDS record in place (same payload shape as createTDS).
 * Used by generate-tds.js's edit mode (?edit=<tds_id>) so a mistake caught
 * on the preview page, or a customer-requested change made later, updates
 * the same record instead of creating a new one. Requires admin/tds_creator.
 * @param {number} id      - The TDS database ID to update
 * @param {Object} payload - Same shape as createTDS's payload
 * @returns {Promise<Object>} Updated TDS record
 */
export const updateTDS = (id, payload) =>
  apiFetch(`/tds/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

/**
 * Create a TDSBatch with N belt records atomically.
 * Used by the belt-queue multi-belt flow in generate-tds.js.
 *
 * Endpoint: POST /api/tds/batch/
 * Payload shape (see batch_views.py create_batch docstring):
 *   { shared: {...}, customer: { customer_id }, belts: [{...}, ...] }
 *
 * @param {Object} payload - Batch creation payload
 * @returns {Promise<Object>} { batch, tds_records, count }
 */
export const createBatch = (payload) =>
  apiFetch('/tds/batch/', { method: 'POST', body: JSON.stringify(payload) });

/**
 * Fetch an existing TDSBatch with a summary of every linked TDS record.
 * Used by generate-tds.html to re-render the batch-created success panel
 * when returning to it (e.g. the batch preview page's "Back" link) instead
 * of only being able to show it right after creation.
 *
 * Endpoint: GET /api/tds/batch/{id}/
 * @param {number} batchId
 * @returns {Promise<Object>} { batch, customer_name, tds_records, count }
 */
export const getBatch = (batchId) => apiFetch(`/tds/batch/${batchId}/`);

/**
 * Approve a TDS record, recording who approved it and when.
 * Only admins and TDS creators can perform this action.
 * @param {number} id         - The TDS database ID
 * @param {string} approvedBy - Name or email of the approving user
 * @returns {Promise<Object>} Updated TDS record
 */
export const approveTDS = (id, approvedBy) =>
  apiFetch(`/tds/${id}/approve`, {
    method: 'PATCH',
    body: JSON.stringify({ approved_by: approvedBy }),
  });

/**
 * Decline (reject) a TDS record, setting its status back to draft.
 * @param {number} id - The TDS database ID
 * @returns {Promise<Object>} Updated TDS record
 */
export const declineTDS = (id) =>
  apiFetch(`/tds/${id}/decline`, { method: 'PATCH' });

/**
 * Permanently delete a TDS record from the database.
 * This action cannot be undone - the UI shows a confirmation dialog first.
 * @param {number} id - The TDS database ID
 * @returns {Promise<null>} Returns null (204 No Content)
 */
export const deleteTDS = (id) =>
  apiFetch(`/tds/${id}/`, { method: 'DELETE' });

/* ══════════════════════════════════════════════════════════
   SECTION: Parameters
   Parameters are the rows on the TDS PDF (e.g. "Tensile Strength",
   "Elongation at Break"). They are grouped under headings and
   can be individually excluded from the PDF export.
══════════════════════════════════════════════════════════ */

/**
 * Fetch all parameter groups and their individual parameters for a brand.
 * Used to populate the PDF options checkboxes (show/hide individual rows).
 * @param {number} [brandId=1] - Brand ID (defaults to 1 = INDUS)
 * @returns {Promise<Object>} Map of { groupName: [{ parameter_id, parameter_name }] }
 */
export const getParameters = (brandId = 1) =>
  apiFetch(`/parameters?brand_id=${brandId}`);

/* ══════════════════════════════════════════════════════════
   SECTION: Users (Admin Panel)
══════════════════════════════════════════════════════════ */

/**
 * Fetch all registered users. Only accessible by admins.
 * @returns {Promise<Array>} Array of user objects
 */
export const listUsers  = () => apiFetch('/users');

/**
 * Create a new user account.
 * @param {Object} payload - { email, full_name, role, password }
 * @returns {Promise<Object>} Created user object
 */
export const createUser = (payload) =>
  apiFetch('/users/', { method: 'POST', body: JSON.stringify(payload) });

/**
 * Update a user's details (name, role, active status, etc.).
 * @param {number} id      - The user's ID
 * @param {Object} payload - Partial user fields to update
 * @returns {Promise<Object>} Updated user object
 */
export const updateUser = (id, payload) =>
  apiFetch(`/users/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });

/**
 * URL for a user's signature image preview (<img src="...">). Not fetched
 * via apiFetch/fetch — the browser sends the auth cookie automatically on a
 * same-origin <img> request, so this is just a plain URL. 404s if the user
 * has no signature on file; the caller should only set it as an <img> src
 * when list/getUser's `has_signature` flag is true.
 * @param {number} id - The user's ID
 * @returns {string}
 */
export const getUserSignatureUrl = (id) => `${API_BASE}/users/${id}/signature`;

/**
 * Upload (or replace) a user's signature image. Optional — not every user
 * needs one. The server crops/resizes it to fit the PDF footer regardless
 * of the source image's size or aspect ratio.
 *
 * Uses raw fetch, not apiFetch, because apiFetch always forces
 * Content-Type: application/json — a multipart/form-data upload needs the
 * browser to set its own Content-Type (with the multipart boundary), so it
 * must never be set manually here.
 * @param {number} id   - The user's ID
 * @param {File} file   - The image file (PNG/JPEG/WEBP)
 * @returns {Promise<Object>} Updated user object
 */
export async function uploadUserSignature(id, file) {
  const body = new FormData();
  body.append('signature', file);
  const res = await fetch(`${API_BASE}/users/${id}/signature`, {
    method: 'PUT', credentials: 'include', headers: getAuthHeaders(), body,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Signature upload failed: HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Remove a user's signature image.
 * @param {number} id - The user's ID
 * @returns {Promise<void>}
 */
export async function deleteUserSignature(id) {
  const res = await fetch(`${API_BASE}/users/${id}/signature`, {
    method: 'DELETE', credentials: 'include', headers: getAuthHeaders(),
  });
  if (!res.ok && res.status !== 404) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Signature removal failed: HTTP ${res.status}`);
  }
}

/* ══════════════════════════════════════════════════════════
   SECTION: OTP / Password Change
   The password-change flow uses a one-time code sent via email:
     1. User calls requestOTP(email) → server emails a 6-digit OTP.
     2. User calls verifyOTP(email, otp, new_password) → server validates
        the OTP and updates the password hash.
   changePassword is an alternative that uses the current password directly
   (used when the user is already signed in and remembers their password).
══════════════════════════════════════════════════════════ */

/**
 * Request a one-time password (OTP) code be sent to the given email address.
 * The OTP is valid for 10 minutes.
 * @param {string} email - The account email to send the OTP to
 * @returns {Promise<Object>} Server confirmation message
 */
export const requestOTP = (email) =>
  apiFetch('/auth/request-otp', { method: 'POST', body: JSON.stringify({ email }) });

/**
 * Verify the OTP code and set a new password in one step.
 * @param {string} email        - The account email
 * @param {string} otp          - The 6-digit code from the email
 * @param {string} new_password - The new password (min 8 characters)
 * @returns {Promise<Object>} Server confirmation message
 */
export const verifyOTP  = (email, otp, new_password) =>
  apiFetch('/auth/verify-otp', { method: 'POST', body: JSON.stringify({ email, otp, new_password }) });

/**
 * Change password using the current (known) password - no OTP needed.
 * @param {string} current_password - The user's existing password
 * @param {string} new_password     - The desired new password
 * @returns {Promise<Object>} Server confirmation message
 */
export const changePassword = (current_password, new_password) =>
  apiFetch('/auth/change-password', { method: 'POST', body: JSON.stringify({ current_password, new_password }) });

/* ══════════════════════════════════════════════════════════
   SECTION: PDF Export
   The PDF is generated server-side (Django renders HTML → WeasyPrint → PDF).
   These helpers build the URL with optional customisation flags and
   trigger a browser download using a temporary object URL.
══════════════════════════════════════════════════════════ */

/**
 * Build a PDF download URL for the given TDS ID with optional display flags.
 *
 * The returned URL points directly at the Django PDF endpoint.
 * It can be used in an <iframe src="..."> for preview, or passed to downloadPdf().
 *
 * @param {number} id    - The TDS database ID
 * @param {Object} [opts={}] - PDF customisation options:
 *   @param {boolean} [opts.showSection]    - Whether to show the "Section" column in parameter table
 *   @param {boolean} [opts.showTestMethod] - Whether to show the "Test Method" column
 *   @param {boolean} [opts.showReference]  - Whether to show the "Reference" column
 *   @param {string[]} [opts.excludeGroups] - Names of parameter groups to omit entirely
 *   @param {number[]} [opts.excludeParams] - Individual parameter_ids to hide
 * @returns {string} Full URL string, e.g. '/api/tds/42/pdf?show_section=false&...'
 */
export function getPdfUrl(id, opts = {}) {
  const qs = new URLSearchParams();

  // Only append flags that differ from the default (default = shown / included)
  if (opts.showSection    === false) qs.set('show_section',     'false');
  if (opts.showTestMethod === false) qs.set('show_test_method', 'false');
  if (opts.showReference  === false) qs.set('show_reference',   'false');

  // Each excluded group/param is appended as a repeated query param
  if (opts.excludeGroups?.length) {
    opts.excludeGroups.forEach(g => qs.append('exclude_groups', g));
  }
  if (opts.excludeParams?.length) {
    opts.excludeParams.forEach(p => qs.append('exclude_params', p));
  }
  if (opts.excludeGiFields?.length) {
    opts.excludeGiFields.forEach(f => qs.append('exclude_gi_fields', f));
  }

  const query = qs.toString();
  return `${API_BASE}/tds/${id}/pdf${query ? '?' + query : ''}`;
}

/**
 * Download the TDS PDF to the user's computer.
 *
 * How it works:
 *  1. Fetch the PDF binary from the server (with auth header).
 *  2. Convert the response to a Blob (raw binary data).
 *  3. Create a temporary object URL pointing to the Blob.
 *  4. Programmatically click a hidden <a> tag to trigger the browser's Save dialog.
 *  5. Remove the hidden link from the DOM and revoke the object URL after 5 s.
 *
 * @param {number} id        - The TDS database ID
 * @param {string} tdsNumber - The human-readable TDS number used for the filename
 * @param {Object} [opts={}] - Same display options as getPdfUrl()
 * @returns {Promise<void>}
 * @throws {Error} If the server returns a non-2xx status
 */
export async function downloadPdf(id, tdsNumber, opts = {}) {
  // Use raw fetch (not apiFetch) because we need the raw binary Blob, not JSON
  const res = await fetch(getPdfUrl(id, opts), { headers: getAuthHeaders() });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `PDF export failed: HTTP ${res.status}`);
  }

  // Convert the response stream to a binary Blob (application/pdf)
  const blob = await res.blob();

  // Create a temporary in-memory URL the browser can open/download
  const url  = URL.createObjectURL(blob);

  // Filename comes from the server's Content-Disposition (pdf_views.py::
  // generate_pdf) rather than being rebuilt here - it carries the record's
  // current revision number (e.g. "TDS-0016_rev_01.pdf"), which this
  // function has no way to know on its own. Falls back to the old plain
  // "TDS-<number>.pdf" only if the header is somehow missing.
  const cd = res.headers.get('Content-Disposition') || '';
  const match = /filename="?([^";]+)"?/i.exec(cd);
  const filename = match ? match[1] : `TDS-${tdsNumber}.pdf`;

  // Build an invisible <a> link, click it to trigger the Save dialog, then clean up
  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  // Delay revoke so the browser has time to start the download before the URL is freed
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/**
 * Download the QAP (Quality Assurance Plan) PDF for a TDS to the user's
 * computer. Mirrors downloadPdf() above, but hits the separate QAP endpoint
 * and requires the PO/Enquiry reference details the QAP document header
 * shows (entered fresh on every download — never persisted server-side,
 * see qap_service.py's build_qap_context docstring).
 *
 * @param {number} id        - The TDS database ID
 * @param {string} tdsNumber - Used for the downloaded filename
 * @param {Object} ref       - { docType: 'PO'|'ENQUIRY', refNo: string, refDate: string }
 * @returns {Promise<void>}
 * @throws {Error} If the server returns a non-2xx status
 */
export async function downloadQapPdf(id, tdsNumber, ref = {}) {
  const qs = new URLSearchParams({
    doc_type: ref.docType || 'PO',
    ref_no:   ref.refNo   || '',
    ref_date: ref.refDate || '',
  });
  const res = await fetch(`${API_BASE}/tds/${id}/qap/pdf?${qs.toString()}`, { headers: getAuthHeaders() });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `QAP export failed: HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const url  = URL.createObjectURL(blob);

  // BUG FIX: this used to always build "QAP-<number>.pdf" client-side
  // regardless of what the server actually named it (qap_views.py::
  // generate_qap_pdf now uses the TDS Document Number / customer name, same
  // as the TDS PDF itself). Same fix as downloadPdf() above: trust the
  // server's Content-Disposition, and use this template only if the header
  // is somehow missing.
  const cd = res.headers.get('Content-Disposition') || '';
  const match = /filename="?([^";]+)"?/i.exec(cd);
  const filename = match ? match[1] : `QAP-${tdsNumber}.pdf`;

  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
