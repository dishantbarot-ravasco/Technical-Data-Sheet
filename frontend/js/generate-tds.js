/**
 * generate-tds.js  v7
 * Single-page compact TDS form (replaces 5-step wizard) - the largest and
 * most complex frontend script in the app. It drives generate-tds.html end
 * to end: dropdown population, cascading EAV lookups, all client-side
 * calculations (weight/packing/splicing - mirrored server-side in
 * django_backend/apps/services/calculations.py, packing_service.py,
 * splicing_service.py so the preview numbers match what the PDF renders),
 * multi-belt batch queueing, and final submission.
 *
 * Weight formulas:
 *   Net/m   = SG × T_mm × (W_mm / 1000)
 *   Gross/m = SG × (T_mm + 0.5) × (W_mm / 1000)
 *
 * Splice formula (IS 14206 Part I):
 *   step_len  = table lookup on ratingPerPly
 *   splice/joint = 0.3×W + step×(plies−1) + buffer
 *   total(m)  = joints × splice / 1000
 *
 * Reading guide - the file is organized into the `/* ══ SECTION ══ *\/`
 * banner comments below, in this order:
 *   1. Auth + module-level state (allCustomers/allReelTypes/.../beltQueue)
 *      and small DOM helpers (val/set/setText/selectedText).
 *   2. Static reference data (ALL_PARAM_GROUPS) that mirrors backend lookup
 *      tables for client-side calculation without a round trip. Container
 *      types and shipping-region weight limits are NOT static data here -
 *      they're fetched live from GET /api/shipping-constraints (see
 *      _refreshShippingConstraints) so they can never drift from the DB.
 *   3. INIT - init() is the page's true entry point (called at the bottom of
 *      this file); it loads dropdown data then calls wireEvents().
 *   4. Dropdown population + cascading EAV lookup: loadAllDropdowns() →
 *      populateSelect()/populateStandardsForBrand() → loadCoverGrades()/
 *      loadBeltRatings() → runLookup() (the actual POST /api/tds/lookup call).
 *   5. Belt description assembly (updateBeltDescription) - builds the
 *      human-readable summary string shown on the form and stored on the record.
 *   6. Dimension/weight calculations (recalcTotal, recalcWeight) - client-side
 *      mirror of the Net/Gross weight formulas above.
 *   7. Reel diameter math (roundUpHalf, computeReelDiam) and packing
 *      calculations (recalcPacking) - client-side mirror of packing_service.py.
 *   8. Splicing calculations (getSpliceStep, recalcSplicing) - client-side
 *      mirror of splicing_service.py.
 *   9. Customer autocomplete + generic searchable-select widget
 *      (wireCustomerAutocomplete, makeSearchable) - the latter is reused for
 *      every long dropdown on this form (brand, standard, cover grade, etc.).
 *  10. Dimensional spec fetch (fetchDimensionalSpecs) - pulls DB tolerance
 *      values so the PDF's "Spec" column can be previewed here too.
 *  11. Breaker helpers (window._brkTop/_brkBot) - small onchange handlers
 *      referenced directly from inline HTML attributes in generate-tds.html.
 *  12. wireEvents() - attaches every listener above to its form control;
 *      read this function's own doc-comment for the full listener map.
 *  13. Multi-belt queue (captureBeltSpec → validateBeltSpec → addBeltToQueue
 *      → renderBeltQueue → removeBeltFromQueue) - lets one TDS submission
 *      cover several belt specs at once (bulk-text import shares this queue;
 *      see django_backend/apps/api/routers/batch_views.py's text_import_batch).
 *  14. Packing override toggle, form validation (validateForm), PDF display
 *      options (buildPdfGroupOptions/getPdfOptions), preview (loadPreview),
 *      and finally submitTDS() - the terminal function that POSTs to
 *      /api/tds or /api/tds/batch/ depending on whether beltQueue has entries.
 */
import {
  requireAuth, populateNavUser, showToast,
} from './auth.js';
import {
  getBootstrap,
  getCoverGrades,
  getFabricStyles, getBeltRatings,
  createCustomer, updateCustomer, searchCustomers,
  tdsLookup, createTDS, updateTDS, getTDS, createBatch, downloadPdf, getParameters,
  getDimensionalSpecs, getShippingConstraints,
} from './api.js';

/**
 * Escape a value for safe insertion into an innerHTML template string.
 * SECURITY: customer name/location/contact/application data rendered by the
 * autocomplete below comes from user-entered form fields stored on the
 * backend, so it must never be trusted as raw HTML. Always run dynamic text
 * through this before interpolating it into a template literal assigned to
 * .innerHTML.
 */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/**
 * BUG FIX: scrolling the mouse wheel over a focused <input type="number">
 * silently increments/decrements its value by the field's `step` (1 for
 * most fields, 0.5 for a few like length-per-roll-override) — the browser's
 * native behavior, not a slider widget, but it looks and feels like one and
 * is exactly this: type "1" in belt-width-mm, then scroll the page while the
 * cursor happens to be over that field, and it silently becomes "2" (or
 * "1.5") with no visual cue. Blurring the field on wheel — instead of
 * letting the browser handle the scroll as a value change — makes the wheel
 * do what a user actually expects (scroll the page) and leaves the typed
 * value untouched. Delegated on document so it covers every current number
 * input on this form without wiring 17 individual listeners, and any future
 * one added to the page needs no extra code.
 */
document.addEventListener('wheel', () => {
  const el = document.activeElement;
  if (el && el.tagName === 'INPUT' && el.type === 'number') {
    el.blur();
  }
}, { passive: true });

/* ── Auth ─────────────────────────────────────────────────── */
const session = await requireAuth();
if (session) populateNavUser();

/* ── State ────────────────────────────────────────────────── */
let allCustomers      = [];   // loaded once from /api/bootstrap
let allReelTypes      = [];   // loaded once from /api/bootstrap
let allStandards      = [];   // loaded once from /api/bootstrap; filtered per-brand in populateStandardsForBrand()
// Splice config loaded from /api/bootstrap (splicing_config key).
// Fallback to IS 14206 defaults so the page still works if bootstrap fails.
let allSplicingConfig = {
  step_table: [
    {max_fabric_rating_kn_m:100,step_length_mm:150},{max_fabric_rating_kn_m:125,step_length_mm:200},
    {max_fabric_rating_kn_m:160,step_length_mm:200},{max_fabric_rating_kn_m:200,step_length_mm:250},
    {max_fabric_rating_kn_m:250,step_length_mm:300},{max_fabric_rating_kn_m:300,step_length_mm:350},
    {max_fabric_rating_kn_m:315,step_length_mm:350},{max_fabric_rating_kn_m:350,step_length_mm:400},
    {max_fabric_rating_kn_m:400,step_length_mm:400},
  ],
  buffers: { hot: 50, cold: 75 },
};
let lookupData    = null; // result of last /api/tds/lookup call
let createdTdsId  = null; // tds_id returned after a successful createTDS call
let allParameters = {};   // { groupName: [{parameter_id, parameter_name}] } for PDF options
let beltQueue     = [];   // array of captured belt-spec objects waiting to be submitted
// True once the user has typed their own text directly into #belt-description.
// While true, updateBeltDescription() stops overwriting the field - the field
// itself is never readonly/locked, so the user can always type into it; this
// flag just decides whether the live auto-fill is allowed to keep overwriting
// what's there. Clearing the field (or matching what auto-fill would produce)
// resets it back to false so auto-fill resumes. See wireEvents()'s 'input'
// listener on #belt-description and _setBeltDescMode() below.
let beltDescDirty = false;
// Set from ?edit=<tds_id> in the URL (see init()). Non-null means this page
// is editing an existing TDS in place — submitTDS() calls updateTDS()
// instead of createTDS(), and the record is never re-numbered/re-created.
// This is also how the preview page's "← Back" link avoids losing data:
// it links back here with the same ?edit=<tds_id> instead of history.back().
let editingTdsId  = null;
// Set from ?batch_id=<id> when editing a belt opened from the batch preview
// page (tds-multi-preview.html) - lets submitTDS() send the user back into
// that batch instead of the single-belt preview once the edit is saved.
let editingBatchId = null;

/* ── DOM utility helpers ──────────────────────────────────── */
/**
 * Get the current .value of an <input> or <select> element.
 * Returns '' (empty string) if the element doesn't exist.
 * @param {string} id - The element's HTML id attribute
 */
const val = (id) => document.getElementById(id)?.value ?? '';

/**
 * Set the .value of an <input> or <select> element.
 * Silently does nothing if the element doesn't exist.
 * @param {string} id  - The element's HTML id
 * @param {*}      v   - The new value (coerced to string)
 */
/* Registry of searchable-select sync functions - keyed by select id.
   Populated by makeSearchable(); used by set() and autoSelect() so that
   programmatic value changes keep the visible input in sync. */
const _searchableSyncs = {};

const set = (id, v) => {
  const el = document.getElementById(id);
  if (el) { el.value = v; _searchableSyncs[id]?.(); }
};

/**
 * Set the .textContent of any element.
 * Used to update read-only display chips and calculated-value spans.
 * @param {string} id  - The element's HTML id
 * @param {*}      v   - The text to display
 */
const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

/**
 * Return the display text of the currently selected <option> in a <select>.
 * Used to build the auto-assembled belt description string.
 * @param {string} id - The <select> element's HTML id
 * @returns {string} The option's text, or '' if the element doesn't exist
 */
const selectedText = (id) => {
  const sel = document.getElementById(id);
  if (!sel) return '';
  return sel.options[sel.selectedIndex]?.text || '';
};


const ALL_PARAM_GROUPS = [
  'General Information',
  'Dimensional Parameters',
  'Belt Construction Parameters',
  'Fabric Parameters',
  'Cover Rubber Properties',
  'After Ageing Cover Rubber Properties',
  'Belt Breaking Strength',
  'Adhesion Values',
  'Troughability',
  'Recommended Minimum Pulley Diameter',
  'Sampling and Testing',
  'Packing and Logistics',
  'Splicing Parameters',
];

/* ══════════════════════════════════════════════════════════
   INIT
══════════════════════════════════════════════════════════ */
/**
 * Entry point called when the page loads.
 * Sets the date field to today, populates the user's name in the footer,
 * loads all dropdown data from the API, and wires all event listeners.
 */
/**
 * Number inputs (belt width, covers, plies, weights, ...) change their value
 * when the mouse wheel scrolls over them while focused - a well-known
 * browser default that has nothing to do with intentionally editing the
 * field. On a long form like this one it's easy to scroll the page with the
 * cursor sitting over a number field and silently corrupt a value without
 * noticing. Blur any focused number input as soon as a wheel event reaches
 * it, so the wheel just scrolls the page like normal instead of nudging
 * the value.
 */
function _disableNumberInputScroll() {
  document.addEventListener('wheel', (e) => {
    const el = document.activeElement;
    if (el && el.tagName === 'INPUT' && el.type === 'number' && el === e.target) {
      el.blur();
    }
  }, { passive: true });
}

async function init() {
  // Set today's date
  const today = new Date().toISOString().slice(0, 10);
  const dateEl = document.getElementById('tds-date');
  if (dateEl) dateEl.value = today;

  // Populate footer user name
  if (session) {
    const nameEl = document.getElementById('footer-user-name');
    if (nameEl) nameEl.textContent = session.full_name || session.email || '-';
  }

  await loadAllDropdowns();
  wireEvents();
  _disableNumberInputScroll();

  // Wrap every <select> with a live-search input.
  // Done AFTER loadAllDropdowns() so options are already present,
  // and AFTER wireEvents() so the native 'change' listeners are in place
  // before makeSearchable() can dispatch synthetic change events.
  [
    'purpose-id', 'belt-type-id', 'brand-id', 'standard-id',
    'cover-grade-id', 'fabric-type-id', 'belt-rating-id', 'fabric-style-id',
    'reel-type-id', 'packing-type-id', 'edge-construction', 'construction-type',
    'shipping-region', 'container-type-id', 'vulcanization-method', 'make-of-fabric',
  ].forEach(makeSearchable);

  // ── Edit mode: ?edit=<tds_id> ────────────────────────────────────────────
  // Reached either from search-tds.html's "Edit" button, or from
  // tds-preview.html's "← Back" link (so backing out of a mistake edits the
  // very record Preview just created, instead of abandoning it and forcing
  // a full re-entry — see prefillFormFromRecord() below).
  const editId = new URLSearchParams(window.location.search).get('edit');
  if (editId) {
    editingTdsId  = +editId;
    editingBatchId = new URLSearchParams(window.location.search).get('batch_id') || null;
    try {
      const record = await getTDS(editingTdsId);
      await prefillFormFromRecord(record);
      _enterEditModeUI(record);
    } catch (err) {
      showToast('Could not load TDS-' + editId + ' for editing: ' + err.message, 'error', 8000);
      editingTdsId = null;
    }
  }
}

/**
 * Switch the page's chrome into "editing an existing TDS" mode: banner at
 * the top, and the submit buttons relabelled so it's never ambiguous
 * whether clicking them creates a new record or updates this one.
 */
function _enterEditModeUI(record) {
  const heading = document.querySelector('.page-title, h1');
  const banner  = document.createElement('div');
  banner.className   = 'edit-mode-banner';
  banner.style.cssText = 'background:#FEF3C7;color:#92400E;border:1px solid #F0B429;' +
    'border-radius:6px;padding:10px 16px;margin-bottom:16px;font-size:13px;font-weight:600;';
  banner.textContent  = `✏ Editing TDS-${record.tds_number} - saving will update this existing record, not create a new one.`;
  (heading?.parentNode || document.body).insertBefore(banner, heading?.nextSibling || document.body.firstChild);

  const previewBtn  = document.getElementById('btn-preview-pdf');
  const draftBtn    = document.getElementById('btn-save-draft');
  // IMPORTANT: never set previewBtn.textContent directly - it destroys the
  // #submit-text span that lives inside it, which submitTDS() (and the
  // batch-generation flow elsewhere in this file) keeps looking up by ID
  // to show progress text ("Generating…", "Saving…", ...). Update just the
  // span's own text instead so that span keeps existing.
  const previewSpan = previewBtn?.querySelector('#submit-text');
  if (previewSpan) previewSpan.textContent = 'Save Changes & Preview PDF';
  if (draftBtn) draftBtn.textContent = 'Save Changes';

  // Editing a single existing record has no multi-belt concept - hide the
  // "add another belt" control so beltQueue can never gain entries here.
  // (submitTDS() checks beltQueue.length before editingTdsId, so a queued
  // belt in edit mode would otherwise get bundled into a brand-new batch
  // instead of updating this record - see submitTDS() for the guard too.)
  const addBeltBtn = document.getElementById('btn-add-belt');
  if (addBeltBtn) addBeltBtn.style.display = 'none';
}

/**
 * Load an existing TDS record's fields back into the form, replaying the
 * same cascading lookups a fresh entry would trigger (brand → standards,
 * standard → cover grades, fabric type → belt ratings/styles, then the EAV
 * lookup itself) so every dependent dropdown and computed chip ends up in
 * the same state it would from manual entry — not just the raw field values.
 *
 * Deliberately calls the loader functions directly (loadCoverGrades(),
 * loadBeltRatings(), runLookup()) rather than dispatching synthetic 'change'
 * events, so each step can be awaited in the correct order before the next
 * one depends on it.
 */
