"""Build an unlocked Specctra wiring continuation from a stock router SES.

The input DSN is retained byte-for-byte outside its empty ``wiring`` block.
Only the pinned two-copper-layer subset emitted by the native KiCad workflow
is accepted; ambiguous or unsupported records fail closed.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from circuit_weaver._specctra_canonical import (
    _direct_blocks,
    _head,
    _leading_tokens,
    _payload,
    _payload_tokens,
    _render_token,
    _replace_spans,
    _root,
    _token_value,
)

_UNIT_TO_UM = {
    "um": Decimal("1"),
    "mm": Decimal("1000"),
    "mil": Decimal("25.4"),
    "inch": Decimal("25400"),
}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def _one(blocks: list[tuple[int, int, str, str]], name: str, description: str) -> str:
    matches = [child for _start, _end, kind, child in blocks if kind.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(f"{description} must contain exactly one {name!r} block")
    return matches[0]


def _decimal(value: str, description: str) -> Decimal:
    if not _NUMBER.fullmatch(value):
        raise ValueError(f"{description} is not a finite decimal")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{description} is not a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{description} is not a finite decimal")
    if len(number.as_tuple().digits) > 40 or abs(number.as_tuple().exponent) > 30:
        raise ValueError(f"{description} exceeds the supported decimal precision")
    if number and not -30 <= number.adjusted() <= 18:
        raise ValueError(f"{description} is outside the supported numeric range")
    return number


def _resolution(block: str, description: str) -> tuple[str, Decimal]:
    atoms = _leading_tokens(block)
    if len(atoms) != 2:
        raise ValueError(f"{description} resolution must declare one unit and one scale")
    unit = atoms[0].casefold()
    if unit not in _UNIT_TO_UM:
        raise ValueError(f"unsupported {description} resolution unit {atoms[0]!r}")
    scale = _decimal(atoms[1], f"{description} resolution scale")
    if scale <= 0:
        raise ValueError(f"{description} resolution scale must be positive")
    return unit, scale


def _dsn_value(value: str, *, ses_unit: str, ses_scale: Decimal, dsn_unit: str, description: str) -> Decimal:
    """Convert a session-resolution value exactly into the declared DSN unit."""
    source = _decimal(value, description)
    exact = Fraction(source) * Fraction(_UNIT_TO_UM[ses_unit]) / Fraction(ses_scale) / Fraction(_UNIT_TO_UM[dsn_unit])
    numerator, denominator = exact.numerator, exact.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    places = max(twos, fives)
    if denominator != 1 or places > 30:
        raise ValueError(f"{description} cannot be represented as a bounded exact DSN decimal")
    if places:
        numerator *= 2 ** (places - twos) * 5 ** (places - fives)
    magnitude_digits = tuple(int(digit) for digit in str(abs(numerator))) or (0,)
    converted = Decimal((int(numerator < 0), magnitude_digits, -places))
    if Fraction(converted) != exact:
        raise ValueError(f"{description} could not be represented exactly")
    if abs(converted) > Decimal("1e12"):
        raise ValueError(f"{description} is outside the supported DSN coordinate range")
    return converted


def _format_decimal(value: Decimal) -> str:
    """Render an exact finite decimal without exponent notation or trailing zeroes."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "+0", ""} else rendered


def _children(block: str) -> list[tuple[int, int, str, str]]:
    return _direct_blocks(block)


def _named_records(block: str, kind: str, description: str) -> list[str]:
    records: list[str] = []
    seen: set[str] = set()
    for _start, _end, child_kind, child in _children(block):
        if child_kind.casefold() != kind.casefold():
            continue
        tokens = _leading_tokens(child)
        if len(tokens) != 1 or not tokens[0] or tokens[0] in seen:
            raise ValueError(f"{description} contains a missing, malformed, or duplicate {kind!r} name")
        seen.add(tokens[0])
        records.append(tokens[0])
    return records


