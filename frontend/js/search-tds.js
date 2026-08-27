/**
 * search-tds.js - Search, filter, and manage TDS records on the Search TDS page.
 *
 * This module handles the full lifecycle of the search/list page:
 *   - Loading all TDS records from the API into the allRecords cache.
 *   - Client-side filtering by keyword, standard, and date range.
 *   - Rendering the results table with View / Download PDF actions per row.
 *   - Detail modal: a tab-based drawer showing full TDS, packing, and splicing info.
 *   - Delete confirmation overlay with double-confirmation before permanent deletion.
 *
 * Filtering is entirely client-side - all records are loaded once and filtered
 * locally for instant response without extra API calls.
 *
 * Imports:
 *   requireAuth, populateNavUser, showToast (from auth.js)
 *   listTDS, getTDS, deleteTDS, downloadPdf, getStandards (from api.js)
 */
import { requireAuth, populateNavUser, showToast } from './auth.js';
import { listTDS, getTDS, deleteTDS, downloadPdf, getStandards } from './api.js';

/**
 * Escape a value for safe insertion into an innerHTML template string.
 * SECURITY: every piece of TDS/customer/standard data rendered on this page
 * ultimately comes from user-entered form fields (customer name, plant
 * location, etc.) stored on the backend, so it must never be trusted as raw
 * HTML — a customer named e.g. `<img src=x onerror=...>` would otherwise
 * execute in every user's session who views this page. Always run dynamic
 * text through this before interpolating it into a template literal that
 * gets assigned to .innerHTML.
 */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Redirect to login if not authenticated
const session = requireAuth();
if (session) populateNavUser();

// Module-level state
let allRecords      = [];  // Full list of all TDS records from the API
let filteredRecs    = [];  // Subset after applying the current filter values
let activeModalId   = null; // tds_id currently open in the detail modal (null = closed)
let pendingDeleteId = null; // tds_id queued for deletion in the confirm overlay

/* ═══════════════════════════════════════════════════════════
   INIT
═══════════════════════════════════════════════════════════ */
/**
 * Entry point. Loads standards for the filter dropdown, fetches all TDS records,
 * then wires up filters, the detail modal, and the delete confirmation dialog.
 */
async function init() {
  await loadStandardsFilter();
  await loadRecords();
  wireFilters();
  wireModal();
  wireConfirmDelete();
  document.getElementById('btn-refresh').addEventListener('click', loadRecords);
}

/**
 * Populate the "Filter by Standard" dropdown with all available standards.
 * Non-critical - errors are silently swallowed so the page still loads.
 */
async function loadStandardsFilter() {
  try {
    const standards = await getStandards();
    const sel = document.getElementById('filter-standard');
    standards.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.standard_id;
      opt.textContent = s.standard_name.split(':')[0].trim();
      sel.appendChild(opt);
    });
  } catch { /* non-critical */ }
}

/**
 * Fetch all TDS records from GET /api/tds and store in allRecords.
 * Shows a loading spinner while fetching. On success, calls applyFilters() to render.
 * On error, replaces the table with an error state and shows a toast.
 */
