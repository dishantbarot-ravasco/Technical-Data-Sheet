"""
apps/services/pdf_renderer.py — HTML + PDF rendering layer for TDS documents.

Django port of FastAPI services/pdf_renderer.py.

Template/static assets now live INSIDE django_backend (self-contained — no
dependency on the archived FastAPI tree):
  tds_app/django_backend/apps/services/pdf_renderer.py   (this file)
  tds_app/django_backend/apps/services/templates/tds.html
  tds_app/django_backend/apps/services/static/indus_logo.png
  tds_app/django_backend/apps/services/static/tuv_logo.png

Previously this pointed at tds_app/backend/{templates,static}/, a directory
that no longer exists on disk (it was renamed to backend_archived/), which
made every PDF/HTML render fail with a Jinja2 TemplateNotFound error. Fixed
by copying the template + logos alongside this module and resolving paths
relative to this file instead of climbing out to tds_app/.

Public API:
    render_tds_html(doc) → str        — returns raw HTML (useful for debugging)
    render_tds_pdf(doc)  → bytes      — returns a PDF binary blob
"""
from __future__ import annotations

import base64
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from apps.services.pdf_service import TDSDocData, TDS_NOTES

# ── File-system paths ─────────────────────────────────────────────────────────
# Path(__file__).parent = apps/services/  → templates/ and static/ live right here.
_SERVICES_DIR  = Path(__file__).parent
_TEMPLATES_DIR = _SERVICES_DIR / "templates"
_STATIC_DIR    = _SERVICES_DIR / "static"

# ── Jinja2 environment ────────────────────────────────────────────────────────
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _logo_data_uri(filename: str) -> str:
    """
    Read a logo image from backend/static/ and return a base64 data URI.
    Returns '' if the file does not exist (template handles missing logos gracefully).
    """
    path = _STATIC_DIR / filename
    if not path.exists():
        return ""
    suffix = path.suffix.lstrip(".").lower()
    mime = "image/png" if suffix == "png" else f"image/{suffix}"
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def render_tds_html(
    doc: TDSDocData,
    exclude_groups: list[str] | None = None,
    exclude_gi_fields: list[str] | None = None,
    show_test_method: bool = True,
    show_reference: bool = True,
) -> str:
    """Render a TDSDocData object to a complete HTML string via Jinja2."""
    template = _jinja_env.get_template("tds.html")
    return template.render(
        doc=doc,
        notes=TDS_NOTES,
        indus_logo=_logo_data_uri("indus_logo.png"),
        tuv_logo=_logo_data_uri("tuv_logo.png"),
        dash="-",
        exclude_groups=set(exclude_groups or []),
        exclude_gi_fields=set(exclude_gi_fields or []),
        show_test_method=show_test_method,
        show_reference=show_reference,
    )


def render_tds_pdf(
    doc: TDSDocData,
    exclude_groups: list[str] | None = None,
    exclude_gi_fields: list[str] | None = None,
    show_test_method: bool = True,
    show_reference: bool = True,
) -> bytes:
    """
    Render a TDSDocData object to PDF bytes via WeasyPrint.
    WeasyPrint is imported lazily to avoid slowing down server startup.
    """
    from weasyprint import HTML  # lazy import  # noqa: PLC0415

    html_str = render_tds_html(
        doc,
        exclude_groups=exclude_groups,
        exclude_gi_fields=exclude_gi_fields,
        show_test_method=show_test_method,
        show_reference=show_reference,
    )
    base_url = str(_STATIC_DIR) + "/"
    return HTML(string=html_str, base_url=base_url).write_pdf(optimize_images=True)


# ─── QAP rendering ───────────────────────────────────────────────────────────

def render_qap_html(context: dict) -> str:
    """
    Render the QAP Jinja2 template to an HTML string.
    context must be the dict returned by qap_service.build_qap_context().
    """
    template = _jinja_env.get_template("qap.html")
    return template.render(**context)


def render_qap_pdf(context: dict) -> bytes:
    """
    Render QAP to PDF bytes via WeasyPrint.
    WeasyPrint is imported lazily to avoid slowing down server startup.
    """
    from weasyprint import HTML  # lazy import  # noqa: PLC0415

    html_str = render_qap_html(context)
    base_url = str(_STATIC_DIR) + "/"
    return HTML(string=html_str, base_url=base_url).write_pdf(optimize_images=True)
