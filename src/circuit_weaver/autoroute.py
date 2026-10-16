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

    details, semantic_error = (
        _validate_dsn_semantics(roots[0]) if kind == "dsn" else _validate_ses_semantics(roots[0])
    )
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
        if missing_nets:
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
                    f"SES session {details['design_name']!r} does not match source DSN "
                    f"design {dsn_design!r}"
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
                    f"SES base_design {details['base_design']!r} does not reference source DSN "
                    f"{dsn_path.name!r}"
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
    return destination.with_name(
        f".{destination.stem}.cw-stage-{uuid.uuid4().hex}{destination.suffix}"
    )


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
                return (
                    "Output appeared while routing; refusing to replace it without --overwrite: "
                    f"{destination}"
                )
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
                "export_dsn", board=str(Path(pcb_path).resolve()), output=str(staging.resolve()),
                python_path=kicad_python_path, timeout=timeout_seconds,
            )
            if result["status"] != "ok":
                _remove_staging_file(staging)
                return _result_error(
                    "Automatic Specctra export requires KiCad's pcbnew Python API. " + result["message"]
                    + ". Alternatively export Specctra DSN in PCB Editor and route that .dsn file."
                )
            result = subprocess.CompletedProcess([], 0, "", "")
        else:
            result = subprocess.run(
                [kicad_cli, "pcb", "export", "specctra", "-o", str(staging), str(pcb_path)],
                capture_output=True, text=True, timeout=timeout_seconds,
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
                clearance_guardband = _apply_specctra_clearance_margin(
                    staging, float(routing_clearance_margin_mm)
                )
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
            structured.get("clearance_violations")
            if isinstance(structured.get("clearance_violations"), dict)
            else {}
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
    final_scores = list(re.finditer(
        r"stage completed:[^\r\n]*final score:[^\r\n]*?\((\d+) unrouted and (\d+) violations\)",
        output_text, re.IGNORECASE,
    ))
    if final_scores:
        stats["incomplete"] = int(final_scores[-1].group(1))
        stats["clearance_violations"] = int(final_scores[-1].group(2))
        stats["statistics_source"] = "final_stage_summary"
    return stats


def _parse_freerouting_version(output_text: str) -> str | None:
    matches = list(_FREEROUTING_VERSION_RE.finditer(output_text))
    return matches[-1].group(1) if matches else None


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


def _modern_router(capability_probe: dict[str, Any] | None) -> bool:
    match = re.match(r"(\d+)\.(\d+)", str((capability_probe or {}).get("version") or ""))
    return bool(match and tuple(map(int, match.groups())) >= (2, 4))


def _effective_optimization_threads(
    *, source_suffix: str, routed_board: bool, attempts: int, requested: int | None,
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
        routing_command.extend([
            "--router.automatic_neckdown=false", "--router.neck_width_um=0", "--router.fanout.enabled=false",
        ])
    if optimization_threads is not None:
        routing_command.extend(["-mt", str(optimization_threads)])
    if optimizer_strategy is not None:
        routing_command.extend(["-us", optimizer_strategy])
    if optimizer_hybrid_ratio is not None:
        routing_command.extend(["-hr", optimizer_hybrid_ratio])
    if optimizer_item_selection is not None:
        routing_command.extend(["-is", optimizer_item_selection])
    if optimizer_improvement_threshold is not None:
        if _modern_router(capability_probe):
            routing_command.append(f"--router.optimizer.improvement_threshold={optimizer_improvement_threshold}")
        else:
            routing_command.extend(["-oit", str(optimizer_improvement_threshold)])
    if seed is not None:
        routing_command.extend(["-random_seed", str(seed)])
    try:
        with tempfile.TemporaryDirectory(prefix=".cw-router-", dir=ses_path.parent) as user_data:
            routing_command.append(f"--user_data_path={Path(user_data).resolve()}")
            result = subprocess.run(
                routing_command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=user_data,
            )
    except subprocess.TimeoutExpired:
        _remove_staging_file(staging)
        return _result_error(f"Freerouting timed out after {timeout_seconds:.0f} seconds")
    except OSError as exc:
        _remove_staging_file(staging)
        return _result_error(f"Could not run Freerouting: {exc}")
    try:
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return _result_error(f"Freerouting failed: {detail}")

        validation = _validate_specctra_artifact(staging, "ses", source_dsn=dsn_path)
        if not validation["valid"]:
            return _result_error(f"Freerouting produced an invalid SES: {validation['reason']}", artifact=validation)
        output_text = f"{result.stdout}\n{result.stderr}"
        stats = _parse_routing_stats(output_text)
        # Recent router logs omit trace/via counts. Measure the validated
        # session itself so alternative strategies can still be ranked.
        if stats["vias"] is None or stats["traces"] is None:
            routes = _first_sexpr_block(staging.read_text(encoding="utf-8"), "network_out") or ""
            if stats["vias"] is None:
                stats["vias"] = len(list(_iter_sexpr_blocks(routes, "via")))
            if stats["traces"] is None:
                stats["traces"] = len(list(_iter_sexpr_blocks(routes, "wire")))
            stats["geometry_counts_source"] = "validated_session"
        router = {
            "version": _parse_freerouting_version(output_text)
            or (capability_probe or {}).get("version"),
            "headless": headless,
            "max_passes": passes,
            "optimization_threads": optimization_threads,
            "optimizer_strategy": optimizer_strategy,
            "optimizer_hybrid_ratio": optimizer_hybrid_ratio,
            "optimizer_item_selection": optimizer_item_selection,
            "optimizer_improvement_threshold": optimizer_improvement_threshold,
            "seed": seed,
        }
        if stats["incomplete"] is None:
            return _result_error(
                "Freerouting did not report final connection completeness; refusing to publish an unverified SES",
                artifact=validation,
                stats=stats,
                router=router,
            )
        if (
            stats["clearance_violations"] is not None
            and stats["clearance_violations"] > 0
        ):
            return _result_error(
                f"Freerouting reported {stats['clearance_violations']} clearance violation(s); SES was not published",
                artifact=validation,
                stats=stats,
                router=router,
            )
        publish_error = _publish_staged_file(staging, ses_path, overwrite=overwrite)
        if publish_error:
            return _result_error(publish_error, stats=stats, router=router)
        validation["path"] = str(ses_path)
        clearance_status = (
            "verified_clear"
            if stats["clearance_violations"] == 0
            else "unreported_by_router_cli"
        )
        return {
            "status": "ok",
            "artifact": validation,
            "stats": stats,
            "router": router,
            "verification": {
                "ses_semantics": "verified",
                "connection_completeness": "verified",
                "clearance": clearance_status,
                "requires_kicad_drc": True,
            },
        }
    finally:
        _remove_staging_file(staging)


def _routing_rank(stats: dict[str, Any]) -> tuple[float, ...]:
    """Prefer complete routes, reported-clear routes, then fewer vias/traces.

    Missing measurements cannot beat a measured zero. Segment count is only
    a final tie-breaker, not a claim about copper length or signal integrity.
    """
    return (
        stats["incomplete"],
        0 if stats["clearance_violations"] == 0 else 1,
        stats["vias"] if stats["vias"] is not None else math.inf,
        stats["traces"] if stats["traces"] is not None else math.inf,
    )


def _route_candidates(
    dsn_path: Path, ses_path: Path, command: list[str], *, attempts: int,
    timeout_seconds: float, overwrite: bool, seed: int | None, **options: Any,
) -> dict[str, Any]:
    """Run an isolated portfolio and atomically publish its best valid session."""
    if attempts == 1:
        return _route_specctra(
            dsn_path, ses_path, command, timeout_seconds=timeout_seconds,
            overwrite=overwrite, seed=seed, **options,
        )
    deadline = time.perf_counter() + timeout_seconds
    attempt_rows: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    winner_path: Path | None = None
    winner_index: int | None = None
    supports_seed = bool((options.get("capability_probe") or {}).get("seed"))
    base_seed = (0 if seed is None else seed) if supports_seed else None
    try:
        input_hash = hashlib.sha256(dsn_path.read_bytes()).hexdigest()
        # Same filesystem as the destination allows atomic final publication.
        # The context also cleans up losing sessions and interrupted attempts.
        with tempfile.TemporaryDirectory(prefix=".cw-routing-", dir=ses_path.parent) as temporary:
            for index in range(attempts):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                path = Path(temporary) / f"candidate-{index + 1}.ses"
                allowance = remaining / (attempts - index)
                attempt_options = dict(options)
                attempt_seed = base_seed + index if base_seed is not None else None
                if not supports_seed and attempt_options.get("optimizer_strategy") is None:
                    strategy = ("greedy", "global", "hybrid")[index % 3]
                    attempt_options["optimizer_strategy"] = strategy
                    attempt_options["optimizer_hybrid_ratio"] = "1:1" if strategy == "hybrid" else None
                result = _route_specctra(
                    dsn_path, path, command, timeout_seconds=allowance,
                    overwrite=False, seed=attempt_seed, **attempt_options,
                )
                row = {"attempt": index + 1, "seed": attempt_seed,
                       "optimizer_strategy": attempt_options.get("optimizer_strategy"),
                       "status": result["status"], "timeout_seconds": allowance}
                if "stats" in result:
                    row["stats"] = result["stats"]
                if "message" in result:
                    row["message"] = result["message"]
                attempt_rows.append(row)
                if hashlib.sha256(dsn_path.read_bytes()).hexdigest() != input_hash:
                    return _result_error("Routing input changed during candidate search; no session was published",
                                         attempts=attempt_rows)
                if result["status"] == "ok" and (
                    winner is None or _routing_rank(result["stats"]) < _routing_rank(winner["stats"])
                ):
                    winner, winner_path, winner_index = result, path, index + 1
            search = {
                "requested_attempts": attempts,
                "completed_attempts": len(attempt_rows),
                "selected_attempt": winner_index,
                "timeout_budget_seconds": timeout_seconds,
                "ranking": ["incomplete", "clearance_unreported", "vias", "traces"],
                "attempts": attempt_rows,
            }
            if winner is None or winner_path is None:
                return _result_error("No routing attempt produced a verified usable session", search=search)
            error = _publish_staged_file(winner_path, ses_path, overwrite=overwrite)
            if error:
                return _result_error(error, search=search)
            winner["artifact"]["path"] = str(ses_path)
            return {**winner, "search": search}
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
) -> dict[str, Any]:
    """Route a real KiCad board or a user-supplied DSN with Freerouting.

    Successful normal operation returns ``output_kind="specctra_session"`` and
    an ``output_path`` ending in ``.ses``. With ``routed_board_path``, import
    the session and require native DRC before publishing the routed PCB.
    A non-zero incomplete count returns ``status="partial"``.
    """
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
        capability_probe=capability_probe,
    )

    elapsed = time.perf_counter() - start_time
    if route_result["status"] != "ok":
        return {
            **route_result,
            "preflight": preflight,
            "input": input_validation,
            **({"routing_clearance_guardband": routing_clearance_guardband}
               if routing_clearance_guardband is not None else {}),
        }

    stats = {**route_result["stats"], "routing_time_seconds": elapsed}
    incomplete = stats["incomplete"]
    clearance_verified = stats["clearance_violations"] == 0
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
        **({"routing_clearance_guardband": routing_clearance_guardband}
           if routing_clearance_guardband is not None else {}),
        **({"search": route_result["search"]} if "search" in route_result else {}),
    }
    if routed_board_path is not None and not incomplete:
        from .pcb_layout import import_routing_session

        imported = import_routing_session(
            source, ses_path, routed_board_path, overwrite=overwrite,
            kicad_python_path=kicad_python_path, expected_hashes=source_hashes,
        )
        if imported["status"] != "ok":
            return {**result, "status": "error", "message": imported["message"], "board_import": imported}
        return {**result, **imported, "session_path": str(ses_path), "requires_kicad_drc": False,
                "session_artifact": result["artifact"],
                "artifact": {"valid": True, "kind": "kicad_pcb", "path": imported["output_path"],
                             "sha256": imported["drc"]["board_sha256"]},
                "verification": {**result["verification"], "requires_kicad_drc": False, "kicad_drc": "passed"},
                "message": "Routing imported into a separate PCB and verified with KiCad DRC"}
    return result
