"""Read native silicon verification results that have a supported report format."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def inspect_silicon_report(check: str, path: Path, declared_top: str) -> str:
    """Reject failed native checks; identify other report formats as attestations."""
    suffix = path.suffix.lower()
    if check == "lvs" and path.name.lower().endswith(".netgen.rpt"):
        lines = path.read_text(encoding="utf-8").splitlines()
        summaries = [line.strip() for line in lines if line.startswith("Final result:")]
        if summaries != ["Final result: Circuits match uniquely."]:
            raise ValueError("Netgen did not report a unique final match")
        matches = [line for line in lines if line.startswith("Device classes ")]
        expected = f"Device classes {declared_top} and {declared_top} are equivalent."
        if not matches or matches[-1].strip() != expected:
            raise ValueError("Netgen final compared cells must match the declared top")
        return "netgen_lvs"
    if check == "provider_precheck" and suffix == ".xml":
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"invalid provider precheck XML: {exc}") from exc
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if not any(suite.get("name") == "Tiny Tapeout Prechecks" for suite in suites):
            return "attested"
        if len(suites) != 1:
            raise ValueError("expected one Tiny Tapeout precheck suite")
        cases = suites[0].findall("testcase")
        names = {case.get("name") for case in cases}
        required = {"Pin check", "Boundary check", "Layer check", "Verilog syntax check"}
        if not required.issubset(names) or None in names or len(names) != len(cases):
            raise ValueError("Tiny Tapeout precheck cases are incomplete or duplicated")
        if any(element.tag in {"error", "failure", "skipped"} for element in root.iter()):
            raise ValueError("Tiny Tapeout precheck has failed or skipped checks")
        for element in [root, *suites]:
            if any(int(element.get(key, "0")) != 0 for key in ("errors", "failures", "skipped")):
                raise ValueError("Tiny Tapeout precheck reports unsuccessful checks")
        return "tiny_tapeout_precheck"
    if check == "timing" and suffix == ".json":
        stats = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(stats, dict):
            raise ValueError("timing metrics must be a JSON object")
        if any(key.startswith("timing__") for key in stats):
            # LibreLane records the worst slack across corners and per-corner results.
            corners = {""}
            for key in stats:
                match = re.fullmatch(r"timing__(?:hold|setup)__ws(__corner:.+)", key)
                if match:
                    corners.add(match.group(1))
            for corner in sorted(corners):
                for kind in ("setup", "hold"):
                    slack = stats.get(f"timing__{kind}__ws{corner}")
                    violations = stats.get(f"timing__{kind}_vio__count{corner}")
                    if type(slack) not in (int, float) or not math.isfinite(slack) or slack < 0:
                        raise ValueError(f"invalid or negative LibreLane {kind} slack{corner}")
                    if type(violations) is not int or violations != 0:
                        raise ValueError(f"LibreLane {kind} violation count must be zero{corner}")
            return "librelane_timing"
        if not any(key.startswith("finish__") for key in stats):
            return "attested"
        for kind in ("setup", "hold"):
            slack = stats.get(f"finish__timing__{kind}__ws")
            if type(slack) not in (int, float) or not math.isfinite(slack) or slack < 0:
                raise ValueError(f"missing, invalid or negative post-route {kind} slack")
            violations = stats.get(f"finish__timing__drv__{kind}_violation_count")
            if violations is not None and (type(violations) is not int or violations != 0):
                raise ValueError(f"{kind} violation count must be zero")
        return "openroad_timing"
    if check == "drc" and suffix in {".lyrdb", ".rdb"}:
        try:
            import klayout.rdb as rdb

            report = rdb.ReportDatabase("")
            report.load(str(path))
        except (ImportError, RuntimeError, OSError) as exc:
            raise ValueError(f"could not read KLayout DRC report: {exc}") from exc
        if report.top_cell_name != declared_top or not any(report.each_category()):
            raise ValueError("DRC report must identify the declared top and contain rule categories")
        if report.num_items():
            raise ValueError(f"DRC report contains {report.num_items()} markers")
        return "klayout_drc"
    if check == "lvs" and suffix == ".lvsdb":
        try:
            import klayout.db as db

            result = db.LayoutVsSchematic()
            result.read(str(path))
            extracted = result.netlist()
            reference = result.reference
            cross_reference = result.xref()
            if extracted is None or reference is None or cross_reference is None:
                raise ValueError("LVS report has no compared netlists")
            roots = {c.name for c in extracted.top_circuits()}
            reference_roots = {c.name for c in reference.top_circuits()}
            top_matched = False
            for pair in cross_reference.each_circuit_pair():
                left, right = pair.first(), pair.second()
                if not left or not right or pair.status() != db.NetlistCrossReference.Match:
                    raise ValueError("LVS contains an unmatched circuit or a comparison warning")
                if left.name == declared_top and left.name in roots and right.name in reference_roots:
                    top_matched = True
            if not top_matched:
                raise ValueError("LVS lacks a matching comparison of the declared root")
        except (ImportError, RuntimeError, OSError) as exc:
            raise ValueError(f"could not read KLayout LVS report: {exc}") from exc
        return "klayout_lvs"
    return "attested"