def _parse_dsn(dsn_text: str) -> tuple[str, int, int, str, set[str], set[str], str]:
    root, root_start, root_end = _root(dsn_text)
    root_blocks = _children(root)
    if _head(root)[0].casefold() != "pcb":
        raise ValueError("input is not a Specctra DSN pcb root")

    structure = _one(root_blocks, "structure", "DSN pcb")
    library = _one(root_blocks, "library", "DSN pcb")
    network = _one(root_blocks, "network", "DSN pcb")
    wiring = _one(root_blocks, "wiring", "DSN pcb")
    unit_block = _one(root_blocks, "unit", "DSN pcb")
    resolution_block = _one(root_blocks, "resolution", "DSN pcb")
    del resolution_block  # DSN numeric coordinates are in the explicitly declared unit.

    unit_tokens = _leading_tokens(unit_block)
    if len(unit_tokens) != 1 or unit_tokens[0].casefold() not in _UNIT_TO_UM:
        raise ValueError("DSN must declare one supported coordinate unit")
    dsn_unit = unit_tokens[0].casefold()

    layers: list[str] = []
    via_names: set[str] = set()
    for _start, _end, kind, child in _children(structure):
        folded = kind.casefold()
        if folded in {"plane", "copper", "zone"}:
            raise ValueError("continuation requires a fresh DSN without planes, copper, or zones")
        if folded == "layer":
            tokens = _leading_tokens(child)
            if len(tokens) != 1 or not tokens[0]:
                raise ValueError("DSN structure contains a malformed layer declaration")
            layer_types = [
                _leading_tokens(attr)
                for _a, _b, attr_kind, attr in _children(child)
                if attr_kind.casefold() == "type"
            ]
            if layer_types != [["signal"]]:
                raise ValueError("continuation requires signal copper layers")
            layers.append(tokens[0])
        elif folded == "via":
            tokens = _leading_tokens(child)
            if len(tokens) != 1 or not tokens[0] or tokens[0] in via_names:
                raise ValueError("DSN structure has a malformed or duplicate via declaration")
            via_names.add(tokens[0])

    if len(layers) != 2 or len({name.casefold() for name in layers}) != 2:
        raise ValueError("continuation supports exactly two distinct copper layers")
    if any(not name.casefold().endswith(".cu") for name in layers):
        raise ValueError("continuation supports only copper signal layers")

    padstack_names = _named_records(library, "padstack", "DSN library")
    if via_names - set(padstack_names):
        raise ValueError("DSN via declaration references an undeclared padstack")
    if not via_names:
        raise ValueError("DSN declares no supported via padstack")

    net_names: set[str] = set()
    for _start, _end, kind, child in _children(network):
        if kind.casefold() == "class":
            continue
        if kind.casefold() != "net":
            raise ValueError(f"unsupported DSN network record {kind!r}")
        tokens = _leading_tokens(child)
        if len(tokens) != 1 or not tokens[0] or tokens[0] in net_names:
            raise ValueError("DSN network has a malformed or duplicate net name")
        net_names.add(tokens[0])
    if not net_names:
        raise ValueError("DSN network contains no nets")

    if _payload(wiring).strip():
        raise ValueError("DSN wiring block is not empty")
    if _children(wiring):
        raise ValueError("DSN wiring block contains pre-existing records")
    return root, root_start, root_end, dsn_unit, net_names, via_names, wiring


def _placement_geometry(
    placement: str, *, unit: str, scale: Decimal, dsn_unit: str
) -> dict[str, tuple[str, Decimal, Decimal, str, Decimal]]:
    geometry: dict[str, tuple[str, Decimal, Decimal, str, Decimal]] = {}
    for _start, _end, kind, component in _children(placement):
        if kind.casefold() == "resolution":
            continue
        if kind.casefold() != "component":
            raise ValueError(f"unsupported placement record {kind!r}")
        component_tokens = _leading_tokens(component)
        if len(component_tokens) != 1:
            raise ValueError("placement component must declare exactly one footprint image")
        image = component_tokens[0]
        for _ps, _pe, place_kind, place in _children(component):
            if place_kind.casefold() != "place":
                raise ValueError(f"unsupported component placement record {place_kind!r}")
            atoms = _leading_tokens(place)
            if len(atoms) != 5:
                raise ValueError("placement record must contain reference, position, side, and angle")
            reference = atoms[0]
            if reference in geometry:
                raise ValueError(f"duplicate placement for {reference!r}")
            x = _dsn_value(atoms[1], ses_unit=unit, ses_scale=scale, dsn_unit=dsn_unit, description="placement x")
            y = _dsn_value(atoms[2], ses_unit=unit, ses_scale=scale, dsn_unit=dsn_unit, description="placement y")
            angle = _decimal(atoms[4], "placement angle") % Decimal(360)
            if angle < 0:
                angle += Decimal(360)
            geometry[reference] = (image, x, y, atoms[3].casefold(), angle)
    return geometry


