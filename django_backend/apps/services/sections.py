"""
apps/services/sections.py — Canonical parameter group order for TDS documents.

This list defines:
  1. The order in which sections appear on the printed/PDF TDS.
  2. The rendering sequence used by the WeasyPrint PDF renderer.

The group names here must match the ``parameter_group`` column values in the
``tds_parameters`` table exactly (case-sensitive).
"""

PARAMETER_GROUP_ORDER: tuple[str, ...] = (
    "General Information",
    "Dimensional Parameters",
    "Belt Construction Parameters",
    "Fabric Parameters",
    "Cover Rubber Properties",
    "After Ageing Cover Rubber Properties",
    "Belt Breaking Strength",
    "Adhesion Values",
    "Troughability",
    "Recommended Minimum Pulley Diameter for Load 60% UP TO 100%",
    # FR / fire-resistant belts (brand_id=3). No-op for other brands — renderer
    # skips any group with no parameters for the current brand.
    "Flame Test",
    "Drum Friction Test",
    "Electrical Surface Resistant Test (Antistatic Test)",
    # OR / oil-resistant belts (brand_id=4). Same no-op rule.
    "Volume Swelling (In 70% Iso-Octane + 30% Toluene OR Petroleum Oil for 70 +/- 2 Hours @ 27 +/- 1 Degree Celsius)",
    "Sampling and Testing",
    "Packing and Logistics",
    "Splicing Parameters",
)

# Groups left out of a "Customer Copy" PDF by default — internal-only detail
# (how the belt is built/tested/packed/spliced) that customers don't need.
# This is the server-side twin of frontend/tds-preview.html's
# DEFAULT_UNCHECKED_GROUPS constant (used to default the single-record
# preview's checkboxes) — keep both lists in sync if either changes. This
# tuple is what batch_views.py's ZIP download and print-all endpoints use to
# build "Customer Copy" output; "Internal Copy" output passes no exclusions
# at all, i.e. every group from PARAMETER_GROUP_ORDER above.
CUSTOMER_COPY_EXCLUDE_GROUPS: tuple[str, ...] = (
    "Fabric Parameters",
    "Sampling and Testing",
    "Packing and Logistics",
    "Splicing Parameters",
)
