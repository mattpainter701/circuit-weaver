"""SPICE netlist exporter for circuit-weaver designs.

Generates .cir netlist files from ComponentDef lists, suitable for
ngspice simulation. Maps component pin_nets to SPICE node names,
inserts .include directives for downloaded SPICE models, and adds
analysis control cards (.tran, .ac, .dc, .op).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .component_db import ComponentDef


def _normalize_node(net_name: str) -> str:
    """Convert a net name to a valid SPICE node identifier.

    SPICE node '0' is ground. Other names are sanitized to alphanumerics/underscores.
    """
    if not net_name:
        return "0"
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

    value = _parse_value(comp.value or "0")
    pin_nets = comp.pin_nets or {}

    # Passives have 2 pins: pin 1 and pin 2
    pins = sorted(pin_nets.keys())
    if len(pins) < 2:
        return None

    node1 = _normalize_node(pin_nets.get(pins[0], ""))
    node2 = _normalize_node(pin_nets.get(pins[1], ""))

    return f"{ref} {node1} {node2} {value}"


def _ic_to_spice_subckt(comp: ComponentDef, model_path: str) -> list[str]:
    """Generate .subckt instance lines for an IC with a downloaded SPICE model.

    Returns list of SPICE lines: [.include directive, instance line].
    """
    ref = comp.source_ref or ""
    mpn = comp.mpn or "unknown"
    pin_nets = comp.pin_nets or {}

    # Build node list from pin ordering
    nodes = []
    for pin_num in sorted(pin_nets.keys()):
        nodes.append(_normalize_node(pin_nets[pin_num]))

    node_str = " ".join(nodes)
    subckt_name = re.sub(r"[^a-zA-Z0-9_]", "_", mpn)

    return [
        f".include {model_path}",
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
    params = params or {}

    if analysis_type == "tran":
        step = params.get("step", "1u")
        stop = params.get("stop", "10m")
        start = params.get("start", "0")
        return [f".tran {step} {stop} {start}"]

    elif analysis_type == "ac":
        variation = params.get("variation", "dec")
        points = params.get("points", 100)
        fstart = params.get("fstart", "1")
        fstop = params.get("fstop", "10Meg")
        return [f".ac {variation} {points} {fstart} {fstop}"]

    elif analysis_type == "dc":
        source = params.get("source", "V1")
        start = params.get("start", "0")
        stop = params.get("stop", "5")
        step = params.get("step", "0.01")
        return [f".dc {source} {start} {stop} {step}"]

    elif analysis_type == "op":
        return [".op"]

    return [f".{analysis_type}"]


def export_spice_netlist(
    components: list[ComponentDef],
    output_path: str | Path,
    *,
    include_dirs: list[str | Path] | None = None,
    analysis_type: str = "tran",
    analysis_params: dict[str, Any] | None = None,
    model_manifest: dict[str, str] | None = None,
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

        # Try as passive first
        passive_line = _passive_to_spice(comp)
        if passive_line:
            passive_lines.append(passive_line)
            continue

        # Try as IC with SPICE model
        mpn = comp.mpn or ""
        if mpn and mpn in model_manifest:
            model_path = model_manifest[mpn]
            if model_path not in included_models:
                ic_lines = _ic_to_spice_subckt(comp, model_path)
                subckt_lines.extend(ic_lines)
                included_models.add(model_path)
            else:
                # Model already included, just add instance
                pin_nets = comp.pin_nets or {}
                nodes = " ".join(_normalize_node(pin_nets[p]) for p in sorted(pin_nets.keys()))
                subckt_name = re.sub(r"[^a-zA-Z0-9_]", "_", mpn)
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
