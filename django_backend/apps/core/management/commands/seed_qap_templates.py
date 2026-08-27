"""
Management command: seed_qap_templates

Reads SAMPLE_QAP.xlsx and populates QAPTemplate / QAPSection / QAPItem rows
for the three active belt categories: General Purpose, Heat Resistant, FR ISO.

Usage:
    python run_django.py seed_qap_templates --file /path/to/SAMPLE_QAP.xlsx
    python run_django.py seed_qap_templates --file /path/to/SAMPLE_QAP.xlsx --replace
    python run_django.py seed_qap_templates --file /path/to/SAMPLE_QAP.xlsx --dry-run

Sheet → Template mapping (OR and FR CAN skipped — data not yet in DB):
    'M24 N17 SAR '       → GP     General Purpose
    'FR ISO'             → FR_ISO Fire Resistant (ISO)
    'HRT1,T2,UHR,SUHR'  → HR     Heat Resistant

Column layout (fixed, confirmed from actual file):
    0  SN                 5  Quantum M      10  D* (skip)
    1  Component          6  Quantum S/C    11  Agency M
    2  Characteristic     7  Reference docs 12  Agency S
    3  Class              8  Acceptance     13  Agency C
    4  Type of check      9  Format         14  Remarks
"""

import re
from django.core.management.base import BaseCommand, CommandError

try:
    import openpyxl
except ImportError:
    openpyxl = None

# ─── Sheet → template config ──────────────────────────────────────────────────
SHEET_MAP = {
    'M24 N17 SAR ':      {'category': 'GP',     'display_name': 'General Purpose'},
    'FR ISO':            {'category': 'FR_ISO', 'display_name': 'Fire Resistant (ISO)'},
    'HRT1,T2,UHR,SUHR': {'category': 'HR',     'display_name': 'Heat Resistant'},
}

# Rows to skip — these are page-break signature lines or legal notes
SKIP_PHRASES = ('customer signature', 'notes:', 'repair norm', 'belt must be offered')

# Section header: SN column starts with "X.0" (e.g. "1.0 RAW MATERIAL")
SECTION_RE = re.compile(r'^(\d+\.0)\b(.*)')
# Item row: SN is "X.Y" or "X.Ya" (e.g. "1.1", "1.11", "2.1a", "4", "5")
ITEM_RE    = re.compile(r'^\d+(\.\d+[a-z]?)?$')


def _val(row, col):
    """Return stripped string from row[col], or '' if missing / None."""
    if col >= len(row):
        return ''
    v = row[col].value
    return str(v).strip() if v is not None else ''


def _agency(row):
    """Combine the three Agency columns (M=11, S=12, C=13) into one string."""
    m = _val(row, 11)
    s = _val(row, 12)
    c = _val(row, 13)
    parts = []
    for label, val in (('M', m), ('S', s), ('C', c)):
        if val and val != '-':
            parts.append(f'{label}:{val}')
    return ' / '.join(parts) if parts else ''


def _parse_sheet(ws):
    """
    Parse one worksheet into a flat list of dicts:
        {'type': 'section', 'code': '1.0', 'name': 'RAW MATERIAL', 'sort': N}
        {'type': 'item', 'section_code': '1.0', 'sn': '1.1', 'component': ..., ...}

    Sub-characteristic rows (empty SN, sub-letter checks like "b) Ash Content")
    are merged into the previous item's 'characteristic' field as newline-separated text.
    """
    all_rows = list(ws.iter_rows())

    # Find the header row (the one where col 0 value is "SN")
    data_start = None
    for idx, row in enumerate(all_rows):
        if _val(row, 0).upper() in ('SN', 'S.N', 'S.NO'):
            data_start = idx + 1   # data starts the row after column headers
            break
    if data_start is None:
        return []

    # Skip the sub-header row (row 6: 'M', 'S/C' for quantum) and
    # the column-number row (row 7: '1','2','3',...) — both come right after
    # the header row. We detect them by checking col 0 is empty or numeric only.
    while data_start < len(all_rows):
        v = _val(all_rows[data_start], 0)
        if not v or v.isdigit():
            data_start += 1
        else:
            break

    results       = []
    sort_counter  = 0
    current_sec   = None
    last_item_idx = None   # index into results[] of the last item added

    for row in all_rows[data_start:]:
        sn_raw = _val(row, 0)

        # ── Skip completely empty rows ───────────────────────────────────────
        if not sn_raw and not _val(row, 1) and not _val(row, 2):
            continue

        # ── Skip signature / notes rows ─────────────────────────────────────
        sn_lower = sn_raw.lower()
        if any(phrase in sn_lower for phrase in SKIP_PHRASES):
            continue
        comp_lower = _val(row, 1).lower()
        if len(sn_raw) > 80:   # long notes text in SN column
            continue

        # ── Section header: "1.0 RAW MATERIAL" ──────────────────────────────
        m = SECTION_RE.match(sn_raw)
        if m:
            code = m.group(1)               # "1.0"
            name = m.group(2).strip()        # "RAW MATERIAL"
            sort_counter += 1
            current_sec   = code
            last_item_idx = None
            results.append({
                'type': 'section',
                'code': code,
                'name': name,
                'sort': sort_counter,
            })
            continue

        # ── Item row: SN like "1.1", "1.11", "3.1a", "4", "5" ──────────────
        if ITEM_RE.match(sn_raw):
            # Auto-create a section if this item has no parent (items 4, 5, etc.)
            if current_sec is None:
                sort_counter += 1
                synthetic_code = f'{sn_raw}.0'
                current_sec    = synthetic_code
                results.append({
                    'type': 'section',
                    'code': synthetic_code,
                    'name': 'Other',
                    'sort': sort_counter,
                })

            sort_counter += 1
            is_static = (current_sec == '1.0')
            item = {
                'type':              'item',
                'section_code':      current_sec,
                'sn':                sn_raw,
                'component':         _val(row, 1),
                'characteristic':    _val(row, 2),
                'check_class':       _val(row, 3),
                'type_of_check':     _val(row, 4),
                'quantum_m':         _val(row, 5),
                'quantum_sc':        _val(row, 6),
                'reference_docs':    _val(row, 7),
                'acceptance_norms':  _val(row, 8),
                'format_of_records': _val(row, 9),
                'agency':            _agency(row),
                'remarks':           _val(row, 14),
                'is_static':         is_static,
                'sort':              sort_counter,
            }
            last_item_idx = len(results)
            results.append(item)
            continue

        # ── Sub-characteristic row (empty SN, "b) Ash Content" style) ────────
        if not sn_raw and _val(row, 2) and last_item_idx is not None:
            sub_char = _val(row, 2)
            prev = results[last_item_idx]
            if prev['type'] == 'item':
                if prev['characteristic']:
                    prev['characteristic'] += '\n' + sub_char
                else:
                    prev['characteristic'] = sub_char
            continue

        # Anything else — skip
    return results


