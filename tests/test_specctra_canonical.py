"""Fail-closed tests for private native-board DSN ordering canonicalization."""

from __future__ import annotations

import tracemalloc

import pytest

from circuit_weaver._specctra_canonical import _canonical_cycle, canonicalize_native_dsn


def _dsn(*, reverse: bool = False, design: str = "board", bad_image: str = "") -> str:
    image_a = '(image "Lib:Part::7" (outline (polygon F.Cu 10 0 0 10 0 10 10 0 10)) (pin 1 0 0 0))'
    image_b = '(image "Lib:Part" (outline (polygon F.Cu 10 0 10 10 10 10 0 0 0)) (pin 1 0 0 0))'
    if bad_image:
        image_b = image_b.replace("(pin 1 0 0 0)", bad_image)
    component_a = '(component "Lib:Part::7" (place B 20 5 front 90))'
    component_b = '(component "Lib:Part" (place A 10 5 front 0))'
    net_a = "(net Z_NET (pins B-1 A-1))"
    net_b = "(net A_NET (pins B-1 A-1))"
    if reverse:
        images = f"{image_b} {image_a}"
        components = f"{component_b} {component_a}"
        nets = f"{net_b} {net_a}"
    else:
        images = f"{image_a} {image_b}"
        components = f"{component_a} {component_b}"
        nets = f"{net_a} {net_b}"
    return f"""(pcb "{design}"
  (parser (string_quote ") (host_cad "KiCad's Pcbnew"))
  (unit um)
  (structure (layer F.Cu (type signal)) (boundary (path pcb 0 0 0 10 0 10 10 0 10 0 0))
    (rule (width 2) (clearance 1)))
  (placement {components})
  (library {images} (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 3))))
  (network {nets} (class "Default" (circuit (use layer F.Cu))))
  (vendor-private (opaque "keep this exact (net fake)"))
)
"""


def test_canonicalizer_stabilizes_order_aliases_and_pin_sets_without_changing_binding():
    left = _dsn()
    right = _dsn(reverse=True)

    canonical_left, left_info = canonicalize_native_dsn(left)
    canonical_right, right_info = canonicalize_native_dsn(right)

    assert canonical_left == canonical_right
    assert left_info["semantic"] == right_info["semantic"]
    assert left_info["semantic"]["design_name"] == "board"
    assert left_info["idempotent"] is True
    assert right_info["idempotent"] is True
    assert left_info["semantic"]["net_membership"] == {
        "A_NET": ("A-1", "B-1"),
        "Z_NET": ("A-1", "B-1"),
    }
    assert canonical_left.count("(component Lib:Part") == 1
    assert canonical_left.count("(place ") == 2
    assert '(vendor-private (opaque "keep this exact (net fake)"))' in canonical_left
    assert "(rule (width 2) (clearance 1))" in canonical_left
    assert '(padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 3)))' in canonical_left


def test_canonicalizer_preserves_distinct_root_design_names_for_ses_binding():
    first, _ = canonicalize_native_dsn(_dsn(design="first.dsn"))
    second, _ = canonicalize_native_dsn(_dsn(design="second.dsn"))
    assert first != second
    assert first.startswith('(pcb "first.dsn"')
    assert second.startswith('(pcb "second.dsn"')


def test_canonicalizer_sorts_composite_quoted_pin_tokens_without_rewriting_them():
    text = _dsn().replace("A-1", '"A ref"-"PAD 1"')
    canonical, info = canonicalize_native_dsn(text)
    assert '"A ref"-"PAD 1"' in canonical
    assert info["semantic"]["net_membership"]["A_NET"] == ('"A ref"-"PAD 1"', "B-1")


@pytest.mark.parametrize(
    "bad_image",
    [
        "(mystery 1 2 3)",
        "(pin 1 0 0)",
    ],
)
def test_canonicalizer_rejects_unknown_or_malformed_image_records(bad_image):
    with pytest.raises(ValueError):
        canonicalize_native_dsn(_dsn(bad_image=bad_image))


@pytest.mark.parametrize(
    "bad_geometry",
    [
        "(outline (polygon F.Cu nope 0 0 10 0 10 10 0 10))",
        "(pin 1 0 NaN 0)",
    ],
)
def test_canonicalizer_rejects_malformed_geometry_numbers(bad_geometry):
    text = _dsn().replace("(outline (polygon F.Cu 10 0 0 10 0 10 10 0 10))", bad_geometry)
    if bad_geometry.startswith("(pin"):
        text = _dsn().replace("(pin 1 0 0 0)", bad_geometry, 1)
    with pytest.raises(ValueError, match="numeric|coordinate"):
        canonicalize_native_dsn(text)


def test_canonicalizer_rejects_unreferenced_or_ambiguous_aliases():
    text = _dsn().replace("(library ", '(library (image "Unused" (outline (polygon F.Cu 0 0 0 1 0 1 1 0 1))) ')
    with pytest.raises(ValueError, match="unreferenced"):
        canonicalize_native_dsn(text)


def test_canonicalizer_rejects_duplicate_place_reference_and_malformed_numeric_pose():
    duplicate = _dsn().replace(
        '(component "Lib:Part" (place A 10 5 front 0))', '(component "Lib:Part" (place B 10 5 front 0))'
    )
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_native_dsn(duplicate)
    malformed = _dsn().replace("(place A 10 5 front 0)", "(place A nope 5 front 0)")
    with pytest.raises(ValueError, match="numeric"):
        canonicalize_native_dsn(malformed)


def test_canonical_polygon_rotation_search_uses_bounded_temporary_memory():
    points = [(str(index), str(index + 1)) for index in range(1500)]
    tracemalloc.start()
    try:
        canonical = _canonical_cycle(points)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(canonical) == len(points)
    assert peak < 3 * 1024 * 1024