async function prefillFormFromRecord(record) {
  // ── Customer ───────────────────────────────────────────────────────────
  if (record.customer_id) {
    const hidId = document.getElementById('customer-id');
    const search = document.getElementById('customer-search');
    if (hidId)  hidId.value  = record.customer_id;
    if (search) search.value = record.customer_name || record.customer?.customer_name || '';
    set('cust-application', record.customer?.application    || '');
    set('cust-location',    record.customer?.plant_location || '');
  }

  // ── Identification ─────────────────────────────────────────────────────
  set('purpose-id',        record.purpose_id      ?? '');
  set('belt-type-id',      record.belt_type_id    ?? '');
  set('tds-doc-number',    record.tds_doc_number  ?? '');
  set('construction-type', record.construction_type || 'Open-End');
  _toggleIntlRow();
  if (_isInternational()) {
    set('shipping-region',   record.shipping_region    ?? '');
    set('container-type-id', record.container_type_id  ?? '');
  }

  // ── Brand → Standard (cascading) ──────────────────────────────────────
  set('brand-id', record.brand_id ?? '');
  populateStandardsForBrand(record.brand_id);
  set('standard-id', record.standard_id ?? '');
  await loadCoverGrades(record.standard_id);
  set('cover-grade-id', record.cover_grade_id ?? '');

  // ── Fabric type → Belt rating / Fabric style (cascading) ───────────────
  set('fabric-type-id', record.fabric_type_id ?? '');
  await loadBeltRatings(record.fabric_type_id);
  set('belt-rating-id', record.belt_rating_id ?? '');
  set('make-of-fabric', record.make_of_fabric || 'MIT');

  // ── Run the EAV lookup — populates carcass/skim/plies chips + fabric
  //    style from the server, exactly as a fresh manual selection would.
  await runLookup();

  // If the stored fabric style differs from what auto-selection just chose,
  // the user had manually picked a different one at creation time — restore it.
  if (record.fabric_style_id && String(val('fabric-style-id')) !== String(record.fabric_style_id)) {
    set('fabric-style-id', record.fabric_style_id);
  }

  // If the stored carcass thickness differs from the auto-filled value,
  // the user had the override toggle on originally — restore that state.
  const autoCarcass = lookupData?.belt_rating?.carcass_thickness_mm;
  if (record.carcass_thickness_mm != null && String(autoCarcass) !== String(record.carcass_thickness_mm)) {
    const toggle = document.getElementById('carcass-override-toggle');
    if (toggle && !toggle.checked) { toggle.checked = true; toggle.dispatchEvent(new Event('change')); }
    set('carcass-thickness-mm', record.carcass_thickness_mm);
  }

  // ── Belt identity / dimensions ──────────────────────────────────────────
  set('belt-width-mm',    record.belt_width_mm  ?? '');
  set('belt-length-m',    record.belt_length_m  ?? '');
  set('edge-construction', record.edge_construction || 'Moulded');
  set('top-cover-mm',     record.top_cover_mm    ?? '');
  set('bottom-cover-mm',  record.bottom_cover_mm ?? '');
  recalcTotal();
  _enforceEndlessMax();

  // Belt description: only restore the stored text if it differs from what
  // auto-assembly would now produce - belt-description is user-editable, so
  // a manually-customised description should survive re-editing the record.
  //
  // Comparison is normalized (see _normalizeBeltDescForCompare) rather than a
  // raw string match: the API round-trips top_cover_mm/bottom_cover_mm/etc.
  // through JSON as floats, so a value originally typed as "6.0" (giving a
  // stored description like "...X 6.0mm...") comes back as JS number 6 and
  // regenerates as "...X 6mm...". A raw compare treated that formatting
  // drift as "the user customised this", which permanently stopped
  // updateBeltDescription() from live-updating the field for the rest of
  // the edit session (see beltDescDirty) - so changing belt width/covers/etc.
  // afterward silently had no effect on the saved description at all.
  beltDescDirty = false;
  updateBeltDescription();
  if (record.belt_description
      && _normalizeBeltDescForCompare(val('belt-description')) !== _normalizeBeltDescForCompare(record.belt_description)) {
    beltDescDirty = true;
    _setBeltDescMode(true);
    set('belt-description', record.belt_description);
  }

  // ── Breakers ──────────────────────────────────────────────────────────
  const brkTopYes = document.querySelector('input[name="brk-top"][value="yes"]');
  const brkTopNo  = document.querySelector('input[name="brk-top"][value="no"]');
  if (record.breaker_top) {
    if (brkTopYes) brkTopYes.checked = true;
    window._brkTop(true);
    set('breaker-top-plies', record.breaker_top_plies ?? '');
  } else if (brkTopNo) { brkTopNo.checked = true; window._brkTop(false); }

  const brkBotYes = document.querySelector('input[name="brk-bot"][value="yes"]');
  const brkBotNo  = document.querySelector('input[name="brk-bot"][value="no"]');
  if (record.breaker_bottom) {
    if (brkBotYes) brkBotYes.checked = true;
    window._brkBot(true);
    set('breaker-bottom-plies', record.breaker_bottom_plies ?? '');
  } else if (brkBotNo) { brkBotNo.checked = true; window._brkBot(false); }

  // ── Packing ───────────────────────────────────────────────────────────
  set('reel-type-id',    record.reel_type_id    ?? '');
  set('packing-type-id', record.packing_type_id ?? '');
  recalcPacking();
  // If the stored packing values differ from what auto-recompute just
  // produced, the user had manually overridden them — reopen the override
  // panel and restore the exact stored values.
  //
  // BUG FIX: this used to compare String(autoLpr) !== String(record.length_per_roll_m)
  // etc. Auto fields are formatted client-side with .toFixed(2) (e.g. "300.00"),
  // while the API returns plain floats (e.g. 300, stringifying to "300") - so the
  // string comparison mismatched on essentially every decimal packing field, on
  // every single edit-load, regardless of whether the user had ever touched the
  // override panel. That false positive opened the override panel and repopulated
  // it with the *previously stored* (now stale) packing values; since submitTDS()
  // always prefers the override fields over the freshly-recalculated auto fields,
  // any edit that changed belt length/width (which legitimately changes packing)
  // silently re-saved the OLD, now-inconsistent packing numbers - e.g. a belt
  // edited from 300m to 450m kept "1 roll x 300m" instead of recomputing to
  // "2 rolls x 225m". Compare numerically (with a small tolerance for float
  // rounding) instead, so only a *genuine* stored/recomputed mismatch reopens
  // the override panel.
  const numsDiffer = (a, b, eps = 0.005) => {
    const na = Number(a), nb = Number(b);
    if (Number.isNaN(na) || Number.isNaN(nb)) return String(a) !== String(b);
    return Math.abs(na - nb) > eps;
  };
  const autoRolls = val('num-rolls'), autoLpr = val('length-per-roll'),
        autoNet   = val('net-weight-kg'), autoGross = val('gross-weight-kg');
  const overridden =
    (record.num_rolls != null          && numsDiffer(autoRolls, record.num_rolls)) ||
    (record.length_per_roll_m != null  && numsDiffer(autoLpr,   record.length_per_roll_m)) ||
    (record.net_weight_kg != null      && numsDiffer(autoNet,   record.net_weight_kg)) ||
    (record.gross_weight_kg != null    && numsDiffer(autoGross, record.gross_weight_kg));
  const hasCustomRollLengths = Array.isArray(record.roll_lengths_m) && record.roll_lengths_m.length > 1;
  if (overridden || hasCustomRollLengths) {
    const fields = document.getElementById('packing-override-fields');
    if (fields && fields.style.display === 'none') togglePackingOverride();
    set('num-rolls-override',       record.num_rolls         ?? '');
    set('length-per-roll-override', record.length_per_roll_m ?? '');
    set('net-weight-kg-override',   record.net_weight_kg     ?? '');
    set('gross-weight-kg-override', record.gross_weight_kg   ?? '');
    // A saved custom (unequal) roll-length split is definitive — restore the
    // exact per-roll values rather than the auto-equal-split renderRollLengthInputs()
    // would otherwise prefill.
    if (hasCustomRollLengths) {
      renderRollLengthInputs(record.roll_lengths_m.length);
      record.roll_lengths_m.forEach((len, i) => set(`roll-len-override-${i}`, len));
      checkRollLengths();
    }
  } else if (record.roll_dimensions) {
    set('roll-dimensions', record.roll_dimensions);
  }

  // ── Splicing ──────────────────────────────────────────────────────────
  const splicingCb = document.getElementById('splicing-required');
  if (splicingCb) {
    splicingCb.checked = !!record.splicing_required;
    splicingCb.dispatchEvent(new Event('change'));
  }
  if (record.splicing_required) {
    set('vulcanization-method', record.vulcanization_method || 'Hot');
    set('num-joints',           record.num_joints ?? '');
    recalcSplicing();
  }
}

/* ══════════════════════════════════════════════════════════
   LOAD DROPDOWNS
══════════════════════════════════════════════════════════ */
/**
 * Fetch all master/reference data from the bootstrap endpoint in one request
 * and populate every <select> dropdown on the form.
 * Also pre-selects the first option for Belt Type and Brand (since there's
 * typically only one of each), and loads the PDF parameter list.
 */
async function loadAllDropdowns() {
  try {
    const d = await getBootstrap();

    allCustomers = d.customers;
    allReelTypes = d.reel_types;
    allStandards = d.standards;
    if (d.splicing_config) allSplicingConfig = d.splicing_config;

    populateSelect('purpose-id',     d.purposes,      'purpose_id',  'purpose_type',  '- Select Purpose -');
    populateSelect('belt-type-id',   d.belt_types,    'belt_id',     'belt_type',     '- Select Belt Type -');
    populateSelect('brand-id',       d.brands,        'brand_id',    'brand_name',    '- Select Brand -');
    populateSelect('fabric-type-id', d.fabric_types,  'id',          'fabric_code',   '- Select Fabric Type -');
    populateSelect('reel-type-id',   d.reel_types,    'id',          'reel_name',     '- None / Manual -');
    populateSelect('packing-type-id',d.packing_types, 'id',          'packing_name',  '- None / Manual -');
    populateSelect('container-type-id', d.container_types, 'id',     'name',          '- Select Container -');

    autoSelect('belt-type-id');
    autoSelect('brand-id');

    // Standard dropdown is brand-scoped (each brand has its own set of standards) -
    // populate it AFTER brand-id has been auto-selected above, filtered to that brand.
    populateStandardsForBrand(val('brand-id'));

    try { allParameters = await getParameters(); } catch { allParameters = {}; }

  } catch (err) {
    showToast('Failed to load form data: ' + err.message, 'error', 6000);
  }
}

/**
 * Fill a <select> element with options built from an array of API objects.
 * Clears existing options and prepends a blank placeholder option.
 *
 * @param {string} id          - The HTML id of the <select> element
 * @param {Object[]} items     - Array of objects returned by the API
 * @param {string} valueKey    - Key on each object to use as <option value="...">
 * @param {string} labelKey    - Key on each object to use as the visible option text
 * @param {string} placeholder - Text shown in the blank first option (e.g. '- Select -')
 */
function populateSelect(id, items, valueKey, labelKey, placeholder) {
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.innerHTML = `<option value="">${placeholder}</option>` +
    items.map(item => `<option value="${item[valueKey]}">${item[labelKey]}</option>`).join('');
}

/**
 * Populate the Standard dropdown with only the standards belonging to the
 * given brand. Standards are brand-scoped (e.g. INDUS SUPER BRUTE has
 * IS 1891/ISO 14890/DIN 22102/etc.; INDUS SUPER THERMO has its own IS 1891
 * (Part 2)/ISO/ARPM/Inhouse set) - without this filter every brand's
 * standards get dumped into one list, which only went unnoticed while
 * INDUS SUPER BRUTE was the only brand in the system.
 *
 * @param {string|number} brandId - The currently selected brand's database ID
 */
function populateStandardsForBrand(brandId) {
  const filtered = brandId
    ? allStandards.filter(s => String(s.brand_id) === String(brandId))
    : allStandards;
  populateSelect('standard-id', filtered, 'standard_id', 'standard_name', '- Select Standard -');
}

/**
 * Auto-select the first real option (index 1) in a <select>.
 * Used for dropdowns that almost always have exactly one choice
 * (e.g. Belt Type = "Flat Belt", Brand = "INDUS SUPER BRUTE").
 *
 * @param {string} id - The HTML id of the <select> element
 */
function autoSelect(id) {
  const sel = document.getElementById(id);
  if (sel && sel.options.length >= 2) {
    sel.selectedIndex = 1;
    _searchableSyncs[id]?.();   // keep searchable wrapper's visible input in sync
  }
}

/* ── Dependent: cover grades ────────────────────────────── */
/**
 * Fetch and populate the Cover Grade dropdown for the chosen testing standard.
 * Cover grade controls the rubber compound (e.g. M = general purpose, H = heat resistant).
 * Called every time the Standard dropdown changes.
 *
 * @param {string|number} standardId - The selected standard's database ID
 */
async function loadCoverGrades(standardId) {
  const sel = document.getElementById('cover-grade-id');
  sel.innerHTML = '<option value="">Loading…</option>';
  refreshSearchableDisplay('cover-grade-id');
  document.getElementById('grade-hint').textContent = '';
  if (!standardId) {
    sel.innerHTML = '<option value="">Select standard first</option>';
    refreshSearchableDisplay('cover-grade-id');
    return;
  }
  try {
    const grades = await getCoverGrades(standardId);
    sel.innerHTML = '<option value="">- Select Cover Grade -</option>' +
      grades.map(g => `<option value="${g.id}">${g.grade_code}</option>`).join('');
  } catch (err) {
    sel.innerHTML = '<option value="">Failed to load</option>';
    showToast('Failed to load cover grades: ' + err.message, 'error');
  }
  refreshSearchableDisplay('cover-grade-id');
}

/* ── Dependent: belt ratings + fabric styles ────────────── */
/**
 * Fetch and populate both the Belt Rating and Fabric Style dropdowns for the chosen fabric type.
 * A belt rating is the total tensile strength + number of plies (e.g. "EP 630/4" = 630 kN/m, 4 plies).
 * A fabric style is the weave pattern and construction variant (e.g. "EP 160/3").
 * Both are fetched in parallel with Promise.all for speed.
 * Called every time the Fabric Type dropdown changes.
 *
 * @param {string|number} fabricTypeId - The selected fabric type's database ID
 */
async function loadBeltRatings(fabricTypeId) {
  const ratingSel = document.getElementById('belt-rating-id');
  const styleSel  = document.getElementById('fabric-style-id');
  ratingSel.innerHTML = '<option value="">Loading…</option>';
  styleSel.innerHTML  = '<option value="">Loading…</option>';
  if (!fabricTypeId) {
    ratingSel.innerHTML = '<option value="">Select fabric type first</option>';
    styleSel.innerHTML  = '<option value="">Select fabric type first</option>';
    refreshSearchableDisplay('belt-rating-id');
    refreshSearchableDisplay('fabric-style-id');
    return;
  }
  try {
    const [ratings, styles] = await Promise.all([
      getBeltRatings(fabricTypeId),
      getFabricStyles(fabricTypeId),
    ]);
    ratingSel.innerHTML = '<option value="">- Select Belt Rating -</option>' +
      ratings.map(r => `<option value="${r.id}">${r.rating_name}</option>`).join('');
    styleSel.innerHTML = '<option value="">- None -</option>' +
      styles.map(s => `<option value="${s.id}">${s.style_name}</option>`).join('');
  } catch (err) {
    ratingSel.innerHTML = '<option value="">Failed to load</option>';
    styleSel.innerHTML  = '<option value="">Failed to load</option>';
    showToast('Failed to load belt ratings/fabric styles: ' + err.message, 'error');
  }
  refreshSearchableDisplay('belt-rating-id');
  refreshSearchableDisplay('fabric-style-id');
}

/* ── EAV lookup ─────────────────────────────────────────── */
/**
 * Fetch the full EAV (Entity-Attribute-Value) spec data for the selected
 * Standard + Cover Grade + Belt Rating combination.
 * Populates the "Computed Values" chip strip (plies, carcass thickness, skim),
 * auto-selects the best matching fabric style, and triggers recalcTotal().
 *
 * This is the main "intelligence" step of the form - after this call,
 * the form knows the rubber and carcass properties needed for all calculations.
 */
