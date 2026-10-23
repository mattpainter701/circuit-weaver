"""SPICE netlist exporter for circuit-weaver designs.

Generates .cir netlist files from ComponentDef lists, suitable for
ngspice simulation. Maps component pin_nets to SPICE node names,
inserts .include directives for downloaded SPICE models, and adds
analysis control cards (.tran, .ac, .dc, .op).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .component_db import ComponentDef


def _normalize_node(net_name: str) -> str:
    """Convert a net name to a valid SPICE node identifier.

    SPICE node '0' is ground. Other names are sanitized to alphanumerics/underscores.
    """
    if not net_name:
        raise ValueError("A SPICE pin has no connected net; empty pins cannot be treated as ground")
    lower = net_name.lower()
    if lower in ("gnd", "ground", "vss", "0"):
        return "0"
    return re.sub(r"[^a-zA-Z0-9_]", "_", net_name)


def _parse_value(value: str) -> str:
    """Parse a component value string to SPICE-compatible format.

    Handles: 10k -> 10k, 100nF -> 100n, 4.7uH -> 4.7u, 10R -> 10,
    100pH -> 100p, 100 ohms -> 100, etc.
    """
    if not value:
        return "0"

    # Strip whitespace and collapse internal spaces
    value = value.strip().replace(" ", "")

    # Handle special R notation first: 10R -> 10, 4R7 -> 4.7
    m = re.match(r"^(\d+)R(\d+)$", value, re.IGNORECASE)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.match(r"^(\d+)R$", value, re.IGNORECASE)
    if m:
        return m.group(1)

    # Remove units: ohm, F, H (strip multi-char units first, then single-char)
    val = re.sub(r"(?i)(ohm|ohms|Ω)$", "", value).strip()
    # Strip trailing F or H ONLY after an SI prefix (100nF -> 100n, 4.7uH -> 4.7u)
    val = re.sub(r"(?i)([munpfkMG])[FH]$", r"\1", val)
    # Strip standalone F or H at end (100F -> 100, but keep SI suffixes like 10k)
    val = re.sub(r"(?i)^([\d.]+)[FH]$", r"\1", val)

    # ngspice interprets a trailing uppercase M as milli, while engineers
    # commonly write M for mega. Expand it so 1M means one megohm and retain
    # lowercase m for milli.
    mega_match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)M", val)
    if mega_match:
        val = f"{mega_match.group(1)}Meg"

    # Already numeric (possibly with SPICE suffix like k, M, u, n, p)
    if re.match(r"^[\d.]+[a-zA-Z]?$", val):
        return val

    return val


def _passive_to_spice(comp: ComponentDef) -> str | None:
    """Convert a passive component (R, C, L) to a SPICE primitive line.

    Returns: e.g. 'R1 net_a net_b 10k' or None if not a recognized passive.
    """
    ref = comp.source_ref or ""
    prefix = ref[0].upper() if ref else ""

    if prefix not in ("R", "C", "L"):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.$-]*", ref):
        raise ValueError(f"Invalid SPICE reference designator: {ref!r}")

    value = _parse_value(comp.value or "0")
    if not value or any(char.isspace() for char in value) or any(char in value for char in "\r\n;\"'"):
        raise ValueError(f"Invalid SPICE value for {ref}: {comp.value!r}")
    pin_nets = comp.pin_nets or {}

    # Passives have 2 pins: pin 1 and pin 2
    if len(pin_nets) != 2:
        return None
    pins = sorted(
        pin_nets.keys(), key=lambda item: (not str(item).isdigit(), int(item) if str(item).isdigit() else str(item))
    )
    if any(not pin_nets.get(pin) for pin in pins):
        raise ValueError(f"{ref} has an unconnected pin; SPICE export cannot infer a ground connection")

    node1 = _normalize_node(pin_nets.get(pins[0], ""))
    node2 = _normalize_node(pin_nets.get(pins[1], ""))

    return f"{ref} {node1} {node2} {value}"


def _ic_to_spice_subckt(comp: ComponentDef, model: str | dict[str, Any]) -> list[str]:
    """Generate .subckt instance lines for an IC with a downloaded SPICE model.

    Returns list of SPICE lines: [.include directive, instance line].
    """
    ref = comp.source_ref or ""
    pin_nets = comp.pin_nets or {}

    if not isinstance(model, dict) or not model.get("path") or not model.get("subckt"):
        raise ValueError(f"{ref} model needs explicit path and subckt metadata")
    pin_order = model.get("pin_order")
    if (
        not isinstance(pin_order, list)
        or not pin_order
        or len(set(map(str, pin_order))) != len(pin_order)
        or set(map(str, pin_order)) != set(map(str, pin_nets))
    ):
        raise ValueError(f"{ref} model needs a pin_order matching its connected pin numbers")
    subckt_name = str(model["subckt"])
    model_path = str(model["path"])
    if not re.fullmatch(r"[A-Za-z0-9_.$-]+", subckt_name) or any(c in model_path for c in "\r\n\"'"):
        raise ValueError(f"{ref} has unsafe SPICE model metadata")
    nodes = [
        _normalize_node(pin_nets[str(pin)]) if str(pin) in pin_nets else _normalize_node(pin_nets[pin])
        for pin in pin_order
    ]

    node_str = " ".join(nodes)
    return [
        f'.include "{model_path}"',
        f"X{ref} {node_str} {subckt_name}",
    ]


def _generate_analysis_cards(
    analysis_type: str,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """Generate SPICE analysis control lines.

    Args:
        analysis_type: 'tran', 'ac', 'dc', or 'op'
        params: Analysis parameters. Defaults provided for each type.

    Returns:
        List of SPICE control lines.
    """
    if params is not None and not isinstance(params, dict):
        raise ValueError("analysis_params must be an object")
    params = params or {}
    if analysis_type not in {"tran", "ac", "dc", "op"}:
        raise ValueError(f"Unsupported SPICE analysis type: {analysis_type!r}")

    def token(name: str, default: Any) -> str:
        value = params.get(name, default)
        text = str(value)
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?(?:meg|[tgkmunpf])?", text, re.IGNORECASE):
            raise ValueError(f"Unsafe or unsupported SPICE analysis parameter {name}: {value!r}")
        return text

    if analysis_type == "tran":
        step = token("step", "1u")
        stop = token("stop", "10m")
        start = token("start", "0")
        return [f".tran {step} {stop} {start}"]

    elif analysis_type == "ac":
        variation = params.get("variation", "dec")
        if variation not in {"dec", "oct", "lin"}:
            raise ValueError(f"Unsupported AC variation: {variation!r}")
        points = params.get("points", 100)
        if isinstance(points, bool) or not isinstance(points, int) or points < 1:
            raise ValueError("AC points must be a positive integer")
        fstart = token("fstart", "1")
        fstop = token("fstop", "10Meg")
        return [f".ac {variation} {points} {fstart} {fstop}"]

    elif analysis_type == "dc":
        source = params.get("source", "V1")
        if not isinstance(source, str) or not re.fullmatch(r"[VI][A-Za-z0-9_.$-]*", source, re.IGNORECASE):
            raise ValueError(f"Unsafe DC source reference: {source!r}")
        start = token("start", "0")
        stop = token("stop", "5")
        step = token("step", "0.01")
        return [f".dc {source} {start} {stop} {step}"]

    elif analysis_type == "op":
        return [".op"]

    return [f".{analysis_type}"]


def _format_testbench_sources(sources: list[dict[str, Any]] | None, components: list[ComponentDef]) -> list[str]:
    """Render explicitly authored voltage/current sources without raw SPICE input."""
    if sources is not None and not isinstance(sources, list):
        raise ValueError("simulation sources must be a list")

    known_nodes: dict[str, str] = {}
    known_original_names: set[str] = set()
    occupied_refs = {str(comp.source_ref or "").casefold() for comp in components}
    for comp in components:
        for net in (comp.pin_nets or {}).values():
            if not isinstance(net, str) or not net.strip():
                continue
            node = _normalize_node(net)
            key = node.casefold()
            original = "0" if node == "0" else net
            if key in known_nodes and known_nodes[key] != original:
                raise ValueError(
                    f"Distinct circuit nets {known_nodes[key]!r} and {net!r} normalize to the same SPICE node"
                )
            known_nodes[key] = original
            known_original_names.add(net.casefold())

    if sources is None:
        return []

    lines: list[str] = []
    seen_source_refs: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"simulation source {index} must be an object")
        allowed = {"ref", "type", "positive", "negative", "dc", "ac"}
        if set(source) - allowed or not {"ref", "type", "positive", "negative"}.issubset(source):
            raise ValueError(
                f"simulation source {index} requires ref/type/positive/negative and accepts only dc/ac values"
            )
        ref = source["ref"]
        source_type = source["type"]
        if not isinstance(ref, str) or not re.fullmatch(r"[VI][A-Za-z0-9_.$-]*", ref, re.IGNORECASE):
            raise ValueError(f"simulation source {index} has an invalid V/I reference")
        if (
            not isinstance(source_type, str)
            or source_type.upper() not in {"V", "I"}
            or ref[0].upper() != source_type.upper()
        ):
            raise ValueError(f"simulation source {ref!r} type must match its V/I reference prefix")
        ref_key = ref.casefold()
        if ref_key in seen_source_refs or ref_key in occupied_refs:
            raise ValueError(f"duplicate simulation source reference {ref!r}")
        seen_source_refs.add(ref_key)

        raw_positive, raw_negative = source["positive"], source["negative"]
        if (
            not isinstance(raw_positive, str)
            or not raw_positive.strip()
            or raw_positive != raw_positive.strip()
            or not isinstance(raw_negative, str)
            or not raw_negative.strip()
            or raw_negative != raw_negative.strip()
        ):
            raise ValueError(f"simulation source {ref} requires named positive and negative nets")
        positive, negative = _normalize_node(raw_positive), _normalize_node(raw_negative)
        for raw, normalized in ((raw_positive, positive), (raw_negative, negative)):
            if normalized != "0" and (
                normalized.casefold() not in known_nodes or raw.casefold() not in known_original_names
            ):
                raise ValueError(f"simulation source {ref} references unconnected net {raw!r}")
        if positive.casefold() == negative.casefold():
            raise ValueError(f"simulation source {ref} connects a net to itself")

        values = []
        for field in ("dc", "ac"):
            value = source.get(field)
            if value is None:
                continue
            if isinstance(value, bool):
                raise ValueError(f"simulation source {ref} {field} value must be finite numeric")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"simulation source {ref} {field} value must be finite numeric") from exc
            if not math.isfinite(number):
                raise ValueError(f"simulation source {ref} {field} value must be finite numeric")
            values.extend([field.upper(), format(number, ".17g")])
        if not values:
            raise ValueError(f"simulation source {ref} must specify dc and/or ac")
        lines.append(f"{ref} {positive} {negative} " + " ".join(values))
    return lines


def export_spice_netlist(
    components: list[ComponentDef],
    output_path: str | Path,
    *,
    include_dirs: list[str | Path] | None = None,
    analysis_type: str = "tran",
    analysis_params: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    model_manifest: dict[str, str | dict[str, Any]] | None = None,
    title: str = "Circuit Weaver Simulation",
) -> Path:
    """Generate a SPICE netlist (.cir) from resolved components.

    Args:
        components: List of ComponentDef with pin_nets populated.
        output_path: Path to write the .cir file.
        include_dirs: Directories to search for SPICE model files.
        analysis_type: Type of analysis ('tran', 'ac', 'dc', 'op').
        analysis_params: Parameters for the analysis card.
        model_manifest: Dict mapping MPN to model file path.
        title: Title line for the netlist.

    Returns:
        Path to the generated .cir file.
    """
    lines: list[str] = []
    lines.append(f"* {title}")
    lines.append("* Generated by circuit-weaver")
    lines.append("")

    # Track which models have been included
    included_models: set[str] = set()
    model_manifest = model_manifest or {}

    # Process components
    passive_lines: list[str] = []
    subckt_lines: list[str] = []
    skipped: list[str] = []

    for comp in components:
        ref = comp.source_ref or ""
        if not ref:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.$-]*", ref):
            raise ValueError(f"Invalid SPICE reference designator: {ref!r}")

        # Try as passive first
        passive_line = _passive_to_spice(comp)
        if passive_line:
            passive_lines.append(passive_line)
            continue

        # Try as IC with SPICE model
        mpn = comp.mpn or ""
        if mpn and mpn in model_manifest:
            model = model_manifest[mpn]
            if (
                not isinstance(model, dict)
                or not model.get("path")
                or not model.get("subckt")
                or not model.get("pin_order")
            ):
                skipped.append(f"* Skipped {ref} ({mpn}) - model lacks explicit subckt/pin_order contract")
                continue
            model_path = model.get("path", "") if isinstance(model, dict) else model
            if model_path not in included_models:
                ic_lines = _ic_to_spice_subckt(comp, model)
                subckt_lines.extend(ic_lines)
                included_models.add(model_path)
            else:
                # Reuse only an explicit model contract; never guess pin order or subcircuit name.
                if not isinstance(model, dict) or not model.get("subckt") or not model.get("pin_order"):
                    raise ValueError(f"{ref} model needs explicit path, subckt, and pin_order metadata")
                pin_order = list(map(str, model["pin_order"]))
                if len(set(pin_order)) != len(pin_order) or set(pin_order) != set(
                    map(str, (comp.pin_nets or {}).keys())
                ):
                    raise ValueError(f"{ref} model pin_order does not match connected pins")
                if not re.fullmatch(r"[A-Za-z0-9_.$-]+", str(model["subckt"])):
                    raise ValueError(f"{ref} has unsafe SPICE subcircuit metadata")
                nodes = " ".join(_normalize_node((comp.pin_nets or {})[p]) for p in pin_order)
                subckt_name = str(model["subckt"])
                subckt_lines.append(f"X{ref} {nodes} {subckt_name}")
        else:
            skipped.append(f"* Skipped {ref} ({mpn or 'no MPN'}) - no SPICE model")

    # Write sections
    if subckt_lines:
        lines.append("* --- IC / Subcircuit models ---")
        lines.extend(subckt_lines)
        lines.append("")

    if passive_lines:
        lines.append("* --- Passive components ---")
        lines.extend(passive_lines)
        lines.append("")

    source_lines = _format_testbench_sources(sources, components)
    if source_lines:
        lines.append("* --- Explicit testbench sources ---")
        lines.extend(source_lines)
        lines.append("")

    if skipped:
        lines.append("* --- Components without SPICE models ---")
        lines.extend(skipped)
        lines.append("")

    # Add analysis card
    lines.append("* --- Analysis ---")
    lines.extend(_generate_analysis_cards(analysis_type, analysis_params))
    lines.append("")

    # Control section
    lines.append(".control")
    lines.append("set filetype=ascii")
    lines.append("run")
    lines.append("write results.raw")
    lines.append(".endc")
    lines.append("")
    lines.append(".end")

    # Write to file
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    # Log to design.log
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        dl.log_generation(artifact_type="spice_netlist", path=str(out), status="ok")

    return out
