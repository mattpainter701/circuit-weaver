"""Thermal analysis for PCB placement.

Computes junction temperatures, identifies hotspots, and generates heatmap SVG.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .component_db import ComponentDef

_COPPER_THETA_PER_CM2 = 50.0


def _load_specs(specs_dir: Path | None) -> tuple[dict, dict]:
    ic_thermal: dict = {}
    metadata: dict = {}
    if not specs_dir or not specs_dir.exists():
        return ic_thermal, metadata
    for name, target in [("ic_thermal.json", ic_thermal), ("metadata.json", metadata)]:
        p = specs_dir / name
        if p.exists():
            try:
                target.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    return ic_thermal, metadata


def _get_thermal_params(comp: ComponentDef, ic_thermal: dict, metadata: dict) -> tuple[float, float, float]:
    mpn = comp.mpn or ""
    meta = metadata.get(mpn, {})
    api = ic_thermal.get(mpn, {})

    def _float(v: object) -> float:
        if isinstance(v, str):
            try:
                v = float(v.split()[0])
            except (ValueError, IndexError):
                return 0.0
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return float(v)
        return 0.0

    theta_ja = _float(meta.get("theta_ja")) or _float(api.get("theta_ja"))
    pdiss_w = _float(meta.get("pdiss_max_w")) or _float(api.get("pdiss_max_w"))
    tj_max = _float(meta.get("tj_max")) or _float(api.get("tj_max"))

    if not theta_ja and pdiss_w > 0:
        fp = (comp.footprint or "").upper()
        if "QFN" in fp or "BGA" in fp:
            theta_ja = 35.0
        elif "SOIC" in fp or "TSSOP" in fp:
            theta_ja = 80.0
        elif "SOT" in fp:
            theta_ja = 150.0
        else:
            theta_ja = 60.0

    if not tj_max:
        tj_max = 125.0

    return theta_ja, pdiss_w, tj_max


def analyze_thermal(
    components: list[ComponentDef],
    placements: dict[str, dict] | None = None,
    *,
    specs_dir: str | Path | None = None,
    ambient_temp_c: float = 25.0,
    margin_c: float = 10.0,
) -> dict:
    """Analyze thermal performance. Returns dict with components, hotspots, proximity_warnings, recommendations."""
    specs_path = Path(specs_dir) if specs_dir else None
    ic_thermal, metadata = _load_specs(specs_path)

    results: list[dict] = []
    hotspots: list[dict] = []
    total_power = 0.0

    for comp in components:
        ref = comp.source_ref or ""
        if not ref:
            continue
        mpn = comp.mpn or ""
        theta_ja, pdiss_w, tj_max_val = _get_thermal_params(comp, ic_thermal, metadata)
        if pdiss_w <= 0:
            continue

        total_power += pdiss_w
        tj_calc = ambient_temp_c + pdiss_w * theta_ja
        tj_margin = tj_max_val - tj_calc
        status = "critical" if tj_margin < 0 else ("warning" if tj_margin < margin_c else "ok")

        suggestion = ""
        if status == "critical":
            needed = (tj_max_val - margin_c - ambient_temp_c) / pdiss_w if pdiss_w > 0 else 0
            if 0 < needed < theta_ja:
                copper_area = _COPPER_THETA_PER_CM2 / needed
                suggestion = (
                    f"Add heatsink or increase copper pour to >{copper_area:.0f} cm\u00b2. Consider thermal vias."
                )
            else:
                suggestion = "Junction temperature exceeds maximum. Add heatsink or external cooling."
        elif status == "warning":
            suggestion = f"Margin only {tj_margin:.0f}\u00b0C. Consider additional copper area or thermal vias."

        results.append(
            {
                "ref": ref,
                "mpn": mpn,
                "theta_ja": round(theta_ja, 1),
                "pdiss_w": round(pdiss_w, 3),
                "tj_calculated": round(tj_calc, 1),
                "tj_max": round(tj_max_val, 1),
                "margin_c": round(tj_margin, 1),
                "status": status,
                "suggestion": suggestion,
            }
        )
        if status in ("critical", "warning"):
            hotspots.append({"ref": ref, "tj_calculated": round(tj_calc, 1), "pdiss_w": round(pdiss_w, 3)})

    proximity_warnings: list[dict] = []
    if placements:
        hot = [r for r in results if r["pdiss_w"] > 0.1]
        for i in range(len(hot)):
            for j in range(i + 1, len(hot)):
                a, b = placements.get(hot[i]["ref"], {}), placements.get(hot[j]["ref"], {})
                if not a or not b:
                    continue
                dist = math.hypot(a.get("x", 0) - b.get("x", 0), a.get("y", 0) - b.get("y", 0))
                combined = hot[i]["pdiss_w"] + hot[j]["pdiss_w"]
                if dist < 10.0 and combined > 0.5:
                    proximity_warnings.append(
                        {
                            "ref_a": hot[i]["ref"],
                            "ref_b": hot[j]["ref"],
                            "distance_mm": round(dist, 1),
                            "combined_heat_w": round(combined, 3),
                        }
                    )

    critical = sum(1 for r in results if r["status"] == "critical")
    warning = sum(1 for r in results if r["status"] == "warning")
    ok_count = sum(1 for r in results if r["status"] == "ok")

    recommendations: list[str] = []
    if critical:
        recommendations.append(f"{critical} component(s) exceed Tj_max \u2014 add heatsinks or increase copper area")
    if warning:
        recommendations.append(f"{warning} component(s) have <{margin_c}\u00b0C margin \u2014 review thermal design")
    if proximity_warnings:
        recommendations.append(
            f"{len(proximity_warnings)} hot component pairs within 10mm \u2014 consider spreading apart"
        )
    if total_power > 5.0:
        recommendations.append(f"Total board power {total_power:.1f}W \u2014 consider airflow or heatsink")
    if not results:
        recommendations.append("Supply component power dissipation data to assess junction temperatures.")
    elif not recommendations:
        recommendations.append("Thermal design looks adequate at current ambient temperature")

    summary = (
        f"{len(results)} power-dissipating components, {total_power:.2f}W total. "
        f"{critical} critical, {warning} warning, {ok_count} ok."
    )

    # Log thermal results to design.log
    from .logging_bridge import get_design_logger

    dl = get_design_logger()
    if dl:
        for r in results:
            if r["status"] in ("critical", "warning"):
                dl.log_thermal(
                    ref=r["ref"],
                    tj_calc=r["tj_calculated"],
                    tj_max=r["tj_max"],
                    status=r["status"],
                )

    return {
        "status": "ok",
        "ambient_temp_c": ambient_temp_c,
        "total_power_w": round(total_power, 3),
        "components": results,
        "hotspots": hotspots,
        "proximity_warnings": proximity_warnings,
        "summary": summary,
        "recommendations": recommendations,
    }


def _tj_to_rgb(t: float) -> tuple[int, int, int]:
    if t < 0.25:
        f = t / 0.25
        return (int(59 + (34 - 59) * f), int(130 + (211 - 130) * f), int(246 + (238 - 246) * f))
    if t < 0.5:
        f = (t - 0.25) / 0.25
        return (int(34 + (250 - 34) * f), int(211 + (204 - 211) * f), int(238 + (21 - 238) * f))
    if t < 0.75:
        f = (t - 0.5) / 0.25
        return (int(250 + (249 - 250) * f), int(204 + (115 - 204) * f), int(21 + (22 - 21) * f))
    f = min((t - 0.75) / 0.25, 1.0)
    return (int(249 + (239 - 249) * f), int(115 + (68 - 115) * f), int(22 + (68 - 22) * f))


def generate_heatmap_svg(
    components: list[ComponentDef],
    placements: dict[str, dict],
    board_width_mm: float = 100.0,
    board_height_mm: float = 80.0,
    *,
    specs_dir: str | Path | None = None,
    ambient_temp_c: float = 25.0,
    output_path: str | Path | None = None,
) -> str:
    """Generate a thermal heatmap SVG. Returns SVG string."""
    specs_path = Path(specs_dir) if specs_dir else None
    ic_thermal, metadata = _load_specs(specs_path)

    scale = 8.0
    svg_w, svg_h, pad = board_width_mm * scale, board_height_mm * scale, 30

    comp_data: list[dict] = []
    max_tj = 25.0
    for comp in components:
        ref = comp.source_ref or ""
        if not ref or ref not in placements:
            continue
        theta_ja, pdiss_w, _ = _get_thermal_params(comp, ic_thermal, metadata)
        if pdiss_w <= 0:
            continue
        tj = ambient_temp_c + pdiss_w * theta_ja
        max_tj = max(max_tj, tj)
        p = placements[ref]
        comp_data.append(
            {
                "ref": ref,
                "x": p.get("x", 0) * scale,
                "y": p.get("y", 0) * scale,
                "tj": tj,
                "pdiss": pdiss_w,
                "radius": max(15, math.sqrt(pdiss_w) * 30),
            }
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w + pad * 2:.0f}" height="{svg_h + pad * 2 + 40:.0f}" '
        f'viewBox="{-pad} {-pad} {svg_w + pad * 2:.0f} {svg_h + pad * 2 + 40:.0f}">',
        "<defs>",
    ]
    for i, c in enumerate(comp_data):
        t = min(c["tj"] / max(max_tj, 1), 1.0)
        r, g, b = _tj_to_rgb(t)
        grad = (
            f'<radialGradient id="g{i}"><stop offset="0%" stop-color="rgb({r},{g},{b})" '
            f'stop-opacity="0.7"/><stop offset="100%" stop-color="rgb({r},{g},{b})" '
            f'stop-opacity="0"/></radialGradient>'
        )
        parts.append(grad)
    parts.append("</defs>")
    bg_rect = (
        f'<rect x="0" y="0" width="{svg_w:.0f}" height="{svg_h:.0f}" fill="#1a1a2e" '
        f'stroke="#475569" stroke-width="2" rx="4"/>'
    )
    parts.append(bg_rect)

    for i, c in enumerate(comp_data):
        parts.append(f'<circle cx="{c["x"]:.1f}" cy="{c["y"]:.1f}" r="{c["radius"]:.0f}" fill="url(#g{i})"/>')
    for c in comp_data:
        ref_text = (
            f'<text x="{c["x"]:.1f}" y="{c["y"]:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" font-size="9" fill="white" '
            f'font-weight="bold">{c["ref"]}</text>'
        )
        parts.append(ref_text)
        data_text = (
            f'<text x="{c["x"]:.1f}" y="{c["y"] + 11:.1f}" text-anchor="middle" '
            f'font-size="7" fill="#94a3b8">{c["tj"]:.0f}\u00b0C / {c["pdiss"]:.2f}W</text>'
        )
        parts.append(data_text)

    legend_y = svg_h + 15
    bar_w = min(svg_w * 0.6, 300)
    bar_x = (svg_w - bar_w) / 2
    title_text = (
        f'<text x="{svg_w / 2:.0f}" y="{legend_y - 3:.0f}" text-anchor="middle" '
        f'font-size="9" fill="#94a3b8">Junction Temperature</text>'
    )
    parts.append(title_text)
    for k in range(int(bar_w)):
        r, g, b = _tj_to_rgb(k / bar_w)
        parts.append(f'<rect x="{bar_x + k:.0f}" y="{legend_y:.0f}" width="1" height="10" fill="rgb({r},{g},{b})"/>')
    min_label = (
        f'<text x="{bar_x:.0f}" y="{legend_y + 22:.0f}" font-size="8" fill="#94a3b8">{ambient_temp_c:.0f}\u00b0C</text>'
    )
    parts.append(min_label)
    max_label = (
        f'<text x="{bar_x + bar_w:.0f}" y="{legend_y + 22:.0f}" text-anchor="end" '
        f'font-size="8" fill="#94a3b8">{max_tj:.0f}\u00b0C</text>'
    )
    parts.append(max_label)
    parts.append("</svg>")

    svg_str = "\n".join(parts)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(svg_str, encoding="utf-8")
    return svg_str