async function runLookup() {
  const standardId   = val('standard-id');
  const coverGradeId = val('cover-grade-id');
  const beltRatingId = val('belt-rating-id');
  if (!standardId || !coverGradeId || !beltRatingId) return;

  try {
    lookupData = await tdsLookup({
      standard_id:    +standardId,
      cover_grade_id: +coverGradeId,
      belt_rating_id: +beltRatingId,
    });

    const plies   = lookupData.belt_rating?.num_plies            ?? '-';
    const carcass = lookupData.belt_rating?.carcass_thickness_mm ?? '-';
    const skim    = lookupData.belt_rating?.interply_skim_mm     ?? '-';

    setText('cv-plies',   plies);
    setText('cv-carcass', carcass ?? '-');
    setText('cv-skim',    skim    ?? '-');

    // Also populate the form fields
    set('cv-plies-field', plies !== '-' ? plies : '');
    set('cv-skim-field',  skim  !== '-' ? skim  : '');

    if (!document.getElementById('carcass-override-toggle').checked) {
      set('carcass-thickness-mm', carcass ?? '');
    }

    // Auto-select fabric style — the SERVER already computed the correct
    // style (apps.services.calculations.auto_select_fabric_style, same
    // function batch import and create_tds use) and returned it as
    // lookupData.fabric_style. This used to be re-derived here by scanning
    // the dropdown's visible text and stripping every non-digit character
    // (`opt.textContent.replace(/[^0-9.]/g, '')`), which silently produces a
    // wrong number for any style name containing a second number (e.g.
    // "EP 200/3" would collapse to "2003"). Trusting the server's answer
    // removes that whole class of bug, and guarantees the option pre-selected
    // here is the exact same one create_tds will save.
    autoSelectFabricStyle();

    recalcTotal();
    document.getElementById('lookup-result').style.display = 'block';
    updateBeltDescription();
  } catch (err) {
    showToast('Lookup failed: ' + err.message, 'error');
  }
}

/**
 * Select the fabric style option that matches the server's authoritative
 * choice (lookupData.fabric_style, computed by auto_select_fabric_style() in
 * apps/services/calculations.py from the belt rating's kN/plies). If the
 * server didn't return a match (e.g. no style is strong enough for this
 * rating), the dropdown is left on "- None -".
 */
function autoSelectFabricStyle() {
  const styleSel = document.getElementById('fabric-style-id');
  const styleId  = lookupData?.fabric_style?.id;
  if (!styleSel || !styleId) return;
  styleSel.value = String(styleId);
  _searchableSyncs['fabric-style-id']?.();
}

/* ══════════════════════════════════════════════════════════
   BELT DESCRIPTION - auto-assembled from spec fields
══════════════════════════════════════════════════════════ */
/**
 * Flip the Belt Description field's badge/hint/highlight between AUTO and
 * MANUAL. The field itself is always editable - this only controls whether
 * it currently looks like a computed field (yellow highlight, AUTO badge)
 * or a field the user has taken over (plain white, MANUAL badge).
 * @param {boolean} isManual
 */
function _setBeltDescMode(isManual) {
  const field = document.getElementById('belt-description');
  const hint  = document.getElementById('belt-desc-hint');
  const badge = document.getElementById('belt-desc-auto-badge');
  if (!field || !hint || !badge) return;
  if (isManual) {
    field.classList.remove('auto-field');
    badge.textContent = 'MANUAL';
    hint.textContent = 'Manually entered · clear the field to resume auto-fill';
    hint.style.color = 'var(--gold-light)';
  } else {
    field.classList.add('auto-field');
    badge.textContent = 'AUTO';
    hint.textContent = 'Auto-fills live · type here to enter your own — same format as the Multiple Belts paste box, and the rest of the form fills in live as you type';
    hint.style.color = '';
  }
}

/**
 * Collapses "6mm" / "6.0mm" / "6.00mm" (any bare-number-plus-"mm" token) down
 * to one canonical form, so two belt descriptions that differ only in
 * decimal formatting compare as equal. See the edit-load belt-description
 * restore logic above for why this matters: a value stored as "...X 6.0mm..."
 * (typed at creation) round-trips through the API as a JSON float and
 * regenerates as "...X 6mm...", which a raw string compare would otherwise
 * treat as a genuine manual customisation.
 */
function _normalizeBeltDescForCompare(s) {
  return (s || '').replace(/(\d+(?:\.\d+)?)mm\b/g, (_, num) => `${parseFloat(num)}mm`);
}

/**
 * Auto-assemble the belt description string from the current form values.
 * Format: {width}mm X {fabric} X {rating} X {top}mm X {bottom}mm X {grade} X {edge} X {construction} {belt type}
 * Example: "600mm X EP X EP 1000/5 X 5mm X 2mm X H X Cut Edge X Open End Flat Belt"
 *
 * Requires at minimum: width, belt rating, top cover, and bottom cover.
 * Clears the description field if any required field is missing.
 * Called on every relevant field change so the field stays up to date.
 */
function updateBeltDescription() {
  // User has typed their own text directly into the field - leave it alone.
  if (beltDescDirty) return;
  _setBeltDescMode(false);

  const w  = val('belt-width-mm')     || '';
  const br = selectedText('belt-rating-id');   // 'EP 1000/5' - already carries the fabric code
  const tc = val('top-cover-mm')      || '';
  const bc = val('bottom-cover-mm')   || '';
  const cg = selectedText('cover-grade-id');   // 'H', 'M', etc.
  const ec = val('edge-construction') || '';
  const ct = val('construction-type') || 'Open-End';  // 'Open-End' or 'Endless'
  const bt = selectedText('belt-type-id');             // 'Flat Belt'

  // Require at minimum: width, rating, top/bottom cover
  if (!w || !br || br === '- Select Belt Rating -' || br === 'Select fabric first' || !tc || !bc) {
    set('belt-description', '');
    return;
  }

  const cg_clean = (cg === 'Select standard first' || cg === '- Select Cover Grade -' || !cg) ? '' : cg;
  const ec_clean = ec || '';

  // For non-Endless belts drop the construction type prefix (show just "Flat Belt").
  // For Endless belts prepend "Endless" (e.g. "Endless Flat Belt").
  const btLabel = (bt && bt !== '- Select Belt Type -') ? bt : 'Flat Belt';
  const beltTypeStr = ct === 'Endless' ? `Endless ${btLabel}` : btLabel;

  // Format: 1200mm X EP 400/3 X 6.0mm X 3.0mm X H X Cut Edge X Flat Belt
  // No separate fabric-code field: `br` (Belt Rating) already starts with it.
  const parts = [
    `${w}mm`,
    br,
    `${tc}mm`,
    `${bc}mm`,
    cg_clean,
    ec_clean,
    beltTypeStr,
  ].filter(Boolean);

  // Append breaker info: "BOT - N X BOB - M" (only include sides that are active)
  const botActive = document.getElementById('breaker-top')?.checked;
  const bobActive = document.getElementById('breaker-bottom')?.checked;
  if (botActive || bobActive) {
    const breakerParts = [];
    if (botActive) {
      const plies = +val('breaker-top-plies') || 1;
      breakerParts.push(`BOT - ${plies}`);
    }
    if (bobActive) {
      const plies = +val('breaker-bottom-plies') || 1;
      breakerParts.push(`BOB - ${plies}`);
    }
    parts.push(breakerParts.join(' X '));
  }

  set('belt-description', parts.join(' X '));
}

/**
 * Find the value of the <select id="selectId"> option whose visible text
 * exactly matches `text` (case-insensitive, trimmed). Returns null if none
 * of its real (non-placeholder) options match.
 */
function _findOptionValueByText(selectId, text) {
  const sel = document.getElementById(selectId);
  if (!sel || !text) return null;
  const needle = text.trim().toLowerCase();
  const opt = Array.from(sel.options).find(o => o.value && o.text.trim().toLowerCase() === needle);
  return opt ? opt.value : null;
}

// Bumped on every liveParseBeltDescription() call so an in-flight call whose
// await (loadBeltRatings' API fetch) resolves after a newer keystroke has
// already started a fresher call can detect it's stale and stop, instead of
// applying an out-of-date Fabric Type's belt ratings over a newer selection.
let _liveParseGen = 0;
// Avoids re-fetching belt ratings on every keystroke once Fabric Type has
// already resolved to the same value — only the fabric TOKEN changing
// (not the surrounding text) should trigger a new /belt-ratings request.
let _liveParseFabricType = null;

/**
 * Reverse of updateBeltDescription() — and using the SAME field order as the
 * Multiple Belts paste box's line format (see the inline
 * `_parseBeltText()` in this file, and its backend counterpart
 * django_backend/apps/api/routers/batch_views.py's text_import_batch), so a
 * user who already knows that format for pasting many belts doesn't have to
 * learn a second one for a single belt:
 *
 *   width X rating X top X bottom X grade X edge X end X type X length
 *     [X bot_plies X bob_plies [X carcass_mm]]
 *
 * No separate fabric field: `rating` (e.g. "EP 400/3") already starts with
 * the fabric code, so it's derived from rating's leading word instead.
 *
 * Example: 1200 X EP 400/3 X 6 X 3 X H X Cut X Open-End X Flat X 300
 *
 * Unlike the batch importer (which only parses once a full line is pasted),
 * this runs on every keystroke (see the 'input' listener in wireEvents) and
 * fills in whichever fields are already resolvable from the tokens typed so
 * far — it never clears a field just because the current text doesn't (yet)
 * match anything, so a field already filled correctly stays filled while the
 * rest of the line is still being typed.
 *
 * Fields that depend on another (Belt Rating needs Fabric Type's options
 * loaded first; Cover Grade needs Applicable Standard already selected) are
 * only matched once their dependency has actually resolved.
 */
async function liveParseBeltDescription() {
  const myGen = ++_liveParseGen;
  const raw = val('belt-description');
  if (!raw.trim()) return;

  // Same delimiter as _parseBeltText(): " X " with real spaces on both
  // sides, so an "X" that's part of a word (e.g. a customer name) never
  // splits a field in half.
  const tokens = raw.split(/\s+X\s+/i).map(t => t.trim());
  const [
    width, rating, top, bottom, grade, edge, endType, beltType,
    length, botPlies, bobPlies, carcassMm,
  ] = tokens;
  // Fabric code is rating's leading word (e.g. "EP 400/3" → "EP") - rating_name
  // always starts with it (see BeltRating.rating_name's DB format).
  const fabric = rating ? rating.trim().split(/\s+/)[0] : '';

  const isNum = (s) => s != null && s !== '' && !isNaN(parseFloat(s));

  // ── Width / Top / Bottom / Length — plain numbers, safe to fill live ────
  if (isNum(width))  set('belt-width-mm',  width);
  if (isNum(top))    set('top-cover-mm',   top);
  if (isNum(bottom)) set('bottom-cover-mm', bottom);
  if (isNum(length)) set('belt-length-m',  length);

  // ── Fabric Type — must resolve before Belt Rating (ratings are per-fabric) ─
  if (fabric) {
    const ftValue = _findOptionValueByText('fabric-type-id', fabric);
    if (ftValue && ftValue !== _liveParseFabricType) {
      _liveParseFabricType = ftValue;
      set('fabric-type-id', ftValue);
      await loadBeltRatings(ftValue);   // populates belt-rating-id + fabric-style-id
      if (myGen !== _liveParseGen) return;   // a newer keystroke has since run its own parse
    }
  }

  // ── Belt Rating (depends on Fabric Type's options, loaded just above) ────
  if (rating) {
    const brValue = _findOptionValueByText('belt-rating-id', rating);
    if (brValue) set('belt-rating-id', brValue);
  }

  // ── Cover Grade (depends on Applicable Standard already being selected) ──
  if (grade) {
    const cgValue = _findOptionValueByText('cover-grade-id', grade);
    if (cgValue) set('cover-grade-id', cgValue);
  }

  // ── Edge construction — same loose "contains cut/moulded" match the ─────
  //    batch importer and its backend counterpart both use.
  if (edge) {
    if (/cut/i.test(edge))          set('edge-construction', 'Cut Edge');
    else if (/mould/i.test(edge))   set('edge-construction', 'Moulded Edge');
  }

  // ── End type / construction — same loose "contains endless" match ───────
  if (endType) {
    set('construction-type', /endless/i.test(endType) ? 'Endless' : 'Open-End');
    _enforceEndlessMax();
  }

  // ── Belt Type ─────────────────────────────────────────────────────────────
  if (beltType) {
    const btValue = _findOptionValueByText('belt-type-id', beltType);
    if (btValue) set('belt-type-id', btValue);
  }

  // ── Breaker plies — 0/blank means that breaker is off, matching the ─────
  //    batch importer's _int_ply()/backend convention.
  if (botPlies != null && botPlies !== '') {
    const n = parseInt(botPlies, 10);
    if (n > 0) {
      document.querySelector('input[name="brk-top"][value="yes"]').checked = true;
      window._brkTop(true);
      set('breaker-top-plies', n);
    } else if (n === 0) {
      document.querySelector('input[name="brk-top"][value="no"]').checked = true;
      window._brkTop(false);
    }
  }
  if (bobPlies != null && bobPlies !== '') {
    const n = parseInt(bobPlies, 10);
    if (n > 0) {
      document.querySelector('input[name="brk-bot"][value="yes"]').checked = true;
      window._brkBot(true);
      set('breaker-bottom-plies', n);
    } else if (n === 0) {
      document.querySelector('input[name="brk-bot"][value="no"]').checked = true;
      window._brkBot(false);
    }
  }

  // ── Optional per-belt carcass override (field 13) ────────────────────────
  if (isNum(carcassMm)) {
    const toggle = document.getElementById('carcass-override-toggle');
    if (toggle && !toggle.checked) { toggle.checked = true; toggle.dispatchEvent(new Event('change')); }
    set('carcass-thickness-mm', carcassMm);
  }

  if (myGen !== _liveParseGen) return;

  // ── Recompute everything downstream, same as picking each field by hand ──
  recalcTotal();
  fetchDimensionalSpecs();
  if (val('cover-grade-id') && val('belt-rating-id')) await runLookup();
}

/* ══════════════════════════════════════════════════════════
   WEIGHT CALCULATIONS
══════════════════════════════════════════════════════════ */
/* ── International logistics helpers ─────────────────────────────────── */
/**
 * Check whether the currently selected Purpose is "International".
 * Used to show/hide the shipping region + container type row and
 * to apply container dimension constraints during packing calculation.
 *
 * @returns {boolean} true if the selected purpose text contains "international"
 */
function _isInternational() {
  // purpose option text contains 'International'
  const sel = document.getElementById('purpose-id');
  if (!sel) return false;
  const opt = sel.options[sel.selectedIndex];
  return opt && opt.text.toLowerCase().includes('international');
}

/**
 * Live shipping-constraint cache. Container height/width/weight limits used
 * to be hardcoded here in CONTAINER_TYPES / REGION_WEIGHT_LIMITS, duplicating
 * the container_types / region_container_weight_limits tables — if an admin
 * ever changed a limit in the database, this file would silently keep using
 * the old number. Now every value comes from GET /api/shipping-constraints
 * (backed by the same apps.services.calculations.get_container_constraints()
 * the backend itself would use), fetched whenever the region or container
 * type changes, and cached here only for the exact (containerTypeId, region)
 * pair that was actually fetched.
 */
let _shippingConstraintsCache = { key: null, data: null };

/**
 * Synchronous read of the cache — returns the fetched constraints ONLY if
 * they match the currently selected container type + region, else null.
 * Never guesses or falls back to a hardcoded number.
 *
 * @returns {{ max_height_m: number, max_width_m: number, max_gross_weight_kg: number } | null}
 */
function _getCachedShippingConstraints() {
  const ctId   = parseInt(val('container-type-id'), 10) || 0;
  const region = val('shipping-region') || '';
  if (!ctId || !region) return null;
  const key = `${ctId}|${region}`;
  return _shippingConstraintsCache.key === key ? _shippingConstraintsCache.data : null;
}

/**
 * Fetch the live shipping constraints for the currently selected container
 * type + region from the backend, cache them, then refresh the constraint-info
 * chip and re-run the packing calculation so the international weight/width
 * warnings reflect the real (never hardcoded) limits.
 * Called whenever the region or container-type dropdown changes.
 */
async function _refreshShippingConstraints() {
  const ctId   = parseInt(val('container-type-id'), 10) || 0;
  const region = val('shipping-region') || '';
  const info = document.getElementById('intl-constraint-info');
  if (!ctId || !region) {
    _shippingConstraintsCache = { key: null, data: null };
    if (info) info.textContent = 'Select region & container to see limits';
    recalcPacking();
    return;
  }
  const key = `${ctId}|${region}`;
  if (info) info.textContent = 'Loading limits…';
  try {
    const c = await getShippingConstraints(ctId, region);
    _shippingConstraintsCache = { key, data: c };
    if (info) {
      info.textContent =
        `Max height: ${c.max_height_m} m | Max width: ${c.max_width_m} m | Max gross: ${c.max_gross_weight_kg} kg`;
    }
  } catch (err) {
    _shippingConstraintsCache = { key: null, data: null };
    if (info) info.textContent = 'Could not load container limits for this region.';
  }
  recalcPacking();
}

