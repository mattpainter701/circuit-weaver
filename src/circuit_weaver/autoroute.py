"""Fail-closed Freerouting integration for KiCad boards and Specctra files.

Callers may provide a ``.dsn`` directly or export a real ``.kicad_pcb`` through
KiCad's native pcbnew API. Optional board output imports the resulting session
and runs KiCad DRC before publication. The CLI exporter is used when available.

Circuit Weaver placement previews are always rejected: they contain placement
hints but intentionally contain no real pads.  Circuit Weaver never invokes
Freerouting's ``-dr`` option as a board input: upstream defines it as a design
rules file, not a direct KiCad routing mode.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ._specctra_canonical import canonicalize_native_dsn

log = logging.getLogger(__name__)

_EFFORT_PASSES = {"fast": 100, "medium": 500, "high": 1000}
_PREVIEW_GENERATOR_RE = re.compile(r'\(generator\s+"[^"]*placement_preview[^"]*"\)')
_FOOTPRINT_RE = re.compile(r"\(footprint\s")
_PAD_RE = re.compile(r"\(pad\s")
_NET_DECL_RE = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
_SPECCTRA_TOKEN = r'"(?:\\.|[^"\\])*"|[^\s()]+'
_FREEROUTING_VERSION_RE = re.compile(r"\bFreerouting\s+v?([0-9]+(?:\.[0-9]+){1,3}(?:[-+][\w.-]+)?)", re.IGNORECASE)
_MIN_ARTIFACT_BYTES = 48
_SPECCTRA_LITERAL_STRING_QUOTE_RE = re.compile(
    r'(\(string_quote\s+)"(?=\s*\))(?!\s*\)\s*"\s*\))',
    re.IGNORECASE,
)
_SPECCTRA_CLEARANCE_VALUE_RE = re.compile(
    r"(?i)(\(clearance\s+)([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?=\s|\))"
)
_NATIVE_BOARD_ROUTING_CLEARANCE_MARGIN_MM = 0.01


def _specctra_scan_text(text: str) -> str:
    """Neutralize Specctra's canonical literal quote declaration for scanning.

    Real DSN producers, including Freerouting's own fixture corpus, commonly
    emit ``(string_quote ")``: the quote character is the declared delimiter,
    not the start of a quoted string.  Replace only that literal character
    with a same-width sentinel so balance/block scanners retain exact indices.
    The older explicitly quoted spelling used by some fixtures remains
    untouched.
    """
    return _SPECCTRA_LITERAL_STRING_QUOTE_RE.sub(r"\1q", text)


def _result_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "message": message, **extra}


def _iter_sexpr_blocks(text: str, keyword: str):
    """Yield balanced top-level blocks beginning with ``(keyword``.

    This is intentionally a small scanner, not a KiCad parser.  It is enough
    to distinguish net declarations inside pads from board-level declarations
    without accepting arbitrary text that happens to contain ``(pad``.
    """
    for start, end in _iter_sexpr_block_spans(text, keyword):
        yield text[start:end]


def _iter_sexpr_block_spans(text: str, keyword: str):
    """Yield source offsets for balanced blocks, excluding quoted lookalikes."""
    target = keyword.casefold()
    scan_text = _specctra_scan_text(text)
    index = 0
    quoted = False
    escaped = False
    while index < len(scan_text):
        char = scan_text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quoted and char == "\\":
            escaped = True
            index += 1
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        if quoted or char != "(":
            index += 1
            continue

        name_start = index + 1
        name_end = name_start + len(keyword)
        delimiter = scan_text[name_end : name_end + 1]
        invalid_delimiter = delimiter and not delimiter.isspace() and delimiter != ")"
        if scan_text[name_start:name_end].casefold() != target or invalid_delimiter:
            index += 1
            continue

        block_start = index
        depth = 0
        block_quoted = False
        block_escaped = False
        for block_end in range(block_start, len(scan_text)):
            block_char = scan_text[block_end]
            if block_escaped:
                block_escaped = False
                continue
            if block_quoted and block_char == "\\":
                block_escaped = True
                continue
            if block_char == '"':
                block_quoted = not block_quoted
                continue
            if block_quoted:
                continue
            if block_char == "(":
                depth += 1
            elif block_char == ")":
                depth -= 1
                if depth == 0:
                    yield block_start, block_end + 1
                    index = block_end + 1
                    break
        else:
            return


def _first_sexpr_block(text: str, keyword: str) -> str | None:
    return next(iter(_iter_sexpr_blocks(text, keyword)), None)


def _block_payload(block: str, keyword: str) -> str:
    opener = re.match(rf"\({re.escape(keyword)}(?=\s|\))", block, re.IGNORECASE)
    if opener is None or not block.endswith(")"):
        return ""
    return block[opener.end() : -1].strip()


def _decode_specctra_token(token: str) -> str:
    if token.startswith('"') and token.endswith('"'):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            value = token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return str(value)
    return token


def _first_payload_token(block: str, keyword: str) -> str | None:
    match = re.match(_SPECCTRA_TOKEN, _block_payload(block, keyword))
    return _decode_specctra_token(match.group(0)) if match else None


def _replace_blocks_at_spans(text: str, spans: list[tuple[int, int]], blocks: list[str]) -> str:
    """Replace balanced blocks in their existing slots, preserving all gap bytes."""
    if len(spans) != len(blocks):
        raise ValueError("Cannot safely reorder Specctra records: block count changed")
    for (start, end), block in reversed(list(zip(spans, blocks, strict=True))):
        text = text[:start] + block + text[end:]
    return text


def _native_placement_variant(text: str, order: str, *, rotation: int = 0) -> tuple[str, list[str]]:
    """Reorder only KiCad-exported placement records and report their reference order.

    Component and place S-expressions are moved as original byte spans. All
    other placement contents and every geometry, net, rule, and library record
    remain byte-identical. Ambiguous identities fail closed.
    """
    if order not in {"ascending", "descending", "original", "rotated"}:
        raise ValueError(f"Unsupported native placement order {order!r}")
    placement_spans = list(_iter_sexpr_block_spans(text, "placement"))
    if len(placement_spans) != 1:
        raise ValueError("Cannot safely reorder native DSN: expected exactly one placement section")
    placement_start, placement_end = placement_spans[0]
    placement = text[placement_start:placement_end]
    component_spans = list(_iter_sexpr_block_spans(placement, "component"))
    if not component_spans:
        raise ValueError("Cannot safely reorder native DSN: placement section has no components")

    entries: list[dict[str, Any]] = []
    all_references: list[str] = []
    for start, end in component_spans:
        block = placement[start:end]
        component_name = _first_payload_token(block, "component")
        place_spans = list(_iter_sexpr_block_spans(block, "place"))
        references: list[str] = []
        place_blocks: list[str] = []
        for place_start, place_end in place_spans:
            place_block = block[place_start:place_end]
            tokens = _payload_tokens(_block_payload(place_block, "place"))
            if not tokens:
                raise ValueError("Cannot safely reorder native DSN: malformed place record")
            reference = _decode_specctra_token(tokens[0])
            if not reference or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", reference):
                raise ValueError("Cannot safely reorder native DSN: place record has an invalid reference")
            references.append(reference)
            all_references.append(reference)
            place_blocks.append(place_block)
        if not component_name or not references:
            raise ValueError("Cannot safely reorder native DSN: component identity is ambiguous")
        if len(references) != len(set(references)):
            raise ValueError("Cannot safely reorder native DSN: duplicate footprint references")
        nested_order = references
        if order == "ascending" or order == "rotated":
            nested_order = sorted(references)
        elif order == "descending":
            nested_order = sorted(references, reverse=True)
        if order != "original":
            by_reference = dict(zip(references, place_blocks, strict=True))
            reordered_places = [by_reference[reference] for reference in nested_order]
            block = _replace_blocks_at_spans(block, place_spans, reordered_places)
        entries.append(
            {
                "span": (start, end),
                "block": block,
                "minimum_reference": min(references),
                "references": nested_order,
            }
        )

    if len(all_references) != len(set(all_references)):
        raise ValueError("Cannot safely reorder native DSN: duplicate footprint references")
    if order == "ascending" or order == "rotated":
        ordered_entries = sorted(entries, key=lambda entry: entry["minimum_reference"])
        if order == "rotated" and ordered_entries:
            offset = rotation % len(ordered_entries)
            ordered_entries = ordered_entries[offset:] + ordered_entries[:offset]
    elif order == "descending":
        ordered_entries = sorted(entries, key=lambda entry: entry["minimum_reference"], reverse=True)
    else:
        ordered_entries = entries

    placement = _replace_blocks_at_spans(
        placement,
        component_spans,
        [entry["block"] for entry in ordered_entries],
    )
    reordered_text = text[:placement_start] + placement + text[placement_end:]
    reference_order = [reference for entry in ordered_entries for reference in entry["references"]]
    return reordered_text, reference_order


def _apply_specctra_clearance_margin(path: Path, margin_mm: float) -> dict[str, Any]:
    """Tighten a KiCad-exported DSN's router rules without changing board DRC.

    Round-trip evidence showed a routed clearance can return slightly below
    the router's nominal value in native DRC. This margin gives the router
    additional headroom; source PCB geometry and native project rules remain
    unchanged.
    """
    if not math.isfinite(margin_mm) or margin_mm <= 0:
        raise ValueError("Specctra clearance margin must be finite and positive")
    text = path.read_text(encoding="utf-8")
    unit_spans = list(_iter_sexpr_block_spans(text, "unit"))
    if len(unit_spans) != 1:
        raise ValueError("Cannot safely apply routing margin: DSN must declare exactly one unit")
    unit_start, unit_end = unit_spans[0]
    unit_block = text[unit_start:unit_end]
    unit = (_first_payload_token(unit_block, "unit") or "").casefold()
    unit_scale = {
        "um": Decimal("1000"),
        "mm": Decimal("1"),
        "mil": Decimal("1") / Decimal("0.0254"),
        "inch": Decimal("1") / Decimal("25.4"),
        "cm": Decimal("0.1"),
    }.get(unit)
    if unit_scale is None:
        raise ValueError(f"Cannot safely apply routing margin: unsupported DSN unit {unit!r}")

    clearance_spans = list(_iter_sexpr_block_spans(text, "clearance"))
    if not clearance_spans:
        raise ValueError("Cannot safely apply routing margin: DSN clearance declarations are missing or ambiguous")
    margin = Decimal(str(margin_mm)) * unit_scale
    replacements: list[tuple[int, int, str]] = []
    unchanged = 0
    for start, end in clearance_spans:
        block = text[start:end]
        block_match = _SPECCTRA_CLEARANCE_VALUE_RE.match(block.strip())
        if block_match is None:
            raise ValueError("Cannot safely apply routing margin: DSN clearance declarations are ambiguous")
        try:
            original = Decimal(block_match.group(2))
        except InvalidOperation as exc:
            raise ValueError("Cannot safely apply routing margin to a nonnumeric DSN clearance") from exc
        if not original.is_finite() or original < 0:
            raise ValueError("Cannot safely apply routing margin to an invalid DSN clearance")
        if _first_sexpr_block(block, "type") is not None:
            unchanged += 1
            continue
        adjusted = original + margin
        rendered = format(adjusted.normalize(), "f")
        replacements.append((start + block_match.start(2), start + block_match.end(2), rendered))
    if not replacements:
        raise ValueError("Cannot safely apply routing margin: DSN has no general clearance rules")
    for start, end, rendered in reversed(replacements):
        text = text[:start] + rendered + text[end:]
    path.write_text(text, encoding="utf-8")
    return {
        "applied": True,
        "margin_mm": float(margin_mm),
        "dsn_unit": unit,
        "declarations_adjusted": len(replacements),
        "typed_exceptions_unchanged": unchanged,
        "native_board_rules_changed": False,
    }


def _payload_tokens(payload: str) -> list[str]:
    """Split a Specctra payload on unquoted whitespace.

    Pin references may contain quoted component/pin fragments such as
    ``"J3"-"D+"`` and quoted identifiers may contain spaces.  Counting raw
    quote-regex matches would therefore count fragments rather than pins.
    """
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in payload:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if quoted and char == "\\":
            current.append(char)
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            current.append(char)
            continue
        if char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _validate_dsn_semantics(root: str) -> tuple[dict[str, Any] | None, str | None]:
    structure_blocks = list(_iter_sexpr_blocks(root, "structure"))
    placement_blocks = list(_iter_sexpr_blocks(root, "placement"))
    network_blocks = list(_iter_sexpr_blocks(root, "network"))
    if len(structure_blocks) != 1 or len(placement_blocks) != 1 or len(network_blocks) != 1:
        return None, "DSN must contain exactly one structure, placement, and network section"

    structure = structure_blocks[0]
    if not list(_iter_sexpr_blocks(structure, "layer")):
        return None, "DSN structure section contains no layers"
    if _first_sexpr_block(structure, "boundary") is None:
        return None, "DSN structure section contains no board boundary"

    placement = placement_blocks[0]
    if not list(_iter_sexpr_blocks(placement, "component")):
        return None, "DSN placement section contains no components"
    if not list(_iter_sexpr_blocks(placement, "place")):
        return None, "DSN placement section contains no placed component instances"

    # A DSN placement identity is the placed reference, independent of which
    # component/image group contains the place record.  Decode using the same
    # quote-aware tokenizer as the rest of the Specctra parser so quoted and
    # unquoted spellings of the same reference compare identically.
    placed_references: set[str] = set()
    for component_block in _iter_sexpr_blocks(placement, "component"):
        for place_block in _iter_sexpr_blocks(component_block, "place"):
            place_tokens = _payload_tokens(_block_payload(place_block, "place"))
            if not place_tokens:
                return None, "DSN placement contains a placed component without a reference"
            reference = _decode_specctra_token(place_tokens[0])
            if not reference:
                return None, "DSN placement contains a placed component without a reference"
            if reference in placed_references:
                return None, f"DSN placement contains duplicate footprint reference {reference!r}"
            placed_references.add(reference)

    network = network_blocks[0]
    net_blocks = list(_iter_sexpr_blocks(network, "net"))
    if not net_blocks:
        return None, "DSN network section contains no nets"
    net_names: set[str] = set()
    pin_counts: dict[str, int] = {}
    for net_block in net_blocks:
        name = _first_payload_token(net_block, "net")
        if not name:
            return None, "DSN network contains a net without a name"
        if name in net_names:
            return None, f"DSN network declares duplicate net {name!r}"
        pins = _first_sexpr_block(net_block, "pins")
        pin_tokens = _payload_tokens(_block_payload(pins, "pins")) if pins else []
        if not pin_tokens:
            return None, f"DSN net {name!r} contains no pins"
        net_names.add(name)
        pin_counts[name] = len(pin_tokens)

    design_name = _first_payload_token(root, "pcb")
    if not design_name:
        return None, "DSN pcb root is missing its design name"
    return {
        "design_name": design_name,
        "nets": net_names,
        "pin_counts": pin_counts,
        "routable_nets": {name for name, count in pin_counts.items() if count >= 2},
    }, None


def _validate_ses_semantics(root: str) -> tuple[dict[str, Any] | None, str | None]:
    routes_blocks = list(_iter_sexpr_blocks(root, "routes"))
    if len(routes_blocks) != 1:
        return None, "SES must contain exactly one routes section"
    routes = routes_blocks[0]
    if _first_sexpr_block(routes, "resolution") is None:
        return None, "SES routes section contains no resolution"
    network_out_blocks = list(_iter_sexpr_blocks(routes, "network_out"))
    if len(network_out_blocks) != 1:
        return None, "SES routes section must contain exactly one network_out section"

    net_blocks = list(_iter_sexpr_blocks(network_out_blocks[0], "net"))
    if not net_blocks:
        return None, "SES network_out section contains no routed nets"
    net_names: set[str] = set()
    for net_block in net_blocks:
        name = _first_payload_token(net_block, "net")
        if not name:
            return None, "SES network_out contains a net without a name"
        if name in net_names:
            return None, f"SES network_out declares duplicate net {name!r}"
        if _first_sexpr_block(net_block, "wire") is None and _first_sexpr_block(net_block, "via") is None:
            return None, f"SES routed net {name!r} contains neither wire nor via data"
        net_names.add(name)

    session_name = _first_payload_token(root, "session")
    if not session_name:
        return None, "SES session root is missing its design name"
    base_design_block = _first_sexpr_block(root, "base_design")
    base_design = _first_payload_token(base_design_block, "base_design") if base_design_block else None
    if not base_design:
        return None, "SES session is missing its base_design reference"
    return {"design_name": session_name, "base_design": base_design, "nets": net_names}, None


def _sexpr_is_balanced(text: str) -> bool:
    text = _specctra_scan_text(text)
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if quoted and char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def _validate_specctra_artifact(
    path: str | Path,
    kind: str,
    *,
    source_dsn: str | Path | None = None,
    allow_partial_nets: bool = False,
) -> dict[str, Any]:
    """Validate a DSN or SES artifact before it crosses a workflow boundary."""
    artifact = Path(path)
    if kind not in {"dsn", "ses"}:
        raise ValueError(f"Unsupported Specctra artifact kind: {kind}")
    if not artifact.exists() or not artifact.is_file():
        return {"valid": False, "reason": f"{kind.upper()} output was not created: {artifact}"}

    size = artifact.stat().st_size
    if size < _MIN_ARTIFACT_BYTES:
        return {
            "valid": False,
            "reason": f"{kind.upper()} artifact is empty or implausibly small ({size} bytes): {artifact}",
            "size_bytes": size,
        }

    try:
        text = artifact.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        return {"valid": False, "reason": f"Could not read {kind.upper()} artifact: {exc}", "size_bytes": size}

    if "\x00" in text:
        return {
            "valid": False,
            "reason": f"{kind.upper()} artifact contains NUL bytes",
            "size_bytes": size,
        }
    if not _sexpr_is_balanced(text):
        return {
            "valid": False,
            "reason": f"{kind.upper()} artifact has unbalanced or unterminated S-expressions",
            "size_bytes": size,
        }
    root_keyword = "pcb" if kind == "dsn" else "session"
    roots = list(_iter_sexpr_blocks(text, root_keyword))
    if len(roots) != 1 or roots[0].strip() != text.strip():
        return {
            "valid": False,
            "reason": f"{kind.upper()} artifact must contain exactly one {root_keyword} root expression",
            "size_bytes": size,
        }

    details, semantic_error = _validate_dsn_semantics(roots[0]) if kind == "dsn" else _validate_ses_semantics(roots[0])
    if semantic_error or details is None:
        return {"valid": False, "reason": semantic_error, "size_bytes": size}

    if kind == "ses" and source_dsn is not None:
        dsn_path = Path(source_dsn)
        dsn_validation = _validate_specctra_artifact(dsn_path, "dsn")
        if not dsn_validation["valid"]:
            return {
                "valid": False,
                "reason": f"Cannot correlate SES with invalid source DSN: {dsn_validation['reason']}",
                "size_bytes": size,
            }
        dsn_nets = set(dsn_validation["nets"])
        unexpected_nets = sorted(set(details["nets"]) - dsn_nets)
        if unexpected_nets:
            return {
                "valid": False,
                "reason": f"SES contains net(s) absent from source DSN: {', '.join(unexpected_nets)}",
                "size_bytes": size,
            }
        # A one-pin net is electrically real but has no connection for an
        # autorouter to create.  Freerouting correctly omits such nets from
        # ``network_out``; require only source nets with at least two pins.
        routable_dsn_nets = set(dsn_validation.get("routable_nets", dsn_nets))
        missing_nets = sorted(routable_dsn_nets - set(details["nets"]))
        if missing_nets and not allow_partial_nets:
            return {
                "valid": False,
                "reason": f"SES is missing net(s) from source DSN: {', '.join(missing_nets)}",
                "size_bytes": size,
            }

        dsn_design = str(dsn_validation["design_name"])
        accepted_designs = {dsn_path.stem.casefold(), dsn_design.casefold()}
        if str(details["design_name"]).casefold() not in accepted_designs:
            return {
                "valid": False,
                "reason": (
                    f"SES session {details['design_name']!r} does not match source DSN " f"design {dsn_design!r}"
                ),
                "size_bytes": size,
            }
        base_name = str(details["base_design"]).replace("\\", "/").rsplit("/", 1)[-1]
        base_stem = Path(base_name).stem
        accepted_bases = {dsn_path.name.casefold(), dsn_path.stem.casefold(), dsn_design.casefold()}
        if base_name.casefold() not in accepted_bases and base_stem.casefold() not in accepted_bases:
            return {
                "valid": False,
                "reason": (
                    f"SES base_design {details['base_design']!r} does not reference source DSN " f"{dsn_path.name!r}"
                ),
                "size_bytes": size,
            }

    result = {
        "valid": True,
        "path": str(artifact),
        "kind": kind,
        "size_bytes": size,
        "design_name": details["design_name"],
        "nets": sorted(details["nets"]),
        "net_count": len(details["nets"]),
    }
    if kind == "dsn":
        result["pin_counts"] = dict(sorted(details["pin_counts"].items()))
        result["routable_nets"] = sorted(details["routable_nets"])
        result["routable_net_count"] = len(details["routable_nets"])
    if kind == "ses":
        result["base_design"] = details["base_design"]
    return result


def preflight_pcb(pcb_path: str | Path) -> dict[str, Any]:
    """Check that a real ``.kicad_pcb`` has routable pad/net connectivity."""
    path = Path(pcb_path)
    if path.suffix.lower() != ".kicad_pcb":
        return {"routable": False, "reason": f"Expected a .kicad_pcb file, got: {path.name}", "stats": {}}
    if not path.exists() or not path.is_file():
        return {"routable": False, "reason": f"PCB file not found: {path}", "stats": {}}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"routable": False, "reason": f"Could not read PCB file {path}: {exc}", "stats": {}}
    footprints = len(_FOOTPRINT_RE.findall(text))
    pads = len(_PAD_RE.findall(text))
    board_level_text = text
    for footprint_block in _iter_sexpr_blocks(text, "footprint"):
        board_level_text = board_level_text.replace(footprint_block, "", 1)
    declared_nets: dict[str, str] = {}
    declaration_error: str | None = None
    for number, name in _NET_DECL_RE.findall(board_level_text):
        if number == "0" or not name:
            continue
        prior_name = declared_nets.get(number)
        if prior_name is not None and prior_name != name:
            declaration_error = f"Board net number {number} is declared with conflicting names"
            break
        prior_number = next((key for key, value in declared_nets.items() if value == name and key != number), None)
        if prior_number is not None:
            declaration_error = f"Board net {name!r} is declared under both {prior_number} and {number}"
            break
        declared_nets[number] = name
    named_nets = set(declared_nets.values())
    connected_pad_nets: set[str] = set()
    invalid_pad_net: tuple[str, str] | None = None
    version = re.search(r"\(version\s+(\d+)\)", text)
    named_pad_format = version is not None and int(version.group(1)) >= 20260206
    for pad_block in _iter_sexpr_blocks(text, "pad"):
        # KiCad 10 stores names directly on pads and omits the board-level
        # numbered net table. Do not interpret absent legacy declarations as
        # absent connectivity, or accept this syntax on older board versions.
        for net_block in _iter_sexpr_blocks(pad_block, "net"):
            tokens = _payload_tokens(_block_payload(net_block, "net"))
            if len(tokens) == 1 and tokens[0].startswith('"'):
                name = _decode_specctra_token(tokens[0])
                if named_pad_format:
                    if name:
                        named_nets.add(name)
                        connected_pad_nets.add(name)
                else:
                    invalid_pad_net = ("<name-only>", name)
        for number, name in _NET_DECL_RE.findall(pad_block):
            if number == "0":
                continue
            if declared_nets.get(number) != name:
                invalid_pad_net = (number, name)
                break
            connected_pad_nets.add(name)
        if invalid_pad_net is not None:
            break

    marker_preview = bool(_PREVIEW_GENERATOR_RE.search(text))
    filename_preview = path.name.lower().endswith("_placement_preview.kicad_pcb")
    is_preview = marker_preview or filename_preview
    stats = {
        "footprints": footprints,
        "pads": pads,
        "nets": len(named_nets),
        "connected_pad_nets": len(connected_pad_nets),
        "placement_preview": is_preview,
    }

    if is_preview:
        return {
            "routable": False,
            "reason": (
                "This file is a Circuit Weaver placement preview; it is not a routed-board source. "
                "Open the generated schematic in KiCad, use Tools -> Update PCB from Schematic, then "
                "export a Specctra DSN from PCB Editor and autoroute that .dsn file."
            ),
            "stats": stats,
        }
    if footprints == 0:
        return {"routable": False, "reason": "Board has no footprints; nothing can be routed.", "stats": stats}
    if pads == 0:
        return {
            "routable": False,
            "reason": "Board footprints have no pads; update the PCB from the schematic first.",
            "stats": stats,
        }
    if not named_nets:
        return {
            "routable": False,
            "reason": "Board declares no named nets; update the PCB from the schematic first.",
            "stats": stats,
        }
    if declaration_error:
        return {"routable": False, "reason": declaration_error, "stats": stats}
    if invalid_pad_net is not None:
        number, name = invalid_pad_net
        return {
            "routable": False,
            "reason": (
                f"Board pad references undeclared or mismatched net {number} {name!r}; "
                "update the PCB from the schematic before routing."
            ),
            "stats": stats,
        }
    if not connected_pad_nets:
        return {
            "routable": False,
            "reason": "Board pads are not assigned to named nets; update the PCB from the schematic first.",
            "stats": stats,
        }
    return {"routable": True, "reason": "", "stats": stats}


def _resolve_executable(candidate: str | Path | None) -> str | None:
    if not candidate:
        return None
    value = os.path.expandvars(os.path.expanduser(str(candidate).strip().strip('"')))
    if not value:
        return None
    path = Path(value)
    if path.exists() and path.is_file():
        return str(path.resolve())
    return shutil.which(value)


def _find_java() -> str | None:
    for candidate in (os.getenv("CIRCUIT_WEAVER_JAVA"), os.getenv("JAVA")):
        resolved = _resolve_executable(candidate)
        if resolved:
            return resolved
    java_home = os.getenv("JAVA_HOME")
    if java_home:
        executable = "java.exe" if os.name == "nt" else "java"
        resolved = _resolve_executable(Path(java_home) / "bin" / executable)
        if resolved:
            return resolved
    executable = "java.exe" if os.name == "nt" else "java"
    portable = _resolve_executable(Path.home() / ".freerouting" / "runtime" / "bin" / executable)
    return portable or shutil.which("java")


def _find_freerouting_jar(explicit_path: str | Path | None = None) -> Path | None:
    """Locate a Freerouting JAR from an explicit path, environment, or home."""
    candidates = (
        explicit_path,
        os.getenv("CIRCUIT_WEAVER_FREEROUTING"),
        os.getenv("FREEROUTING_PATH"),
        Path.home() / ".freerouting" / "freerouting.jar",
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(os.path.expandvars(os.path.expanduser(str(candidate).strip().strip('"'))))
        if path.suffix.lower() == ".jar" and path.exists() and path.is_file():
            return path.resolve()
    return None


def _find_freerouting_command(explicit_path: str | Path | None = None) -> list[str] | None:
    """Return a shell-free command prefix for Freerouting."""
    if explicit_path:
        explicit = str(explicit_path)
        if explicit.lower().endswith(".jar"):
            jar = _find_freerouting_jar(explicit_path)
            java = _find_java()
            return [java, "-jar", str(jar)] if jar is not None and java else None
        resolved = _resolve_executable(explicit_path)
        return [resolved] if resolved else None

    jar = _find_freerouting_jar(explicit_path)
    if jar is not None:
        java = _find_java()
        if java:
            return [java, "-jar", str(jar)]

    candidates = (explicit_path, os.getenv("CIRCUIT_WEAVER_FREEROUTING"), os.getenv("FREEROUTING_PATH"))
    for candidate in candidates:
        if candidate and not str(candidate).lower().endswith(".jar"):
            resolved = _resolve_executable(candidate)
            if resolved:
                return [resolved]
    launcher = shutil.which("freerouting")
    return [launcher] if launcher else None


def _find_kicad_cli(explicit_path: str | Path | None = None) -> str | None:
    """Locate kicad-cli from an explicit path, environment, or PATH."""
    for candidate in (
        explicit_path,
        os.getenv("CIRCUIT_WEAVER_KICAD_CLI"),
        os.getenv("KICAD_CLI"),
        "kicad-cli",
        *(f"C:/Program Files/KiCad/{v}/bin/kicad-cli.exe" for v in ("10.0", "9.0", "8.0")),
    ):
        resolved = _resolve_executable(candidate)
        if resolved:
            return resolved
    return None


def _kicad_cli_supports_specctra(kicad_cli: str, timeout_seconds: float = 10) -> bool:
    """Probe the installed CLI instead of assuming it exposes GUI exporters."""
    try:
        result = subprocess.run(
            [kicad_cli, "pcb", "export", "--help"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "specctra" in f"{result.stdout}\n{result.stderr}".lower()


def _validate_timeout(timeout_seconds: float, label: str) -> str | None:
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return f"{label} timeout must be a finite number greater than zero"
    return None


def _prepare_destination(path: Path, *, overwrite: bool) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Could not create output directory {path.parent}: {exc}"
    if path.exists():
        if not path.is_file():
            return f"Output path exists but is not a file: {path}"
        if not overwrite:
            return f"Output already exists; pass overwrite=True (CLI: --overwrite) to replace it: {path}"
    return None


def _staging_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.stem}.cw-stage-{uuid.uuid4().hex}{destination.suffix}")


def _remove_staging_file(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        log.warning("Could not remove routing staging file %s", path, exc_info=True)


def _publish_staged_file(staging: Path, destination: Path, *, overwrite: bool) -> str | None:
    try:
        if overwrite:
            os.replace(staging, destination)
        else:
            # ``exists()`` followed by ``replace()`` has a race in which a file
            # created by another process can be overwritten despite the caller
            # declining --overwrite.  A same-directory hard link is an atomic
            # no-clobber publication: the OS fails it if *anything* already
            # occupies the destination name.
            try:
                os.link(staging, destination)
            except FileExistsError:
                return "Output appeared while routing; refusing to replace it without --overwrite: " f"{destination}"
            try:
                staging.unlink()
            except OSError:
                # The destination already names the complete validated bytes.
                # The caller's finally block will make another cleanup attempt.
                log.warning("Could not remove published routing staging link %s", staging, exc_info=True)
    except OSError as exc:
        return f"Could not atomically publish output {destination}: {exc}"
    return None


def export_dsn(
    pcb_path: str | Path,
    dsn_path: str | Path,
    timeout_seconds: float = 120,
    *,
    kicad_cli_path: str | Path | None = None,
    kicad_python_path: str | Path | None = None,
    overwrite: bool = False,
    routing_clearance_margin_mm: float = 0.0,
) -> dict[str, Any]:
    """Export and validate Specctra DSN through native KiCad or a capable CLI."""
    try:
        source = Path(pcb_path)
        if source.suffix.lower() == ".kicad_pcb":
            # DSN rule classes come from project settings; never export routing
            # input using silent defaults when only a legacy .pro is available.
            from .pcb_layout import _require_modern_project_settings

            _require_modern_project_settings(source)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _result_error(str(exc))
    timeout_error = _validate_timeout(timeout_seconds, "Specctra export")
    if timeout_error:
        return _result_error(timeout_error)
    if (
        not isinstance(routing_clearance_margin_mm, (int, float))
        or isinstance(routing_clearance_margin_mm, bool)
        or not math.isfinite(routing_clearance_margin_mm)
        or routing_clearance_margin_mm < 0
    ):
        return _result_error("routing_clearance_margin_mm must be a finite nonnegative number")

    destination = Path(dsn_path)
    if destination.suffix.lower() != ".dsn":
        return _result_error(f"Specctra export output must end in .dsn: {destination}")
    prepare_error = _prepare_destination(destination, overwrite=overwrite)
    if prepare_error:
        return _result_error(prepare_error)

    preflight = preflight_pcb(pcb_path)
    if not preflight["routable"]:
        return _result_error(f"Board failed DSN-export preflight: {preflight['reason']}", preflight=preflight)

    kicad_cli = _find_kicad_cli(kicad_cli_path)
    cli_export = bool(kicad_cli and _kicad_cli_supports_specctra(kicad_cli, timeout_seconds=min(timeout_seconds, 10)))

    staging = _staging_path(destination)
    try:
        if not cli_export:
            from .kicad_bridge import run_kicad

            result = run_kicad(
                "export_dsn",
                board=str(Path(pcb_path).resolve()),
                output=str(staging.resolve()),
                python_path=kicad_python_path,
                timeout=timeout_seconds,
            )
            if result["status"] != "ok":
                _remove_staging_file(staging)
                return _result_error(
                    "Automatic Specctra export requires KiCad's pcbnew Python API. "
                    + result["message"]
                    + ". Alternatively export Specctra DSN in PCB Editor and route that .dsn file."
                )
            result = subprocess.CompletedProcess([], 0, "", "")
        else:
            result = subprocess.run(
                [kicad_cli, "pcb", "export", "specctra", "-o", str(staging), str(pcb_path)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        _remove_staging_file(staging)
        return _result_error("kicad-cli Specctra export timed out")
    except OSError as exc:
        _remove_staging_file(staging)
        return _result_error(f"Could not run kicad-cli: {exc}")
    try:
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return _result_error(f"kicad-cli Specctra export failed: {detail}")

        clearance_guardband = None
        if routing_clearance_margin_mm > 0:
            try:
                clearance_guardband = _apply_specctra_clearance_margin(staging, float(routing_clearance_margin_mm))
            except (OSError, ValueError) as exc:
                return _result_error(f"Could not apply conservative router-only clearance margin: {exc}")

        validation = _validate_specctra_artifact(staging, "dsn")
        if not validation["valid"]:
            return _result_error(f"kicad-cli produced an invalid DSN: {validation['reason']}", artifact=validation)
        publish_error = _publish_staged_file(staging, destination, overwrite=overwrite)
        if publish_error:
            return _result_error(publish_error)
        validation["path"] = str(destination)
        result_payload = {
            "status": "ok",
            "message": "Specctra DSN exported, validated, and atomically published",
            "output_path": str(destination),
            "output_kind": "specctra_dsn",
            "artifact": validation,
        }
        if clearance_guardband is not None:
            result_payload["routing_clearance_guardband"] = clearance_guardband
        return result_payload
    finally:
        _remove_staging_file(staging)


def _find_statistics_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        required = {"connections", "traces", "vias", "clearance_violations"}
        if required.issubset(value):
            return value
        for nested in reversed(list(value.values())):
            found = _find_statistics_object(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in reversed(value):
            found = _find_statistics_object(nested)
            if found is not None:
                return found
    return None


def _last_structured_statistics(output_text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", output_text):
        try:
            decoded, _end = decoder.raw_decode(output_text[match.start() :])
        except json.JSONDecodeError:
            continue
        statistics = _find_statistics_object(decoded)
        if statistics is not None:
            candidates.append(statistics)
    return candidates[-1] if candidates else None


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _parse_routing_stats(output_text: str) -> dict[str, Any]:
    """Extract final routing statistics without treating unknown values as zero."""
    structured = _last_structured_statistics(output_text)
    if structured is not None:
        connections = structured.get("connections") if isinstance(structured.get("connections"), dict) else {}
        traces = structured.get("traces") if isinstance(structured.get("traces"), dict) else {}
        vias = structured.get("vias") if isinstance(structured.get("vias"), dict) else {}
        clearance = (
            structured.get("clearance_violations") if isinstance(structured.get("clearance_violations"), dict) else {}
        )
        return {
            "traces": _nonnegative_int(traces.get("total_count")),
            "vias": _nonnegative_int(vias.get("total_count")),
            "incomplete": _nonnegative_int(connections.get("incomplete_count")),
            "max_connections": _nonnegative_int(connections.get("maximum_count")),
            "clearance_violations": _nonnegative_int(clearance.get("total_count")),
            "statistics_source": "structured_json",
        }

    stats: dict[str, Any] = {
        "traces": None,
        "vias": None,
        "incomplete": None,
        "max_connections": None,
        "clearance_violations": None,
        "statistics_source": "text_summary",
    }
    patterns = {
        "traces": (r"traces?\s*[:=]\s*(\d+)", r"(\d+)\s+traces?"),
        "vias": (r"vias?\s*[:=]\s*(\d+)", r"(\d+)\s+vias?"),
        "incomplete": (
            r"(?:incompletes?|unrouted|unconnected)\s*[:=]\s*(\d+)",
            r"(?:incomplete|unrouted|unconnected)\s+connections?\s*[:=]\s*(\d+)",
            r"connections?\s+not\s+found\s*[:=]\s*(\d+)",
            r"(\d+)\s+(?:incompletes?|unrouted|unconnected)",
            r"(\d+)\s+connections?\s+not\s+found",
        ),
        "clearance_violations": (
            r"clearance(?:\s+|_)violations?\s*[:=]\s*(\d+)",
            r"(\d+)\s+clearance(?:\s+|_)violations?",
        ),
    }
    for key, alternatives in patterns.items():
        matches: list[re.Match[str]] = []
        for pattern in alternatives:
            matches.extend(re.finditer(pattern, output_text, re.IGNORECASE))
        if matches:
            final_match = max(matches, key=lambda item: item.start())
            stats[key] = int(final_match.group(1))
    # Freerouting 2.4 reports the final stage score instead of the older JSON
    # statistics object. Ignore pass/progress scores: only final summaries
    # establish completion and violations for publication.
    final_scores = list(
        re.finditer(
            r"stage completed:[^\r\n]*final score:[^\r\n]*?\((\d+) unrouted and (\d+) violations\)",
            output_text,
            re.IGNORECASE,
        )
    )
    if final_scores:
        stats["incomplete"] = int(final_scores[-1].group(1))
        stats["clearance_violations"] = int(final_scores[-1].group(2))
        stats["statistics_source"] = "final_stage_summary"
    return stats


def _parse_freerouting_version(output_text: str) -> str | None:
    matches = list(_FREEROUTING_VERSION_RE.finditer(output_text))
    return matches[-1].group(1) if matches else None


def _freerouting_drc_report(
    command: list[str],
    dsn_path: Path,
    *,
    session_path: Path | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any] | None:
    """Return FreeRouting's structured DRC report, failing closed on unknown output."""
    try:
        with tempfile.TemporaryDirectory(prefix=".cw-router-drc-", dir=dsn_path.parent) as folder:
            folder_path = Path(folder)
            report_path = folder_path / "drc.json"
            input_path = str(dsn_path.resolve())
            if session_path is not None:
                input_path += "+" + str(session_path.resolve())
            args = [
                *command,
                "-de",
                input_path,
                "-drc",
                str(report_path),
                "-da",
                "--gui.enabled=false",
                f"--user_data_path={folder_path}",
            ]
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=folder_path,
            )
            if result.returncode != 0 or not report_path.is_file():
                return None
            report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
        return None
    return report


def _native_pad_rows(inspection: dict[str, Any]) -> list[dict[str, Any]]:
    """Index authoritative KiCad pad identities for matching a DRC endpoint."""
    rows: list[dict[str, Any]] = []
    footprints = inspection.get("footprints")
    if not isinstance(footprints, list):
        return []
    for footprint in footprints:
        if not isinstance(footprint, dict) or not isinstance(footprint.get("ref"), str):
            continue
        for pad in footprint.get("pads", []):
            if not isinstance(pad, dict):
                continue
            try:
                x, y = float(pad["x"]), float(pad["y"])
                number, net = str(pad["number"]), str(pad.get("net") or "")
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(x) and math.isfinite(y) and number and net:
                rows.append({"ref": footprint["ref"], "number": number, "net": net, "x": x, "y": y})
    return rows


def _specctra_physical_pad_index(dsn_text: str) -> list[dict[str, Any]] | None:
    """Resolve DSN endpoint coordinates to placed reference/pad/net identities."""
    units = list(_iter_sexpr_blocks(dsn_text, "unit"))
    libraries = list(_iter_sexpr_blocks(dsn_text, "library"))
    placements = list(_iter_sexpr_blocks(dsn_text, "placement"))
    networks = list(_iter_sexpr_blocks(dsn_text, "network"))
    if not (len(units) == len(libraries) == len(placements) == len(networks) == 1):
        return None
    unit_name = (_first_payload_token(units[0], "unit") or "").casefold()
    to_mm = {"um": 0.001, "mm": 1.0, "mil": 0.0254, "inch": 25.4, "cm": 10.0}.get(unit_name)
    if to_mm is None:
        return None

    pin_nets: dict[str, set[str]] = {}
    for net_block in _iter_sexpr_blocks(networks[0], "net"):
        net_name = _first_payload_token(net_block, "net")
        pins_block = _first_sexpr_block(net_block, "pins")
        if not net_name or pins_block is None:
            return None
        for pin_token in _payload_tokens(_block_payload(pins_block, "pins")):
            pin_name = _decode_specctra_token(pin_token)
            pin_nets.setdefault(pin_name, set()).add(net_name)

    image_pins: dict[str, list[tuple[str, float, float]]] = {}
    for image_block in _iter_sexpr_blocks(libraries[0], "image"):
        image_name = _first_payload_token(image_block, "image")
        if not image_name:
            return None
        parsed: list[tuple[str, float, float]] = []
        for pin_block in _iter_sexpr_blocks(image_block, "pin"):
            payload = _block_payload(pin_block, "pin")
            payload = re.sub(r"\(rotate\s+[+-]?(?:\d+(?:\.\d*)?|\.\d+)\)", " ", payload)
            tokens = _payload_tokens(payload)
            if len(tokens) < 4:
                return None
            try:
                pin_name = _decode_specctra_token(tokens[1])
                px, py = float(tokens[2]) * to_mm, float(tokens[3]) * to_mm
            except (ValueError, TypeError, OverflowError):
                return None
            if not math.isfinite(px) or not math.isfinite(py):
                return None
            parsed.append((pin_name, px, py))
        image_pins[image_name] = parsed

    rows: list[dict[str, Any]] = []
    for component_block in _iter_sexpr_blocks(placements[0], "component"):
        image_name = _first_payload_token(component_block, "component")
        if not image_name or image_name not in image_pins:
            return None
        for place_block in _iter_sexpr_blocks(component_block, "place"):
            tokens = _payload_tokens(_block_payload(place_block, "place"))
            if len(tokens) < 5:
                return None
            reference = _decode_specctra_token(tokens[0])
            side = _decode_specctra_token(tokens[3]).casefold()
            if side != "front":
                # Back-side mirroring differs between DSN writers; do not guess.
                return None
            try:
                ox, oy, rotation = float(tokens[1]) * to_mm, float(tokens[2]) * to_mm, float(tokens[4])
            except (ValueError, TypeError, OverflowError):
                return None
            if not all(math.isfinite(v) for v in (ox, oy, rotation)):
                return None
            radians = math.radians(rotation)
            cosine, sine = math.cos(radians), math.sin(radians)
            for pin_name, local_x, local_y in image_pins[image_name]:
                net_names = pin_nets.get(f"{reference}-{pin_name}", set())
                if len(net_names) > 1:
                    return None
                if not net_names:
                    continue
                global_x = ox + cosine * local_x - sine * local_y
                global_y = oy + sine * local_x + cosine * local_y
                rows.append(
                    {
                        "ref": reference,
                        "number": pin_name,
                        "net": next(iter(net_names)),
                        "x": global_x,
                        "y": -global_y,
                        "dsn_position_mm": {"x": global_x, "y": global_y},
                    }
                )
    return rows


def _same_native_pad_clearance_pairs(
    report: dict[str, Any],
    native_pads: list[dict[str, Any]],
    dsn_pads: list[dict[str, Any]],
    *,
    tolerance_mm: float = 0.002,
) -> dict[str, Any] | None:
    """Qualify only fixed same-net overlap pairs between lands of one KiCad pad.

    FreeRouting 2.4.1 reports intentional same-net geometry overlaps as clearance
    failures. The exception is deliberately limited to pairs whose two endpoints
    both resolve to the same native footprint reference, pad number, and net.
    Coordinates and FreeRouting item UUIDs remain in the returned audit rows.
    """
    try:
        tolerance_mm = float(tolerance_mm)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        report.get("$schema") != "https://schemas.kicad.org/drc.v1.json"
        or report.get("coordinateUnits") != "mm"
        or not math.isfinite(tolerance_mm)
        or tolerance_mm <= 0
    ):
        return None
    violations = report.get("violations")
    if not isinstance(violations, list):
        return None
    for pad in [*native_pads, *dsn_pads]:
        if (
            not isinstance(pad, dict)
            or not all(isinstance(pad.get(key), str) and pad[key] for key in ("ref", "number", "net"))
        ):
            return None
        try:
            x, y = float(pad["x"]), float(pad["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
    rows: list[dict[str, Any]] = []
    signatures: set[tuple[Any, ...]] = set()
    for violation in violations:
        if not isinstance(violation, dict) or violation.get("type") not in {"clearance", "holeClearance"}:
            return None
        items = violation.get("items")
        if not isinstance(items, list) or len(items) != 2:
            return None
        endpoints: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("pos"), dict):
                return None
            description = item.get("description")
            net_match = re.fullmatch(r"Pin \[(.*)\]", description) if isinstance(description, str) else None
            if net_match is None:
                return None
            try:
                dsn_x = float(item["pos"]["x"])
                dsn_y = float(item["pos"]["y"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(dsn_x) or not math.isfinite(dsn_y):
                return None
            # Specctra coordinates mirror KiCad's Y axis.
            native_x, native_y = dsn_x, -dsn_y
            native_matches = {
                (pad["ref"], pad["number"], pad["net"])
                for pad in native_pads
                if abs(pad["x"] - native_x) <= tolerance_mm and abs(pad["y"] - native_y) <= tolerance_mm
            }
            dsn_matches = [
                pad
                for pad in dsn_pads
                if abs(pad["x"] - native_x) <= tolerance_mm and abs(pad["y"] - native_y) <= tolerance_mm
            ]
            if len(native_matches) != 1 or not dsn_matches:
                return None
            reference, number, net = next(iter(native_matches))
            aliases = sorted(
                {
                    pad["number"]
                    for pad in dsn_matches
                    if pad["ref"] == reference
                    and pad["net"] == net
                    and (pad["number"] == number or re.fullmatch(re.escape(number) + r"@\d+", pad["number"]))
                }
            )
            if not aliases or any(
                pad["ref"] != reference
                or pad["net"] != net
                or not (pad["number"] == number or re.fullmatch(re.escape(number) + r"@\d+", pad["number"]))
                for pad in dsn_matches
            ):
                return None
            if net != net_match.group(1):
                return None
            endpoints.append(
                {
                    "identity": (reference, number, net),
                    "ref": reference,
                    "pad_number": number,
                    "net": net,
                    "dsn_position_mm": {"x": dsn_x, "y": dsn_y},
                    "native_position_mm": {"x": native_x, "y": native_y},
                    "source_dsn_pin_aliases": aliases,
                    "item_uuid": str(item.get("uuid") or ""),
                }
            )
        if endpoints[0]["identity"] != endpoints[1]["identity"]:
            return None
        endpoints.sort(
            key=lambda row: (row["native_position_mm"]["x"], row["native_position_mm"]["y"], row["item_uuid"])
        )
        description = str(violation.get("description") or "")
        pair_signature = (
            str(violation["type"]),
            description,
            tuple(
                (
                    e["ref"],
                    e["pad_number"],
                    e["net"],
                    tuple(e["source_dsn_pin_aliases"]),
                    e["native_position_mm"]["x"],
                    e["native_position_mm"]["y"],
                )
                for e in endpoints
            ),
        )
        if pair_signature in signatures:
            return None
        signatures.add(pair_signature)
        rows.append(
            {
                "type": violation["type"],
                "description": description,
                "endpoints": [
                    {key: value for key, value in endpoint.items() if key != "identity"} for endpoint in endpoints
                ],
                "signature": pair_signature,
            }
        )
    return {"count": len(rows), "pairs": rows, "signatures": signatures}


def _compare_clearance_reports(
    baseline: dict[str, Any],
    final: dict[str, Any],
    native_pads: list[dict[str, Any]],
    dsn_pads: list[dict[str, Any]],
    router_violation_count: int | None,
) -> dict[str, Any]:
    """Verify final FreeRouting violations are exactly the qualified fixed baseline pairs."""
    baseline_pairs = _same_native_pad_clearance_pairs(baseline, native_pads, dsn_pads)
    final_pairs = _same_native_pad_clearance_pairs(final, native_pads, dsn_pads)
    if baseline_pairs is None or final_pairs is None:
        return {"valid": False, "reason": "DRC report contains a pair that is not a same-native-pad overlap"}
    if baseline_pairs["count"] == 0:
        return {"valid": False, "reason": "baseline contains no qualifying fixed-pad overlaps"}
    if baseline_pairs["signatures"] != final_pairs["signatures"]:
        return {
            "valid": False,
            "reason": "final FreeRouting clearance-pair identities or geometry differ from the fixed baseline",
            "baseline_pairs": baseline_pairs["pairs"],
            "final_pairs": final_pairs["pairs"],
        }
    if router_violation_count != final_pairs["count"]:
        return {
            "valid": False,
            "reason": "FreeRouting summary count does not match its final DRC pair table",
            "baseline_pairs": baseline_pairs["pairs"],
            "final_pairs": final_pairs["pairs"],
            "router_clearance_violations": router_violation_count,
        }
    return {
        "valid": True,
        "classification": "unchanged_preexisting_same_pad_overlaps",
        "baseline_pairs": baseline_pairs["pairs"],
        "final_pairs": final_pairs["pairs"],
        "count": final_pairs["count"],
    }


def _preexisting_fixed_pad_clearance_baseline(
    command: list[str],
    board_path: Path,
    dsn_path: Path,
    *,
    kicad_python_path: str | Path | None,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Authorize a narrow router-only baseline exception for native-board output."""
    from .kicad_bridge import run_kicad

    inspection = run_kicad(
        "inspect",
        board=str(board_path.resolve()),
        python_path=kicad_python_path,
        timeout=min(timeout_seconds, 60),
    )
    if inspection.get("status") != "ok":
        return None
    native_pads = _native_pad_rows(inspection)
    try:
        dsn_text = dsn_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    dsn_pads = _specctra_physical_pad_index(dsn_text)
    if not native_pads or dsn_pads is None:
        return None
    report = _freerouting_drc_report(
        command,
        dsn_path,
        timeout_seconds=min(timeout_seconds, 30),
    )
    if report is None:
        return None
    qualified = _same_native_pad_clearance_pairs(report, native_pads, dsn_pads)
    if qualified is None or qualified["count"] == 0:
        return None
    return {"report": report, "native_pads": native_pads, "dsn_pads": dsn_pads, "pairs": qualified["pairs"]}


def _probe_freerouting_capabilities(command: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(prefix="cw-router-probe-") as user_data:
            result = subprocess.run(
                [*command, "-help", "-da", f"--user_data_path={user_data}"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=user_data,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"probe_ok": False, "version": None, "seed": False, "reason": str(exc)}
    output_text = f"{result.stdout}\n{result.stderr}"
    return {
        "probe_ok": result.returncode == 0,
        "version": _parse_freerouting_version(output_text),
        "seed": bool(re.search(r"(?<!\S)-random_seed(?:\s|$)", output_text)),
        "reason": "" if result.returncode == 0 else f"help exited with code {result.returncode}",
    }


def _freerouting_v25_or_newer(capability_probe: dict[str, Any] | None) -> bool:
    match = re.match(r"(\d+)\.(\d+)", str((capability_probe or {}).get("version") or ""))
    return bool(match and tuple(map(int, match.groups())) >= (2, 5))


def _modern_router(capability_probe: dict[str, Any] | None) -> bool:
    match = re.match(r"(\d+)\.(\d+)", str((capability_probe or {}).get("version") or ""))
    return bool(match and tuple(map(int, match.groups())) >= (2, 4))


def _effective_optimization_threads(
    *,
    source_suffix: str,
    routed_board: bool,
    attempts: int,
    requested: int | None,
) -> int | None:
    """Keep native-board DRC-valid routing as the default while honoring explicit choices."""
    if requested is not None:
        return requested
    if source_suffix == ".kicad_pcb" and routed_board:
        # Freerouting 2.4.1's optimizer can emit sub-class-width neck segments
        # despite neckdown/fanout being disabled. Skip optimizer by default
        # when the caller asks us to publish a native DRC-checked board.
        return 0
    return 1 if attempts > 1 else None


_ROUTER_LOG_HEAD_BYTES = 64 * 1024
_ROUTER_LOG_PARSE_TAIL_BYTES = 4 * 1024 * 1024
_ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES = 64 * 1024


def _router_log_window(path: Path, *, head_bytes: int, tail_bytes: int) -> tuple[str, str]:
    """Read bounded parser windows and discard an uncertain leading tail fragment."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size <= head_bytes + tail_bytes:
            raw = stream.read(size)
            split = min(size, head_bytes)
            # Do not decode a multi-byte code point in two independent windows.
            while split > 0 and split < size and raw[split] & 0xC0 == 0x80:
                split -= 1
            return (
                raw[:split].decode("utf-8", errors="replace"),
                raw[split:].decode("utf-8", errors="replace"),
            )

        head = stream.read(head_bytes)
        tail_start = size - tail_bytes
        stream.seek(tail_start - 1)
        previous_byte = stream.read(1)
        stream.seek(tail_start)
        raw_tail = stream.read(tail_bytes)
        if previous_byte != b"\n":
            newline = raw_tail.find(b"\n")
            raw_tail = raw_tail[newline + 1 :] if newline >= 0 else b""
    return head.decode("utf-8", errors="replace"), raw_tail.decode("utf-8", errors="replace")


def _router_log_metadata(path: Path, *, parse_tail_bytes: int) -> dict[str, Any]:
    """Hash a full on-disk router log without reading it into memory."""
    try:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        return {"path": str(path), "available": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": str(path),
        "available": True,
        "sha256": digest.hexdigest(),
        "bytes": size,
        "preserved_full_log": True,
        "parse_head_limit_bytes": _ROUTER_LOG_HEAD_BYTES,
        "parse_tail_limit_bytes": parse_tail_bytes,
        "parse_window_truncated": size > _ROUTER_LOG_HEAD_BYTES + parse_tail_bytes,
        "diagnostic_tail_bytes": min(size, _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES),
        "diagnostic_tail_truncated": size > _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES,
    }


def _router_log_bundle(stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    return {
        "stdout": _router_log_metadata(stdout_path, parse_tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES),
        "stderr": _router_log_metadata(stderr_path, parse_tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES),
        "parse_window": {
            "head_bytes_per_stream": _ROUTER_LOG_HEAD_BYTES,
            "tail_bytes_per_stream": _ROUTER_LOG_PARSE_TAIL_BYTES,
            "diagnostic_tail_bytes_per_stream": _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES,
            "statistics_are_fail_closed": True,
        },
    }


def _preserve_rejected_session(staging: Path, log_dir: Path) -> dict[str, Any]:
    """Keep an invalid SES beside this attempt's private logs for diagnosis."""
    target = log_dir / "rejected.ses"
    try:
        if not staging.is_file():
            return {"available": False, "reason": "router did not create an SES"}
        # log_dir is unique to this attempt. Do not publish the artifact at the
        # requested output path, even when it is syntactically well-formed.
        shutil.copyfile(staging, target)
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "path": str(target),
        "sha256": digest.hexdigest(),
        "bytes": size,
        "published": False,
    }


def _read_router_diagnostic(path: Path) -> str:
    """Return only a bounded UTF-8-safe tail for returned error diagnostics."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(max(0, size - _ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES))
        return stream.read(_ROUTER_LOG_DIAGNOSTIC_TAIL_BYTES).decode("utf-8", errors="replace")


def _run_router_to_logs(
    command: list[str], *, timeout_seconds: float, cwd: Path, stdout_path: Path, stderr_path: Path
) -> subprocess.CompletedProcess[bytes]:
    """Run the router with disk-backed pipes; subprocess.run still owns timeout cleanup."""
    with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
        return subprocess.run(
            command,
            stdout=stdout_stream,
            stderr=stderr_stream,
            timeout=timeout_seconds,
            cwd=cwd,
        )


def _partial_route_timeout_budget(
    host_timeout_seconds: float,
    *,
    capability_probe: dict[str, Any] | None,
    enabled: bool,
) -> dict[str, Any]:
    """Describe the cooperative stop budget used only by private partial routing.

    Keep the process-level timeout authoritative. A shorter Freerouting job
    deadline gives its normal CLI path time to serialize an SES before the
    host kills the process.
    """
    result: dict[str, Any] = {
        "host_timeout_seconds": host_timeout_seconds,
        "reserved_export_seconds": 60 if enabled else 0,
        "internal_job_timeout_seconds": None,
        "configured": False,
    }
    if not enabled:
        result["reason"] = "private partial-route mode is disabled"
        return result
    if not _freerouting_v25_or_newer(capability_probe):
        result["reason"] = "requires Freerouting 2.5.0 or newer"
        return result
    internal_seconds = math.floor(host_timeout_seconds - 60)
    if internal_seconds <= 0:
        result["reason"] = "host timeout does not leave a positive whole-second router budget"
        return result
    result.update(
        {
            "internal_job_timeout_seconds": internal_seconds,
            "configured": True,
            "reason": "configured; this records a requested deadline, not a confirmed timeout",
        }
    )
    return result


def _format_job_timeout(timeout_seconds: int) -> str:
    hours, remainder = divmod(timeout_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _route_specctra(
    dsn_path: Path,
    ses_path: Path,
    command: list[str],
    *,
    passes: int,
    timeout_seconds: float,
    overwrite: bool,
    headless: bool,
    optimization_threads: int | None,
    optimizer_strategy: str | None,
    optimizer_hybrid_ratio: str | None,
    optimizer_item_selection: str | None,
    optimizer_improvement_threshold: float | None,
    seed: int | None,
    capability_probe: dict[str, Any] | None,
    clearance_baseline: dict[str, Any] | None = None,
    logs_root: Path | None = None,
    allow_partial_nets: bool = False,
    cooperative_partial_timeout: bool = False,
) -> dict[str, Any]:
    prepare_error = _prepare_destination(ses_path, overwrite=overwrite)
    if prepare_error:
        return _result_error(prepare_error)
    staging = _staging_path(ses_path)
    routing_command = [
        *command,
        "-de",
        str(dsn_path.resolve()),
        "-do",
        str(staging.resolve()),
        "-mp",
        str(passes),
        "-l",
        "en",
        "-da",
    ]
    if headless:
        routing_command.append("--gui.enabled=false")
    if _modern_router(capability_probe):
        # Default fine-pitch fanout/necking can introduce copper below the
        # imported class width. Route with the DSN widths and vias instead.
        routing_command.extend(
            [
                "--router.automatic_neckdown=false",
                "--router.neck_width_um=0",
                "--router.fanout.enabled=false",
            ]
        )
    v25_or_newer = _freerouting_v25_or_newer(capability_probe)
    timeout_budget = _partial_route_timeout_budget(
        timeout_seconds,
        capability_probe=capability_probe,
        enabled=cooperative_partial_timeout,
    )
    if timeout_budget["internal_job_timeout_seconds"] is not None:
        routing_command.append(
            "--router.job_timeout="
            + _format_job_timeout(timeout_budget["internal_job_timeout_seconds"])
        )
    if optimization_threads is not None:
        if _modern_router(capability_probe) and optimization_threads == 0:
            routing_command.append("--router.optimizer.enabled=false")
        elif _modern_router(capability_probe):
            routing_command.append(
                f"--router.optimizer.max_threads={optimization_threads}"
            )
        else:
            routing_command.extend(["-mt", str(optimization_threads)])
    if optimizer_strategy is not None:
        strategy = "GLOBAL_OPTIMAL" if v25_or_newer and optimizer_strategy == "global" else optimizer_strategy
        routing_command.extend(["-us", strategy.upper() if v25_or_newer else strategy])
    if optimizer_hybrid_ratio is not None and not v25_or_newer:
        routing_command.extend(["-hr", optimizer_hybrid_ratio])
    if optimizer_item_selection is not None:
        selection = optimizer_item_selection.upper() if v25_or_newer else optimizer_item_selection
        routing_command.extend(["-is", selection])
    if optimizer_improvement_threshold is not None:
        if _modern_router(capability_probe):
            routing_command.append(f"--router.optimizer.improvement_threshold={optimizer_improvement_threshold}")
        else:
            routing_command.extend(["-oit", str(optimizer_improvement_threshold)])
    if seed is not None:
        routing_command.extend(["-random_seed", str(seed)])
    try:
        stable_logs_root = Path(logs_root) if logs_root is not None else ses_path.parent / ".circuit-weaver-router-logs"
        stable_logs_root.mkdir(parents=True, exist_ok=True)
        log_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=stable_logs_root))
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"
        stdout_path.touch()
        stderr_path.touch()
    except OSError as exc:
        _remove_staging_file(staging)
        return _result_error(f"Could not prepare persistent Freerouting logs: {exc}")
    try:
        with tempfile.TemporaryDirectory(prefix=".cw-router-", dir=ses_path.parent) as user_data:
            routing_command.append(f"--user_data_path={Path(user_data).resolve()}")
            result = _run_router_to_logs(
                routing_command,
                timeout_seconds=timeout_seconds,
                cwd=Path(user_data),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
    except subprocess.TimeoutExpired:
        _remove_staging_file(staging)
        log_bundle = _router_log_bundle(stdout_path, stderr_path)
        if not all(log_bundle[name].get("available") for name in ("stdout", "stderr")):
            return _result_error(
                f"Freerouting timed out after {timeout_seconds:.0f} seconds; could not verify complete raw logs",
                router_logs=log_bundle,
            )
        try:
            diagnostic_stdout = _read_router_diagnostic(stdout_path)
            diagnostic_stderr = _read_router_diagnostic(stderr_path)
        except OSError as log_error:
            _remove_staging_file(staging)
            return _result_error(
                f"Freerouting timed out after {timeout_seconds:.0f} seconds; "
                f"could not read bounded diagnostics: {log_error}",
                router_logs=log_bundle,
            )
        return _result_error(
            f"Freerouting timed out after {timeout_seconds:.0f} seconds",
            diagnostic_stdout=diagnostic_stdout,
            diagnostic_stderr=diagnostic_stderr,
            router_logs=log_bundle,
        )
    except OSError as exc:
        _remove_staging_file(staging)
        log_bundle = _router_log_bundle(stdout_path, stderr_path)
        return _result_error(
            f"Could not run Freerouting: {exc}",
            router_logs=log_bundle,
        )

    log_bundle = _router_log_bundle(stdout_path, stderr_path)
    if not all(log_bundle[name].get("available") for name in ("stdout", "stderr")):
        _remove_staging_file(staging)
        return _result_error(
            "Could not verify complete persisted Freerouting logs; refusing route publication",
            router_logs=log_bundle,
        )
    try:
        stdout_head, stdout_tail = _router_log_window(
            stdout_path, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES
        )
        stderr_head, stderr_tail = _router_log_window(
            stderr_path, head_bytes=_ROUTER_LOG_HEAD_BYTES, tail_bytes=_ROUTER_LOG_PARSE_TAIL_BYTES
        )
    except OSError as log_error:
        _remove_staging_file(staging)
        return _result_error(
            f"Could not read persisted Freerouting logs: {log_error}; refusing route publication",
            router_logs=log_bundle,
        )
    version_text = "\n".join((stdout_head, stderr_head, stdout_tail, stderr_tail))
    stdout_parse_text = (
        stdout_tail
        if log_bundle["stdout"]["parse_window_truncated"]
        else f"{stdout_head}{stdout_tail}"
    )
    stderr_parse_text = (
        stderr_tail
        if log_bundle["stderr"]["parse_window_truncated"]
        else f"{stderr_head}{stderr_tail}"
    )
    output_text = f"{stdout_parse_text}\n{stderr_parse_text}"
    if result.returncode != 0:
        _remove_staging_file(staging)
        try:
            diagnostic_stdout = _read_router_diagnostic(stdout_path)
            diagnostic_stderr = _read_router_diagnostic(stderr_path)
        except OSError as log_error:
            return _result_error(
                f"Freerouting failed with exit code {result.returncode}; "
                f"could not read bounded diagnostics: {log_error}",
                router_logs=log_bundle,
            )
        detail = diagnostic_stderr.strip() or diagnostic_stdout.strip() or f"exit code {result.returncode}"
        return _result_error(
            f"Freerouting failed: {detail}",
            diagnostic_stdout=diagnostic_stdout,
            diagnostic_stderr=diagnostic_stderr,
            router_logs=log_bundle,
        )
    try:
        stats = _parse_routing_stats(output_text)
        router = {
            "version": _parse_freerouting_version(version_text) or (capability_probe or {}).get("version"),
            "logs": log_bundle,
            "headless": headless,
            "max_passes": passes,
            "optimization_threads": optimization_threads,
            "optimizer_strategy": optimizer_strategy,
            "optimizer_hybrid_ratio": optimizer_hybrid_ratio,
            "optimizer_item_selection": optimizer_item_selection,
            "optimizer_improvement_threshold": optimizer_improvement_threshold,
            "seed": seed,
            **({"timeout_budget": timeout_budget} if cooperative_partial_timeout else {}),
        }
        validation = _validate_specctra_artifact(
            staging, "ses", source_dsn=dsn_path, allow_partial_nets=allow_partial_nets,
        )
        if not validation["valid"]:
            rejected_session = _preserve_rejected_session(staging, log_dir)
            try:
                diagnostic_stdout = _read_router_diagnostic(stdout_path)
                diagnostic_stderr = _read_router_diagnostic(stderr_path)
            except OSError as log_error:
                diagnostic_stdout = ""
                diagnostic_stderr = f"Could not read bounded router diagnostics: {log_error}"
            return _result_error(
                f"Freerouting produced an invalid SES: {validation['reason']}",
                artifact=validation,
                rejected_session=rejected_session,
                stats=stats,
                router=router,
                diagnostic_stdout=diagnostic_stdout,
                diagnostic_stderr=diagnostic_stderr,
                router_logs=log_bundle,
            )
        session_text = staging.read_text(encoding="utf-8")
        stats.update(_measure_session_trace_lengths(session_text))
        # Recent router logs omit trace/via counts. Measure the validated
        # session itself so alternative strategies can still be ranked.
        if stats["vias"] is None or stats["traces"] is None:
            routes = _first_sexpr_block(session_text, "network_out") or ""
            if stats["vias"] is None:
                stats["vias"] = len(list(_iter_sexpr_blocks(routes, "via")))
            if stats["traces"] is None:
                stats["traces"] = len(list(_iter_sexpr_blocks(routes, "wire")))
            stats["geometry_counts_source"] = "validated_session"
        if stats["incomplete"] is None:
            return _result_error(
                "Freerouting did not report final connection completeness; refusing to publish an unverified SES",
                artifact=validation,
                rejected_session=_preserve_rejected_session(staging, log_dir),
                stats=stats,
                router=router,
                router_logs=log_bundle,
            )
        clearance_audit = None
        if clearance_baseline is not None:
            final_drc = _freerouting_drc_report(
                command,
                dsn_path,
                session_path=staging,
                timeout_seconds=min(timeout_seconds, 30),
            )
            if final_drc is None:
                return _result_error(
                    "Could not obtain the final FreeRouting DRC pair table for pre-existing pad overlaps",
                    artifact=validation,
                    rejected_session=_preserve_rejected_session(staging, log_dir),
                    stats=stats,
                    router=router,
                    router_logs=log_bundle,
                )
            clearance_audit = _compare_clearance_reports(
                clearance_baseline["report"],
                final_drc,
                clearance_baseline["native_pads"],
                clearance_baseline["dsn_pads"],
                stats["clearance_violations"],
            )
            if not clearance_audit["valid"]:
                return _result_error(
                    "FreeRouting clearance violations changed from the qualified fixed-pad baseline: "
                    + str(clearance_audit.get("reason", "unclassified violation")),
                    artifact=validation,
                    rejected_session=_preserve_rejected_session(staging, log_dir),
                    stats=stats,
                    router=router,
                    clearance_audit=clearance_audit,
                    router_logs=log_bundle,
                )
        elif stats["clearance_violations"] is not None and stats["clearance_violations"] > 0:
            return _result_error(
                f"Freerouting reported {stats['clearance_violations']} clearance violation(s); SES was not published",
                artifact=validation,
                rejected_session=_preserve_rejected_session(staging, log_dir),
                stats=stats,
                router=router,
                router_logs=log_bundle,
            )
        publish_error = _publish_staged_file(staging, ses_path, overwrite=overwrite)
        if publish_error:
            return _result_error(publish_error, stats=stats, router=router, router_logs=log_bundle)
        validation["path"] = str(ses_path)
        clearance_status = (
            "unchanged_preexisting_same_pad_overlaps_requires_native_drc"
            if clearance_audit is not None
            else "verified_clear"
            if stats["clearance_violations"] == 0
            else "unreported_by_router_cli"
        )
        if clearance_audit is not None:
            stats["clearance_violations_raw"] = stats["clearance_violations"]
            stats["clearance_violations_classified_preexisting"] = clearance_audit["count"]
        return {
            "status": "ok",
            "artifact": validation,
            "stats": stats,
            "router": router,
            "router_logs": log_bundle,
            **({"clearance_audit": clearance_audit} if clearance_audit is not None else {}),
            "verification": {
                "ses_semantics": "verified",
                "connection_completeness": "verified",
                "clearance": clearance_status,
                "requires_kicad_drc": True,
            },
        }
    finally:
        _remove_staging_file(staging)


def _measure_session_trace_lengths(text: str) -> dict[str, Any]:
    """Measure all SES wire centerlines, or report unavailable without a partial sum.

    SES coordinates use the routes resolution (units per declared physical
    unit), which can differ from the DSN export's coordinate scale. Unsupported
    wire geometry must not look shorter than a fully measured candidate.
    """
    unavailable = {"trace_length_mm": None, "trace_lengths_by_net_mm": {}, "trace_length_source": "unavailable"}
    routes = _first_sexpr_block(text, "routes") or ""
    resolutions = list(_iter_sexpr_blocks(routes, "resolution"))
    if len(resolutions) != 1:
        return {**unavailable, "trace_length_reason": "missing or ambiguous routes resolution"}
    resolution = _payload_tokens(_block_payload(resolutions[0], "resolution"))
    unit_mm = {"um": 0.001, "mm": 1.0, "cm": 10.0, "mil": 0.0254, "inch": 25.4}
    try:
        if len(resolution) != 2:
            raise ValueError("invalid resolution")
        scale = float(resolution[1])
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("invalid resolution scale")
        factor = unit_mm[_decode_specctra_token(resolution[0]).casefold()] / scale
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("invalid physical coordinate scale")
    except (ValueError, KeyError, OverflowError):
        return {**unavailable, "trace_length_reason": "unsupported routes resolution"}

    network = _first_sexpr_block(routes, "network_out") or ""
    net_lengths: dict[str, float] = {}
    wire_count = 0
    for net in _iter_sexpr_blocks(network, "net"):
        name = _first_payload_token(net, "net")
        if not name or name in net_lengths:
            return {**unavailable, "trace_length_reason": "missing or duplicate routed net identity"}
        lengths = []
        for wire in _iter_sexpr_blocks(net, "wire"):
            paths = list(_iter_sexpr_blocks(wire, "path"))
            if len(paths) != 1 or _first_sexpr_block(wire, "qarc") is not None:
                return {**unavailable, "trace_length_reason": "unsupported wire geometry"}
            tokens = _payload_tokens(_block_payload(paths[0], "path"))
            try:
                if len(tokens) < 6 or (len(tokens) - 2) % 2:
                    raise ValueError("invalid path coordinate count")
                values = [float(value) for value in tokens[1:]]
                if not all(math.isfinite(value) for value in values) or values[0] < 0:
                    raise ValueError("invalid path coordinate or width")
                coordinates = list(zip(values[1::2], values[2::2], strict=True))
                length = math.fsum(
                    math.dist(first, second) * factor for first, second in zip(coordinates, coordinates[1:])
                )
                if not math.isfinite(length):
                    raise ValueError("non-finite path length")
            except (ValueError, OverflowError):
                return {**unavailable, "trace_length_reason": "invalid path geometry"}
            lengths.append(length)
            wire_count += 1
        try:
            net_lengths[name] = math.fsum(lengths)
        except OverflowError:
            return {**unavailable, "trace_length_reason": "non-finite net length"}
    if not wire_count:
        return {**unavailable, "trace_length_reason": "no measurable wire paths"}
    try:
        total = math.fsum(net_lengths.values())
    except OverflowError:
        return {**unavailable, "trace_length_reason": "non-finite total length"}
    if not math.isfinite(total):
        return {**unavailable, "trace_length_reason": "non-finite total length"}
    return {
        "trace_length_mm": round(total, 6),
        "trace_lengths_by_net_mm": {net: round(value, 6) for net, value in sorted(net_lengths.items())},
        "trace_length_source": "validated_session_polyline_centerlines",
        "trace_length_scope": "summed wire centerlines; excludes via barrels and copper inside pads",
    }


def _routing_rank(stats: dict[str, Any]) -> tuple[float, ...]:
    """Prefer complete, reported-clear routes, then shorter copper and fewer vias.

    Missing measurements cannot beat a measured zero. Segment count is only
    a final tie-breaker. Copper length is geometric, not a signal-integrity or
    local loop-inductance measurement.
    """
    return (
        stats["incomplete"],
        0 if stats["clearance_violations"] == 0 else 1,
        stats.get("trace_length_mm") if stats.get("trace_length_mm") is not None else math.inf,
        stats["vias"] if stats["vias"] is not None else math.inf,
        stats["traces"] if stats["traces"] is not None else math.inf,
    )


def _route_candidates(
    dsn_path: Path,
    ses_path: Path,
    command: list[str],
    *,
    attempts: int,
    timeout_seconds: float,
    overwrite: bool,
    seed: int | None,
    native_placement_portfolio: bool = False,
    logs_root: Path | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Run an isolated portfolio and atomically publish its best valid session."""
    stable_logs_root = Path(logs_root) if logs_root is not None else ses_path.parent / ".circuit-weaver-router-logs"
    if attempts == 1 and not native_placement_portfolio:
        result = _route_specctra(
            dsn_path,
            ses_path,
            command,
            timeout_seconds=timeout_seconds,
            overwrite=overwrite,
            seed=seed,
            logs_root=stable_logs_root,
            **options,
        )
        if options.get("cooperative_partial_timeout"):
            result["timeout_budget"] = _partial_route_timeout_budget(
                timeout_seconds,
                capability_probe=options.get("capability_probe"),
                enabled=True,
            )
        return result
    deadline = time.perf_counter() + timeout_seconds
    attempt_rows: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    winner_path: Path | None = None
    winner_index: int | None = None
    supports_seed = bool((options.get("capability_probe") or {}).get("seed"))
    base_seed = (0 if seed is None else seed) if supports_seed else None
    try:
        input_snapshot = dsn_path.read_bytes()
        input_hash = hashlib.sha256(input_snapshot).hexdigest()
        native_dsn_text = None
        native_dsn_canonicalization = None
        if native_placement_portfolio:
            native_dsn_text, canonicalization = canonicalize_native_dsn(input_snapshot.decode("utf-8"))
            native_dsn_canonicalization = {
                "source_sha256": canonicalization["source_sha256"],
                "canonical_sha256": canonicalization["canonical_sha256"],
                "canonical_bytes_changed": canonicalization["canonical_bytes_changed"],
                "canonicalization_idempotent": canonicalization["idempotent"],
                "root_design_name_preserved": True,
                "image_alias_count": len(canonicalization["image_alias_map"]),
                "placement_count": len(canonicalization["semantic"]["placements"]),
                "net_count": len(canonicalization["semantic"]["net_membership"]),
            }
        # Same filesystem as the destination allows atomic final publication.
        # The context also cleans up losing sessions and interrupted attempts.
        with tempfile.TemporaryDirectory(prefix=".cw-routing-", dir=ses_path.parent) as temporary:
            for index in range(attempts):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                path = Path(temporary) / f"candidate-{index + 1}.ses"
                attempt_options = dict(options)
                attempt_seed = base_seed + index if base_seed is not None else None
                route_dsn = dsn_path
                placement_order = None
                candidate_hash = None
                placement_order_kind = None
                if native_placement_portfolio:
                    if index == 0:
                        placement_order_kind = "ascending"
                    elif index == 1:
                        placement_order_kind = "descending"
                    elif index == 2:
                        placement_order_kind = "original"
                    else:
                        placement_order_kind = "rotated"
                    variant_text, placement_order = _native_placement_variant(
                        native_dsn_text,
                        placement_order_kind,
                        rotation=index - 2,
                    )
                    variant_dir = Path(temporary) / f"input-{index + 1}"
                    variant_dir.mkdir()
                    # Keep the exact input basename/design identity for SES
                    # validation while isolating each independently-routed DSN.
                    route_dsn = variant_dir / dsn_path.name
                    route_dsn.write_bytes(variant_text.encode("utf-8"))
                    candidate_hash = hashlib.sha256(route_dsn.read_bytes()).hexdigest()
                if (
                    not supports_seed
                    and attempt_options.get("optimizer_strategy") is None
                    and (not native_placement_portfolio or attempt_options.get("optimization_threads") not in {None, 0})
                ):
                    if _freerouting_v25_or_newer(options.get("capability_probe")):
                        # v2.5 removed the legacy GREEDY/HYBRID/RANDOM modes. Vary
                        # only supported item ordering while keeping GLOBAL_OPTIMAL.
                        attempt_options["optimizer_strategy"] = "global"
                        if attempt_options.get("optimizer_item_selection") is None:
                            attempt_options["optimizer_item_selection"] = (
                                "sequential" if index % 2 == 0 else "prioritized"
                            )
                    else:
                        strategy = ("greedy", "global", "hybrid")[index % 3]
                        attempt_options["optimizer_strategy"] = strategy
                        attempt_options["optimizer_hybrid_ratio"] = "1:1" if strategy == "hybrid" else None
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                allowance = remaining / (attempts - index)
                log.info(
                    "Freerouting portfolio candidate %d/%d started "
                    "(placement order=%s, optimizer=%s, timeout allowance=%.1fs)",
                    index + 1,
                    attempts,
                    placement_order_kind or "unchanged",
                    attempt_options.get("optimizer_strategy"),
                    allowance,
                )
                result = _route_specctra(
                    route_dsn,
                    path,
                    command,
                    timeout_seconds=allowance,
                    overwrite=False,
                    seed=attempt_seed,
                    logs_root=stable_logs_root,
                    **attempt_options,
                )
                row = {
                    "attempt": index + 1,
                    "seed": attempt_seed,
                    "optimizer_strategy": attempt_options.get("optimizer_strategy"),
                    "status": result["status"],
                    "timeout_seconds": allowance,
                }
                if options.get("cooperative_partial_timeout"):
                    row["timeout_budget"] = _partial_route_timeout_budget(
                        allowance,
                        capability_probe=options.get("capability_probe"),
                        enabled=True,
                    )
                if placement_order is not None:
                    row["placement_order_strategy"] = placement_order_kind
                    row["placement_order"] = placement_order
                    row["variant_sha256"] = candidate_hash
                if "stats" in result:
                    row["stats"] = result["stats"]
                if "router_logs" in result:
                    row["router_logs"] = result["router_logs"]
                for key in ("artifact", "rejected_session", "router"):
                    if key in result:
                        row[key] = result[key]
                if "message" in result:
                    row["message"] = result["message"]
                for key in ("diagnostic_stdout", "diagnostic_stderr"):
                    if key in result:
                        row[key] = result[key]
                measured_incomplete = (
                    result.get("stats", {}).get("incomplete") if result.get("status") == "ok" else None
                )
                log.info(
                    "Freerouting portfolio candidate %d/%d finished "
                    "(status=%s, measured incomplete=%s, timeout allowance=%.1fs)",
                    index + 1,
                    attempts,
                    result["status"],
                    measured_incomplete,
                    allowance,
                )
                attempt_rows.append(row)
                if hashlib.sha256(dsn_path.read_bytes()).hexdigest() != input_hash:
                    return _result_error(
                        "Routing input changed during candidate search; no session was published", attempts=attempt_rows
                    )
                if candidate_hash is not None and hashlib.sha256(route_dsn.read_bytes()).hexdigest() != candidate_hash:
                    return _result_error(
                        "Native routing variant changed during candidate search; no session was published",
                        attempts=attempt_rows,
                    )
                if result["status"] == "ok" and (
                    winner is None or _routing_rank(result["stats"]) < _routing_rank(winner["stats"])
                ):
                    winner, winner_path, winner_index = result, path, index + 1
            search = {
                "requested_attempts": attempts,
                "completed_attempts": len(attempt_rows),
                "selected_attempt": winner_index,
                "timeout_budget_seconds": timeout_seconds,
                "ranking": ["incomplete", "clearance_unreported", "trace_length_mm", "vias", "traces"],
                "attempts": attempt_rows,
            }
            if native_dsn_canonicalization is not None:
                search["native_dsn_canonicalization"] = native_dsn_canonicalization
            if options.get("cooperative_partial_timeout"):
                search["timeout_budget"] = {
                    "overall_host_timeout_seconds": timeout_seconds,
                    "reserved_export_seconds_per_attempt": 60,
                    "internal_job_timeout_policy": (
                        "floor(each attempt's subprocess timeout allowance minus 60 seconds), "
                        "only for Freerouting 2.5.0+ and a positive whole-second result"
                    ),
                    "per_attempt_internal_job_timeout_seconds": [
                        row["timeout_budget"]["internal_job_timeout_seconds"]
                        for row in attempt_rows
                        if "timeout_budget" in row
                    ],
                }
            if winner is None or winner_path is None:
                return _result_error("No routing attempt produced a verified usable session", search=search)
            error = _publish_staged_file(winner_path, ses_path, overwrite=overwrite)
            if error:
                return _result_error(error, search=search)
            winner["artifact"]["path"] = str(ses_path)
            return {**winner, "search": search}
    except (ValueError, UnicodeDecodeError) as exc:
        return _result_error(f"Cannot prepare native routing-order variant: {exc}", attempts=attempt_rows)
    except OSError as exc:
        return _result_error(f"Could not stage routing candidates: {exc}", attempts=attempt_rows)


def autoroute_pcb(
    pcb_path: str | Path,
    output_path: str | None = None,
    effort: str = "medium",
    timeout_seconds: float = 300,
    *,
    max_passes: int | None = None,
    overwrite: bool = False,
    headless: bool = True,
    optimization_threads: int | None = None,
    optimizer_strategy: str | None = None,
    optimizer_hybrid_ratio: str | None = None,
    optimizer_item_selection: str | None = None,
    optimizer_improvement_threshold: float | None = None,
    seed: int | None = None,
    attempts: int = 1,
    freerouting_path: str | Path | None = None,
    kicad_cli_path: str | Path | None = None,
    kicad_python_path: str | Path | None = None,
    routed_board_path: str | Path | None = None,
    repair_open_connections: bool = False,
    recovery_timeout_seconds: float = 600,
    max_repair_links: int = 256,
    _allow_partial_nets: bool = False,
) -> dict[str, Any]:
    """Route a real KiCad board or a user-supplied DSN with Freerouting.

    Successful normal operation returns ``output_kind="specctra_session"`` and
    an ``output_path`` ending in ``.ses``. With ``routed_board_path``, import
    the session and require native DRC before publishing the routed PCB.
    A non-zero incomplete count returns ``status="partial"``.
    """
    if not isinstance(repair_open_connections, bool):
        return _result_error("repair_open_connections must be a boolean")
    if repair_open_connections:
        from .autoroute_recovery import autoroute_with_recovery

        return autoroute_with_recovery(
            pcb_path, output_path, routed_board_path=routed_board_path,
            recovery_timeout_seconds=recovery_timeout_seconds, max_repair_links=max_repair_links,
            effort=effort, timeout_seconds=timeout_seconds, max_passes=max_passes,
            overwrite=overwrite, headless=headless, optimization_threads=optimization_threads,
            optimizer_strategy=optimizer_strategy, optimizer_hybrid_ratio=optimizer_hybrid_ratio,
            optimizer_item_selection=optimizer_item_selection,
            optimizer_improvement_threshold=optimizer_improvement_threshold, seed=seed,
            attempts=attempts, freerouting_path=freerouting_path,
            kicad_cli_path=kicad_cli_path, kicad_python_path=kicad_python_path,
        )
    if _allow_partial_nets and (routed_board_path is None or Path(pcb_path).suffix.lower() != ".kicad_pcb"):
        return _result_error("Partial-net recovery requires an isolated native board destination")
    timeout_error = _validate_timeout(timeout_seconds, "Routing")
    if timeout_error:
        return _result_error(timeout_error)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 32:
        return _result_error("attempts must be an integer from 1 through 32")
    source = Path(pcb_path)
    if not source.exists() or not source.is_file():
        return _result_error(f"Routing input not found: {source}")
    if effort not in _EFFORT_PASSES:
        return _result_error(f"Unknown effort '{effort}'; choose one of {sorted(_EFFORT_PASSES)}")
    if max_passes is not None and (
        not isinstance(max_passes, int) or isinstance(max_passes, bool) or not 0 <= max_passes <= 9999
    ):
        return _result_error("max_passes must be an integer from 0 (unlimited) through 9999")
    if optimization_threads is not None and (
        not isinstance(optimization_threads, int)
        or isinstance(optimization_threads, bool)
        or not 0 <= optimization_threads <= 1024
    ):
        return _result_error("optimization_threads must be an integer from 0 (disabled) through 1024")
    if optimizer_strategy not in {None, "greedy", "global", "hybrid"}:
        return _result_error("optimizer_strategy must be greedy, global, or hybrid")
    if optimizer_hybrid_ratio is not None and not re.fullmatch(r"[1-9]\d*:[1-9]\d*", optimizer_hybrid_ratio):
        return _result_error("optimizer_hybrid_ratio must use positive-integer m:n syntax, for example 1:1")
    if optimizer_strategy == "hybrid" and optimizer_hybrid_ratio is None:
        return _result_error("optimizer_strategy='hybrid' requires optimizer_hybrid_ratio")
    if optimizer_hybrid_ratio is not None and optimizer_strategy != "hybrid":
        return _result_error("optimizer_hybrid_ratio is only valid with optimizer_strategy='hybrid'")
    if optimizer_item_selection not in {None, "sequential", "random", "prioritized"}:
        return _result_error("optimizer_item_selection must be sequential, random, or prioritized")
    if optimizer_improvement_threshold is not None and (
        not isinstance(optimizer_improvement_threshold, (int, float))
        or isinstance(optimizer_improvement_threshold, bool)
        or not math.isfinite(optimizer_improvement_threshold)
        or not 0 <= optimizer_improvement_threshold <= 100
    ):
        return _result_error("optimizer_improvement_threshold must be a percentage from 0 through 100")
    if seed is not None and (
        not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 9_223_372_036_854_775_807
    ):
        return _result_error("seed must be an integer from 0 through 9223372036854775807")
    if seed is not None and seed + attempts - 1 > 9_223_372_036_854_775_807:
        return _result_error("seed plus attempts exceeds the Freerouting seed range")
    if not isinstance(headless, bool):
        return _result_error("headless must be a boolean")
    if not isinstance(overwrite, bool):
        return _result_error("overwrite must be a boolean")

    source_suffix = source.suffix.lower()
    if source_suffix == ".kicad_pcb":
        try:
            # Keep DSN-only routing available, but do not route from a native
            # board whose legacy project rules cannot be carried into DRC.
            from .pcb_layout import _require_modern_project_settings

            _require_modern_project_settings(source)
        except (OSError, ValueError) as exc:
            return _result_error(str(exc))
    source_hashes = {}
    if routed_board_path is not None:
        if source_suffix != ".kicad_pcb":
            return _result_error("Routed board output requires the source .kicad_pcb, not a standalone DSN")
        destination = Path(routed_board_path).resolve()
        if destination.suffix.lower() != ".kicad_pcb" or destination == source.resolve():
            return _result_error("Choose a separate .kicad_pcb output for the routed board")
        output_error = _prepare_destination(destination, overwrite=overwrite)
        if output_error:
            return _result_error(output_error)
        try:
            source_hashes = {
                path.resolve(): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                for path in (source, source.with_suffix(".kicad_pro"), source.with_suffix(".kicad_dru"))
            }
        except OSError as exc:
            return _result_error(f"Could not read routing input: {exc}")
    if source_suffix == ".dsn":
        input_validation = _validate_specctra_artifact(source, "dsn")
        if not input_validation["valid"]:
            return _result_error(f"Invalid Specctra DSN input: {input_validation['reason']}")
        dsn_path = source
        preflight = None
    elif source_suffix == ".kicad_pcb":
        preflight = preflight_pcb(source)
        if not preflight["routable"]:
            return _result_error(f"Board failed routing preflight: {preflight['reason']}", preflight=preflight)
        input_validation = {
            "valid": True,
            "path": str(source),
            "kind": "kicad_pcb",
            "stats": preflight["stats"],
        }
        dsn_path = source.with_name(f"{source.stem}_autoroute.dsn")
    else:
        return _result_error("Routing input must be a real .kicad_pcb board or a Specctra .dsn file")

    ses_path = Path(output_path) if output_path else source.with_suffix(".ses")
    if ses_path.suffix.lower() != ".ses":
        return _result_error(f"Specctra routing output must end in .ses: {ses_path}")
    output_error = _prepare_destination(ses_path, overwrite=overwrite)
    if output_error:
        return _result_error(output_error)

    kicad_cli: str | None = None
    if source_suffix == ".kicad_pcb":
        dsn_output_error = _prepare_destination(dsn_path, overwrite=overwrite)
        if dsn_output_error:
            return _result_error(dsn_output_error)
        kicad_cli = _find_kicad_cli(kicad_cli_path)

    freerouting_command = _find_freerouting_command(freerouting_path)
    if freerouting_command is None:
        return _result_error(
            "Freerouting was not found. Set CIRCUIT_WEAVER_FREEROUTING to a launcher or JAR path, "
            "or install the freerouting launcher on PATH.",
            input=input_validation,
        )

    passes = _EFFORT_PASSES[effort] if max_passes is None else max_passes
    capability_probe = _probe_freerouting_capabilities(
        freerouting_command,
        timeout_seconds=min(timeout_seconds, 10),
    )
    if _freerouting_v25_or_newer(capability_probe):
        if optimizer_strategy in {"greedy", "hybrid"}:
            return _result_error(
                f"Freerouting {capability_probe['version']} removed optimizer_strategy={optimizer_strategy!r}; "
                "use 'global' or omit the strategy."
            )
        if optimizer_hybrid_ratio is not None:
            return _result_error(
                f"Freerouting {capability_probe['version']} removed -hr; omit optimizer_hybrid_ratio."
            )
        if optimizer_item_selection == "random":
            return _result_error(
                f"Freerouting {capability_probe['version']} removed random optimizer item selection; "
                "use 'sequential' or 'prioritized'."
            )
    if seed is not None and (not capability_probe["probe_ok"] or not capability_probe["seed"]):
        return _result_error(
            "The installed Freerouting build does not advertise -random_seed; "
            "omit --seed or install a build whose -help output includes that option.",
            router=capability_probe,
        )

    start_time = time.perf_counter()
    routing_clearance_guardband = None
    if source_suffix != ".dsn":
        export_result = export_dsn(
            source,
            dsn_path,
            timeout_seconds=min(timeout_seconds, 120),
            kicad_cli_path=kicad_cli,
            kicad_python_path=kicad_python_path,
            overwrite=overwrite,
            routing_clearance_margin_mm=_NATIVE_BOARD_ROUTING_CLEARANCE_MARGIN_MM,
        )
        if export_result["status"] != "ok":
            return {**export_result, "preflight": preflight, "input": input_validation}
        routing_clearance_guardband = export_result.get("routing_clearance_guardband")
    clearance_baseline = None
    if source_suffix == ".kicad_pcb" and routed_board_path is not None:
        clearance_baseline = _preexisting_fixed_pad_clearance_baseline(
            freerouting_command,
            source.resolve(),
            dsn_path.resolve(),
            kicad_python_path=kicad_python_path,
            timeout_seconds=timeout_seconds,
        )
    route_result = _route_candidates(
        dsn_path,
        ses_path,
        freerouting_command,
        attempts=attempts,
        passes=passes,
        timeout_seconds=timeout_seconds,
        overwrite=overwrite,
        headless=headless,
        optimization_threads=_effective_optimization_threads(
            source_suffix=source_suffix,
            routed_board=routed_board_path is not None,
            attempts=attempts,
            requested=optimization_threads,
        ),
        optimizer_strategy=optimizer_strategy,
        optimizer_hybrid_ratio=optimizer_hybrid_ratio,
        optimizer_item_selection=optimizer_item_selection,
        optimizer_improvement_threshold=optimizer_improvement_threshold,
        seed=seed,
        native_placement_portfolio=source_suffix == ".kicad_pcb",
        logs_root=ses_path.parent / ".circuit-weaver-router-logs",
        capability_probe=capability_probe,
        clearance_baseline=clearance_baseline,
        **({"allow_partial_nets": True} if _allow_partial_nets else {}),
        **({"cooperative_partial_timeout": True} if _allow_partial_nets else {}),
    )

    elapsed = time.perf_counter() - start_time
    if route_result["status"] != "ok":
        return {
            **route_result,
            "preflight": preflight,
            "input": input_validation,
            **(
                {"routing_clearance_guardband": routing_clearance_guardband}
                if routing_clearance_guardband is not None
                else {}
            ),
        }

    stats = {**route_result["stats"], "routing_time_seconds": elapsed}
    incomplete = stats["incomplete"]
    clearance_verified = (
        stats["clearance_violations"] == 0
        or route_result.get("verification", {}).get("clearance")
        == "unchanged_preexisting_same_pad_overlaps_requires_native_drc"
    )
    status = "partial" if incomplete else "ok" if clearance_verified else "review_required"
    message = (
        f"Freerouting produced a validated Specctra session at {ses_path}. "
        "Import it in KiCad PCB Editor with File -> Import -> Specctra Session."
    )
    if incomplete:
        message += f" {incomplete} connection(s) remain incomplete; routing is partial."
    if not clearance_verified:
        message += (
            " Freerouting's CLI did not report a final clearance-violation count; "
            "import the SES and run KiCad DRC before accepting the routing."
        )

    result = {
        "status": status,
        "output_path": str(ses_path),
        "output_kind": "specctra_session",
        "message": message,
        "input": input_validation,
        "preflight": preflight,
        "artifact": route_result["artifact"],
        "stats": stats,
        "router": route_result["router"],
        "verification": route_result["verification"],
        "routing_complete": incomplete == 0,
        "fabrication_ready": False,
        "requires_kicad_drc": True,
        **(
            {"clearance_audit": route_result["clearance_audit"]}
            if "clearance_audit" in route_result
            else {}
        ),
        **(
            {"routing_clearance_guardband": routing_clearance_guardband}
            if routing_clearance_guardband is not None
            else {}
        ),
        **({"search": route_result["search"]} if "search" in route_result else {}),
    }
    if routed_board_path is not None and not incomplete:
        from .pcb_layout import import_routing_session

        imported = import_routing_session(
            source,
            ses_path,
            routed_board_path,
            overwrite=overwrite,
            kicad_python_path=kicad_python_path,
            expected_hashes=source_hashes,
        )
        if imported["status"] != "ok":
            verification = dict(result.get("verification", {}))
            verification["connection_completeness"] = "native_import_rejected"
            native_drc = imported.get("drc")
            verification["kicad_drc"] = (
                "failed"
                if isinstance(native_drc, dict)
                and native_drc.get("status") == "ok"
                and native_drc.get("passed") is False
                else "not_verified"
            )
            return {
                **result,
                "status": "error",
                "message": imported["message"],
                "routing_complete": False,
                "requires_kicad_drc": True,
                "verification": verification,
                "board_import": imported,
            }
        return {
            **result,
            **imported,
            "session_path": str(ses_path),
            "requires_kicad_drc": False,
            "session_artifact": result["artifact"],
            "artifact": {
                "valid": True,
                "kind": "kicad_pcb",
                "path": imported["output_path"],
                "sha256": imported["drc"]["board_sha256"],
            },
            "verification": {
                **result["verification"],
                "requires_kicad_drc": False,
                "kicad_drc": "passed",
                "clearance": (
                    "native_drc_passed_with_unchanged_preexisting_same_pad_overlaps"
                    if result["verification"]["clearance"]
                    == "unchanged_preexisting_same_pad_overlaps_requires_native_drc"
                    else result["verification"]["clearance"]
                ),
            },
            "message": (
                "Routing imported into a separate PCB and verified with KiCad DRC. "
                f"Freerouting reported {result['stats'].get('clearance_violations_raw', 0)} "
                "clearance pairs; all matched unchanged pre-existing same-pad geometry."
                if "clearance_audit" in result
                else "Routing imported into a separate PCB and verified with KiCad DRC"
            ),
        }
    return result
