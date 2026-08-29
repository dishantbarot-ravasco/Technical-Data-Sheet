"""
qap_service.py — QAP template resolution and PDF context building.

Two public functions:
    resolve_qap_template(tds)  → QAPTemplate | None
    build_qap_context(tds, template) → dict  (passed straight to Jinja2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# ─── standard_id → QAP category ──────────────────────────────────────────────
STANDARD_TO_QAP_CATEGORY = {
    # General Purpose
    1:  'GP',   # IS 1891 Part 1
    2:  'GP',   # ISO 14890
    3:  'GP',   # DIN 22102
    4:  'GP',   # AS/IS (Grade 1/2)
    5:  'GP',   # AS 1332 (M, A, N)
    6:  'GP',   # SANS (A, C, M, N)
    7:  'GP',   # Inhouse (Crusher+, UAR, RTR etc.)
    # Heat Resistant
    8:  'HR',   # IS 1891 Part 2 (T1, T2)
    9:  'HR',   # ISO 4195 (Class 1/2/3)
    10: 'HR',   # ARPM (Class 1/2/3)
    11: 'HR',   # Inhouse HR (SHAR, UHR, SUHR, HR-OR)
    # Fire Resistant (ISO)
    12: 'FR_ISO',  # AS 4606 (F, S)
    13: 'FR_ISO',  # FRAS
    14: 'FR_ISO',  # SANS 1173 (F)
    # OR and FR_CAN not yet mapped — data not in DB
}

# ─── Mid-section page-break points per QAP category ─────────────────────────
# Each entry is an (sn, component) PAIR, not a bare SN - the source data has
# duplicate SNs within a category (e.g. FR_ISO's "3.5" is used by BOTH
# "Troughability" AND "Cover Rubber Properties"), so matching on SN alone
# forced the break before the wrong one of the two.
#
# Layout matches the reference document exactly:
#   page 1 = 1.1-1.5, page 2 = 1.6-1.12, page 3 = 2.1-2.6 (own section break,
#   see SECTION_BREAK_CODES), page 4 = 3.1-3.4 (+3.5 Troughability for
#   FR_ISO, which numbers Adhesion/Troughability one SN later than GP/HR),
#   page 5 = Cover Rubber Properties + items 4/5 + notes.
# OR / FR_CAN: fill in once those templates are seeded.
MID_BREAKS = {
    'GP':     {('1.6', 'Protective\nAgent'), ('3.5', 'Cover Rubber Properties')},
    'HR':     {('1.6', 'Protective\nAgent'), ('3.5', 'Cover Rubber Properties')},
    'FR_ISO': {('1.6', 'Protective\nAgent'), ('3.5', 'Cover Rubber Properties')},
    'OR':     set(),
    'FR_CAN': set(),
}

# ─── Section codes that must always start at the top of a fresh page ────────
# Without this, a section heading (e.g. "2.0 IN PROCESS INSPECTION") can land
# mid-page with only its first item or two before the natural page break,
# which looks like the section data is "merged" into the previous section's
# leftover space. Section '1.0' is intentionally excluded — it's always the
# first thing on page 1, so a forced break there would just add a blank page.
SECTION_BREAK_CODES = {
    'GP':     {'2.0', '3.0'},
    'HR':     {'2.0', '3.0'},
    'FR_ISO': {'2.0', '3.0'},
    'OR':     set(),
    'FR_CAN': set(),
}

# ─── Notes text per QAP category ─────────────────────────────────────────────
# Page 5 notes block - transcribed verbatim (wording/numbering as-is) from the
# source spreadsheet's own "Notes:" cell for each category, not paraphrased.
# GP and FR_ISO share the exact same text in the source; HR has its own
# distinct wording (different repair-norm numbering and units - "per 100m of
# belt length" instead of "per 100 sq.m of belt surface").
_GP_FR_ISO_NOTES = (
    "Notes:\n"
    "1. Belts must be offered for visual inspection without any surface finishing, top & "
    "bottom cover surface aesthetics improvement activities.\n"
    "2. Repair Norms shall be reflected in the internal procedure and shall be followed "
    "during manufacturing & Internal Inspection.\n"
    "REPAIR NORMS\n"
    "A) Patch Repairs: Localized rectification of surface blemishes / defects in cured belt "
    "by using rubber compound similar to the mother compound up to top carcass may be done "
    "followed by hot vulcanization\n"
    "B) Buffing / Dough Filling: Entrapment of foreign matters may be buffed suitably. Depth "
    "of buffing should not exceed the difference in thickness of the cover rubber (as "
    "measured in test sample for the purpose of acceptance of cover rubber thickness) and the "
    "specified minimum cover thickness. Where the indentation depth is more, the same may be "
    "filled with rubber compound followed by vulcanization locally\n"
    "C) The repairs of size up to 25mm x 25mm (625mm Sq.) shall not be considered as repair\n"
    "D) Maximum number of repairs as per A, above shall be limited to 5 per 100 sq.m of belt "
    "surface (rounded up to the higher unit).\n"
    "E) Total number of repairs as per A and B, above shall not exceed more than 10 per 100 "
    "sq.m of belt surface (rounded up to the higher unit).\n"
    "F) In case of patch repair as indicated in 1 above the maximum size/area of each repair "
    "shall be limited to 1/5W x 1/5W, with one dimension maximum 1/5W where W is the width of "
    "the belt\n"
    "G) The gap between plies at longitudinal Ply joint area may be checked at one point over "
    "a belt length during inspection by removing 100 mm x 25 mm cover rubber from the "
    "finished belt. This portion shall be repaired by vulcanization and shall not be "
    "considered as repair. Longitudinal Joints may be provided only for belt."
)

QAP_NOTES = {
    'GP':     _GP_FR_ISO_NOTES,
    'FR_ISO': _GP_FR_ISO_NOTES,
    'HR': (
        "Notes:\n"
        "1. Belts must be offered for visual inspection without any surface finishing, top & "
        "bottom cover surface aesthetics improvement activities.\n"
        "2. Repair Norms shall be reflected in the internal procedure and shall be followed "
        "during manufacturing & Internal Inspection.\n"
        "3. Patch Repairs Norms:\n"
        "a) Localized rectification of surface blemishes / defects in cured belt by using "
        "rubber compound similar to the mother compound up to top carcass may be done "
        "followed by hot vulcanization\n"
        "b) Buffing / Dough Filling: Entrapment of foreign matters may be buffed suitably. "
        "Depth of buffing should not exceed the difference in thickness of the cover rubber "
        "(as measured in test sample for the purpose of acceptance of cover rubber thickness) "
        "and the specified minimum cover thickness. Where the indentation depth is more, the "
        "same may be filled with rubber compound followed by vulcanization locally\n"
        "c) The repairs of size up to 25mm x 25mm (625mm Sq.) shall not be considered as "
        "repair\n"
        "d) Maximum number of repairs as per 'a' above shall be limited to 5 per 100m of belt "
        "length (rounded up to the higher unit).\n"
        "d) Total number of repairs as per 'a' and 'b' above shall not exceed more than 10 per "
        "100m of belt length (rounded up to the higher unit).\n"
        "e) In case of patch repair as indicated in 1 above the maximum size/area of each "
        "repair shall be limited to 1/5W x 1/5W, with one dimension maximum 1/5W where W is "
        "the width of the belt\n"
        "f) The gap between plies at longitudinal Ply joint area may be checked at one point "
        "over a belt length during inspection by removing 100 mm x 25 mm cover rubber from "
        "the finished belt. This portion shall be repaired by vulcanization and shall not be "
        "considered as repair. Longitudinal Joints may be provided only for belt."
    ),
    'OR':     '',
    'FR_CAN': '',
}


def resolve_qap_template(tds):
    """
    Return the QAPTemplate that applies to this TDS, or None if no match.

    Resolution order:
    1. Look up standard_id in STANDARD_TO_QAP_CATEGORY
    2. Fetch the active QAPTemplate for that category
    3. Return None (with a warning log) if the category has no active template yet
    """
    from apps.core.models import QAPTemplate

    standard_id = tds.standard_id
    category    = STANDARD_TO_QAP_CATEGORY.get(standard_id)

    if not category:
        logger.warning(
            'resolve_qap_template: standard_id=%s has no QAP category mapping — '
            'tds_id=%s will have no QAP.', standard_id, tds.tds_id
        )
        return None

    try:
        return QAPTemplate.objects.get(category=category, is_active=True)
    except QAPTemplate.DoesNotExist:
        logger.warning(
            'resolve_qap_template: QAPTemplate for category=%s not seeded yet — '
            'run seed_qap_templates first.', category
        )
        return None


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class QAPCell:
    """
    One rendered <td>, covering `rowspan` physical rows starting at this one.
    A physical row that has NO QAPCell for a given column is covered by an
    earlier row's rowspan and renders no <td> at all for that column - exactly
    how the source Excel represents "same value as the row above" (a merged
    cell), see _column_cells() below.
    """
    value:   str
    rowspan: int


@dataclass
class QAPPhysicalRow:
    """
    One literal row of the source spreadsheet within an item group - either
    the group's first row (the SN/Component row) or one of its QAPItemSubRow
    children. `char` is always its own cell (every physical row's
    characteristic text is unique - it's never merged with the row above).
    Every other column is a QAPCell when this row STARTS a new merge run for
    that column, or None when it's covered by an earlier row's rowspan.
    """
    char:         str
    typ_cell:     Optional[QAPCell]
    cls_cell:     Optional[QAPCell]
    qm_cell:      Optional[QAPCell]
    qsc_cell:     Optional[QAPCell]
    ref_cell:     Optional[QAPCell]
    acc_cell:     Optional[QAPCell]
    fmt_cell:     Optional[QAPCell]
    d_cell:       Optional[QAPCell]
    m_cell:       Optional[QAPCell]
    s_cell:       Optional[QAPCell]
    c_cell:       Optional[QAPCell]
    remarks_cell: Optional[QAPCell]


@dataclass
class QAPItemGroup:
    """One logical QAP item (one SN) - the merged SN/Component cell spans
    `rowspan` physical rows, listed in `rows` (see QAPPhysicalRow)."""
    sn:      str
    comp:    str
    rowspan: int                              # total physical rows in this group
    rows:    List[QAPPhysicalRow] = field(default_factory=list)

    @property
    def is_merged(self) -> bool:
        """True for narrative-only rows (e.g. item 4 'Identification & Marking')
        that carry no inspection class — rendered as a single merged cell
        spanning the whole row instead of the normal column layout. The first
        physical row always gets its own QAPCell for every column (even a
        blank one - see _column_cells), so this checks the cell's VALUE, not
        whether a cell exists."""
        if not self.rows:
            return True
        cell = self.rows[0].cls_cell
        return not (cell.value if cell else '').strip()

    @property
    def first_char(self) -> str:
        """Characteristic text of the first physical row - used for the
        narrative (is_merged) rendering, which has only one row."""
        return self.rows[0].char if self.rows else ''


@dataclass
class QAPSectionData:
    code:        str
    name:        str
    item_groups: List[QAPItemGroup] = field(default_factory=list)


# ─── Agency string parser ─────────────────────────────────────────────────────

def _parse_agency(agency_str: str):
    """
    Split the combined agency string "M:P / S:W / C:V" into (m, s, c) tuples.
    Returns three strings; missing or '-' values come back as ''.

    Examples:
        "M:P / S:W"          → ('P', 'W', '')
        "M:P / S:W / C:V"    → ('P', 'W', 'V')
        "M:P"                → ('P', '', '')
        ""                   → ('', '', '')
    """
    m = s = c = ''
    for part in (agency_str or '').split('/'):
        part = part.strip()
        if part.upper().startswith('M:'):
            m = part[2:].strip()
        elif part.upper().startswith('S:'):
            s = part[2:].strip()
        elif part.upper().startswith('C:'):
            c = part[2:].strip()
    return m, s, c


# ─── Excel-accurate merged-cell computation ──────────────────────────────────

def _column_cells(values: List[str]) -> List[Optional[QAPCell]]:
    """
    Turn a list of per-physical-row string values for ONE column into the
    QAPCell/None sequence that reproduces the source Excel's merged cells:
    a blank value means that row's cell was blank in the spreadsheet, i.e.
    visually merged with the nearest non-blank cell above it - so it gets
    None here (render nothing) and extends the previous cell's rowspan.
    A non-blank value always starts a brand-new cell/rowspan run, even if it
    happens to repeat the previous run's text.

    The first row is always the start of a run (rowspan >= 1) even if its own
    value is blank, since a table row can't omit its very first cell in a
    column - only rows COVERED by an earlier rowspan can skip rendering.
    """
    n = len(values)
    cells: List[Optional[QAPCell]] = [None] * n
    run_start = 0
    run_value = values[0]
    for i in range(1, n):
        if values[i]:
            cells[run_start] = QAPCell(value=run_value, rowspan=i - run_start)
            run_start = i
            run_value = values[i]
    cells[run_start] = QAPCell(value=run_value, rowspan=n - run_start)
    return cells


def _flat_cells(values: List[str]) -> List[QAPCell]:
    """
    Same forward-fill semantics as _column_cells (a blank value means "same as
    the row above"), but every row gets its OWN rowspan=1 cell instead of a
    merged/covered one.

    Used for "compound" items (see is_compound below) as a deliberately less
    elegant but bulletproof fallback: WeasyPrint's table layout has a
    reproducible bug where a rowspan cell that's supposed to reach an item
    group's LAST physical row silently gets dropped/truncated when that group
    also contains ANOTHER column with a shorter, independent merge run partway
    through (confirmed via isolated reproduction - identical HTML structure
    rendered correctly in one test and incorrectly in another, so the trigger
    is content/height-dependent, not purely structural, and not worth chasing
    further for a document used for QA sign-off). Repeating the resolved value
    on every row costs a nicer merged look but can never silently lose data.
    """
    cells = []
    current = values[0]
    for v in values:
        if v:
            current = v
        cells.append(QAPCell(value=current, rowspan=1))
    return cells


# ─── Context builder ──────────────────────────────────────────────────────────

def build_qap_context(tds, template, doc_type=None, ref_no=None, ref_date=None):
    """
    Build the full Jinja2 render context for a QAP PDF.

    doc_type / ref_no / ref_date come from the pre-download popup on the
    frontend (PO vs Enquiry) and are NOT persisted anywhere — they're passed
    straight through from the request query params for this one render only.
        doc_type  — 'PO' | 'ENQUIRY' | None (defaults to 'PO' label if unset)
        ref_no    — the PO / Enquiry number the user typed in
        ref_date  — the PO / Enquiry date the user typed in

    Returns a dict with:
        company_name      — Ravasco Transmission & Packing Pvt Ltd.
        qap_title          — category-specific title string, with cover grade
        customer_name      — from TDS customer, or blank
        doc_number         — QAP-{tds_number}
        cover_grade        — grade code (also shown in the header in place of Rev)
        tds_number         — e.g. '0042'
        tds_date           — formatted date string
        belt_description   — from TDS
        standard_name       — from TDS standard
        ref_label_no        — 'PO No :' or 'Enquiry No :'
        ref_label_date       — 'PO Date :' or 'Enquiry Date :'
        ref_no / ref_date    — values typed in on the download popup
        sections            — list of QAPSectionData (each with .item_groups)
        logo_data_uri       — base64 PNG for Indus logo
        tuv_logo_data_uri   — base64 PNG for TÜV SÜD logo
        stamp_data_uri      — base64 PNG for Ravasco company sign & stamp
        notes               — notes/repair-norms text (GP only, else '')
    """
    from apps.core.models import QAPRecord
    from apps.services.pdf_renderer import _logo_data_uri

    # ── Header fields ─────────────────────────────────────────────────────────
    customer_name = ''
    if tds.customer:
        customer_name = tds.customer.customer_name or ''

    doc_number = f'QAP-{tds.tds_number}'
    try:
        rec        = QAPRecord.objects.get(tds_id=tds.tds_id)
        doc_number = rec.doc_number or doc_number
    except QAPRecord.DoesNotExist:
        pass

    tds_date_str = tds.tds_date.strftime('%d/%m/%Y') if tds.tds_date else ''
    cover_grade  = tds.cover_grade.grade_code if tds.cover_grade else ''

    # ── PO / Enquiry (ephemeral — entered fresh on every download, never saved) ─
    doc_type = (doc_type or 'PO').upper()
    if doc_type == 'ENQUIRY':
        ref_label_no, ref_label_date = 'Enquiry No :', 'Enquiry Date :'
    else:
        doc_type = 'PO'
        ref_label_no, ref_label_date = 'PO No :', 'PO Date :'
    ref_no   = ref_no or ''
    ref_date = ref_date or ''

    TITLE_MAP = {
        'GP':     'QUALITY ASSURANCE PLAN FOR GENERAL PURPOSE{grade} TEXTILE REINFORCED RUBBER CONVEYOR BELT',
        'HR':     'QUALITY ASSURANCE PLAN FOR HEAT RESISTANT{grade} TEXTILE REINFORCED RUBBER CONVEYOR BELT',
        'FR_ISO': 'QUALITY ASSURANCE PLAN FOR FIRE RESISTANT{grade} TEXTILE REINFORCED RUBBER CONVEYOR BELT',
        'OR':     'QUALITY ASSURANCE PLAN FOR OIL RESISTANT{grade} TEXTILE REINFORCED RUBBER CONVEYOR BELT',
        'FR_CAN': 'MANUFACTURING QUALITY PLAN FOR FIRE RESISTANT{grade} TEXTILE REINFORCED RUBBER CONVEYOR BELT',
    }
    grade_suffix = f' (Grade - {cover_grade})' if cover_grade else ''
    qap_title = TITLE_MAP.get(template.category, 'QUALITY ASSURANCE PLAN{grade}').format(grade=grade_suffix)

    # ── Sections and item groups ───────────────────────────────────────────────
    sections_data: List[QAPSectionData] = []

    for section in template.sections.prefetch_related('items__sub_rows_data').order_by('sort_order'):
        sec_data = QAPSectionData(code=section.section_code, name=section.section_name)

        # PERF (fixed): this used to call section.items.order_by('sort_order'),
        # which builds a brand-new queryset instead of using the prefetch_related
        # cache above -- re-querying the DB once per section on every QAP PDF
        # render. QAPItem.Meta.ordering is already ['sort_order'], so plain
        # section.items.all() returns the prefetched rows, already in the
        # right order, with no extra query.
        for item in section.items.all():
            # Combine the group's first physical row (the item itself) with
            # its sub-rows (item.sub_rows_data, already ordered by sort_order
            # via that model's Meta) into one physical-row sequence, each with
            # every column exactly as the source spreadsheet had it - blank
            # where that row's cell was blank.
            physical = [item] + list(item.sub_rows_data.all())
            n = len(physical)

            chars   = [p.characteristic or '' for p in physical]
            typs    = [p.type_of_check   or '' for p in physical]
            clss    = [p.check_class     or '' for p in physical]
            qms     = [p.quantum_m       or '' for p in physical]
            qscs    = [p.quantum_sc      or '' for p in physical]
            refs    = [p.reference_docs  or '' for p in physical]
            accs    = [p.acceptance_norms or '' for p in physical]
            fmts    = [p.format_of_records or '' for p in physical]
            ds      = [p.record_mark     or '' for p in physical]
            agencys = [p.agency          or '' for p in physical]
            remarks = [p.remarks         or '' for p in physical]

            # "Compound" item = at least one sub-row overrides class/quantum/
            # reference/acceptance rather than just adding characteristic text
            # (e.g. item 3.5 "Cover Rubber Properties" restating a different
            # Reference Document for "Angular Tear Strength"/"Abrasion Loss").
            # Type of Check varying per sub-row is normal even for simple items
            # (e.g. "a) ... Physical" / "b) ... Chemical") so it's excluded
            # from this check - only cls/qm/qsc/ref/acc matter here.
            is_compound = any(
                clss[i] or qms[i] or qscs[i] or refs[i] or accs[i]
                for i in range(1, n)
            )
            cell_fn = _flat_cells if is_compound else _column_cells

            typ_cells = cell_fn(typs)
            cls_cells = cell_fn(clss)
            qm_cells  = cell_fn(qms)
            qsc_cells = cell_fn(qscs)
            ref_cells = cell_fn(refs)
            acc_cells = cell_fn(accs)
            fmt_cells = cell_fn(fmts)
            d_cells   = cell_fn(ds)
            agency_cells = cell_fn(agencys)
            remarks_cells = cell_fn(remarks)

            # Agency is stored as one combined "M:x / S:y / C:z" string per
            # merge-run - split each surviving cell into its M/S/C values,
            # keeping the SAME rowspan (they always change together).
            m_cells = [None] * n
            s_cells = [None] * n
            c_cells = [None] * n
            for i, cell in enumerate(agency_cells):
                if cell is None:
                    continue
                m_v, s_v, c_v = _parse_agency(cell.value)
                m_cells[i] = QAPCell(value=m_v, rowspan=cell.rowspan)
                s_cells[i] = QAPCell(value=s_v, rowspan=cell.rowspan)
                c_cells[i] = QAPCell(value=c_v, rowspan=cell.rowspan)

            rows = [
                QAPPhysicalRow(
                    char=chars[i], typ_cell=typ_cells[i], cls_cell=cls_cells[i],
                    qm_cell=qm_cells[i], qsc_cell=qsc_cells[i], ref_cell=ref_cells[i],
                    acc_cell=acc_cells[i], fmt_cell=fmt_cells[i], d_cell=d_cells[i],
                    m_cell=m_cells[i], s_cell=s_cells[i], c_cell=c_cells[i],
                    remarks_cell=remarks_cells[i],
                )
                for i in range(n)
            ]

            grp = QAPItemGroup(
                sn      = item.sn or '',
                comp    = item.component or '',
                rowspan = n,
                rows    = rows,
            )
            sec_data.item_groups.append(grp)

        sections_data.append(sec_data)

    # ── Logos ─────────────────────────────────────────────────────────────────
    try:
        logo_data_uri = _logo_data_uri('indus_logo.png')
    except Exception:
        logo_data_uri = ''

    try:
        tuv_logo_data_uri = _logo_data_uri('tuv_logo.png')
    except Exception:
        tuv_logo_data_uri = ''

    try:
        stamp_data_uri = _logo_data_uri('ravasco_stamp.png')
    except Exception:
        stamp_data_uri = ''

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes = QAP_NOTES.get(template.category, '')

    # ── Mid-section page-break points ─────────────────────────────────────────
    mid_breaks           = MID_BREAKS.get(template.category, set())
    section_break_codes = SECTION_BREAK_CODES.get(template.category, set())

    return {
        'company_name':      'Ravasco Transmission & Packing Pvt Ltd.',
        'qap_title':         qap_title,
        'customer_name':     customer_name,
        'doc_number':        doc_number,
        'cover_grade':       cover_grade,
        'tds_number':        tds.tds_number,
        'tds_date':          tds_date_str,
        'belt_description':  tds.belt_description or '',
        'standard_name':     tds.standard.standard_name if tds.standard else '',
        'doc_type':          doc_type,
        'ref_label_no':      ref_label_no,
        'ref_label_date':    ref_label_date,
        'ref_no':            ref_no,
        'ref_date':          ref_date,
        'sections':          sections_data,
        'logo_data_uri':     logo_data_uri,
        'tuv_logo_data_uri': tuv_logo_data_uri,
        'stamp_data_uri':    stamp_data_uri,
        'notes':             notes,
        'mid_breaks':        mid_breaks,
        'section_break_codes': section_break_codes,
    }