/**
 * Show or hide the international logistics fields (shipping region, container type)
 * based on the current purpose selection. Clears those fields when switching to Domestic
 * so stale international data doesn't accidentally get submitted.
 */
function _toggleIntlRow() {
  const row = document.getElementById('intl-logistics-row');
  if (!row) return;
  const intl = _isInternational();
  row.style.display = intl ? '' : 'none';
  if (!intl) {
    // Clear international fields when switching back to domestic
    set('shipping-region', '');
    set('container-type-id', '');
    document.getElementById('intl-weight-warning').style.display = 'none';
    _shippingConstraintsCache = { key: null, data: null };
  }
  recalcPacking();
}

/**
 * Enforce a maximum belt length of 100 m when the construction type is "Endless".
 * Endless belts are loops - they must fit inside the container during shipping,
 * so very long lengths are physically impossible. Shows a note to the user and
 * clamps the value down if they've already entered something too large.
 */
function _enforceEndlessMax() {
  const input  = document.getElementById('belt-length-m');
  const note   = document.getElementById('belt-length-note');
  const isEndless = (val('construction-type') || '').toLowerCase() === 'endless';
  if (isEndless) {
    input.max = 100;
    const v = parseFloat(input.value) || 0;
    if (note) note.style.display = 'inline';
    if (v > 100) {
      input.value = 100;
      recalcWeight();
      recalcPacking();
      updateBeltDescription();
    }
  } else {
    input.max = 2000;
    if (note) note.style.display = 'none';
  }
}

/**
 * Recompute total belt thickness = top cover + bottom cover + carcass thickness.
 * Updates the read-only #total-thickness-mm field, then cascades into
 * recalcWeight() (weight per metre) and updateBeltDescription().
 */
function recalcTotal() {
  const top     = parseFloat(val('top-cover-mm'))         || 0;
  const bottom  = parseFloat(val('bottom-cover-mm'))      || 0;
  const carcass = parseFloat(val('carcass-thickness-mm')) || 0;
  const total   = top + bottom + carcass;
  set('total-thickness-mm', total > 0 ? total.toFixed(1) : '');
  recalcWeight();
  updateBeltDescription();
}

/**
 * Compute belt weight per metre (net and gross) using specific gravity from the cover grade.
 * Formulas (from IS 1891 / standard references):
 *   Net weight/m  (kg/m) = SG × thickness_mm × (width_mm / 1000)
 *   Gross weight/m (kg/m) = SG × (thickness_mm + 0.5) × (width_mm / 1000)
 * The +0.5 mm on gross accounts for packaging wrapping material.
 * Updates all weight chips across the form and cascades into recalcPacking().
 * Does nothing if any of width, thickness, or specific gravity is missing.
 */
function recalcWeight() {
  const width  = parseFloat(val('belt-width-mm'))      || 0;
  const length = parseFloat(val('belt-length-m'))      || 0;
  const total  = parseFloat(val('total-thickness-mm')) || 0;
  const sg     = lookupData?.cover_grade?.specific_gravity || 0;

  const panel  = document.getElementById('weight-calc-panel');
  const lookup = document.getElementById('lookup-result');

  if (!width || !total || !sg) {
    if (panel)  panel.style.display  = 'none';
    return;
  }

  // Weight is a precise decimal figure, not rounded up to the nearest 0.5
  // (that rounding is only for physical roll dimensions - see roundUpHalf's
  // docstring). The per-metre chip and the total are both derived from the
  // same unrounded netPm/grossPm so "Net Wt/m" × length always reconciles
  // with the displayed total, and both match Packing & Logistics below.
  const netPm   = sg * total         * (width / 1000);
  const grossPm = sg * (total + 0.5) * (width / 1000);

  const netPmD   = netPm.toFixed(2);
  const grossPmD = grossPm.toFixed(2);
  const netTotD   = length ? (netPm   * length).toFixed(2) : '-';
  const grossTotD = length ? (grossPm * length).toFixed(2) : '-';

  // Update lookup chip strip
  setText('cv-weight-pm',    netPmD);
  setText('cv-gross-pm',     grossPmD);
  setText('cv-total-weight', netTotD);
  setText('cv-total-gross',  grossTotD);
  if (lookup) lookup.style.display = 'block';

  const formula = `Net: ${sg} × ${total} × ${(width/1000).toFixed(3)} = ${netPmD} kg/m`
                + `  |  Gross: ${sg} × ${(total+0.5)} × ${(width/1000).toFixed(3)} = ${grossPmD} kg/m`;
  setText('cv-weight-formula', formula);

  // Update weight chips in belt config section
  setText('cv-weight-pm-2',    netPmD);
  setText('cv-gross-pm-2',     grossPmD);
  setText('cv-total-weight-2', netTotD);
  setText('cv-total-gross-2',  grossTotD);
  if (panel) panel.style.display = 'block';

  recalcPacking();
}

/* ══════════════════════════════════════════════════════════
   REEL DIAMETER
══════════════════════════════════════════════════════════ */
/**
 * Round up to the nearest 0.5 (e.g. 1.2 → 1.5, 1.21 → 1.5, 1.7 → 2.0).
 * Apply to all physical display values (lengths, diameters, weights).
 */
function roundUpHalf(v) {
  return Math.ceil(v * 2) / 2;
}

/**
 * Compute the outer reel diameter D (metres) for a wound belt roll.
 * Three formula variants depending on reel geometry:
 *
 *   circular  (default): D = sqrt(4/π × d_m × L + k²)
 *   twin      (two drums): D = sqrt(4/π × d_m × L/2 + k²)   [belt splits across two drums]
 *   elliptical:           D = sqrt(4/π × d_m × L + term²) − 2l/π   where term = k + 2l/π
 *
 * Variables:
 *   d_m = belt cross-section area equivalent thickness in metres = thickness_mm / 1000
 *   L   = belt length in metres
 *   k   = reel core diameter in metres (from reel_types.core_diameter_m)
 *   l   = center-to-center distance in metres (elliptical only, from reel_types.center_to_center_m)
 *
 * @param {string} formulaKey  - 'circular', 'twin', or 'elliptical'
 * @param {number} d_m         - Effective belt thickness in metres (gross, incl. packaging allowance)
 * @param {number} beltLengthM - Total belt length in metres
 * @param {number} k           - Reel core diameter in metres
 * @param {number} l           - Center-to-center distance in metres (elliptical reels only)
 * @returns {number} Outer reel diameter D in metres
 */
function computeReelDiam(formulaKey, d_m, beltLengthM, k, l) {
  if (formulaKey === 'twin') {
    return Math.sqrt((4 / Math.PI) * d_m * (beltLengthM / 2) + k * k);
  } else if (formulaKey === 'elliptical') {
    const term = k + (2 * l / Math.PI);
    return Math.sqrt((4 / Math.PI) * d_m * beltLengthM + term * term) - (2 * l / Math.PI);
  }
  return Math.sqrt((4 / Math.PI) * d_m * beltLengthM + k * k); // circular
}

/* ══════════════════════════════════════════════════════════
   PACKING PREVIEW
══════════════════════════════════════════════════════════ */
/**
 * Compute and display live packing estimates in the "Packing Preview" panel.
 * Mirrors the exact logic in backend/services/packing_service.py.
 *
 * Flow:
 *  1. Compute reel diameter D using computeReelDiam().
 *  2. If D > max allowed diameter (reel max OR container height for international):
 *       Back-calculate max length per roll → ceil to get num_rolls.
 *       Twin reels are always kept as multiples of 2.
 *  3. Compute net and gross weight for the full order.
 *  4. Show/hide warnings if container weight or width limits are exceeded.
 *  5. Pre-fill the packing form fields so the user doesn't have to enter them manually.
 *
 * Does nothing if reel type, thickness, belt length, or belt width is missing.
 */
function recalcPacking() {
  const panel      = document.getElementById('packing-calc-panel');
  const reelId     = val('reel-type-id');
  const totalThick = parseFloat(val('total-thickness-mm')) || 0;
  const beltLength = parseFloat(val('belt-length-m'))      || 0;
  const beltWidth  = parseInt(val('belt-width-mm'), 10)    || 0;
  const sg         = lookupData?.cover_grade?.specific_gravity || 0;

  if (!reelId || !totalThick || !beltLength || !beltWidth) {
    if (panel) panel.style.display = 'none';
    return;
  }

  const reel = allReelTypes.find(r => String(r.id) === reelId);
  if (!reel) { if (panel) panel.style.display = 'none'; return; }

  const k    = parseFloat(reel.core_diameter_m);
  const l    = reel.center_to_center_m ? parseFloat(reel.center_to_center_m) : 1.32;
  const base = parseInt(reel.num_rolls_base, 10);
  const d_m  = totalThick / 1000;   // per Formulae.md - +0.5 is gross weight only

  // International: cap reel diameter by container height; domestic: use reel's own max
  // (constraints come from the live /api/shipping-constraints fetch, cached in
  // _shippingConstraintsCache by _refreshShippingConstraints() — never hardcoded)
  const intlConstraints = _isInternational() ? _getCachedShippingConstraints() : null;
  const reelMaxD = parseFloat(reel.max_roll_diameter_m) || Infinity;
  const maxD = intlConstraints
    ? Math.min(reelMaxD, intlConstraints.max_height_m)
    : reelMaxD;
  // International: reel width must fit container width
  const reelWidthM = (beltWidth + 100) / 1000;
  const maxWidthM  = intlConstraints ? intlConstraints.max_width_m : Infinity;
  const widthOk    = reelWidthM <= maxWidthM;

  let D = computeReelDiam(reel.formula_key, d_m, beltLength, k, l);
  let numRolls, lenPerRoll;

  if (D > maxD) {
    if (reel.formula_key === 'twin') {
      const lPerSingle = (maxD * maxD - k * k) * Math.PI / (4 * d_m);
      const numPairs   = Math.ceil(beltLength / (2 * lPerSingle));
      numRolls   = numPairs * 2;
      lenPerRoll = beltLength / numRolls;
    } else if (reel.formula_key === 'elliptical') {
      const offset   = 2 * l / Math.PI;
      const innerD   = maxD + offset;
      const innerK   = k   + offset;
      const lPerRoll0 = (innerD * innerD - innerK * innerK) * Math.PI / (4 * d_m);
      numRolls   = Math.ceil(beltLength / lPerRoll0);
      lenPerRoll = beltLength / numRolls;
    } else {
      const lPerRoll0 = (maxD * maxD - k * k) * Math.PI / (4 * d_m);
      numRolls   = Math.ceil(beltLength / lPerRoll0);
      lenPerRoll = beltLength / numRolls;
    }
    D = maxD;
  } else {
    numRolls   = base;
    lenPerRoll = beltLength / base;
  }

  const rollH = roundUpHalf(D).toFixed(2);
  const rollW = roundUpHalf(reelWidthM).toFixed(2);

  // Same precise-decimal weight (no round-up-to-0.5) as recalcWeight() above,
  // so the Belt Specs and Packing & Logistics previews always show the same
  // net/gross figures for the same belt.
  const netPm   = sg > 0 ? sg * totalThick         * (beltWidth / 1000) : 0;
  const grossPm = sg > 0 ? sg * (totalThick + 0.5) * (beltWidth / 1000) : 0;
  const netWt   = netPm   > 0 ? (netPm   * beltLength).toFixed(2) : null;
  const grossWt = grossPm > 0 ? (grossPm * beltLength).toFixed(2) : null;

  setText('pc-d',     rollH);
  setText('pc-rolls', numRolls);
  setText('pc-lpr',   roundUpHalf(lenPerRoll).toFixed(2));
  setText('pc-dims',  `H: ${rollH} m × W: ${rollW} m`);
  setText('pc-net',   netWt   ?? '-');
  setText('pc-gross', grossWt ?? '-');

  // Populate the auto fields in packing row
  set('num-rolls',        String(numRolls));
  set('roll-dimensions',  `H: ${rollH} m × W: ${rollW} m`);
  set('length-per-roll',  roundUpHalf(lenPerRoll).toFixed(2));
  set('net-weight-kg',    netWt   ?? '');
  set('gross-weight-kg',  grossWt ?? '');

  // Auto-sync number of splice joints = number of rolls (each roll end needs a joint)
  if (document.getElementById('splicing-required')?.checked) {
    set('num-joints', String(numRolls));
    recalcSplicing();
  }

  if (panel) panel.style.display = 'block';

  // International: gross weight warning + width warning
  const weightWarning = document.getElementById('intl-weight-warning');
  if (intlConstraints && weightWarning) {
    const grossKg    = grossWt ? parseFloat(grossWt) : 0;
    const overWeight = grossKg > intlConstraints.max_gross_weight_kg;
    const overWidth  = !widthOk;
    if (overWeight || overWidth) {
      const msgs = [];
      if (overWeight) msgs.push(`Gross weight ${grossKg.toFixed(0)} kg exceeds container limit of ${intlConstraints.max_gross_weight_kg} kg`);
      if (overWidth)  msgs.push(`Reel width ${rollW} m exceeds container width limit of ${maxWidthM} m`);
      weightWarning.innerHTML = '⚠ ' + msgs.join(' | ');
      weightWarning.style.display = 'block';
    } else {
      weightWarning.style.display = 'none';
    }
  } else if (weightWarning) {
    weightWarning.style.display = 'none';
  }
}

/* ══════════════════════════════════════════════════════════
   SPLICING CALCULATION
══════════════════════════════════════════════════════════ */
/**
 * Look up the splice step length (mm) for a given rating per ply.
 * Uses allSplicingConfig.step_table, loaded from /api/bootstrap → splicing_config,
 * which reflects the live SpliceStepLookup DB table used by splicing_service.py.
 * Falls back to 400 mm if ratingPerPly exceeds all thresholds.
 *
 * NOTE: Do NOT hardcode the step table here — update via the DB/admin panel.
 * Both this function and splicing-calculator.html read the same API source
 * so they always match the values printed on the PDF.
 *
 * @param {number} ratingPerPly - kN/m per ply (e.g. 200 for EP 800/4)
 * @returns {number} Step length in mm (e.g. 250)
 */
function getSpliceStep(ratingPerPly) {
  for (const row of allSplicingConfig.step_table) {
    if (ratingPerPly <= row.max_fabric_rating_kn_m) return row.step_length_mm;
  }
  return 400;
}

/**
 * Compute and display splicing parameters in the Splicing section.
 * Formula per IS 14206 Part I : 1995:
 *   ratingPerPly = beltRating_kN_m ÷ numPlies
 *   stepLength   = getSpliceStep(ratingPerPly)
 *   spliceLength = round(0.3 × width_mm + stepLength × (plies − 1) + buffer)
 *   totalExtra_m = numJoints × spliceLength ÷ 1000
 * Buffer = 50 mm (hot vulcanisation) | 75 mm (cold vulcanisation).
 * Does nothing if the "Splicing Required" checkbox is unchecked,
 * or if any required input (plies, belt rating, width) is missing.
 */
