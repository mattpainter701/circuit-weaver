"""Execute declared negative controls against the acceptance preflight.

Controls operate on deep-copied cases. Their declared expectations are kept
unchanged, so a control can only be detected when the real preflight reports a
    failure or declared unverified finding attributable to the mutation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable

Preflight = Callable[[dict[str, Any]], dict[str, Any]]


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot(case: dict[str, Any]) -> dict[str, str]:
    return {
        "case_sha256": _hash(case),
        "spec_sha256": _hash(case.get("spec")),
        "expectations_sha256": _hash(case.get("expectations")),
    }


def _component_rows(spec: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def is_raw_component(value: dict[str, Any]) -> bool:
        # Raw project components carry an identity/part declaration. Placement
        # and annotation records can repeat ``ref`` without describing a part.
        return "ic" in value or "mpn" in value or ("value" in value and "footprint" in value)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if str(value.get("ref", "")) == ref and is_raw_component(value):
                rows.append(value)
                # Canonical blocks may repeat their own identity in params;
                # that nested declaration is part of this same component.
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)
    # Nested dictionaries may themselves contain the same row. Deduplicate by
    # identity while preserving source order.
    return list({id(row): row for row in rows}.values())


def _mutate_connectivity(case: dict[str, Any], mutation: dict[str, Any]) -> tuple[bool, str]:
    endpoint = mutation.get("endpoint")
    ref = str(endpoint.get("ref", "")) if isinstance(endpoint, dict) else ""
    pad = str(endpoint.get("pad", "")) if isinstance(endpoint, dict) else ""
    replacement = str(mutation.get("replace_net", ""))
    if not ref or not pad or not replacement:
        return False, "connectivity mutation is missing endpoint ref, pad, or replacement net"
    rows = _component_rows(case.get("spec", {}), ref)
    if len(rows) != 1:
        return False, f"expected one raw component for {ref}, found {len(rows)}"
    component = rows[0]
    old_net = next(
        (
            str(row.get("net"))
            for row in case.get("expectations", {}).get("required_endpoints", [])
            if isinstance(row, dict)
            and str(row.get("ref", "")) == ref
            and str(row.get("pad", "")) == pad
            and row.get("net")
        ),
        None,
    )
    extra = component.get("pin_nets_extra")
    if isinstance(extra, dict):
        extra[pad] = replacement
        return True, f"set spec component {ref} pin_nets_extra[{pad!r}]"
    signals = component.get("signal_nets")
    if isinstance(signals, dict):
        key = pad if pad in signals else (str(int(pad) - 1) if pad.isdigit() and int(pad) > 0 else None)
        if key in signals:
            signals[key] = replacement
            return True, f"set spec component {ref} signal_nets[{key!r}]"
    if isinstance(signals, str) and pad.isdigit() and int(pad) > 0:
        values = [item.strip() for item in signals.split(",")]
        index = int(pad) - 1
        if index < len(values):
            values[index] = replacement
            component["signal_nets"] = ",".join(values)
            return True, f"set spec component {ref} signal_nets entry for pad {pad}"
    if isinstance(component.get("pin_nets"), dict):
        component["pin_nets"][pad] = replacement
        return True, f"set spec component {ref} pin_nets[{pad!r}]"
    # Template-backed ICs may not declare a raw pad map: their default pins
    # come from the product registry. The supported surgical override is
    # recognized by the real compiler and takes precedence over those defaults.
    if "ic" in component:
        component["pin_nets_extra"] = {pad: replacement}
        return True, f"set spec component {ref} pin_nets_extra[{pad!r}]"
    # Subcircuit-backed components express pad connectivity through semantic
    # fields such as txd_net or sensor_p_net. Restrict this fallback to the
    # endpoint component and the net already declared for the exact endpoint.
    if old_net is not None:
        changed: list[str] = []

        def replace_semantic_fields(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if key.endswith("_net") and isinstance(child, str) and child == old_net:
                        value[key] = replacement
                        changed.append(path)
                    elif key == "signal_nets" and isinstance(child, str):
                        tokens = [part.strip() for part in child.split(",")]
                        if old_net in tokens:
                            value[key] = ",".join(replacement if token == old_net else token for token in tokens)
                            changed.append(path)
                    else:
                        replace_semantic_fields(child, path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    replace_semantic_fields(child, f"{prefix}[{index}]")

        replace_semantic_fields(component)
        if changed:
            return True, f"replaced {old_net!r} with {replacement!r} in {ref} fields: {', '.join(changed)}"
    return False, f"component {ref} has no supported raw pad-to-net mapping"


def _mutate_stackup(case: dict[str, Any], mutation: dict[str, Any]) -> tuple[bool, str]:
    count = mutation.get("copper_layers")
    layers = mutation.get("ground_plane_layers")
    if isinstance(count, bool) or not isinstance(count, int) or not isinstance(layers, list):
        return False, "stackup mutation requires an integer copper layer count and layer list"
    case["copper_layers"] = count
    plane = case.get("ground_plane")
    if not isinstance(plane, dict):
        return False, "case has no ground-plane declaration to mutate"
    plane["layers"] = copy.deepcopy(layers)
    return True, f"set copper_layers={count} and ground_plane.layers"


def _mutate_library(case: dict[str, Any], mutation: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(mutation.get("remove_library_entry"), str):
        return False, "library mutation has no declared removal request"
    expectations = case.get("expectations", {})
    required = expectations.get("required_components", []) if isinstance(expectations, dict) else []
    refs = [str(row.get("ref", "")) for row in required if isinstance(row, dict) and row.get("ref")]
    for ref in refs:
        rows = _component_rows(case.get("spec", {}), ref)
        if len(rows) != 1:
            continue
        spec = case["spec"]
        overrides = spec.setdefault("approved_overrides", [])
        if not isinstance(overrides, list):
            return False, "spec approved_overrides is not a list"
        overrides.append(
            {
                "target": ref,
                "kind": "footprint_override",
                "value": "CircuitWeaver_NegativeControl:DefinitelyMissingFootprint",
            }
        )
        return True, f"added a recognized footprint_override for {ref} to an unresolved qualified footprint"
    return False, "no unique required component could receive a missing-footprint override"


def _mutate_limits(case: dict[str, Any], mutation: dict[str, Any]) -> tuple[bool, str]:
    fields = mutation.get("remove_declared")
    limits = case.get("declared_limits")
    if not isinstance(fields, list) or not isinstance(limits, dict):
        return False, "case has no declared-limits object or removal list"
    declared = limits.get("declared")
    if not isinstance(declared, dict):
        return False, "case declared_limits has no declared object"
    removed = []
    for field in fields:
        if isinstance(field, str) and field in declared:
            del declared[field]
            removed.append(field)
    if not removed:
        return False, "none of the declared fields in this control are present"
    return True, f"removed declared limit fields: {', '.join(removed)}"


def _apply(case: dict[str, Any], control: dict[str, Any]) -> tuple[bool, str]:
    mutation = control.get("mutation")
    if not isinstance(mutation, dict):
        return False, "control has no mutation object"
    kind = control.get("kind")
    if kind == "connectivity":
        return _mutate_connectivity(case, mutation)
    if kind == "stackup":
        return _mutate_stackup(case, mutation)
    if kind == "library":
        return _mutate_library(case, mutation)
    if kind == "limits":
        return _mutate_limits(case, mutation)
    return False, f"unsupported control kind: {kind!r}"


def _checks(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict) or not isinstance(result.get("checks"), list):
        return []
    return [row for row in result["checks"] if isinstance(row, dict)]


def _target_failed(result: Any, check_name: str, target: str | None = None) -> bool:
    for row in _checks(result):
        if row.get("check") != check_name or row.get("status") != "failed":
            continue
        if target is None or str(row.get("target", "")) == target:
            return True
    return False


def _check_names(kind: Any) -> tuple[str, ...]:
    return {
        "connectivity": ("required_endpoint",),
        "stackup": ("stackup",),
        "library": ("footprint_resolution", "library_part", "footprint_asset_resolution"),
        "limits": ("declared_limits", "electrical_limits"),
    }.get(kind, ())


def _relevant_evidence(result: Any, kind: Any, target: str | None = None) -> list[dict[str, Any]]:
    names = set(_check_names(kind))
    return [
        row
        for row in _checks(result)
        if row.get("check") in names and (target is None or str(row.get("target", "")) == target)
    ]


def _detects(control: dict[str, Any], mutated: Any) -> tuple[bool, str]:
    kind = control.get("kind")
    mutation = control.get("mutation", {})
    if kind == "connectivity":
        endpoint = mutation.get("endpoint", {}) if isinstance(mutation, dict) else {}
        target = f"{endpoint.get('ref', '')}.{endpoint.get('pad', '')}"
        return _target_failed(mutated, "required_endpoint", target), f"preflight required_endpoint failure for {target}"
    if kind == "stackup":
        return _target_failed(mutated, "stackup"), "preflight stackup check failed"
    if kind == "library":
        checks = _checks(mutated)
        relevant = [
            row
            for row in checks
            if row.get("check") in {"footprint_resolution", "library_part", "footprint_asset_resolution"}
        ]
        return any(row.get("status") == "failed" for row in relevant), "preflight library-resolution check failed"
    if kind == "limits":
        checks = _checks(mutated)
        relevant = [row for row in checks if row.get("check") in {"declared_limits", "electrical_limits"}]
        return any(
            row.get("status") in {"failed", "unverified"} for row in relevant
        ), "preflight reported declared limits unverified"
    return False, "no detector is defined for this control kind"


def run_negative_controls(case: dict[str, Any], preflight_callable: Preflight) -> list[dict[str, Any]]:
    """Run every declared mutation independently and preserve auditable evidence.

    A control is ``detected`` only when the positive baseline preflight passes,
    the mutation is applied to a copy, and the actual preflight produces the
    corresponding failed check (or an unverified finding for a limit control).
    Exceptions, unsupported mutations, and
    unobservable properties remain ``unverified`` rather than being counted
    as rejection.
    """
    if not isinstance(case, dict):
        raise TypeError("case must be a dictionary")
    if not callable(preflight_callable):
        raise TypeError("preflight_callable must be callable")
    baseline_case = copy.deepcopy(case)
    baseline_snapshot = _snapshot(baseline_case)
    baseline_result: Any = None
    baseline_error = None
    try:
        baseline_result = preflight_callable(baseline_case)
        if not isinstance(baseline_result, dict):
            baseline_error = "preflight returned a non-object result"
    except Exception as exc:
        baseline_error = f"{type(exc).__name__}: {exc}"

    rows = []
    controls = case.get("controls", [])
    for control in controls if isinstance(controls, list) else []:
        if not isinstance(control, dict):
            continue
        mutated_case = copy.deepcopy(case)
        mutated_snapshot_before = _snapshot(mutated_case)
        applied, mutation_reason = _apply(mutated_case, control)
        mutated_snapshot = _snapshot(mutated_case)
        mutated_result: Any = None
        preflight_error = baseline_error
        detected = False
        detection_reason = ""
        if (
            baseline_error is None
            and isinstance(baseline_result, dict)
            and baseline_result.get("status") == "passed"
            and applied
        ):
            try:
                mutated_result = preflight_callable(mutated_case)
                if not isinstance(mutated_result, dict):
                    preflight_error = "mutated preflight returned a non-object result"
                else:
                    detected, detection_reason = _detects(control, mutated_result)
            except Exception as exc:
                preflight_error = f"{type(exc).__name__}: {exc}"
        elif baseline_error is None and isinstance(baseline_result, dict) and baseline_result.get("status") != "passed":
            preflight_error = "positive baseline preflight did not pass"

        if baseline_error is not None or preflight_error is not None or not applied:
            status = "unverified"
            reason = preflight_error or mutation_reason
        elif detected:
            status = "detected"
            reason = detection_reason
        elif control.get("kind") in {"library", "limits"}:
            evidence = _relevant_evidence(mutated_result, control.get("kind"))
            if not evidence or all(row.get("status") == "unverified" for row in evidence):
                status = "unverified"
                reason = "preflight has no local evidence for this property"
            else:
                status = "undetected"
                reason = detection_reason or "mutation was applied but the corresponding preflight check did not fail"
        else:
            status = "undetected"
            reason = detection_reason or "mutation was applied but the corresponding preflight check did not fail"

        endpoint = control.get("mutation", {}).get("endpoint", {}) if isinstance(control.get("mutation"), dict) else {}
        target = f"{endpoint.get('ref', '')}.{endpoint.get('pad', '')}" if endpoint else None
        rows.append(
            {
                "control_id": control.get("control_id"),
                "kind": control.get("kind"),
                "expected_result": control.get("expected_result"),
                "status": status,
                "reason": reason,
                "mutation": copy.deepcopy(control.get("mutation")),
                "mutation_applied": applied,
                "mutation_detail": mutation_reason,
                "expectations_unchanged": mutated_snapshot["expectations_sha256"]
                == baseline_snapshot["expectations_sha256"],
                "baseline_snapshot": baseline_snapshot,
                "mutated_snapshot_before": mutated_snapshot_before,
                "mutated_snapshot": mutated_snapshot,
                "baseline_preflight": {
                    "status": baseline_result.get("status") if isinstance(baseline_result, dict) else None,
                    "checks": _relevant_evidence(baseline_result, control.get("kind"), target),
                },
                "mutated_preflight": {
                    "status": mutated_result.get("status") if isinstance(mutated_result, dict) else None,
                    "checks": _relevant_evidence(mutated_result, control.get("kind"), target),
                    "error": preflight_error,
                },
            }
        )
    return rows
