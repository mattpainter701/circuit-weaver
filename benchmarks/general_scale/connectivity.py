"""Native PCB connectivity checks for the general-scale benchmark.

The verifier deliberately consumes only KiCad's ``inspect`` result and plain
contract mappings.  It has no dependency on Circuit Weaver's installed
package, so a benchmark can validate the exact runtime selected by its runner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ConnectivityContractError(ValueError):
    """Raised when a channel contract or its native PCB evidence is invalid."""


_REQUIRED = (
    "frontend_ref", "frontend_p_pad", "sensor_p_net", "frontend_n_pad",
    "sensor_n_net", "frontend_ref_pad", "reference_net", "reference_ref",
    "reference_output_pad", "frontend_out_pad", "frontend_out_net",
    "adc_ref", "adc_channel", "adc_pad", "adc_net", "adc_filter_ref",
    "adc_filter_input_pad", "adc_filter_output_pad", "input_connector_ref",
    "input_positive_pad", "input_positive_net", "input_negative_pad",
    "input_negative_net",
)
_OPTIONAL_PAIRS = (
    ("post_stage_ref", "post_stage_input_pad", "post_stage_output_pad", "post_stage_output_net"),
)


def _nonempty_string(contract: Mapping[str, Any], key: str, index: int) -> str:
    value = contract.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConnectivityContractError(f"channel {index}: {key} must be a non-empty string")
    return value.strip()


def _pad_number(contract: Mapping[str, Any], key: str, index: int) -> str:
    value = contract.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ConnectivityContractError(f"channel {index}: {key} must identify one physical pad")
    return str(value).strip()


def _channel_rows(contracts: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(contracts, Mapping):
        contracts = contracts.get("required_channels")
    if not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)) or not contracts:
        raise ConnectivityContractError("required_channels must be a non-empty sequence")
    if any(not isinstance(item, Mapping) for item in contracts):
        raise ConnectivityContractError("each required_channels entry must be a mapping")
    return contracts


def _footprints(inventory: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(inventory, Mapping):
        raise ConnectivityContractError("native inspect inventory must be a mapping")
    if inventory.get("status", "ok") != "ok":
        raise ConnectivityContractError("native inspect inventory status is not ok")
    rows = inventory.get("footprints")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ConnectivityContractError("native inspect inventory has no footprints sequence")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConnectivityContractError("native footprint entry must be a mapping")
        ref = row.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ConnectivityContractError("native footprint has no reference")
        if ref in result:
            raise ConnectivityContractError(f"duplicate native footprint reference {ref}")
        pads = row.get("pads")
        if not isinstance(pads, Sequence) or isinstance(pads, (str, bytes)):
            raise ConnectivityContractError(f"native footprint {ref} has no pads sequence")
        seen: dict[str, list[Any]] = {}
        for pad in pads:
            if not isinstance(pad, Mapping):
                raise ConnectivityContractError(f"native footprint {ref} contains an invalid pad")
            number = pad.get("number")
            if number == "" and _proven_unassigned_non_copper_pad(pad, row):
                continue
            if not isinstance(number, (str, int)) or isinstance(number, bool) or not str(number):
                raise ConnectivityContractError(f"native footprint {ref} has a pad without a number")
            number = str(number)
            seen.setdefault(number, []).append(pad.get("net"))
        for number, nets in seen.items():
            if len(nets) > 1 and (
                any(not isinstance(net, str) or not net for net in nets)
                or len(set(nets)) != 1
            ):
                raise ConnectivityContractError(
                    f"native footprint {ref} has conflicting duplicate pad {number} instances"
                )
        result[ref] = row
    return result


def _proven_unassigned_non_copper_pad(pad: Mapping[str, Any], footprint: Mapping[str, Any]) -> bool:
    """Prove an unnumbered pad is a non-electrical stencil-only aperture."""
    layers = pad.get("layers")
    copper_layers = pad.get("copper_layers")
    return (
        pad.get("net") == ""
        and isinstance(pad.get("attribute"), int)
        and not isinstance(pad.get("attribute"), bool)
        and isinstance(layers, list)
        and bool(layers)
        and all(isinstance(layer, int) and not isinstance(layer, bool) for layer in layers)
        and isinstance(copper_layers, list)
        and not copper_layers
        and footprint.get("native_known_copper_layer_inventory_complete") is True
    )


def _require_pad_net(
    footprints: Mapping[str, Mapping[str, Any]], ref: str, pad_number: str, net: str,
    context: str,
) -> None:
    footprint = footprints.get(ref)
    if footprint is None:
        raise ConnectivityContractError(f"{context}: native footprint {ref} is missing")
    matching = [pad for pad in footprint["pads"] if str(pad["number"]) == pad_number]
    if not matching:
        raise ConnectivityContractError(
            f"{context}: {ref} pad {pad_number} is missing"
        )
    for pad in matching:
        actual = pad.get("net")
        if not isinstance(actual, str) or not actual:
            raise ConnectivityContractError(f"{context}: {ref} pad {pad_number} is unconnected")
        if actual != net:
            raise ConnectivityContractError(
                f"{context}: {ref} pad {pad_number} is on {actual!r}, expected {net!r}"
            )


def verify_native_connectivity(inventory: Mapping[str, Any], contracts: Any) -> dict[str, Any]:
    """Verify declared channel paths against actual native PCB pad/net data.

    ``contracts`` is either the ``required_channels`` list or a case mapping
    containing that key.  Every path proves both external input pins, the
    front-end differential inputs/reference/output, optional post-amplifier,
    ADC series filter, and selected ADC input pad. ADC channel numbers are
    checked for uniqueness; pad identities always come from the contract.
    """
    rows = _channel_rows(contracts)
    native = _footprints(inventory)
    claimed_adc_channels: set[tuple[str, int]] = set()
    claimed_singletons: dict[str, str] = {}
    component_roles: dict[str, str] = {}
    verified = []

    for index, raw in enumerate(rows):
        channel = dict(raw)
        for key in _REQUIRED:
            if key not in channel:
                raise ConnectivityContractError(f"channel {index}: missing required field {key}")
        for group in _OPTIONAL_PAIRS:
            supplied = [key in channel for key in group]
            if any(supplied) and not all(supplied):
                raise ConnectivityContractError(
                    f"channel {index}: optional post-stage fields must be supplied together"
                )

        frontend = _nonempty_string(channel, "frontend_ref", index)
        adc = _nonempty_string(channel, "adc_ref", index)
        connector = _nonempty_string(channel, "input_connector_ref", index)
        p_net = _nonempty_string(channel, "sensor_p_net", index)
        n_net = _nonempty_string(channel, "sensor_n_net", index)
        ref_net = _nonempty_string(channel, "reference_net", index)
        adc_net = _nonempty_string(channel, "adc_net", index)
        p_input_net = _nonempty_string(channel, "input_positive_net", index)
        n_input_net = _nonempty_string(channel, "input_negative_net", index)
        if p_net == n_net or p_input_net == n_input_net:
            raise ConnectivityContractError(f"channel {index}: differential input nets must be distinct")
        adc_channel = channel["adc_channel"]
        if isinstance(adc_channel, bool) or not isinstance(adc_channel, int) or adc_channel < 0:
            raise ConnectivityContractError(f"channel {index}: adc_channel must be a non-negative integer")
        key = (adc, adc_channel)
        if key in claimed_adc_channels:
            raise ConnectivityContractError(f"duplicate ADC channel {adc}[{adc_channel}]")
        claimed_adc_channels.add(key)

        roles = ((frontend, "frontend_ref"), (connector, "input_connector_ref"))
        for ref, role in roles:
            if ref in claimed_singletons:
                raise ConnectivityContractError(
                    f"channel {index}: reference {ref} reused for {role} and {claimed_singletons[ref]}"
                )
            claimed_singletons[ref] = role
        for ref, role in ((frontend, "frontend"), (adc, "adc"), (connector, "input connector")):
            prior = component_roles.setdefault(ref, role)
            if prior != role:
                raise ConnectivityContractError(
                    f"channel {index}: reference {ref} conflicts as {prior} and {role}"
                )
        _require_pad_net(native, connector, _pad_number(channel, "input_positive_pad", index), p_input_net,
                         f"channel {index} positive input connector")
        _require_pad_net(native, connector, _pad_number(channel, "input_negative_pad", index), n_input_net,
                         f"channel {index} negative input connector")
        if p_input_net != p_net or n_input_net != n_net:
            raise ConnectivityContractError(
                f"channel {index}: connector input nets do not match front-end differential nets"
            )
        _require_pad_net(native, frontend, _pad_number(channel, "frontend_p_pad", index), p_net,
                         f"channel {index} positive front-end input")
        _require_pad_net(native, frontend, _pad_number(channel, "frontend_n_pad", index), n_net,
                         f"channel {index} negative front-end input")
        _require_pad_net(native, frontend, _pad_number(channel, "frontend_ref_pad", index), ref_net,
                         f"channel {index} reference")
        reference = _nonempty_string(channel, "reference_ref", index)
        reference_pad = _pad_number(channel, "reference_output_pad", index)
        prior = component_roles.setdefault(reference, "reference source")
        if prior != "reference source":
            raise ConnectivityContractError(
                f"channel {index}: reference {reference} conflicts as {prior} and reference source"
            )
        _require_pad_net(native, reference, reference_pad, ref_net, f"channel {index} reference source")

        frontend_net = _nonempty_string(channel, "frontend_out_net", index)
        _require_pad_net(native, frontend, _pad_number(channel, "frontend_out_pad", index), frontend_net,
                         f"channel {index} front-end output")
        filter_input_net = frontend_net
        if "post_stage_ref" in channel:
            stage = _nonempty_string(channel, "post_stage_ref", index)
            if stage in claimed_singletons:
                raise ConnectivityContractError(f"channel {index}: reused component reference {stage}")
            claimed_singletons[stage] = "post_stage_ref"
            prior = component_roles.setdefault(stage, "post-stage")
            if prior != "post-stage":
                raise ConnectivityContractError(
                    f"channel {index}: reference {stage} conflicts as {prior} and post-stage"
                )
            stage_in = _pad_number(channel, "post_stage_input_pad", index)
            stage_out = _pad_number(channel, "post_stage_output_pad", index)
            _require_pad_net(native, stage, stage_in, frontend_net, f"channel {index} post-stage input")
            filter_input_net = _nonempty_string(channel, "post_stage_output_net", index)
            _require_pad_net(native, stage, stage_out, filter_input_net,
                             f"channel {index} post-stage output")
        filter_ref = _nonempty_string(channel, "adc_filter_ref", index)
        if filter_ref in claimed_singletons:
            raise ConnectivityContractError(f"channel {index}: reused ADC filter reference {filter_ref}")
        claimed_singletons[filter_ref] = "adc_filter_ref"
        prior = component_roles.setdefault(filter_ref, "ADC filter")
        if prior != "ADC filter":
            raise ConnectivityContractError(
                f"channel {index}: reference {filter_ref} conflicts as {prior} and ADC filter"
            )
        _require_pad_net(native, filter_ref, _pad_number(channel, "adc_filter_input_pad", index),
                         filter_input_net, f"channel {index} ADC filter input")
        _require_pad_net(native, filter_ref, _pad_number(channel, "adc_filter_output_pad", index),
                         adc_net, f"channel {index} ADC filter output")
        _require_pad_net(native, adc, _pad_number(channel, "adc_pad", index), adc_net,
                         f"channel {index} ADC input {adc_channel}")
        verified.append({"frontend_ref": frontend, "adc_ref": adc,
                         "adc_channel": adc_channel, "adc_net": adc_net,
                         "input_connector_ref": connector, "reference_net": ref_net})

    return {"status": "pass", "verified_channels": len(verified), "channels": verified}