function recalcSplicing() {
  if (!document.getElementById('splicing-required').checked) return;

  const numJoints  = parseInt(val('num-joints'), 10) || 0;
  const vulMethod  = (val('vulcanization-method') || 'Hot').toLowerCase();
  const beltWidth  = parseInt(val('belt-width-mm'), 10) || 0;
  const numPlies   = lookupData?.belt_rating?.num_plies || 0;

  // Use the kN/m value the SERVER already parsed from rating_name
  // (apps.services.calculations.parse_belt_rating, returned as rating_kn_m by
  // POST /api/tds/lookup) instead of re-parsing the display text here with a
  // separate regex — this used to be its own `ratingName.match(/(\d+)\//)`,
  // which could disagree with the backend's parser on an edge-case rating_name.
  const beltKn = lookupData?.belt_rating?.rating_kn_m || 0;

  if (!numPlies || !beltKn || !beltWidth) {
    set('sp-step-len-field',       '');
    set('sp-total-splice-field',   '');
    return;
  }

  const ratingPerPly = parseFloat((beltKn / numPlies).toFixed(2));
  const stepLen      = getSpliceStep(ratingPerPly);
  // Buffer from DB via allSplicingConfig (loaded at bootstrap); IS 14206 defaults as fallback.
  const buffer       = allSplicingConfig.buffers[vulMethod] ?? (vulMethod === 'cold' ? 75 : 50);
  // Plain nearest-integer rounding here, matching the backend EXACTLY
  // (apps.services.calculations.splice_length_mm uses Python's round(), not
  // "round up to the nearest 0.5" — that half-unit helper belongs to weight/
  // packing figures, not splice length. Using it here used to make this
  // preview disagree with what's actually saved and printed on the PDF,
  // sometimes by a lot once total_extra_length_m compounds it - e.g. a real
  // 4.656 m would preview as a rounded-up 5.0 m).
  const spliceLen    = Math.round(0.3 * beltWidth + stepLen * (numPlies - 1) + buffer);
  const totalExtra   = numJoints > 0
    ? Math.round(numJoints * spliceLen / 1000 * 1000) / 1000  // matches backend's round(x, 3)
    : null;

  // Populate the visible form fields
  set('sp-step-len-field',     stepLen.toFixed(2));
  set('sp-total-splice-field', totalExtra !== null ? totalExtra.toFixed(2) : '');

  // Also keep hidden span elements populated for JS compatibility
  setText('sp-rating-ply', ratingPerPly.toFixed(2));
  setText('sp-step-len',   stepLen.toFixed(2));
  setText('sp-splice-len', spliceLen.toFixed(2));
  setText('sp-total-extra', totalExtra !== null ? totalExtra.toFixed(2) : '-');
}

/* ══════════════════════════════════════════════════════════
   AUTOCOMPLETE / SEARCHABLE-LIST SHARED HELPERS
══════════════════════════════════════════════════════════ */
/**
 * Position a body-anchored dropdown list directly under an input, matching
 * its width. wireCustomerAutocomplete() and makeSearchable() each used to
 * carry their own copy of this exact getBoundingClientRect() math - extracted
 * here so there's one place to fix if the positioning ever needs to change
 * (e.g. flipping above the input near the bottom of the viewport).
 */
function positionDropdown(inputEl, listEl) {
  const r = inputEl.getBoundingClientRect();
  listEl.style.left  = r.left  + 'px';
  listEl.style.top   = r.bottom + 'px';
  listEl.style.width = r.width  + 'px';
}

/**
 * Rank a list of items against a query: items whose label STARTS WITH the
 * query sort above items that merely contain it, then alphabetically.
 * Both autocomplete implementations below had their own copy of this
 * comparator inlined in a .sort() call; consolidated here.
 * Does not mutate `items` - returns a new sorted array.
 * @param {Array} items
 * @param {string} query
 * @param {(item: any) => string} labelOf - returns the text to match/sort on
 */
function sortByQueryMatch(items, query, labelOf) {
  const q = (query || '').toLowerCase();
  // Rank tiers: 0 = name starts with query, 1 = some word starts with query
  // (e.g. "Alliance Fibres" for "f"), 2 = query merely appears mid-word.
  // Without the word-boundary tier, a query like "f" ranks names containing
  // an incidental "f" (e.g. "Infrastructure") above names where a whole word
  // actually starts with F, which reads as the search "not working".
  function tier(name) {
    if (name.startsWith(q)) return 0;
    if (name.split(/\s+/).some(word => word.startsWith(q))) return 1;
    return 2;
  }
  return items.slice().sort((a, b) => {
    const an = labelOf(a).toLowerCase();
    const bn = labelOf(b).toLowerCase();
    const at = tier(an);
    const bt = tier(bn);
    if (at !== bt) return at - bt;
    return an.localeCompare(bn);
  });
}

/**
 * Wire the two "close this dropdown" triggers shared by both autocomplete
 * widgets: a mousedown outside every element in `containers`, or any scroll
 * while the dropdown is open. Both implementations had their own copy of
 * these two document/window listeners; consolidated here so the outside-
 * click and close-on-scroll behaviour stays identical between them.
 * @param {Element[]} containers - elements a click INSIDE should not close
 * @param {() => boolean} isOpenFn - whether the dropdown is currently open
 *        (checked before the scroll handler closes it - mirrors each
 *        widget's original guard exactly)
 * @param {() => void} onClose - called when an outside interaction fires
 */
function wireDropdownAutoClose(containers, isOpenFn, onClose) {
  document.addEventListener('mousedown', (e) => {
    if (containers.some(el => el.contains(e.target))) return;
    onClose();
  });
  // capture:true is needed to see scroll events from elements that don't
  // bubble them (the page body, other ancestors) - but that also means this
  // fires for a scroll INSIDE the dropdown list itself (mouse wheel over the
  // options), since capture reaches window before the event target. Without
  // the containers check below, every wheel tick inside the list closed the
  // dropdown on the spot, making its own scrollbar effectively unusable.
  window.addEventListener('scroll', (e) => {
    if (!isOpenFn()) return;
    if (containers.some(el => el.contains(e.target))) return;
    onClose();
  }, true);
}

/* ══════════════════════════════════════════════════════════
   CUSTOMER AUTOCOMPLETE
══════════════════════════════════════════════════════════ */
/**
 * Set up the customer name autocomplete behaviour.
 * As the user types, filters the locally-cached allCustomers list and shows
 * matching options in a dropdown list. Also provides an "Add new customer" option.
 *
 * When an existing customer is selected: fills in their contact/application/location.
 * When "Add new customer" is chosen: reveals the new-customer mini-form.
 * Clicking anywhere outside the autocomplete wrapper closes the dropdown.
 */
function wireCustomerAutocomplete() {
  const inp     = document.getElementById('customer-search');
  const list    = document.getElementById('customer-list');
  const hidId   = document.getElementById('customer-id');
  const newForm = document.getElementById('new-customer-form');

  // Body-portal: move list to <body> so it escapes .section-card { overflow:hidden }
  document.body.appendChild(list);
  list.style.cssText = [
    'position:fixed',
    'z-index:9999',
    'display:none',
    'max-height:220px',
    'overflow-y:auto',
    'background:var(--bg-card)',
    'border:1px solid #F5A623',
    'border-top:none',
    'box-shadow:var(--shadow)',
    'font-size:12.5px',
  ].join(';');

  function openList()  { positionDropdown(inp, list); list.style.display = 'block'; }
  function closeList() { list.style.display = 'none'; }

  let debounceTimer = null;
  let requestSeq = 0;

  inp.addEventListener('input', () => {
    const q = inp.value.toLowerCase().trim();
    hidId.value = '';
    clearCustomerDetailFields();
    if (!q) { closeList(); if (debounceTimer) clearTimeout(debounceTimer); return; }

    if (debounceTimer) clearTimeout(debounceTimer);
    const mySeq = ++requestSeq;
    debounceTimer = setTimeout(async () => {
      let matches = [];
      try {
        // The server now ranks matches (starts-with > word-starts-with >
        // mid-word contains) before truncating to `limit`, so it's safe to
        // ask for exactly what's displayed instead of over-fetching and
        // re-ranking client-side.
        matches = await searchCustomers(q, 10);
      } catch {
        // Fall back to whatever's cached locally if the search call fails.
        matches = allCustomers.filter(c => c.customer_name.toLowerCase().includes(q));
      }
      if (mySeq !== requestSeq) return; // a newer keystroke already superseded this request
      renderCustomerMatches(sortByQueryMatch(matches, q, c => c.customer_name).slice(0, 10));
    }, 200);
  });

  function renderCustomerMatches(matches) {
    list.innerHTML = matches.map(c => `
      <div class="autocomplete-item"
           data-id="${c.customer_id}" data-name="${escapeHtml(c.customer_name)}"
           data-contact="${escapeHtml(c.contact_person || '')}" data-application="${escapeHtml(c.application || '')}"
           data-location="${escapeHtml(c.plant_location || '')}">
        ${escapeHtml(c.customer_name)}
        <small>${escapeHtml([c.application, c.plant_location].filter(Boolean).join(' · ') || '')}</small>
      </div>`).join('') +
      `<div class="autocomplete-item new-customer" data-new="1">
         ➕ Add new customer: "<strong>${escapeHtml(inp.value)}</strong>"
       </div>`;
    openList();
  }

  list.addEventListener('click', (e) => {
    const item = e.target.closest('.autocomplete-item');
    if (!item) return;
    closeList();
    if (item.dataset.new) {
      hidId.value = '';
      document.getElementById('nc-name').value = inp.value;
      newForm.classList.add('open');
      inp.value = inp.value + ' (new)';
      clearCustomerDetailFields();
    } else {
      hidId.value = item.dataset.id;
      inp.value   = item.dataset.name;
      newForm.classList.remove('open');
      set('cust-contact',     item.dataset.contact);
      set('cust-application', item.dataset.application);
      set('cust-location',    item.dataset.location);
    }
  });

  wireDropdownAutoClose([inp, list], () => list.style.display !== 'none', closeList);
}

/**
 * BUG FIX: loadCoverGrades()/loadBeltRatings() below replace a wrapped
 * <select>'s options wholesale (e.g. "Select standard first" -> the real
 * cover grade list) once its data arrives — but makeSearchable()'s visible
 * text input only reads the select's first-option text ONCE, at wrap time,
 * for its placeholder. Without this, the box keeps showing a stale
 * "Select standard first" placeholder even after a standard IS selected and
 * real cover grades HAVE loaded — the field looks broken/unpopulated (and
 * looks like the fields "aren't wired together") even though clicking into
 * it reveals the real, selectable options underneath the whole time.
 * Call this right after reassigning a wrapped select's innerHTML.
 *
 * @param {string} selectId - HTML id of a <select> already wrapped by makeSearchable()
 */
function refreshSearchableDisplay(selectId) {
  const sel = document.getElementById(selectId);
  const inp = sel?.parentElement?.querySelector('.ss-input');
  if (!sel || !inp) return;
  const phOpt = sel.options[0];
  inp.placeholder = (phOpt && !phOpt.value) ? phOpt.text : '';
  const selectedOpt = sel.options[sel.selectedIndex];
  inp.value = (selectedOpt && selectedOpt.value) ? selectedOpt.text : '';
}

/**
 * Wrap a native <select> with a live-search text input + filtered dropdown.
 *
 * The dropdown list is appended to <body> and positioned with
 * getBoundingClientRect() so it is NEVER clipped by overflow:hidden on any
 * ancestor element (e.g. .section-card).
 *
 * The native <select> stays hidden so that:
 *   - val(id) / selectedText(id) / all existing .addEventListener('change')
 *     calls continue to work without any changes elsewhere.
 *   - Form validation, captureBeltSpec(), submitTDS() are unaffected.
 *
 * @param {string} selectId - HTML id of the <select> to wrap
 */
function makeSearchable(selectId) {
  const nativeSel = document.getElementById(selectId);
  if (!nativeSel || nativeSel.tagName !== 'SELECT') return;

  // ── Build wrapper (holds only the input; list goes on body) ──────────
  const wrap = document.createElement('div');
  wrap.className = 'ss-wrap';
  nativeSel.parentNode.insertBefore(wrap, nativeSel);
  wrap.appendChild(nativeSel);
  nativeSel.style.display = 'none';

  // ── Visible search input ──────────────────────────────────────────────
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = 'finp ss-input';
  inp.autocomplete = 'off';
  inp.spellcheck = false;
  const phOpt = nativeSel.options[0];
  if (phOpt && !phOpt.value) inp.placeholder = phOpt.text;
  // ACCESSIBILITY (fixed): the page's <label for="${selectId}"> still points
  // at the now-hidden native <select> — the actual visible, interactive
  // control (this input) had no accessible name at all (axe-core flagged
  // this as a critical "missing label" failure on every field this wraps).
  // Copying the existing label's text into aria-label gives the input the
  // same name a sighted user already reads next to it, with no visual or
  // behavioral change.
  const existingLabel = document.querySelector(`label[for="${selectId}"]`);
  if (existingLabel) inp.setAttribute('aria-label', existingLabel.textContent.trim());
  wrap.appendChild(inp);

  // ── Dropdown list - appended to <body> to escape overflow:hidden ──────
  const lst = document.createElement('div');
  lst.className = 'autocomplete-list ss-list';
  // Override the stylesheet's position:absolute with fixed so we can place
  // it anywhere in the viewport regardless of scroll position.
  lst.style.cssText = 'position:fixed;z-index:9999;display:none;max-height:220px;overflow-y:auto;';
  document.body.appendChild(lst);

  let isOpen = false;

  function openList()  { isOpen = true;  positionDropdown(inp, lst); lst.style.display = 'block'; }
  function closeList() { isOpen = false; lst.style.display = 'none'; }

  // ── Helpers ───────────────────────────────────────────────────────────
  const currentLabel = () => {
    const opt = nativeSel.options[nativeSel.selectedIndex];
    return (opt && opt.value) ? opt.text : '';
  };

  const syncDisplay = () => { inp.value = currentLabel(); };

  const getOpts = () => Array.from(nativeSel.options).filter(o => o.value !== '');

  function renderList(query) {
    const q = (query || '').toLowerCase().trim();
    let opts = getOpts();

    if (q) {
      opts = sortByQueryMatch(opts.filter(o => o.text.toLowerCase().includes(q)), q, o => o.text);
    }

    lst.innerHTML = opts.length
      ? opts.slice(0, 60).map(o => {
          const active = String(o.value) === String(nativeSel.value) ? ' ss-active' : '';
          return `<div class="autocomplete-item${active}" data-value="${o.value}" tabindex="-1">${o.text}</div>`;
        }).join('')
      : '<div class="autocomplete-item" style="color:var(--text-muted);cursor:default;">No matches</div>';

    openList();
  }

  function pickValue(value, label) {
    nativeSel.value = value;
    inp.value = label;
    closeList();
    nativeSel.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ── Input events ──────────────────────────────────────────────────────
  inp.addEventListener('focus', () => renderList(''));

  inp.addEventListener('input', () => renderList(inp.value));

  inp.addEventListener('mousedown', (e) => {
    if (isOpen) { e.preventDefault(); closeList(); syncDisplay(); }
  });

  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Escape')    { e.preventDefault(); closeList(); syncDisplay(); inp.blur(); return; }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!isOpen) renderList('');
      lst.querySelector('[data-value]')?.focus();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const first = lst.querySelector('[data-value]');
      if (first) pickValue(first.dataset.value, first.textContent.trim());
    }
  });

  // ── List events ───────────────────────────────────────────────────────
  lst.addEventListener('click', (e) => {
    const item = e.target.closest('[data-value]');
    if (item) pickValue(item.dataset.value, item.textContent.trim());
  });

  lst.addEventListener('keydown', (e) => {
    const cur = document.activeElement;
    if (e.key === 'Enter') {
      e.preventDefault();
      if (cur?.dataset?.value != null) pickValue(cur.dataset.value, cur.textContent.trim());
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = cur.nextElementSibling;
      if (next?.dataset?.value != null) next.focus();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = cur.previousElementSibling;
      if (prev?.dataset?.value != null) prev.focus(); else inp.focus();
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); closeList(); syncDisplay(); inp.focus(); }
  });

  // ── Close on outside click / scroll ─────────────────────────────────
  wireDropdownAutoClose([wrap, lst], () => isOpen, () => { closeList(); syncDisplay(); });

  // ── Watch for option changes (populateSelect, loadCoverGrades, etc.) ──
  new MutationObserver(syncDisplay).observe(nativeSel, { childList: true });

  // ── Register for programmatic sync via set() and autoSelect() ─────────
  _searchableSyncs[selectId] = syncDisplay;

  syncDisplay();
}

/**
 * Clear the customer contact/application/location fields.
 * Called when the customer search input is cleared or a new customer is being added.
 */
function clearCustomerDetailFields() {
  set('cust-contact', ''); set('cust-application', ''); set('cust-location', '');
}

/* ══════════════════════════════════════════════════════════
   DIMENSIONAL SPEC FETCH
══════════════════════════════════════════════════════════ */
/**
 * Fetch the dimensional tolerance specifications for the current standard + dimensions.
 * Results are stored in window._dimSpecs and used by the PDF preview step to display
 * the correct spec tolerance string for each dimensional parameter (e.g. belt width ±2 mm).
 * Called whenever the standard or belt width changes.
 * Silently ignores errors - dimensional specs are informational and non-blocking.
 */