def _validate_placement(dsn_root: str, session_root: str, *, dsn_unit: str) -> None:
    dsn_placement = _one(_children(dsn_root), "placement", "DSN pcb")
    ses_placement = _one(_children(session_root), "placement", "SES session")
    ses_resolution = [child for _a, _b, kind, child in _children(ses_placement) if kind.casefold() == "resolution"]
    if len(ses_resolution) > 1:
        raise ValueError("SES placement contains ambiguous resolution declarations")
    # DSN placement values are in DSN units. SES placement values use its own
    # resolution, which is normally repeated inside the placement block.
    if ses_resolution:
        ses_unit, ses_scale = _resolution(ses_resolution[0], "SES placement")
    else:
        ses_unit, ses_scale = dsn_unit, Decimal(1)
    dsn_geometry = _placement_geometry(dsn_placement, unit=dsn_unit, scale=Decimal(1), dsn_unit=dsn_unit)
    ses_geometry = _placement_geometry(ses_placement, unit=ses_unit, scale=ses_scale, dsn_unit=dsn_unit)
    if dsn_geometry != ses_geometry:
        raise ValueError("SES placement differs from the original DSN placement")


def _parse_wire(
    wire: str,
    *,
    net: str,
    ses_unit: str,
    ses_scale: Decimal,
    dsn_unit: str,
    layers: set[str],
) -> tuple[str, int]:
    if _leading_tokens(wire):
        raise ValueError("SES wire contains unsupported header attributes")
    children = _children(wire)
    paths = [child for _start, _end, kind, child in children if kind.casefold() == "path"]
    extras = [kind for _start, _end, kind, _child in children if kind.casefold() != "path"]
    if len(paths) != 1 or extras:
        raise ValueError("SES wire must contain exactly one supported path")
    if _payload(wire).split() and not children:
        raise ValueError("SES wire contains unsupported payload")
    atoms = _payload_tokens(_payload(paths[0]))
    if len(atoms) < 6 or (len(atoms) - 2) % 2:
        raise ValueError("SES path must contain a layer, width, and at least two coordinate pairs")
    layer = _token_value(atoms[0])
    if layer not in layers:
        raise ValueError(f"SES path references undeclared DSN layer {layer!r}")
    width = _dsn_value(
        atoms[1], ses_unit=ses_unit, ses_scale=ses_scale, dsn_unit=dsn_unit, description="SES path width"
    )
    if width <= 0:
        raise ValueError("SES path width must be positive")
    coords = [
        _dsn_value(atom, ses_unit=ses_unit, ses_scale=ses_scale, dsn_unit=dsn_unit, description="SES coordinate")
        for atom in atoms[2:]
    ]
    pairs = list(zip(coords[::2], coords[1::2], strict=True))
    if len(pairs) < 2 or len(set(pairs)) < 2:
        raise ValueError("SES path is degenerate after conversion to DSN units")
    path_fields = [_render_token(layer), _format_decimal(width), *(_format_decimal(value) for value in coords)]
    path = "(path " + " ".join(path_fields) + ")"
    return f"(wire {path} (net {_render_token(net)}) (type normal))", 1


def _parse_via(
    via: str,
    *,
    net: str,
    ses_unit: str,
    ses_scale: Decimal,
    dsn_unit: str,
    dsn_vias: set[str],
    ses_vias: set[str],
) -> str:
    if _children(via):
        raise ValueError("SES via contains unsupported nested records")
    atoms = _payload_tokens(_payload(via))
    if len(atoms) != 3:
        raise ValueError("SES via must contain a padstack and one coordinate pair")
    name = _token_value(atoms[0])
    if name not in dsn_vias or name not in ses_vias:
        raise ValueError(f"SES via references undeclared padstack {name!r}")
    x = _dsn_value(atoms[1], ses_unit=ses_unit, ses_scale=ses_scale, dsn_unit=dsn_unit, description="SES via x")
    y = _dsn_value(atoms[2], ses_unit=ses_unit, ses_scale=ses_scale, dsn_unit=dsn_unit, description="SES via y")
    fields = f"{_render_token(name)} {_format_decimal(x)} {_format_decimal(y)}"
    return f"(via {fields} (net {_render_token(net)}) (type normal))"