async function loadRecords() {
  const wrap = document.getElementById('table-wrap');
  wrap.innerHTML = `<div class="loading-overlay"><div class="spinner spinner-lg"></div><span>Loading…</span></div>`;
  document.getElementById('results-count').textContent = '';
  try {
    allRecords = await listTDS();
    applyFilters();
  } catch (err) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <h3>Cannot reach backend</h3>
        <p>Make sure the backend is running<br><small>${escapeHtml(err.message)}</small></p>
      </div>`;
    showToast('Backend error: ' + err.message, 'error');
  }
}

/* ═══════════════════════════════════════════════════════════
   FILTERS
═══════════════════════════════════════════════════════════ */
/**
 * Attach 'input' and 'change' listeners to all filter controls so that
 * applyFilters() runs live as the user types or selects values.
 * Also wires the "Clear Filters" button to reset all filter inputs.
 */
function wireFilters() {
  ['filter-search','filter-standard','filter-from','filter-to'].forEach(id => {
    document.getElementById(id).addEventListener('input',  applyFilters);
    document.getElementById(id).addEventListener('change', applyFilters);
  });
  document.getElementById('btn-clear-filters').addEventListener('click', () => {
    ['filter-search','filter-standard','filter-from','filter-to'].forEach(id => {
      document.getElementById(id).value = '';
    });
    applyFilters();
  });
}

/**
 * Filter allRecords using the current filter control values and re-render the table.
 * Filters applied (all are ANDed together):
 *   - Text search: matches TDS number, customer name, standard name, or belt rating
 *   - Standard: must match the selected standard_id
 *   - Date range: tds_date must fall within from/to dates
 */
function applyFilters() {
  const q       = document.getElementById('filter-search').value.toLowerCase().trim();
  const stdId   = document.getElementById('filter-standard').value;
  const fromStr = document.getElementById('filter-from').value;
  const toStr   = document.getElementById('filter-to').value;

  filteredRecs = allRecords.filter(t => {
    if (q && !(
      t.tds_number.toLowerCase().includes(q) ||
      (t.customer?.customer_name||'').toLowerCase().includes(q) ||
      (t.standard?.standard_name||'').toLowerCase().includes(q) ||
      (t.belt_rating?.rating_name||'').toLowerCase().includes(q)
    )) return false;
    if (stdId && String(t.standard_id) !== stdId) return false;
    const d = new Date(t.tds_date);
    if (fromStr && d < new Date(fromStr)) return false;
    if (toStr   && d > new Date(toStr))   return false;
    return true;
  });
  renderTable();
}

/* ═══════════════════════════════════════════════════════════
   TABLE RENDER
═══════════════════════════════════════════════════════════ */
/**
 * Render the results table from the filteredRecs array.
 * Shows an empty-state message if there are no results.
 * Each row has "View" (opens detail modal) and "⬇ PDF" (downloads PDF) buttons.
 */
function renderTable() {
  const count = filteredRecs.length;
  const total = allRecords.length;
  document.getElementById('results-count').innerHTML =
    `Showing <strong>${count}</strong> of ${total} records`;

  const wrap = document.getElementById('table-wrap');
  if (!filteredRecs.length) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔍</div>
        <h3>${allRecords.length ? 'No results match your filters' : 'No TDS records yet'}</h3>
        <p>${allRecords.length ? 'Try adjusting the filters above'
            : 'Click "New TDS" to generate your first Technical Data Sheet'}</p>
      </div>`;
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>TDS Number</th>
          <th>Date</th>
          <th>Customer</th>
          <th>Standard</th>
          <th>Belt Rating</th>
          <th>Width × Length</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${filteredRecs.map(t => `
          <tr>
            <td class="td-mono">${escapeHtml(t.tds_number)}</td>
            <td class="td-muted">${new Date(t.tds_date).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})}</td>
            <td>${t.customer?.customer_name ? escapeHtml(t.customer.customer_name) : '<span class="td-muted">-</span>'}</td>
            <td><span class="badge badge-muted" style="font-size:9px;">${escapeHtml(stdShort(t.standard?.standard_name))}</span></td>
            <td class="td-muted" style="font-size:11px;">${escapeHtml(t.belt_rating?.rating_name) || '-'}</td>
            <td class="td-muted" style="font-size:12px;">${t.belt_width_mm} mm × ${parseFloat(t.belt_length_m).toFixed(0)} m</td>
            <td>
              <div class="table-actions">
                <button class="btn btn-ghost btn-sm" data-action="view" data-id="${t.tds_id}">View</button>
                <button class="btn btn-outline btn-sm" data-action="pdf"
                        data-id="${t.tds_id}" data-num="${escapeHtml(t.tds_number)}">⬇ PDF</button>
              </div>
            </td>
          </tr>`).join('')}
      </tbody>
    </table>`;

  wrap.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', handleRowAction);
  });
}

/**
 * Shorten a full standard name to a compact badge label.
 * e.g. "IS 1891 (Part 1) : 1994" → "IS 1891", "ISO 14890 : 2013" → "ISO 14890".
 *
 * @param {string|null} name - Full standard name from the API
 * @returns {string} Shortened label or '-' if name is falsy
 */
function stdShort(name) {
  if (!name) return '-';
  return name.replace('IS 1891 (Part 1)','IS 1891').split(':')[0].trim();
}

// Groups a "Customer Copy" download omits. Mirrors tds-preview.html's
// DEFAULT_UNCHECKED_GROUPS and apps/services/sections.py's
// CUSTOMER_COPY_EXCLUDE_GROUPS on the backend - keep all three in sync. Only
// affects what a generated PDF includes, never the stored TDSInput record.
const CUSTOMER_COPY_EXCLUDE_GROUPS = [
  'Fabric Parameters',
  'Sampling and Testing',
  'Packing and Logistics',
  'Splicing Parameters',
];

/**
 * Read the page-level "Customer Copy / Internal Copy" toggle (results-header)
 * and return the excludeGroups array to pass into downloadPdf()/getPdfUrl().
 */
function getSelectedExcludeGroups() {
  const copyType = document.querySelector('input[name="copy-type"]:checked')?.value || 'customer';
  return copyType === 'customer' ? CUSTOMER_COPY_EXCLUDE_GROUPS : [];
}

/**
 * Dispatch a row button click to the correct handler (view or pdf download).
 * Uses data attributes (data-action, data-id, data-num) set during table render.
 *
 * @param {Event} e - Click event from a table action button
 */
async function handleRowAction(e) {
  const { action, id, num } = e.currentTarget.dataset;
  if (action === 'view') openModal(+id);
  if (action === 'pdf')  handleDownloadPdf(+id, num);
}

/**
 * Download the PDF for a specific TDS, showing progress toasts.
 * Uses the downloadPdf() API helper which creates a temporary blob URL.
 * Respects the page-level Customer Copy / Internal Copy toggle.
 *
 * @param {number} id  - tds_id of the record to download
 * @param {string} num - TDS number string (e.g. "0042") used in the filename
 */
async function handleDownloadPdf(id, num) {
  try {
    showToast('Preparing PDF…', 'info', 2000);
    await downloadPdf(id, num, { excludeGroups: getSelectedExcludeGroups() });
    showToast('PDF downloaded.', 'success');
  } catch (err) {
    showToast('PDF error: ' + err.message, 'error');
  }
}

/* ═══════════════════════════════════════════════════════════
   DETAIL MODAL
═══════════════════════════════════════════════════════════ */
/**
 * Wire up the detail modal's close buttons, backdrop click-to-dismiss,
 * tab switching, and the PDF download / Delete action buttons.
 */
function wireModal() {
  const backdrop = document.getElementById('detail-modal');
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-btn-close').addEventListener('click', closeModal);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeModal(); });

  document.querySelectorAll('.modal-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`tab-${tab.dataset.tab}`)?.classList.add('active');
    });
  });

  document.getElementById('modal-btn-pdf').addEventListener('click', () => {
    if (activeModalId) {
      const rec = allRecords.find(r => r.tds_id === activeModalId);
      handleDownloadPdf(activeModalId, rec?.tds_number || activeModalId);
    }
  });
  document.getElementById('modal-btn-delete').addEventListener('click', () => {
    if (activeModalId) openConfirmDelete(activeModalId);
  });
}

/**
 * Open the detail modal for a given TDS record.
 * Uses the locally-cached record from allRecords if available (instant).
 * Falls back to a GET /api/tds/{id} fetch if the record is not cached.
 *
 * @param {number} id - tds_id of the record to display
 */
async function openModal(id) {
  activeModalId = id;
  const modal = document.getElementById('detail-modal');
  modal.classList.add('open');
  document.querySelectorAll('.modal-tab').forEach((t,i) => t.classList.toggle('active', i===0));
  document.querySelectorAll('.tab-panel').forEach((p,i) => p.classList.toggle('active', i===0));
  // Always fetch the full TDSOut - the list cache only holds TDSBriefOut which
  // is missing purpose, fabric_type, cover_grade, packing, splicing fields, etc.
  try {
    const rec = await getTDS(id);
    populateModal(rec);
  } catch (err) {
    showToast('Failed to load details: ' + err.message, 'error');
  }
}

/** Close the detail modal and clear the active record reference. */
function closeModal() {
  document.getElementById('detail-modal').classList.remove('open');
  activeModalId = null;
}

/**
 * Return the value as a string, or an em-dash if the value is null/undefined/''.
 * Optionally appends a unit suffix (e.g. ' mm', ' kg').
 *
 * @param {*} v        - Value to display
 * @param {string} [suffix=''] - Unit suffix to append when value is present
 * @returns {string}
 */
function dash(v, suffix='') {
  return (v===null||v===undefined||v==='') ? '-' : `${v}${suffix}`;
}

/**
 * Fill all detail modal fields from a TDS record object.
 * Covers three tab panels: Overview, Packing & Logistics, Splicing Parameters.
 *
 * @param {Object} t - Full TDS record object returned by the API
 */
function populateModal(t) {
  document.getElementById('modal-title').textContent  = `TDS-${t.tds_number}`;
  document.getElementById('d-tds-number').textContent = t.tds_number;
  document.getElementById('d-date').textContent       = new Date(t.tds_date).toLocaleDateString('en-IN',{day:'2-digit',month:'long',year:'numeric'});
  document.getElementById('d-standard').textContent   = t.standard?.standard_name || '-';
  document.getElementById('d-purpose').textContent    = t.purpose?.purpose_type   || '-';
  document.getElementById('d-customer').textContent   = t.customer?.customer_name || '-';
  document.getElementById('d-application').textContent= t.customer?.application   || '-';
  document.getElementById('d-location').textContent   = t.customer?.plant_location|| '-';
  document.getElementById('d-width').textContent      = dash(t.belt_width_mm, ' mm');
  document.getElementById('d-length').textContent     = dash(parseFloat(t.belt_length_m).toFixed(1), ' m');
  document.getElementById('d-construction').textContent= t.construction_type || '-';
  document.getElementById('d-edge').textContent       = t.edge_construction  || '-';
  document.getElementById('d-rating').textContent     = t.belt_rating?.rating_name || '-';
  document.getElementById('d-fabric').textContent     = t.fabric_type?.fabric_code || '-';
  document.getElementById('d-grade').textContent      = t.cover_grade?.grade_code  || '-';
  document.getElementById('d-plies').textContent      = dash(t.num_plies);
  document.getElementById('d-top').textContent        = dash(t.top_cover_mm, ' mm');
  document.getElementById('d-bottom').textContent     = dash(t.bottom_cover_mm, ' mm');
  document.getElementById('d-carcass').textContent    = dash(t.carcass_thickness_mm, ' mm');
  document.getElementById('d-total-thick').textContent= dash(t.total_thickness_mm, ' mm');
  document.getElementById('d-reel').textContent       = t.reel_type?.reel_name || (t.reel_type_id ? `Reel ${t.reel_type_id}` : '-');
  document.getElementById('d-packing-type').textContent= t.packing_type?.packing_name || (t.packing_type_id ? `Type ${t.packing_type_id}` : '-');
  document.getElementById('d-rolls').textContent      = dash(t.num_rolls);
  document.getElementById('d-len-roll').textContent   = t.length_per_roll_m ? parseFloat(t.length_per_roll_m).toFixed(1)+' m' : '-';
  document.getElementById('d-roll-dims').textContent  = t.roll_dimensions || '-';
  document.getElementById('d-net-wt').textContent     = t.net_weight_kg   ? parseFloat(t.net_weight_kg).toFixed(1)+' kg' : '-';
  document.getElementById('d-gross-wt').textContent   = t.gross_weight_kg ? parseFloat(t.gross_weight_kg).toFixed(1)+' kg' : '-';
  document.getElementById('d-splice-req').textContent = t.splicing_required ? 'Yes' : 'No';
  document.getElementById('d-vuln-method').textContent= t.vulcanization_method || '-';
  document.getElementById('d-joints').textContent     = dash(t.num_joints);
  document.getElementById('d-step-len').textContent   = t.step_length_mm   ? t.step_length_mm+' mm' : '-';
  document.getElementById('d-splice-len').textContent = t.splice_length_mm ? t.splice_length_mm+' mm' : '-';
  document.getElementById('d-extra-len').textContent  = t.total_extra_length_m ? parseFloat(t.total_extra_length_m).toFixed(3)+' m' : '-';
}

/* ═══════════════════════════════════════════════════════════
   DELETE CONFIRMATION
═══════════════════════════════════════════════════════════ */
/**
 * Wire up the delete confirmation overlay.
 * "Cancel" just closes the overlay. "Delete" calls DELETE /api/tds/{id},
 * closes both the overlay and the detail modal, and refreshes the record list.
 */
function wireConfirmDelete() {
  document.getElementById('confirm-cancel').addEventListener('click', closeConfirmDelete);
  document.getElementById('confirm-delete').addEventListener('click', async () => {
    if (!pendingDeleteId) return;
    try {
      await deleteTDS(pendingDeleteId);
      showToast('TDS deleted.', 'success');
      closeConfirmDelete(); closeModal();
      await loadRecords();
    } catch (err) {
      showToast('Delete failed: ' + err.message, 'error');
      closeConfirmDelete();
    }
  });
}

/**
 * Show the delete confirmation overlay for a specific TDS record.
 * Populates the confirmation message with the TDS number before showing.
 *
 * @param {number} id - tds_id of the record to delete
 */
function openConfirmDelete(id) {
  pendingDeleteId = id;
  const rec = allRecords.find(r => r.tds_id === id);
  document.getElementById('confirm-msg').textContent =
    `TDS-${rec?.tds_number||id} will be permanently deleted. This cannot be undone.`;
  document.getElementById('confirm-overlay').classList.add('open');
}

/** Close the delete confirmation overlay and clear the pending delete ID. */
function closeConfirmDelete() {
  pendingDeleteId = null;
  document.getElementById('confirm-overlay').classList.remove('open');
}

if (session) init().catch(err => showToast('Page error: ' + err.message, 'error'));
