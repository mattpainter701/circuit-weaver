"""Fetch SPICE models and S-parameter files for circuit components.

Downloads .subckt/.cir SPICE models from manufacturer websites.

Usage:
    from circuit_weaver.spice_fetcher import fetch_spice_models
    result = fetch_spice_models(spec, output_dir="./project")
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from pathlib import Path

from .component_db import ComponentDef
from .dispatcher import compile_design_ir

log = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (circuit-weaver spice-fetcher)"
_TIMEOUT = 30

_SPICE_URL_PATTERNS: list[tuple[str, str]] = [
    ("texas instruments", "https://www.ti.com/lit/zip/{mpn}"),
    ("ti", "https://www.ti.com/lit/zip/{mpn}"),
    ("analog devices", "https://www.analog.com/media/en/simulation-models/{mpn_lower}.cir"),
    ("adi", "https://www.analog.com/media/en/simulation-models/{mpn_lower}.cir"),
    ("microchip", "https://ww1.microchip.com/downloads/en/DeviceDoc/{mpn}.zip"),
    ("onsemi", "https://www.onsemi.com/download/model/{mpn_lower}.lib"),
    ("on semiconductor", "https://www.onsemi.com/download/model/{mpn_lower}.lib"),
]


def _download_file(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
            if len(data) < 50:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
    except Exception:
        return False


def _guess_manufacturer(comp: ComponentDef) -> str:
    mfr = (comp.source_manufacturer or "").lower()
    if mfr:
        return mfr
    mpn = (comp.mpn or "").upper()
    if mpn.startswith(("TPS", "TLV", "LM", "OPA", "INA", "ADS", "DRV", "BQ", "TMS", "MSP")):
        return "texas instruments"
    if mpn.startswith(("AD", "ADP", "ADM", "LTC", "LT", "MAX", "HMC")):
        return "analog devices"
    if mpn.startswith(("MCP", "PIC", "ATSAM", "dsPIC")):
        return "microchip"
    if mpn.startswith(("NCV", "NCP", "FAN")):
        return "onsemi"
    return ""


def _try_spice_urls(mpn: str, manufacturer: str, dest_dir: Path) -> Path | None:
    mfr_lower = manufacturer.lower()
    for pattern_mfr, url_template in _SPICE_URL_PATTERNS:
        if pattern_mfr not in mfr_lower:
            continue
        url = url_template.format(mpn=mpn.upper(), mpn_lower=mpn.lower())
        ext = Path(url).suffix or ".zip"
        safe_name = re.sub(r"[^\w\-.]", "_", mpn)
        dest = dest_dir / f"{safe_name}{ext}"
        if _download_file(url, dest):
            return dest
    return None


def _is_analog_component(comp: ComponentDef) -> bool:
    combined = f"{(comp.description or '').lower()} {(comp.mpn or '').lower()}"
    return any(
        kw in combined
        for kw in (
            "op amp",
            "opamp",
            "amplifier",
            "comparator",
            "regulator",
            "ldo",
            "buck",
            "boost",
            "converter",
            "reference",
            "dac",
            "adc",
            "transistor",
            "mosfet",
            "bjt",
            "diode",
            "zener",
        )
    )


def _is_high_speed_component(comp: ComponentDef) -> bool:
    combined = f"{(comp.description or '').lower()} {(comp.mpn or '').lower()}"
    return any(
        kw in combined
        for kw in (
            "usb",
            "ddr",
            "lvds",
            "pcie",
            "mipi",
            "ethernet",
            "serdes",
            "rf",
            "mixer",
            "oscillator",
            "vco",
            "pll",
            "balun",
        )
    )


def fetch_spice_models(
    spec: dict,
    output_dir: str | Path = ".",
    *,
    include_s_params: bool = False,
    delay: float = 0.5,
) -> dict:
    """Fetch SPICE models for design components. Returns status dict."""
    output_dir = Path(output_dir)
    spice_dir = output_dir / "spice_models"
    sparam_dir = output_dir / "s_params" if include_s_params else None

    try:
        compiled = compile_design_ir(spec)
    except Exception as e:
        return {"status": "error", "message": f"Failed to compile spec: {e}", "project": spec.get("project", "Unknown")}

    project_name = spec.get("project", "Unknown")
    seen: set[str] = set()
    candidates: list[ComponentDef] = []
    for comp in compiled.components:
        mpn = comp.mpn or ""
        if not mpn or mpn in seen:
            continue
        if _is_analog_component(comp) or (include_s_params and _is_high_speed_component(comp)):
            seen.add(mpn)
            candidates.append(comp)

    manifest: dict = {}
    warnings: list[str] = []
    spice_ok = spice_fail = sparam_ok = sparam_fail = 0

    for comp in candidates:
        mpn = comp.mpn or ""
        manufacturer = _guess_manufacturer(comp)
        entry: dict = {
            "mpn": mpn,
            "manufacturer": manufacturer or comp.source_manufacturer or "",
            "ref": comp.source_ref or "",
        }

        if _is_analog_component(comp):
            result = _try_spice_urls(mpn, manufacturer, spice_dir)
            if result:
                entry.update(spice_file=result.name, spice_status="ok")
                spice_ok += 1
            else:
                entry["spice_status"] = "not_found"
                spice_fail += 1
            if delay > 0:
                time.sleep(delay)

        if include_s_params and _is_high_speed_component(comp) and sparam_dir:
            entry.update(sparam_status="manual_required", sparam_note=f"Check {manufacturer or 'manufacturer'} website")
            sparam_fail += 1
            warnings.append(f"S-parameters for {mpn}: check manufacturer website manually")

        manifest[mpn] = entry

        # Log each lookup to design.log
        from .logging_bridge import get_design_logger

        dl = get_design_logger()
        if dl:
            dl.log_part_lookup(
                mpn=mpn,
                source="spice_model",
                status=entry.get("spice_status", "unknown"),
                details={"manufacturer": entry.get("manufacturer", "")},
            )

    spice_dir.mkdir(parents=True, exist_ok=True)
    (spice_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    if sparam_dir:
        sparam_dir.mkdir(parents=True, exist_ok=True)

    return {
        "status": "ok",
        "project": project_name,
        "spice_dir": str(spice_dir),
        "sparam_dir": str(sparam_dir) if sparam_dir else None,
        "components_checked": len(candidates),
        "spice_downloaded": spice_ok,
        "spice_not_found": spice_fail,
        "sparam_downloaded": sparam_ok,
        "sparam_not_found": sparam_fail,
        "manifest": manifest,
        "warnings": warnings,
    }


def resolve_spice_models(manifest_path: Path) -> dict[str, str | dict[str, object]]:
    """Read the model manifest without promoting path-only entries to models.

    Args:
        manifest_path: Path to spice_models/manifest.json

    Returns:
        MPNs with an existing contained file map to an absolute path when the
        manifest is legacy/path-only. Explicit contracts map to
        ``{path, subckt, pin_order}`` only when the declared subcircuit and pin
        count match the model file. The netlist exporter deliberately treats
        legacy path-only entries as unsupported.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return {}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(manifest, dict):
        return {}

    result: dict[str, str | dict[str, object]] = {}
    model_dir = manifest_path.parent.resolve()

    for mpn, entry in manifest.items():
        if not isinstance(entry, dict) or entry.get("spice_status") != "ok":
            continue
        filename = entry.get("spice_file")
        if not isinstance(filename, str) or not filename.strip():
            continue
        relative_path = Path(filename)
        try:
            model_file = (relative_path if relative_path.is_absolute() else model_dir / relative_path).resolve()
            if not model_file.is_relative_to(model_dir) or not model_file.is_file():
                continue
        except (OSError, RuntimeError):
            continue

        subckt = entry.get("subckt")
        pin_order = entry.get("pin_order")
        if subckt is None or pin_order is None:
            # Preserve the old path-only shape so consumers can explain that
            # the download exists but lacks an authored electrical contract.
            result[str(mpn)] = str(model_file)
            continue
        if (
            not isinstance(subckt, str)
            or not re.fullmatch(r"[A-Za-z0-9_.$-]+", subckt)
            or not isinstance(pin_order, list)
            or not pin_order
            or any(
                not isinstance(pin, (str, int)) or not re.fullmatch(r"[A-Za-z0-9_.$-]+", str(pin)) for pin in pin_order
            )
            or len(set(map(str, pin_order))) != len(pin_order)
        ):
            result[str(mpn)] = str(model_file)
            continue
        try:
            model_text = model_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            result[str(mpn)] = str(model_file)
            continue
        declarations: dict[str, int] = {}
        for line in model_text.splitlines():
            declaration = re.match(r"^\s*\.subckt\s+(\S+)(?:\s+(.*?))?\s*$", line, re.I)
            if declaration:
                pins = (declaration.group(2) or "").split()
                lower_pins = [pin.lower() for pin in pins]
                if "params:" in lower_pins:
                    pins = pins[: lower_pins.index("params:")]
                declarations[declaration.group(1).lower()] = len(pins)
        if declarations.get(subckt.lower()) != len(pin_order):
            result[str(mpn)] = str(model_file)
            continue
        result[str(mpn)] = {"path": str(model_file), "subckt": subckt, "pin_order": list(map(str, pin_order))}

    return result
