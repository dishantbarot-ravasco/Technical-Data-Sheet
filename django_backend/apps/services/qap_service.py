"""
qap_service.py — QAP template resolution and PDF context building.

Two public functions:
    resolve_qap_template(tds)  → QAPTemplate | None
    build_qap_context(tds, template) → dict  (passed straight to Jinja2)
"""

import logging
from dataclasses import dataclass, field
from typing import List

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

# ─── Mid-section page-break SNs per QAP category ────────────────────────────
# SNs listed here trigger a forced page-break BEFORE that item row.
# GP layout:  page 1 = 1.1-1.7, page 2 = 1.8-1.12+sig, page 4 = 3.1-3.5,
#             page 5 = items 4+5+notes+sig.
# HR / FR_ISO / OR / FR_CAN: fill in once those templates are seeded and
# the actual SN values are known; leave empty set in the meantime.
MID_BREAK_SNS = {
    'GP':     {'1.8', '4'},
    'HR':     set(),
    'FR_ISO': set(),
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
    'HR':     set(),
    'FR_ISO': set(),
    'OR':     set(),
    'FR_CAN': set(),
}

# ─── Notes text per QAP category ─────────────────────────────────────────────
# Page 5 notes block. Only GP currently has full text.
QAP_NOTES = {
    'GP': (
        "NOTES:\n"
        "1. The above Quality Assurance Plan is prepared for the Conveyor Belt and covers all "
        "stages of manufacture from raw material procurement to final despatch.\n"
        "2. All test reports / certificates shall be maintained by the manufacturer and shall "
        "be made available to the customer / their representative for verification on request.\n"
        "3. Customer / their representative shall be given 48 hours advance notice for "
        "witnessing of tests.\n"
        "4. Acceptance norms shall be as per applicable standard and / or mutually agreed "
        "specifications.\n"
        "5. All measuring and test equipment used shall be calibrated as per the calibration "
        "schedule maintained by the manufacturer.\n\n"
        "REPAIR NORMS:\n"
        "Minor surface defects on the top cover of the belt such as air pockets, blisters, "
        "cuts, etc., are repaired by buffing and hot / cold repair methods as per the "
        "manufacturer's standard repair procedure. Such repairs shall not affect the "
        "performance of the belt and shall be acceptable provided the repaired area does not "
        "exceed the limits specified in the applicable standard."
    ),
    'HR':     '',
    'FR_ISO': '',
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
class QAPSubRow:
    """One sub-characteristic line within an item group (rows 2+ only)."""
    char: str
    typ:  str   # same as parent's type_of_check (repeated for column alignment)


@dataclass
class QAPItemGroup:
    """
    One logical QAP item (one SN).

    Sub-characteristics are split from item.characteristic on '\\n' by the seeder.
    All other columns (qm, qsc, ref, acc, fmt, agency, remarks) are stored once
    at the item level and span all sub-rows via HTML rowspan.
    """
    sn:      str
    comp:    str
    cls:     str
    qm:      str   # quantum_m  (rowspan = all sub-rows)
    qsc:     str   # quantum_sc (rowspan = all sub-rows)
    ref:     str   # reference_docs (rowspan)
    acc:     str   # acceptance_norms (rowspan)
    fmt:     str   # format_of_records (rowspan)
    d:       str   # record_mark — 'D' column (rowspan)
    m:       str   # agency M value  (rowspan)
    s:       str   # agency S value  (rowspan)
    c:       str   # agency C value  (rowspan)
    remarks: str   # (rowspan)

    rowspan:    int   # total number of characteristic lines (≥ 1)
    first_char: str   # characteristic text for the first <tr>
    first_typ:  str   # type_of_check for the first <tr>

    # Remaining sub-rows (index 1+) — only char + typ needed
    sub_rows: List[QAPSubRow] = field(default_factory=list)

    @property
    def is_merged(self) -> bool:
        """True for narrative-only rows (e.g. item 4 'Identification & Marking')
        that carry no inspection class — rendered as a single merged cell
        spanning the whole row instead of the normal column layout."""
        return not (self.cls or '').strip()


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

    for section in template.sections.prefetch_related('items').order_by('sort_order'):
        sec_data = QAPSectionData(code=section.section_code, name=section.section_name)

        # PERF (fixed): this used to call section.items.order_by('sort_order'),
        # which builds a brand-new queryset instead of using the prefetch_related
        # cache above -- re-querying the DB once per section on every QAP PDF
        # render. QAPItem.Meta.ordering is already ['sort_order'], so plain
        # section.items.all() returns the prefetched rows, already in the
        # right order, with no extra query.
        for item in section.items.all():
            # Sub-characteristics are stored as '\n'-joined lines in characteristic.
            # Split them into individual rows; guarantee at least one entry.
            chars = [c.strip() for c in (item.characteristic or '').split('\n') if c.strip()]
            if not chars:
                chars = ['']

            n    = len(chars)
            typ  = item.type_of_check or ''
            m_v, s_v, c_v = _parse_agency(item.agency)

            # For FINAL INSPECTION (section 3.0), the customer's "C" column is
            # WITNESS, not VERIFICATION — the source data still says 'V' for
            # historical reasons, so override it here at render time.
            if section.section_code == '3.0' and c_v == 'V':
                c_v = 'W'

            grp = QAPItemGroup(
                sn      = item.sn or '',
                comp    = item.component or '',
                cls     = item.check_class or '',
                qm      = item.quantum_m or '',
                qsc     = item.quantum_sc or '',
                ref     = item.reference_docs or '',
                acc     = item.acceptance_norms or '',
                fmt     = item.format_of_records or '',
                d       = item.record_mark or '',
                m       = m_v,
                s       = s_v,
                c       = c_v,
                remarks = item.remarks or '',
                rowspan    = n,
                first_char = chars[0],
                first_typ  = typ,
                sub_rows   = [QAPSubRow(char=ch, typ=typ) for ch in chars[1:]],
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

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes = QAP_NOTES.get(template.category, '')

    # ── Mid-section page-break SNs ────────────────────────────────────────────
    mid_break_sns     = MID_BREAK_SNS.get(template.category, set())
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
        'notes':             notes,
        'mid_break_sns':     mid_break_sns,
        'section_break_codes': section_break_codes,
    }
