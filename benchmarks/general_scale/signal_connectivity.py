"""Strict native PCB signal-path contracts for general-scale cases.

Contracts are checked against KiCad's ``inspect`` footprint/pad/net inventory.
They describe expected physical connectivity; they do not prove component
function or electrical correctness.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

if __package__:
    from .connectivity import ConnectivityContractError, _footprints, _require_pad_net
else:  # runner loads this helper directly from its directory for installed-package mode
    from connectivity import ConnectivityContractError, _footprints, _require_pad_net


def _rows(contracts: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(contracts, Mapping):
        contracts = contracts.get("required_paths")
    if not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)) or not contracts:
        raise ConnectivityContractError("required_paths must be a non-empty sequence")
    if any(not isinstance(item, Mapping) for item in contracts):
        raise ConnectivityContractError("each required_paths entry must be a mapping")
    return contracts


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectivityContractError(f"{context} must be a non-empty string")
    return value.strip()


def _pad(value: Any, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ConnectivityContractError(f"{context} must identify one physical pad")
    return str(value).strip()


def _endpoint(value: Any, context: str) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ConnectivityContractError(f"{context} must be a reference/pad mapping")
    return _text(value.get("ref"), f"{context}.ref"), _pad(value.get("pad"), f"{context}.pad")


def _is_power_net(net: str) -> bool:
    upper = net.strip().upper()
    roots = ("GND", "AGND", "DGND", "PGND", "VSS", "VDD", "VCC", "VBUS", "VIN", "VBAT", "VSYS")
    return any(upper == root or upper.startswith(root + "_") for root in roots) or upper in {
        "3V3",
        "5V",
        "+3V3",
        "+5V",
        "12V",
        "+12V",
        "-12V",
    }


def verify_native_signal_paths(inventory: Mapping[str, Any], contracts: Any) -> dict[str, Any]:
    """Require controller/source-to-load paths to match native PCB pad nets.

    Each path has a ``source``, one or more ``loads``, and ordered ``segments``.
    A segment lists a net and all required physical ``{ref, pad}`` endpoints.
    Multi-net paths also provide one ``through`` component per boundary, with
    input/output pads joining the neighboring segments. A through declaration
    validates physical net assignment only; it makes no claim about the
    component's internal behavior.
    """
    rows = _rows(contracts)
    native = _footprints(inventory)
    seen_ids: set[str] = set()
    verified: list[dict[str, Any]] = []

    for index, raw in enumerate(rows):
        context = f"path {index}"
        path_id = _text(raw.get("id"), f"{context}.id")
        if path_id in seen_ids:
            raise ConnectivityContractError(f"duplicate required path id {path_id}")
        seen_ids.add(path_id)

        source = _endpoint(raw.get("source"), f"{context}.source")
        raw_loads = raw.get("loads")
        if not isinstance(raw_loads, Sequence) or isinstance(raw_loads, (str, bytes)) or not raw_loads:
            raise ConnectivityContractError(f"{context}.loads must be a non-empty sequence")
        loads = [_endpoint(item, f"{context}.loads[{i}]") for i, item in enumerate(raw_loads)]
        if len(set(loads)) != len(loads) or source in loads:
            raise ConnectivityContractError(f"{context} source and loads must identify distinct physical pads")

        raw_segments = raw.get("segments")
        if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)) or not raw_segments:
            raise ConnectivityContractError(f"{context}.segments must be a non-empty sequence")
        segment_rows: list[tuple[str, set[tuple[str, str]]]] = []
        for segment_index, segment in enumerate(raw_segments):
            segment_context = f"{context}.segments[{segment_index}]"
            if not isinstance(segment, Mapping):
                raise ConnectivityContractError(f"{segment_context} must be a mapping")
            net = _text(segment.get("net"), f"{segment_context}.net")
            if _is_power_net(net):
                raise ConnectivityContractError(f"{segment_context} cannot use power-only net {net!r}")
            raw_endpoints = segment.get("endpoints")
            if not isinstance(raw_endpoints, Sequence) or isinstance(raw_endpoints, (str, bytes)):
                raise ConnectivityContractError(f"{segment_context}.endpoints must be a sequence")
            endpoints = [_endpoint(item, f"{segment_context}.endpoints[{i}]") for i, item in enumerate(raw_endpoints)]
            endpoint_refs = [ref for ref, _ in endpoints]
            if len(endpoints) < 2 or len(set(endpoint_refs)) != len(endpoint_refs):
                raise ConnectivityContractError(
                    f"{segment_context} needs at least two endpoints on different references"
                )
            if len(set(endpoints)) != len(endpoints):
                raise ConnectivityContractError(f"{segment_context} contains a duplicate endpoint")
            for ref, pad_number in endpoints:
                _require_pad_net(native, ref, pad_number, net, segment_context)
            segment_rows.append((net, set(endpoints)))

        if source not in segment_rows[0][1]:
            raise ConnectivityContractError(f"{context}.source is not an endpoint of the first segment")
        missing_loads = [endpoint for endpoint in loads if endpoint not in segment_rows[-1][1]]
        if missing_loads:
            raise ConnectivityContractError(f"{context} loads are not endpoints of the final segment: {missing_loads}")

        raw_through = raw.get("through", [])
        if not isinstance(raw_through, Sequence) or isinstance(raw_through, (str, bytes)):
            raise ConnectivityContractError(f"{context}.through must be a sequence")
        if len(raw_through) != len(segment_rows) - 1:
            raise ConnectivityContractError(f"{context}.through must contain one bridge per adjacent segment pair")
        through_refs: set[str] = set()
        for boundary, bridge in enumerate(raw_through):
            bridge_context = f"{context}.through[{boundary}]"
            if not isinstance(bridge, Mapping):
                raise ConnectivityContractError(f"{bridge_context} must be a mapping")
            ref = _text(bridge.get("ref"), f"{bridge_context}.ref")
            input_pad = _pad(bridge.get("input_pad"), f"{bridge_context}.input_pad")
            output_pad = _pad(bridge.get("output_pad"), f"{bridge_context}.output_pad")
            if input_pad == output_pad or ref in through_refs:
                raise ConnectivityContractError(f"{bridge_context} must identify distinct pads and bridge refs")
            through_refs.add(ref)
            before_net, before_endpoints = segment_rows[boundary]
            after_net, after_endpoints = segment_rows[boundary + 1]
            input_endpoint, output_endpoint = (ref, input_pad), (ref, output_pad)
            if input_endpoint not in before_endpoints or output_endpoint not in after_endpoints:
                raise ConnectivityContractError(
                    f"{bridge_context} pads must join the preceding and following segment endpoints"
                )
            if before_net == after_net:
                raise ConnectivityContractError(f"{bridge_context} must bridge two distinct signal nets")
            _require_pad_net(native, ref, input_pad, before_net, bridge_context)
            _require_pad_net(native, ref, output_pad, after_net, bridge_context)

        raw_returns = raw.get("return_assertions", [])
        if not isinstance(raw_returns, Sequence) or isinstance(raw_returns, (str, bytes)):
            raise ConnectivityContractError(f"{context}.return_assertions must be a sequence")
        signal_refs = {ref for _, endpoints in segment_rows for ref, _ in endpoints}
        signal_pads = {endpoint for _, endpoints in segment_rows for endpoint in endpoints}
        checked_returns = []
        for return_index, assertion in enumerate(raw_returns):
            return_context = f"{context}.return_assertions[{return_index}]"
            if not isinstance(assertion, Mapping):
                raise ConnectivityContractError(f"{return_context} must be a mapping")
            return_net = _text(assertion.get("net"), f"{return_context}.net")
            if not _is_power_net(return_net):
                raise ConnectivityContractError(f"{return_context}.net must be a power or return net")
            raw_endpoints = assertion.get("endpoints")
            if not isinstance(raw_endpoints, Sequence) or isinstance(raw_endpoints, (str, bytes)):
                raise ConnectivityContractError(f"{return_context}.endpoints must be a sequence")
            endpoints = [_endpoint(item, f"{return_context}.endpoints[{i}]") for i, item in enumerate(raw_endpoints)]
            if len(endpoints) < 2 or len(set(endpoints)) != len(endpoints):
                raise ConnectivityContractError(f"{return_context} needs at least two distinct pad endpoints")
            if len({ref for ref, _ in endpoints}) < 2 or any(ref not in signal_refs for ref, _ in endpoints):
                raise ConnectivityContractError(
                    f"{return_context} endpoints must use at least two references already in the signal path"
                )
            if any(endpoint in signal_pads for endpoint in endpoints):
                raise ConnectivityContractError(f"{return_context} must use pads distinct from signal-path pads")
            for ref, pad_number in endpoints:
                _require_pad_net(native, ref, pad_number, return_net, return_context)
            checked_returns.append(
                {
                    "net": return_net,
                    "endpoints": [{"ref": ref, "pad": pad} for ref, pad in endpoints],
                }
            )

        verified.append(
            {
                "id": path_id,
                "source": {"ref": source[0], "pad": source[1]},
                "loads": [{"ref": ref, "pad": number} for ref, number in loads],
                "nets": [net for net, _ in segment_rows],
                "endpoint_count": sum(len(endpoints) for _, endpoints in segment_rows),
                "return_assertions": checked_returns,
            }
        )

    return {
        "status": "pass",
        "verified_paths": len(verified),
        "verified_return_assertions": sum(len(path["return_assertions"]) for path in verified),
        "paths": verified,
    }
