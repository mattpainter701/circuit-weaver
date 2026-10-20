from __future__ import annotations

import pytest

from circuit_weaver.allocator import SheetAllocation
from circuit_weaver.component_db import ComponentDef, PinDef
from circuit_weaver.generator import (
    _pick_root_sheet_geometry,
    _root_sheet_height_for_ports,
)
from circuit_weaver.placer import split_sheet_allocation
from circuit_weaver.primitives import snap


def test_root_hierarchy_port_height_fits_standard_page_at_capacity() -> None:
    # Four child sheets with 297 ports fit the A0 usable height, including
    # the root-sheet top margin and title block reservation.
    height = _root_sheet_height_for_ports(297)
    paper, *_ = _pick_root_sheet_geometry(4, snap(50), height)
    assert paper == "A0"


def test_root_hierarchy_port_height_fails_closed_above_a0_capacity() -> None:
    # The relay stress fixture's 320-port child previously emitted an A0 root
    # page with pins beyond the physical page edge.
    height = _root_sheet_height_for_ports(320)
    with pytest.raises(ValueError, match="does not fit within standard KiCad pages through A0"):
        _pick_root_sheet_geometry(4, snap(50), height)


def test_sheet_split_partitions_scoped_and_unscoped_annotations_once() -> None:
    components = [
        ComponentDef(
            mpn=f"TEST_{ref}",
            ref_prefix="U",
            source_ref=ref,
            pins=[PinDef("1", "SIG", "bidirectional", "L")],
        )
        for ref in ("U1", "U2", "U10", "U20")
    ]
    notes = ["Scoped note for U1", "Scoped note for U10", "Project note A", "Project note B"]
    allocation = SheetAllocation(
        name="main", title="Main", paper="A3", components=components,
        sheet_annotations=notes,
    )

    halves = split_sheet_allocation(allocation)

    assert halves is not None
    combined = halves[0].sheet_annotations + halves[1].sheet_annotations
    assert sorted(combined) == sorted(notes)
    assert "Scoped note for U1" in halves[0].sheet_annotations
    assert "Scoped note for U1" not in halves[1].sheet_annotations
    assert "Scoped note for U10" in halves[1].sheet_annotations
    assert "Scoped note for U10" not in halves[0].sheet_annotations
    assert abs(len(halves[0].sheet_annotations) - len(halves[1].sheet_annotations)) <= 1