async function fetchDimensionalSpecs() {
  const standardId  = val('standard-id');
  const beltWidthMm = val('belt-width-mm');
  if (!standardId || !beltWidthMm) return;

  try {
    const specs = await getDimensionalSpecs(+standardId, {
      belt_width_mm:        +beltWidthMm,
      top_cover_mm:         +val('top-cover-mm')          || undefined,
      bottom_cover_mm:      +val('bottom-cover-mm')       || undefined,
      carcass_thickness_mm: +val('carcass-thickness-mm')  || undefined,
      total_thickness_mm:   +val('total-thickness-mm')    || undefined,
    });
    window._dimSpecs = specs;   // { "1": { parameter_name, spec_value }, ... }
  } catch (e) {
    console.warn('dimensional-specs fetch failed:', e);
  }
}

/* ══════════════════════════════════════════════════════════
   BREAKER HELPERS (called from inline onchange)
══════════════════════════════════════════════════════════ */
window._brkTop = (show) => {
  document.getElementById('breaker-top-plies').style.display = show ? 'inline-block' : 'none';
  document.getElementById('breaker-top').checked = show;
  updateBeltDescription();
};
window._brkBot = (show) => {
  document.getElementById('breaker-bottom-plies').style.display = show ? 'inline-block' : 'none';
  document.getElementById('breaker-bottom').checked = show;
  updateBeltDescription();
};

/* ══════════════════════════════════════════════════════════
   WIRE EVENTS
══════════════════════════════════════════════════════════ */
/**
 * Attach all event listeners on the TDS form.
 * Called once during init() after dropdowns have loaded.
 * Groups listeners by feature:
 *   - Standard → reloads cover grades, resets lookup state
 *   - Fabric type → reloads belt ratings & fabric styles
 *   - Cover grade + belt rating → triggers EAV lookup
 *   - Cover/carcass thickness → recalculates total thickness and weight
 *   - Belt dimensions → recalculates packing and belt description
 *   - Purpose → shows/hides international logistics row
 *   - Splicing fields → recalculates splice lengths
 *   - Submit/draft buttons → validateForm + submitTDS
 */

function wireEvents() {
  wireCustomerAutocomplete();

  // Brand → re-filter Standard dropdown to that brand's standards only, then
  // reset everything downstream (standard, cover grade, lookup) since the
  // previous selections no longer apply to the newly chosen brand.
  document.getElementById('brand-id').addEventListener('change', (e) => {
    populateStandardsForBrand(e.target.value);
    document.getElementById('standard-id').dispatchEvent(new Event('change'));
  });

  // Standard → cover grades + re-fetch dimensional specs
  document.getElementById('standard-id').addEventListener('change', (e) => {
    loadCoverGrades(e.target.value);
    resetLookupState();
    fetchDimensionalSpecs();
  });

  // Fabric type → ratings + styles
  document.getElementById('fabric-type-id').addEventListener('change', (e) => {
    loadBeltRatings(e.target.value);
    resetLookupState();
  });

  // Cover grade or belt rating → lookup
  ['cover-grade-id', 'belt-rating-id'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', runLookup);
  });

  // Cover thickness → recalc + re-fetch dimensional specs
  ['top-cover-mm', 'bottom-cover-mm'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
      recalcTotal();
      fetchDimensionalSpecs();
    });
  });

  // Carcass override toggle
  document.getElementById('carcass-override-toggle').addEventListener('change', (e) => {
    const field = document.getElementById('carcass-thickness-mm');
    const hint  = document.getElementById('carcass-hint');
    if (e.target.checked) {
      field.removeAttribute('readonly');
      field.classList.remove('auto-field');
      hint.textContent = 'Override active - enter manually';
      hint.style.color = 'var(--gold-light)';
    } else {
      field.setAttribute('readonly', '');
      field.classList.add('auto-field');
      hint.textContent = 'Auto-filled · toggle to override';
      hint.style.color = '';
      const carcass = lookupData?.belt_rating?.carcass_thickness_mm;
      if (carcass != null) { set('carcass-thickness-mm', carcass); recalcTotal(); }
    }
  });

  // Belt description: always editable. Typing directly into it marks it
  // dirty (MANUAL) so live auto-fill stops overwriting it, and — the moment
  // Applicable Standard is already selected — reads the line back into
  // Cover Grade / Fabric Type / Belt Rating / dimensions / etc. live, on
  // every keystroke (see liveParseBeltDescription()). Clearing the field (or
  // it happening to match what auto-fill would've produced anyway) hands
  // control back to auto-fill. See updateBeltDescription() / _setBeltDescMode().
  document.getElementById('belt-description').addEventListener('input', (e) => {
    beltDescDirty = e.target.value.trim() !== '';
    if (beltDescDirty) {
      _setBeltDescMode(true);
      liveParseBeltDescription();
    } else {
      updateBeltDescription();
    }
  });

  document.getElementById('carcass-thickness-mm').addEventListener('input', () => {
    recalcTotal();
    fetchDimensionalSpecs();
  });

  // Belt width / length → weight + packing + belt desc + dimensional specs
  document.getElementById('belt-width-mm').addEventListener('input', () => {
    recalcWeight();
    recalcPacking();
    updateBeltDescription();
    recalcSplicing();
    fetchDimensionalSpecs();
  });
  document.getElementById('belt-length-m').addEventListener('input', () => {
    recalcWeight();
    recalcPacking();
    updateBeltDescription();
    recalcSplicing();
    _enforceEndlessMax();
  });

  // Construction type toggle: endless caps belt length at 100 m
  document.getElementById('construction-type').addEventListener('change', () => {
    _enforceEndlessMax();
  });

  // Purpose → show/hide international logistics row
  document.getElementById('purpose-id').addEventListener('change', _toggleIntlRow);

  // Region / container → fetch live limits from the DB, then rerun packing
  document.getElementById('shipping-region').addEventListener('change', _refreshShippingConstraints);
  document.getElementById('container-type-id').addEventListener('change', _refreshShippingConstraints);

  // Spec fields that affect belt description
  ['cover-grade-id', 'fabric-type-id', 'belt-rating-id', 'edge-construction',
   'construction-type', 'belt-type-id'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', updateBeltDescription);
  });
  // Breaker checkboxes and plies also affect belt description
  ['breaker-top-plies', 'breaker-bottom-plies'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', updateBeltDescription);
  });

  // Reel type → live packing preview
  document.getElementById('reel-type-id').addEventListener('change', recalcPacking);

  // Splicing toggle
  document.getElementById('splicing-required').addEventListener('change', (e) => {
    const on  = e.target.checked;
    document.getElementById('splicing-fields').style.display       = on ? 'block' : 'none';
    document.getElementById('splice-toggle-text').textContent      = on ? 'Yes' : 'No';
    document.getElementById('splice-toggle-text').style.color      = on
      ? '#fff' : 'rgba(255,255,255,.65)';
    if (on) {
      // Pre-fill joints from current num-rolls (1 joint per roll end)
      const rolls = val('num-rolls');
      if (rolls) set('num-joints', rolls);
      recalcSplicing();
    }
  });
  document.getElementById('vulcanization-method').addEventListener('change', recalcSplicing);
  document.getElementById('num-joints').addEventListener('input', recalcSplicing);

  // Manual override toggle
  document.getElementById('btn-toggle-override')?.addEventListener('click', togglePackingOverride);

  // Override fields - real-time display update + splicing sync
  document.getElementById('num-rolls-override')?.addEventListener('input', () => {
    const v = +document.getElementById('num-rolls-override').value || null;
    if (v !== null) {
      document.getElementById('pc-rolls').textContent = v;
      // Sync num-joints = num-rolls when splicing is on
      if (document.getElementById('splicing-required')?.checked) {
        set('num-joints', String(v));
        recalcSplicing();
      }
    }
    renderRollLengthInputs(v || 0);
  });
  document.getElementById('length-per-roll-override')?.addEventListener('input', () => {
    const v = parseFloat(document.getElementById('length-per-roll-override').value);
    if (!isNaN(v)) document.getElementById('pc-lpr').textContent = v.toFixed(2);
  });
  document.getElementById('net-weight-kg-override')?.addEventListener('input', () => {
    const v = parseFloat(document.getElementById('net-weight-kg-override').value);
    if (!isNaN(v)) document.getElementById('pc-net').textContent = v.toFixed(2);
  });
  document.getElementById('gross-weight-kg-override')?.addEventListener('input', () => {
    const v = parseFloat(document.getElementById('gross-weight-kg-override').value);
    if (!isNaN(v)) document.getElementById('pc-gross').textContent = v.toFixed(2);
  });

  // Footer buttons
  document.getElementById('btn-add-belt')?.addEventListener('click', addBeltToQueue);
  document.getElementById('btn-save-draft').addEventListener('click', () => submitTDS('draft'));
  document.getElementById('btn-preview-pdf').addEventListener('click', () => submitTDS('preview'));
}

/**
 * Reset all EAV-dependent computed values and chip displays back to their initial state.
 * Called when the user changes Standard or Fabric Type, invalidating any previous lookup result.
 * Hides the weight/packing panels since they depend on the now-invalid lookupData.
 */
function resetLookupState() {
  lookupData = null;
  document.getElementById('lookup-result').style.display = 'none';
  document.getElementById('grade-hint').textContent = '';
  setText('cv-plies',   '-');
  setText('cv-carcass', '-');
  setText('cv-skim',    '-');
  set('carcass-thickness-mm', '');
  set('total-thickness-mm',   '');
  set('cv-plies-field',  '');
  set('cv-skim-field',   '');
  beltDescDirty = false;
  _liveParseFabricType = null;   // force liveParseBeltDescription() to re-fetch next time
  _setBeltDescMode(false);
  set('belt-description', '');
  const panels = ['weight-calc-panel', 'packing-calc-panel'];
  panels.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
}

/* ══════════════════════════════════════════════════════════
   MULTI-BELT QUEUE
══════════════════════════════════════════════════════════ */

/**
 * Capture all belt-specific form fields into a plain object for the queue.
 * Shared customer/document fields (purpose, standard, customer, etc.) are
 * NOT captured here - they are added at submit time as sharedFields.
 */
function captureBeltSpec() {
  const numPlies   = parseInt(val('cv-plies-field'), 10) || 0;
  const splicingOn = document.getElementById('splicing-required').checked;
  return {
    belt_description:     val('belt-description').trim() || null,
    belt_length_m:        parseFloat(val('belt-length-m')),
    belt_width_mm:        +val('belt-width-mm'),
    edge_construction:    val('edge-construction'),
    construction_type:    val('construction-type') || 'Open-End',
    cover_grade_id:       +val('cover-grade-id'),
    fabric_type_id:       +val('fabric-type-id'),
    fabric_style_id:      +val('fabric-style-id')   || null,
    make_of_fabric:       val('make-of-fabric')      || 'MIT',
    belt_rating_id:       +val('belt-rating-id'),
    num_plies:            numPlies,
    top_cover_mm:         parseFloat(val('top-cover-mm')),
    bottom_cover_mm:      parseFloat(val('bottom-cover-mm')),
    carcass_from_rating:  parseFloat(lookupData?.belt_rating?.carcass_thickness_mm ?? val('carcass-thickness-mm')),
    carcass_thickness_mm: parseFloat(val('carcass-thickness-mm')),
    breaker_top:          document.getElementById('breaker-top')?.checked    || false,
    breaker_top_plies:    +val('breaker-top-plies')    || null,
    breaker_bottom:       document.getElementById('breaker-bottom')?.checked || false,
    breaker_bottom_plies: +val('breaker-bottom-plies') || null,
    reel_type_id:         +val('reel-type-id')    || null,
    packing_type_id:      +val('packing-type-id') || null,
    // Packing fields (num_rolls, length_per_roll_m, net/gross weight): only sent when
    // the user has explicitly typed into the "-override" inputs. The auto-computed
    // display fields (num-rolls, net-weight-kg, ...) are a live client-side PREVIEW
    // only and are deliberately NOT sent here - the backend always recomputes these
    // authoritatively server-side (packing_service.compute_packing()) unless an
    // explicit override is present, so Belt Specs and Packing & Logistics stay in
    // sync instead of silently trusting whatever the browser's preview happened to
    // compute.
    num_rolls:            +val('num-rolls-override') || null,
    roll_dimensions:      null,
    length_per_roll_m:    parseFloat(val('length-per-roll-override')) || null,
    roll_lengths_m:       _customRollLengthsOrNull(),
    net_weight_kg:        parseFloat(val('net-weight-kg-override'))   || null,
    gross_weight_kg:      parseFloat(val('gross-weight-kg-override')) || null,
    splicing_required:    splicingOn,
    vulcanization_method: splicingOn ? (val('vulcanization-method') || 'Hot') : null,
    num_joints:           splicingOn ? (+val('num-joints') || null) : null,
    _splicingOn:          splicingOn,   // internal - stripped before API call
  };
}

/**
 * Validate belt-specific required fields only (no customer/document check).
 * @returns {string[]} Array of error messages; empty = valid.
 */
function validateBeltSpec() {
  const errors = [];
  if (!val('cover-grade-id'))       errors.push('Cover Grade is required.');
  if (!val('fabric-type-id'))       errors.push('Fabric Type is required.');
  if (!val('belt-rating-id'))       errors.push('Belt Rating is required.');
  if (!val('top-cover-mm'))         errors.push('Top Cover thickness is required.');
  if (!val('bottom-cover-mm'))      errors.push('Bottom Cover thickness is required.');
  if (!val('carcass-thickness-mm')) errors.push('Carcass Thickness not loaded - select Belt Rating first.');
  if (!val('belt-width-mm'))        errors.push('Belt Width is required.');
  if (!val('belt-length-m'))        errors.push('Belt Length is required.');
  if (!val('edge-construction'))    errors.push('Edge Construction is required.');
  if (document.getElementById('splicing-required').checked && !val('num-joints'))
    errors.push('Number of splice joints is required when splicing is enabled.');
  return errors;
}

/**
 * Validate current belt spec, push it onto beltQueue, then clear belt fields
 * so the user can fill the next belt's specification.
 */