def _parse_session(
    session_text: str,
    *,
    dsn_unit: str,
    net_names: set[str],
    dsn_vias: set[str],
    layers: set[str],
) -> tuple[list[str], dict[str, Any]]:
    session_text = session_text.strip()
    session_kind, _ = _head(session_text)
    if session_kind.casefold() != "session":
        raise ValueError("input is not a Specctra SES session")
    blocks = _children(session_text)
    allowed_root = {"base_design", "placement", "was_is", "routes"}
    if any(kind.casefold() not in allowed_root for _start, _end, kind, _child in blocks):
        raise ValueError("SES contains an unsupported session-level record")
    _one(blocks, "base_design", "SES session")
    routes = _one(blocks, "routes", "SES session")
    was_is = _one(blocks, "was_is", "SES session")
    if _payload(was_is).strip() or _children(was_is):
        raise ValueError("SES was_is block must be empty")
    route_blocks = _children(routes)
    allowed_routes = {"resolution", "parser", "library_out", "network_out"}
    if any(kind.casefold() not in allowed_routes for _start, _end, kind, _child in route_blocks):
        raise ValueError("SES routes contain an unsupported record")
    resolution = _one(route_blocks, "resolution", "SES routes")
    ses_unit, ses_scale = _resolution(resolution, "SES")
    library_out = _one(route_blocks, "library_out", "SES routes")
    network_out = _one(route_blocks, "network_out", "SES routes")

    ses_vias: set[str] = set()
    for _start, _end, kind, child in _children(library_out):
        if kind.casefold() != "padstack":
            raise ValueError(f"unsupported SES library_out record {kind!r}")
        tokens = _leading_tokens(child)
        if len(tokens) != 1 or not tokens[0] or tokens[0] in ses_vias:
            raise ValueError("SES library_out has a malformed or duplicate padstack")
        if tokens[0] not in dsn_vias:
            raise ValueError(f"SES library_out padstack {tokens[0]!r} is not declared by the DSN")
        ses_vias.add(tokens[0])

    rendered_records: list[str] = []
    seen_nets: set[str] = set()
    net_count = wire_count = via_count = 0
    for _start, _end, kind, net in _children(network_out):
        if kind.casefold() != "net":
            raise ValueError(f"unsupported SES network_out record {kind!r}")
        tokens = _leading_tokens(net)
        if len(tokens) != 1 or not tokens[0] or tokens[0] in seen_nets:
            raise ValueError("SES network_out has a malformed or duplicate net name")
        name = tokens[0]
        if name not in net_names:
            raise ValueError(f"SES routes undeclared DSN net {name!r}")
        seen_nets.add(name)
        records: list[str] = []
        for _rs, _re, route_kind, route in _children(net):
            if route_kind.casefold() == "wire":
                rendered, count = _parse_wire(
                    route,
                    net=name,
                    ses_unit=ses_unit,
                    ses_scale=ses_scale,
                    dsn_unit=dsn_unit,
                    layers=layers,
                )
                records.append(rendered)
                wire_count += count
            elif route_kind.casefold() == "via":
                records.append(
                    _parse_via(
                        route,
                        net=name,
                        ses_unit=ses_unit,
                        ses_scale=ses_scale,
                        dsn_unit=dsn_unit,
                        dsn_vias=dsn_vias,
                        ses_vias=ses_vias,
                    )
                )
                via_count += 1
            else:
                raise ValueError(f"unsupported SES route record {route_kind!r}")
        if not records:
            raise ValueError(f"SES routed net {name!r} has no wire or via records")
        if len(records) != len(set(records)):
            raise ValueError(f"SES net {name!r} contains duplicate routed records")
        rendered_records.extend("    " + record for record in records)
        net_count += 1

    if not rendered_records:
        raise ValueError("SES network_out contains no supported routed nets")
    report = {
        "net_count": net_count,
        "wire_count": wire_count,
        "via_count": via_count,
        "dsn_unit": dsn_unit,
        "ses_resolution_unit": ses_unit,
        "ses_resolution_scale": str(ses_scale),
        "record_type": "normal",
        "coordinate_conversion": "exact Decimal conversion; source SES grid retained",
    }
    return rendered_records, report


def continuation_dsn(dsn_text: str, session_text: str) -> tuple[str, dict[str, Any]]:
    """Insert stock SES routes into the DSN's one empty wiring block.

    Every generated path and via is marked ``(type normal)`` so a subsequent
    stock router pass can reroute it. Any unsupported record or ambiguous
    declaration is rejected before returning a modified DSN.
    """
    root, root_start, root_end, dsn_unit, net_names, dsn_vias, wiring = _parse_dsn(dsn_text)
    session_text = session_text.strip()
    _validate_placement(root, session_text, dsn_unit=dsn_unit)
    structure = _one(_children(root), "structure", "DSN pcb")
    layers = {
        tokens[0]
        for _start, _end, kind, child in _children(structure)
        if kind.casefold() == "layer"
        for tokens in [_leading_tokens(child)]
        if len(tokens) == 1
    }
    rendered_records, report = _parse_session(
        session_text,
        dsn_unit=dsn_unit,
        net_names=net_names,
        dsn_vias=dsn_vias,
        layers=layers,
    )

    wiring_start, wiring_end = next(
        (start, end)
        for start, end, kind, _child in _children(root)
        if kind.casefold() == "wiring"
    )
    rendered_wiring = "(wiring\n" + "\n".join(rendered_records) + "\n  )"
    updated_root = _replace_spans(root, {(wiring_start, wiring_end): rendered_wiring})
    updated_dsn = dsn_text[:root_start] + updated_root + dsn_text[root_end:]
    return updated_dsn, report