class Command(BaseCommand):
    help = 'Seed QAP template data from SAMPLE_QAP.xlsx into the database.'

    def add_arguments(self, parser):
        parser.add_argument('--file',    required=True,       help='Path to SAMPLE_QAP.xlsx')
        parser.add_argument('--replace', action='store_true', help='Wipe and re-seed if templates already exist')
        parser.add_argument('--dry-run', action='store_true', help='Print what would be created without touching the DB')

    def handle(self, *args, **options):
        if openpyxl is None:
            raise CommandError('openpyxl is not installed. Run: pip install openpyxl')

        from apps.core.models import QAPTemplate, QAPSection, QAPItem

        file_path = options['file']
        replace   = options['replace']
        dry_run   = options['dry_run']

        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
        except FileNotFoundError:
            raise CommandError(f'File not found: {file_path}')
        except Exception as e:
            raise CommandError(f'Could not open Excel file: {e}')

        self.stdout.write(f'Opened: {file_path}')
        self.stdout.write(f'Available sheets: {wb.sheetnames}')

        if replace and not dry_run:
            cats = [v['category'] for v in SHEET_MAP.values()]
            deleted = QAPTemplate.objects.filter(category__in=cats).delete()
            self.stdout.write(self.style.WARNING(f'Wiped existing QAP data: {deleted}'))

        totals = {'templates': 0, 'sections': 0, 'items': 0}

        for sheet_name, cfg in SHEET_MAP.items():
            category, display_name = cfg['category'], cfg['display_name']

            if sheet_name not in wb.sheetnames:
                self.stdout.write(self.style.WARNING(
                    f'  Sheet "{sheet_name}" not found — skipping {category}'))
                continue

            if not replace and QAPTemplate.objects.filter(category=category).exists():
                self.stdout.write(
                    f'  {category}: already seeded — skipping (use --replace to overwrite)')
                continue

            self.stdout.write(f'\n  Parsing sheet: "{sheet_name}" → {category}')
            ws   = wb[sheet_name]
            rows = _parse_sheet(ws)

            sections = [r for r in rows if r['type'] == 'section']
            items    = [r for r in rows if r['type'] == 'item']
            self.stdout.write(f'    Found {len(sections)} sections, {len(items)} items')

            if dry_run:
                for r in rows[:20]:
                    self.stdout.write(f'      {r}')
                if len(rows) > 20:
                    self.stdout.write(f'      ... ({len(rows) - 20} more rows)')
                continue

            template = QAPTemplate.objects.create(
                category=category,
                display_name=display_name,
                is_active=True,
            )
            totals['templates'] += 1

            section_objs = {}
            for sec in sections:
                obj = QAPSection.objects.create(
                    template=template,
                    section_code=sec['code'],
                    section_name=sec['name'],
                    sort_order=sec['sort'],
                )
                section_objs[sec['code']] = obj
                totals['sections'] += 1

            for item in items:
                sec_obj = section_objs.get(item['section_code'])
                if sec_obj is None:
                    self.stdout.write(self.style.WARNING(
                        f'    Orphan item {item["sn"]} (section {item["section_code"]}) — skipping'))
                    continue
                QAPItem.objects.create(
                    section=sec_obj,
                    sn=item['sn'],
                    component=item['component'],
                    characteristic=item['characteristic'],
                    check_class=item['check_class'],
                    type_of_check=item['type_of_check'],
                    quantum_m=item['quantum_m'],
                    quantum_sc=item['quantum_sc'],
                    reference_docs=item['reference_docs'],
                    acceptance_norms=item['acceptance_norms'],
                    format_of_records=item['format_of_records'],
                    agency=item['agency'],
                    remarks=item['remarks'],
                    is_static=item['is_static'],
                    sort_order=item['sort'],
                )
                totals['items'] += 1

            self.stdout.write(self.style.SUCCESS(
                f'    Created "{display_name}" — {len(sections)} sections, {len(items)} items'))

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN complete — no data written.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done. Created: {totals["templates"]} templates, '
                f'{totals["sections"]} sections, {totals["items"]} items.'))