function addBeltToQueue() {
  const errors = validateBeltSpec();
  const errEl  = document.getElementById('nav-error');
  if (errors.length) {
    errEl.textContent = '⚠ ' + errors[0];
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }
  errEl.textContent = '';

  beltQueue.push(captureBeltSpec());
  renderBeltQueue();
  clearBeltSpecFields();

  const num = beltQueue.length;
  showToast(`Belt ${num} added to queue. Fill Belt ${num + 1} specification below.`, 'success', 3000);
  document.getElementById('tds-form-wrap')?.querySelector('.section-card')
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/**
 * Remove a belt from the queue by index. Exposed on window for inline onclick.
 * @param {number} index - Zero-based index of the belt to remove
 */
function removeBeltFromQueue(index) {
  beltQueue.splice(index, 1);
  renderBeltQueue();
}
window.removeBeltFromQueue = removeBeltFromQueue;

/**
 * Reset all belt-specific fields (sections ②③④⑤) to their blank/default state
 * after a belt has been added to the queue.
 * Shared customer/document fields (section ①) are left untouched.
 */
function clearBeltSpecFields() {
  // ② Belt Specification
  set('fabric-type-id',  '');
  loadBeltRatings('');        // clears belt-rating-id and fabric-style-id dropdowns
  set('cover-grade-id',  '');
  set('top-cover-mm',    '');
  set('bottom-cover-mm', '');
  set('belt-width-mm',   '');
  set('make-of-fabric',  'MIT');

  // ③ Belt Configuration
  set('belt-length-m',    '');
  set('edge-construction', 'Cut Edge');
  const brkTopNo = document.querySelector('input[name="brk-top"][value="no"]');
  const brkBotNo = document.querySelector('input[name="brk-bot"][value="no"]');
  if (brkTopNo) { brkTopNo.checked = true; window._brkTop(false); }
  if (brkBotNo) { brkBotNo.checked = true; window._brkBot(false); }

  // ④ Packing
  set('reel-type-id',      '');
  set('packing-type-id',   '');
  set('num-rolls',         '');
  set('roll-dimensions',   '');
  set('net-weight-kg',     '');
  set('gross-weight-kg',   '');
  const overrideFields = document.getElementById('packing-override-fields');
  if (overrideFields) overrideFields.style.display = 'none';

  // ⑤ Splicing
  const splReq = document.getElementById('splicing-required');
  if (splReq) splReq.checked = false;
  const splFields = document.getElementById('splicing-fields');
  if (splFields) splFields.style.display = 'none';
  const splText = document.getElementById('splice-toggle-text');
  if (splText) { splText.textContent = 'No'; splText.style.color = ''; }
  set('num-joints', '');

  // Reset all EAV-computed values
  resetLookupState();
}

/**
 * Render the queued belt cards in #belt-queue-container.
 * Also updates the footer badge count and the "+ Add Belt" button label.
 */
function renderBeltQueue() {
  // Update footer badge
  const badge = document.getElementById('belt-queue-badge');
  if (badge) badge.style.display = beltQueue.length ? 'inline-block' : 'none';
  const countEl = document.getElementById('belt-queue-count');
  if (countEl) countEl.textContent = beltQueue.length;

  // Update add-belt button label
  const addBtn = document.getElementById('btn-add-belt');
  if (addBtn) {
    addBtn.textContent = beltQueue.length
      ? `+ Add Belt (${beltQueue.length + 1} total)`
      : '+ Add Belt';
  }

  // Render queue cards
  const container = document.getElementById('belt-queue-container');
  if (!container) return;
  if (beltQueue.length === 0) { container.innerHTML = ''; container.style.display = 'none'; return; }

  container.style.display = 'block';
  container.innerHTML = `
    <div class="section-card" style="border-color:var(--gold-border);">
      <div class="section-card-header" style="background:#1e3a5f;">
        <h3 style="color:#fff;">Queued Belts - ${beltQueue.length} ready</h3>
        <span class="sub" style="color:rgba(255,255,255,.7);">
          Fill the form below for Belt ${beltQueue.length + 1}
        </span>
      </div>
      <div class="section-card-body" style="padding:10px 14px;">
        ${beltQueue.map((b, i) => `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;
                      padding:8px 12px;background:var(--bg-section);border:1px solid var(--border);
                      border-radius:var(--radius);margin-bottom:6px;">
            <div style="min-width:0;flex:1;">
              <span style="font-family:var(--font-head);font-size:11px;font-weight:700;
                           color:var(--navy);white-space:nowrap;">Belt ${i + 1}</span>
              <span style="font-size:11px;color:var(--text-muted);margin-left:8px;">
                ${b.belt_description || `${b.belt_width_mm || '?'}mm × ${b.belt_length_m || '?'}m`}
              </span>
            </div>
            <button type="button" onclick="removeBeltFromQueue(${i})"
                    style="flex-shrink:0;padding:2px 8px;font-size:12px;background:transparent;
                           border:1px solid #dc2626;border-radius:3px;color:#dc2626;
                           cursor:pointer;font-weight:600;">× Remove</button>
          </div>
        `).join('')}
      </div>
    </div>`;
}

/**
 * Navigate to tds-multi-preview.html - same experience as single-belt tds-preview.html
 * but with belt tabs. Sidebar, inline row checkboxes, Download/Print all identical.
 * @param {Array} results - Array of TDSOut objects returned from createTDS calls
 */
// showMultiTDSSuccess — DEPRECATED / NO LONGER CALLED.
// Previously navigated to tds-multi-preview.html?tds_ids=... after N+1 individual
// createTDS calls (beltQueue path). tds-multi-preview.html only reads batch_id,
// so that navigation always failed with "No batch_id in the URL." The beltQueue
// path now uses createBatch() and navigates with batch_id instead.
// Kept here for reference; can be removed in a future cleanup.
function showMultiTDSSuccess(results) {
  const tdsIds  = results.map(r => r.tds_id).join(',');
  const tdsNums = results.map(r => r.tds_number).join(',');
  const qs = new URLSearchParams({ tds_ids: tdsIds, tds_numbers: tdsNums });
  showToast(`${results.length} TDS created! Opening preview…`, 'success', 1500);
  setTimeout(() => {
    window.location.href = `tds-multi-preview.html?${qs.toString()}`;
  }, 600);
}

/* ══════════════════════════════════════════════════════════
   PACKING OVERRIDE TOGGLE
══════════════════════════════════════════════════════════ */
/**
 * Toggle the visibility of the manual packing override fields.
 * Normally, packing fields (num_rolls, roll_dimensions, weights) are auto-filled
 * by the packing calculator. This override lets the user manually correct them
 * if the calculated values need adjustment (e.g. non-standard reel from the factory).
 */
function togglePackingOverride() {
  const fields = document.getElementById('packing-override-fields');
  const btn    = document.getElementById('btn-toggle-override');
  const open   = fields.style.display !== 'none';
  fields.style.display = open ? 'none' : 'block';
  btn.textContent      = open ? '✏ Manual Override' : '✕ Hide Override';

  // When opening: pre-fill override fields with current auto-computed values
  // so the user edits from the calculated baseline rather than from scratch,
  // and num-joints syncs immediately without the user needing to retype.
  if (!open) {
    const curRolls = val('num-rolls');
    const curLpr   = val('length-per-roll');
    const curNet   = val('net-weight-kg');
    const curGross = val('gross-weight-kg');

    if (curRolls) {
      const el = document.getElementById('num-rolls-override');
      if (el && !el.value) el.value = curRolls;
    }
    if (curLpr) {
      const el = document.getElementById('length-per-roll-override');
      if (el && !el.value) el.value = curLpr;
    }
    if (curNet) {
      const el = document.getElementById('net-weight-kg-override');
      if (el && !el.value) el.value = curNet;
    }
    if (curGross) {
      const el = document.getElementById('gross-weight-kg-override');
      if (el && !el.value) el.value = curGross;
    }

    // Sync num-joints from the pre-filled num-rolls value (if splicing is on)
    const numRollsOverride = +val('num-rolls-override') || +val('num-rolls') || 0;
    if (numRollsOverride && document.getElementById('splicing-required')?.checked) {
      set('num-joints', String(numRollsOverride));
      recalcSplicing();
    }

    renderRollLengthInputs(numRollsOverride);
  }
}

/* ══════════════════════════════════════════════════════════
   MANUAL OVERRIDE — UNEQUAL ROLL LENGTHS
   Lets the user split a belt into rolls of DIFFERENT lengths
   (e.g. 200m + 100m) instead of the auto-computed equal split, as long as
   every individual roll still fits the reel's max diameter. Mirrors
   apps/services/packing_service.py::validate_custom_roll_lengths() —
   keep the two in sync if the formulas ever change.
══════════════════════════════════════════════════════════ */

/**
 * Read the current per-roll length override inputs.
 * Returns NaN entries for any blank/invalid field.
 */
function getRollLengthsOverride() {
  const n = +val('num-rolls-override') || 0;
  if (n < 2) return { count: 0, values: [], allFilled: false, allEqual: true };
  const values = [];
  for (let i = 0; i < n; i++) {
    values.push(parseFloat(val(`roll-len-override-${i}`)));
  }
  const allFilled = values.every(v => !isNaN(v) && v > 0);
  const allEqual  = allFilled && values.every(v => Math.abs(v - values[0]) < 0.005);
  return { count: n, values, allFilled, allEqual };
}

/**
 * Payload value for roll_lengths_m: the custom per-roll array when the user
 * has filled in genuinely UNEQUAL lengths, otherwise null (falls back to the
 * existing uniform length_per_roll_m path — matches the backend's defensive
 * >1-distinct-value guard in pdf_service.py's belt_len_display).
 */
function _customRollLengthsOrNull() {
  const { allFilled, allEqual, values } = getRollLengthsOverride();
  return (allFilled && !allEqual) ? values : null;
}

/**
 * Build (or resize) the per-roll length input row inside the override panel.
 * Called whenever the override roll count changes. Preserves values already
 * typed for indices still in range; new rows prefill with an equal split of
 * the belt length so the user edits from a sane baseline.
 */
function renderRollLengthInputs(n) {
  const wrap   = document.getElementById('roll-lengths-override-wrap');
  const inputs = document.getElementById('roll-lengths-override-inputs');
  if (!wrap || !inputs) return;

  if (!n || n < 2) {
    wrap.style.display = 'none';
    inputs.innerHTML = '';
    return;
  }

  const beltLength = parseFloat(val('belt-length-m')) || 0;
  const equalSplit = beltLength > 0 ? beltLength / n : null;

  const existing = [];
  for (let i = 0; i < inputs.children.length; i++) {
    const el = inputs.children[i]?.querySelector('input');
    existing.push(el ? el.value : '');
  }

  inputs.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const prefill = existing[i] || (equalSplit !== null ? equalSplit.toFixed(2) : '');
    const div = document.createElement('div');
    div.innerHTML = `
      <label class="flbl" for="roll-len-override-${i}">Roll ${i + 1} Length (m)</label>
      <input type="number" id="roll-len-override-${i}" class="finp roll-len-override" min="0.5" step="0.5" value="${prefill}" />
    `;
    inputs.appendChild(div);
  }
  wrap.style.display = 'block';

  inputs.querySelectorAll('.roll-len-override').forEach(el => {
    el.addEventListener('input', checkRollLengths);
  });

  checkRollLengths();
}

/**
 * Live feedback on the per-roll length inputs: flags any individual roll
 * that would exceed the reel's max diameter (red border), and shows whether
 * the entered lengths sum to the belt's total length.
 */
function checkRollLengths() {
  const badge = document.getElementById('roll-lengths-sum-check');
  const { count, values, allFilled } = getRollLengthsOverride();
  if (!badge || count < 2) return;

  const beltLength = parseFloat(val('belt-length-m')) || 0;
  const reelId     = val('reel-type-id');
  const reel       = allReelTypes.find(r => String(r.id) === reelId);
  const totalThick = parseFloat(val('total-thickness-mm')) || 0;
  const d_m        = totalThick / 1000;
  const k          = reel ? parseFloat(reel.core_diameter_m) : 0;
  const l          = reel && reel.center_to_center_m ? parseFloat(reel.center_to_center_m) : 1.32;
  const maxD       = reel ? (parseFloat(reel.max_roll_diameter_m) || Infinity) : Infinity;
  // Twin rolls behave as independent circular spools once given as a literal
  // per-roll length — see the backend function's docstring for why the twin
  // formula (which halves the length again) is wrong here.
  const diamFormula = (reel && reel.formula_key === 'twin') ? 'circular' : (reel?.formula_key || 'circular');

  document.querySelectorAll('.roll-len-override').forEach(el => {
    const v = parseFloat(el.value);
    if (isNaN(v) || v <= 0 || !reel || !d_m) { el.style.borderColor = ''; el.title = ''; return; }
    const D = computeReelDiam(diamFormula, d_m, v, k, l);
    if (D > maxD) {
      el.style.borderColor = '#e53935';
      el.title = `This roll would need a ${D.toFixed(2)}m diameter, exceeding the reel's ${maxD.toFixed(2)}m maximum.`;
    } else {
      el.style.borderColor = '';
      el.title = '';
    }
  });

  if (!allFilled) {
    badge.textContent = '';
    return;
  }
  const sum  = values.reduce((a, b) => a + b, 0);
  const diff = sum - beltLength;
  if (Math.abs(diff) <= 0.01) {
    badge.textContent = '✓ matches belt length';
    badge.style.color = '#2e7d32';
  } else {
    badge.textContent = `✕ sums to ${sum.toFixed(2)} m, belt is ${beltLength.toFixed(2)} m`;
    badge.style.color = '#e53935';
  }
}

/* ══════════════════════════════════════════════════════════
   VALIDATION
══════════════════════════════════════════════════════════ */
/**
 * Validate all required form fields before submitting.
 * Checks: Purpose, Belt Type, Standard, Brand, Customer, Cover Grade,
 *         Fabric Type, Belt Rating, Top/Bottom Cover, Carcass Thickness,
 *         Belt Width, Belt Length, Edge Construction,
 *         and number of joints if splicing is required.
 *
 * @returns {string[]} Array of human-readable error messages. Empty array = valid.
 */
function validateForm() {
  const errors = [];

  if (!val('purpose-id'))              errors.push('Purpose is required.');
  if (!val('belt-type-id'))            errors.push('Belt Type is required.');
  if (!val('standard-id'))             errors.push('Standard is required.');
  if (!val('brand-id'))                errors.push('Brand is required.');

  const customerId = val('customer-id');
  const newForm    = document.getElementById('new-customer-form');
  // Valid states: an existing customer is selected (customerId set),
  // OR the "Add new customer" mini-form is open (newForm has 'open' class).
  // Typing in the search box WITHOUT selecting from the dropdown is not enough -
  // the customer would never be saved to the DB.
  if (!customerId && !newForm.classList.contains('open'))
    errors.push('Please select an existing customer or click "➕ Add new customer" from the dropdown.');
  if (newForm.classList.contains('open') && !val('nc-name').trim())
    errors.push('New customer company name is required.');

  if (!val('cover-grade-id'))         errors.push('Cover Grade is required.');
  if (!val('fabric-type-id'))         errors.push('Fabric Type is required.');
  if (!val('belt-rating-id'))         errors.push('Belt Rating is required.');
  if (!val('top-cover-mm'))           errors.push('Top Cover thickness is required.');
  if (!val('bottom-cover-mm'))        errors.push('Bottom Cover thickness is required.');
  if (!val('carcass-thickness-mm'))   errors.push('Carcass Thickness not loaded - select Belt Rating first.');

  if (!val('belt-width-mm'))          errors.push('Belt Width is required.');
  if (!val('belt-length-m'))          errors.push('Belt Length is required.');
  if (!val('edge-construction'))      errors.push('Edge Construction is required.');

  if (document.getElementById('splicing-required').checked && !val('num-joints'))
    errors.push('Number of splice joints is required when splicing is enabled.');

  const rollLengths = getRollLengthsOverride();
  if (rollLengths.count >= 2) {
    if (!rollLengths.allFilled) {
      errors.push('All individual roll lengths must be filled in (or clear the roll count override to use an equal split).');
    } else {
      const sum      = rollLengths.values.reduce((a, b) => a + b, 0);
      const beltLen  = parseFloat(val('belt-length-m')) || 0;
      if (Math.abs(sum - beltLen) > 0.01) {
        errors.push(`Individual roll lengths sum to ${sum.toFixed(2)} m, which must equal the belt length of ${beltLen.toFixed(2)} m.`);
      }
    }
  }

  return errors;
}

/* ══════════════════════════════════════════════════════════
   PDF OPTIONS BUILDER
══════════════════════════════════════════════════════════ */
/**
 * Build the PDF customisation panel - a checklist of parameter groups and individual
 * parameters the user can include or exclude from the printed TDS.
 * Renders one group checkbox per section, with individual parameter checkboxes indented below.
 * Checking/unchecking a group disables all its child parameter checkboxes.
 *
 * @param {boolean} splicingRequired - If false, the "Splicing Parameters" group is omitted.
 */
function buildPdfGroupOptions(splicingRequired) {
  const container = document.getElementById('pdf-group-options');
  const groups    = splicingRequired
    ? ALL_PARAM_GROUPS
    : ALL_PARAM_GROUPS.filter(g => g !== 'Splicing Parameters');
  const hasParams = allParameters && Object.keys(allParameters).length > 0;

  container.innerHTML = groups.map(g => {
    const params    = hasParams ? (allParameters[g] || []) : [];
    const paramsHtml = params.map(p => `
      <label style="display:flex;align-items:center;gap:6px;font-size:11px;
                    cursor:pointer;padding:2px 0;color:var(--text-muted);">
        <input type="checkbox" class="pdf-param-cb" value="${p.parameter_id}" data-group="${g}" checked />
        ${p.parameter_name}
      </label>`).join('');
    return `
      <div style="margin-bottom:6px;">
        <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;padding:3px 0;font-weight:600;">
          <input type="checkbox" class="pdf-group-cb" value="${g}" checked /> ${g}
        </label>
        ${params.length ? `<div class="pdf-param-list" data-group="${g}"
          style="padding-left:20px;border-left:2px solid var(--border);margin-left:6px;margin-top:2px;">
          ${paramsHtml}</div>` : ''}
      </div>`;
  }).join('');

  container.querySelectorAll('.pdf-group-cb').forEach(groupCb => {
    groupCb.addEventListener('change', () => {
      const paramList = container.querySelector(
        `.pdf-param-list[data-group="${groupCb.value.replace(/"/g, '\\"')}"]`
      );
      if (!paramList) return;
      paramList.querySelectorAll('.pdf-param-cb').forEach(cb => {
        cb.disabled = !groupCb.checked;
        cb.parentElement.style.opacity = groupCb.checked ? '1' : '0.4';
      });
    });
  });
}

