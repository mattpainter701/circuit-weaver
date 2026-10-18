"""Circuit Weaver.

Programmatic circuit design, validation, and KiCad artifact generation.
"""

from .design_docs import (
    generate_all_docs,
    generate_assembly_guide_csv,
    generate_datasheet_index,
    generate_ordering_checklist,
    generate_power_budget_csv,
)
from .design_scorer import score_design_comprehensive
from .dfm_checker import check_dfm, dfm_report
from .enclosure_designer import generate_enclosure_scad, render_enclosure_stl
from .kicad_placement_api import check_kicad_available, detect_kicad_version, update_board_placements
from .pcb_copper_cleanup import cleanup_copper
from .pcb_return_paths import refine_return_paths
from .review_report import generate_review_report_html
from .sourcing_auditor import audit_bom

__all__ = [
    "__version__",
    "audit_bom",
    "check_dfm",
    "cleanup_copper",
    "dfm_report",
    "generate_all_docs",
    "generate_assembly_guide_csv",
    "generate_datasheet_index",
    "generate_enclosure_scad",
    "generate_ordering_checklist",
    "generate_power_budget_csv",
    "generate_review_report_html",
    "render_enclosure_stl",
    "refine_return_paths",
    "score_design_comprehensive",
    "check_kicad_available",
    "detect_kicad_version",
    "update_board_placements",
]

__version__ = "0.35.0"