/**
 * Read the current state of the PDF options panel and return a structured options object.
 * Used by loadPreview() and the download/print flow in tds-preview.html.
 *
 * @returns {{ excludeGroups: string[], excludeParams: number[], showSection: boolean, showTestMethod: boolean, showReference: boolean }}
 */
function getPdfOptions() {
  return {
    excludeGroups:  [...document.querySelectorAll('.pdf-group-cb:not(:checked)')].map(cb => cb.value),
    excludeParams:  [...document.querySelectorAll('.pdf-param-cb:not(:checked):not([disabled])')].map(cb => +cb.value),
    showSection:    document.getElementById('opt-show-section').checked,
    showTestMethod: document.getElementById('opt-show-testmethod').checked,
    showReference:  document.getElementById('opt-show-reference').checked,
  };
}

/* ══════════════════════════════════════════════════════════
   PREVIEW LOADER
══════════════════════════════════════════════════════════ */
/**
 * Build the PDF URL from current PDF options and load it into the embedded iframe.
 * Switches the view from the parameter-selection panel to the preview pane,
 * shows a loading spinner while the PDF renders (can take 1-3 seconds via WeasyPrint),
 * and scrolls the preview into view.
 *
 * @param {number} tdsId     - Database ID of the just-created TDS record
 * @param {string} tdsNumber - Human-readable TDS number (e.g. "0042") for display
 */
function loadPreview(tdsId, tdsNumber) {
  const opts    = getPdfOptions();
  const params  = new URLSearchParams();

  if (opts.excludeGroups.length)  params.set('exclude_groups',  opts.excludeGroups.join(','));
  if (opts.excludeParams.length)  params.set('exclude_params',  opts.excludeParams.join(','));
  if (!opts.showSection)          params.set('show_section',    'false');
  if (!opts.showTestMethod)       params.set('show_test_method','false');
  if (!opts.showReference)        params.set('show_reference',  'false');

  const pdfUrl = `/api/tds/${tdsId}/pdf?${params.toString()}`;

  const paramPanel  = document.getElementById('param-select-panel');
  const previewPane = document.getElementById('preview-panel');
  const loading     = document.getElementById('preview-loading');
  const iframe      = document.getElementById('tds-preview-iframe');

  // Hide selection panel, show preview
  paramPanel.style.display  = 'none';
  previewPane.style.display = 'block';
  loading.style.display     = 'block';
  iframe.style.display      = 'none';

  iframe.onload = () => {
    loading.style.display = 'none';
    iframe.style.display  = 'block';
  };
  iframe.src = pdfUrl;

  previewPane.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ══════════════════════════════════════════════════════════
   SUBMIT
══════════════════════════════════════════════════════════ */
/**
 * Validate the form, create (or update) the customer, then POST the TDS record to the API.
 * Handles two modes:
 *   'preview' - navigates to tds-preview.html (single belt) or tds-multi-preview.html
 *               (multi-belt queue) with sidebar column toggles and inline row checkboxes.
 *   'draft'   - saves and shows a toast; stays on the form page.
 *
 * Steps:
 *  1. Run validateForm() - abort with error banner if invalid.
 *  2. Create a new customer via POST /api/customers, or PATCH an existing one
 *     if the user updated their contact/application/location.
 *  3. Build the full TDS payload (matches TDSCreateIn schema in schemas.py).
 *  4. POST to /api/tds - server auto-computes thickness, weight, packing, and splicing.
 *  5a. Multi-belt: loop all queued belts + current, then navigate to tds-multi-preview.html.
 *  5b. Single-belt preview: navigate to tds-preview.html with tds_id + splicing flag.
 *  5c. Draft mode: show success toast, re-enable buttons.
 *
 * @param {'preview'|'draft'} [mode='preview'] - What to do after the TDS is created
 */
async function submitTDS(mode = 'preview') {
  const errors = validateForm();
  const errEl  = document.getElementById('nav-error');
  if (errors.length) {
    errEl.textContent = '⚠ ' + errors[0];
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }
  errEl.textContent = '';

  const saveDraftBtn  = document.getElementById('btn-save-draft');
  const previewBtn    = document.getElementById('btn-preview-pdf');
  const addBeltBtn    = document.getElementById('btn-add-belt');
  const submitText    = document.getElementById('submit-text');

  saveDraftBtn.disabled  = true;
  previewBtn.disabled    = true;
  if (addBeltBtn) addBeltBtn.disabled = true;
  submitText.textContent = beltQueue.length > 0
    ? `Generating ${beltQueue.length + 1} TDS…`
    : 'Generating…';

  // RACE FIX: `return` inside the try block below still runs `finally` before
  // actually returning, so on a successful submit the buttons were being
  // re-enabled immediately, up to ~800ms before the setTimeout()-delayed
  // navigation actually left the page — a fast double-click in that window
  // could fire a second create-TDS/create-batch POST. This flag lets `finally`
  // know a redirect is already scheduled, so it leaves the buttons disabled
  // instead (the page is about to unload anyway).
  let navigatingAway = false;

  try {
    // ── Step 1: Create or update customer ────────────────────────────────────
    // Case A - new customer typed by user: POST /api/customers
    // Case B - existing customer selected: PATCH /api/customers/{id} to update
    //          contact/application/location if the user filled them in
    let customerId = +val('customer-id') || null;
    const newForm  = document.getElementById('new-customer-form');
    if (newForm?.classList.contains('open')) {
      // Case A: brand-new customer
      // Note: contact/application/location use the shared cust-* fields
      // (nc-* only covers the company name; the detail fields below the
      //  autocomplete are shared between new and existing customer flows)
      const nc = await createCustomer({
        customer_name:  val('nc-name').trim(),
        contact_person: val('cust-contact').trim()     || null,
        application:    val('cust-application').trim() || null,
        plant_location: val('cust-location').trim()    || null,
      });
      customerId = nc.customer_id;
      allCustomers.push(nc); // keep in-memory cache up to date
    } else if (customerId) {
      // Case B: existing customer - patch contact/application/location if filled
      const contact  = val('cust-contact').trim()     || null;
      const applic   = val('cust-application').trim() || null;
      const location = val('cust-location').trim()    || null;
      if (contact || applic || location) {
        await updateCustomer(customerId, {
          contact_person: contact,
          application:    applic,
          plant_location: location,
        });
      }
    }

    // ── Step 2: Resolve derived values ────────────────────────────────────────
    const numPlies   = parseInt(val('cv-plies-field'), 10) || 0;
    const splicingOn = document.getElementById('splicing-required').checked;
    const isIntl     = _isInternational();

    // ── Step 3: Build TDS payload (matches TDSCreateIn schema in schemas.py) ──
    // Notes:
    //   - total_thickness_mm is server-computed (top + bottom + carcass)
    //   - interply_skim_mm is server-fetched from belt_rating_values (param_id=5)
    //   - step_length_mm / splice_length_mm / total_extra_length_m are server-computed
    const payload = {
      // Identification
      purpose_id:        +val('purpose-id'),
      belt_type_id:      +val('belt-type-id'),
      brand_id:          +val('brand-id'),
      standard_id:       +val('standard-id'),
      tds_doc_number:    val('tds-doc-number').trim() || null,
      construction_type: val('construction-type') || 'Open-End',

      // International logistics - only included when purpose = International
      // Validated server-side: both are required when purpose_id == 2
      shipping_region:    isIntl ? (val('shipping-region') || null) : null,
      container_type_id:  isIntl ? (+val('container-type-id') || null) : null,

      // Customer
      customer_id:       customerId,

      // Belt identity
      belt_description:  val('belt-description').trim() || null,
      belt_length_m:     parseFloat(val('belt-length-m')),
      belt_width_mm:     +val('belt-width-mm'),
      edge_construction: val('edge-construction'),

      // Spec selection
      cover_grade_id:    +val('cover-grade-id'),
      fabric_type_id:    +val('fabric-type-id'),
      fabric_style_id:   +val('fabric-style-id')  || null,
      make_of_fabric:    val('make-of-fabric')     || 'MIT',
      belt_rating_id:    +val('belt-rating-id'),

      // Construction dimensions
      // carcass_from_rating = the original rating default (for reference on the TDS)
      // carcass_thickness_mm = the final value (may differ if the user overrode it)
      num_plies:            numPlies,
      top_cover_mm:         parseFloat(val('top-cover-mm')),
      bottom_cover_mm:      parseFloat(val('bottom-cover-mm')),
      carcass_from_rating:  parseFloat(lookupData?.belt_rating?.carcass_thickness_mm ?? val('carcass-thickness-mm')),
      carcass_thickness_mm: parseFloat(val('carcass-thickness-mm')),

      // Optional breaker plies (reinforcement layers above/below carcass)
      breaker_top:          document.getElementById('breaker-top')?.checked    || false,
      breaker_top_plies:    +val('breaker-top-plies')    || null,
      breaker_bottom:       document.getElementById('breaker-bottom')?.checked || false,
      breaker_bottom_plies: +val('breaker-bottom-plies') || null,

      // Packing - reel/packing type for reference. num_rolls/length_per_roll_m/net &
      // gross weight are ONLY sent when the user has explicitly typed into the
      // "-override" inputs; otherwise they're left null so the server always runs
      // its own authoritative compute_packing() rather than trusting whatever this
      // page's live preview happened to compute (that mismatch was the root cause
      // of Belt Specs vs. Packing & Logistics showing different weights).
      reel_type_id:       +val('reel-type-id')    || null,
      packing_type_id:    +val('packing-type-id') || null,
      num_rolls:          +val('num-rolls-override') || null,
      roll_dimensions:    null,
      length_per_roll_m:  parseFloat(val('length-per-roll-override')) || null,
      roll_lengths_m:     _customRollLengthsOrNull(),
      net_weight_kg:      parseFloat(val('net-weight-kg-override'))   || null,
      gross_weight_kg:    parseFloat(val('gross-weight-kg-override')) || null,

      // Splicing - server computes step/splice length from the belt rating
      splicing_required:    splicingOn,
      vulcanization_method: splicingOn ? (val('vulcanization-method') || 'Hot') : null,
      num_joints:           splicingOn ? (+val('num-joints') || null) : null,
    };

    // ── Step 4: POST to API ────────────────────────────────────────────────────

    // Multi-belt: submit all queued belts + the current form as one atomic batch.
    // Uses POST /api/tds/batch/ (create_batch endpoint) so a real TDSBatch record
    // is created and the multi-preview page can load it via ?batch_id=.
    // Previously this called createTDS N+1 times (individual records with no
    // batch_id) and then navigated to tds-multi-preview.html?tds_ids=..., which
    // tds-multi-preview.html does not support — it only reads batch_id, so that
    // navigation always showed an error "No batch_id in the URL." Fixed by
    // switching to the batch endpoint and navigating with the returned batch_id.
    if (beltQueue.length > 0 && !editingTdsId) {
      const allBelts = [...beltQueue, captureBeltSpec()];
      const batchPayload = {
        shared: {
          purpose_id:           +val('purpose-id'),
          brand_id:             +val('brand-id'),
          standard_id:          +val('standard-id'),
          tds_doc_number:       val('tds-doc-number').trim() || null,
          make_of_fabric:       val('make-of-fabric') || 'MIT',
          splicing_required:    splicingOn,
          vulcanization_method: splicingOn ? (val('vulcanization-method') || 'Hot') : null,
          reel_type_id:         +val('reel-type-id')    || null,
          packing_type_id:      +val('packing-type-id') || null,
          shipping_region:      isIntl ? (val('shipping-region') || null) : null,
        },
        customer: { customer_id: customerId },
        belts: allBelts.map(b => {
          // Strip internal _splicingOn tracking key — not part of the API schema
          const { _splicingOn: _s, ...beltFields } = b;
          return {
            ...beltFields,
            // belt_type_id and container_type_id are shared-form fields, not
            // captured per-belt by captureBeltSpec(), so read them from the form.
            belt_type_id:      +val('belt-type-id'),
            container_type_id: isIntl ? (+val('container-type-id') || null) : null,
          };
        }),
      };

      const batchResult = await createBatch(batchPayload);
      const batchId     = batchResult?.batch?.batch_id;
      beltQueue = [];
      renderBeltQueue();

      if (mode === 'draft') {
        const savedCount = batchResult.count || batchResult.tds_records?.length || 0;
        showToast(`${savedCount} TDS saved as draft.`, 'success', 5000);
      } else {
        const createdCount = batchResult.count || batchResult.tds_records?.length || 0;
        showToast(`${createdCount} TDS created! Opening preview…`, 'success', 1500);
        navigatingAway = true;
        setTimeout(() => {
          window.location.href = 'tds-multi-preview.html?batch_id=' + batchId;
        }, 600);
      }
      return;
    }

    // Single-belt flow — update the existing record if we're in edit mode
    // (editingTdsId set from ?edit=<id> in init()), otherwise create new.
    const tds = editingTdsId
      ? await updateTDS(editingTdsId, payload)
      : await createTDS(payload);
    createdTdsId = tds.tds_id;

    // ── Step 5: Build the PDF options panel now that we know splicing status ───
    // (group checkboxes - Splicing Parameters only shown when splicing_required)
    buildPdfGroupOptions(splicingOn);

    // ── Step 6: Handle mode ───────────────────────────────────────────────────
    const verb = editingTdsId ? 'updated' : 'created';
    if (mode === 'preview') {
      // If this edit was opened from a batch preview (?batch_id=<id> passed
      // alongside ?edit=<tds_id>), go back into that batch instead of the
      // single-belt preview - otherwise the edit saves fine but the user is
      // dropped outside the batch entirely and it reads as "my change vanished."
      if (editingTdsId && editingBatchId) {
        showToast(`TDS-${tds.tds_number} ${verb}! Returning to batch…`, 'success', 2000);
        navigatingAway = true;
        setTimeout(() => {
          window.location.href = `tds-multi-preview.html?batch_id=${editingBatchId}`;
        }, 600);
        return;
      }
      // Navigate directly to the preview page (options sidebar is built in)
      showToast(`TDS-${tds.tds_number} ${verb}! Opening preview…`, 'success', 2000);
      navigatingAway = true;
      setTimeout(() => {
        const qs = new URLSearchParams({
          tds_id:         tds.tds_id,
          tds_number:     tds.tds_number,
          tds_doc_number: tds.tds_doc_number || '',
          splicing:       splicingOn ? 'true' : 'false',
        });
        window.location.href = `tds-preview.html?${qs.toString()}`;
      }, 800);
      return; // don't fall through to finally re-enabling buttons
    } else {
      // Draft save - just confirm, stay on form
      showToast(`TDS-${tds.tds_number} ${editingTdsId ? 'updated' : 'saved as draft'}.`, 'success', 5000);
    }

  } catch (err) {
    // Display error in the sticky nav bar and scroll to top
    errEl.textContent = '⚠ ' + err.message;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    showToast('Error: ' + err.message, 'error', 8000);
  } finally {
    // Re-enable submit buttons on error or a stay-on-page success (draft
    // save). A navigate-away success (navigatingAway) leaves them disabled
    // until the redirect actually happens, closing the double-submit window.
    if (!navigatingAway) {
      saveDraftBtn.disabled  = false;
      previewBtn.disabled    = false;
      if (addBeltBtn) addBeltBtn.disabled = false;
      submitText.textContent = 'Generate & Preview PDF';
    }
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────
// ES modules are deferred by default so the DOM is ready when this executes.
// Only initialise if the user is authenticated (requireAuth redirects otherwise).
if (session) {
  init().catch(err => {
    console.error('TDS form init failed:', err);
    showToast('Page load error: ' + err.message, 'error');
  });
}
