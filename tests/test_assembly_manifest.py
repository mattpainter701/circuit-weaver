from __future__ import annotations

import pytest

from circuit_weaver.assembly_manifest import AssemblyManifest, AssemblyManifestError, build_assembly_manifest
from circuit_weaver.component_db import BypassCap, ComponentDef, PinDef, StrapConfig


def _pins(count: int) -> list[PinDef]:
    return [PinDef(str(index + 1), f"P{index + 1}", "bidirectional", "L") for index in range(count)]


def test_manifest_is_exhaustive_stable_and_reserves_explicit_refs() -> None:
    controller = ComponentDef(
        mpn="CTRL",
        source_ref="U1",
        ref_prefix="U",
        value="CTRL",
        footprint="Package_QFP:LQFP-32",
        pins=_pins(8),
        power_pins={"1": "VDD", "2": "GND"},
        straps=[StrapConfig("3", "BOOT", "GND", "10k", "Resistor_SMD:R_0402")],
        functional_section="digital",
        block_id="controller",
    )
    reserved_cap = ComponentDef(
        mpn="1uF",
        source_ref="C7",
        ref_prefix="C",
        value="1uF",
        footprint="Capacitor_SMD:C_0603",
    )

    first = build_assembly_manifest([controller, reserved_cap])
    second = build_assembly_manifest([controller, reserved_cap])

    assert [item.reference for item in first.items] == [item.reference for item in second.items]
    assert {item.reference for item in first.items} == {"U1", "C7", "C8", "R1"}
    assert len(first.items) == 4
    support = [item for item in first.items if item.source_kind != "component"]
    assert {item.source_kind for item in support} == {"bypass", "strap"}
    assert all(item.owner_ref == "U1" for item in support)
    assert all(item.functional_section == "digital" for item in support)
    assert all(item.block_id == "controller" for item in support)


def test_manifest_assigns_reference_to_primary_without_source_ref() -> None:
    manifest = build_assembly_manifest([ComponentDef(mpn="NOREF", ref_prefix="U")])
    assert [item.reference for item in manifest.items] == ["U1"]


def test_support_pin_intent_survives_manifest_round_trip_and_legacy_payloads():
    owner = ComponentDef(mpn="MCU", source_ref="U2", pins=_pins(9),
                         power_pins={"9": "VDD", "8": "GND"},
                         bypass_caps=[BypassCap("9", "VDD", "GND", "10nF", "Capacitor_SMD:C_0402")])
    manifest = build_assembly_manifest([owner], include_auto_bypass=False)
    cap = next(item for item in manifest.items if item.source_kind == "bypass")
    assert cap.owner_pin == "9"
    restored = AssemblyManifest.from_dict(manifest.to_dict())
    assert next(item for item in restored.items if item.reference == cap.reference).owner_pin == "9"
    legacy = manifest.to_dict()
    for item in legacy["items"]:
        item.pop("owner_pin")
    assert all(item.owner_pin == "" for item in AssemblyManifest.from_dict(legacy).items)


def test_reordered_same_block_parts_keep_reference_bound_to_physical_identity() -> None:
    def resistor(value: str) -> ComponentDef:
        return ComponentDef(
            mpn=value,
            value=value,
            ref_prefix="R",
            footprint="Resistor_SMD:R_0402_1005Metric",
            block_id="feedback-divider",
            functional_section="power",
        )

    first = build_assembly_manifest(
        [resistor("1k"), resistor("10k")],
        include_auto_bypass=False,
    )
    reordered = build_assembly_manifest(
        [resistor("10k"), resistor("1k")],
        include_auto_bypass=False,
        previous_manifest=first,
    )

    assert {item.value: item.reference for item in first.items} == {
        "1k": "R1",
        "10k": "R2",
    }
    assert {item.value: item.reference for item in reordered.items} == {
        "10k": "R2",
        "1k": "R1",
    }


def test_manifest_rejects_duplicate_explicit_references() -> None:
    components = [
        ComponentDef(mpn="A", source_ref="U1"),
        ComponentDef(mpn="B", source_ref="U1"),
    ]
    with pytest.raises(AssemblyManifestError, match="Duplicate explicit"):
        build_assembly_manifest(components)


def test_manifest_includes_declared_bypass_and_strap_values() -> None:
    component = ComponentDef(
        mpn="SENSOR",
        source_ref="U2",
        bypass_caps=[BypassCap("1", "VDD", "GND", "100nF", "Capacitor_SMD:C_0402")],
        straps=[StrapConfig("2", "ADDR", "GND", "4.7k", "Resistor_SMD:R_0402")],
    )
    manifest = build_assembly_manifest([component], include_auto_bypass=False)
    assert [(item.source_kind, item.value) for item in manifest.items] == [
        ("component", "SENSOR"),
        ("bypass", "100nF"),
        ("strap", "4.7k"),
    ]


def test_virtual_topology_container_is_rendered_but_only_its_real_straps_are_physical() -> None:
    topology = ComponentDef(
        mpn="PULLUPS_ONLY",
        source_ref="RP1",
        ref_prefix="RP",
        value="I2C_PULLUPS",
        footprint="",
        pins=_pins(4),
        pin_nets={"3": "SENSOR_SDA", "4": "SENSOR_SCL"},
        power_pins={"1": "VDD_IO", "2": "GND"},
        straps=[
            StrapConfig("R_SDA", "SENSOR_SDA", "VDD_IO", "4.7k", "Resistor_SMD:R_0402_1005Metric"),
            StrapConfig("R_SCL", "SENSOR_SCL", "VDD_IO", "4.7k", "Resistor_SMD:R_0402_1005Metric"),
        ],
    )
    topology.is_virtual_container = True

    manifest = build_assembly_manifest([topology], include_auto_bypass=False)

    assert [item.reference for item in manifest.items] == ["R1", "R2"]
    assert all(item.source_kind == "strap" for item in manifest.items)
    assert all(item.owner_ref == "RP1" for item in manifest.items)
    assert [(item.net1, item.net2, item.footprint) for item in manifest.items] == [
        ("SENSOR_SDA", "VDD_IO", "Resistor_SMD:R_0402_1005Metric"),
        ("SENSOR_SCL", "VDD_IO", "Resistor_SMD:R_0402_1005Metric"),
    ]
    assert manifest.prepared_components == []
    assert len(manifest.render_components) == 1
    assert [strap.source_ref for strap in manifest.render_components[0].straps] == ["R1", "R2"]


def test_virtual_container_exception_does_not_hide_missing_physical_footprints() -> None:
    physical = ComponentDef(mpn="SENSOR", source_ref="U1", ref_prefix="U", footprint="")
    manifest = build_assembly_manifest([physical], include_auto_bypass=False)
    assert manifest.missing_footprint_refs() == ["U1"]


def test_prepared_components_carry_manifest_support_references() -> None:
    component = ComponentDef(
        mpn="SENSOR",
        source_ref="U2",
        bypass_caps=[BypassCap("1", "VDD", "GND", "100nF", "Capacitor_SMD:C_0402")],
        straps=[StrapConfig("2", "ADDR", "GND", "4.7k", "Resistor_SMD:R_0402")],
    )

    manifest = build_assembly_manifest([component], include_auto_bypass=False)
    prepared = manifest.prepared_components[0]
    support_refs = [item.reference for item in manifest.items if item.source_kind != "component"]
    prepared_refs = [
        *(str(getattr(item, "source_ref", "")) for item in prepared.bypass_caps),
        *(str(getattr(item, "source_ref", "")) for item in prepared.straps),
    ]

    assert prepared.source_ref == manifest.items[0].reference
    assert prepared_refs == support_refs


def _revision_owner(ref: str, values: list[str]) -> ComponentDef:
    return ComponentDef(
        mpn=ref,
        source_ref=ref,
        bypass_caps=[
            BypassCap("1", f"VDD_{ref}", "GND", value, "Capacitor_SMD:C_0402")
            for value in values
        ],
    )


def test_support_references_remain_semantically_stable_across_revisions() -> None:
    before = build_assembly_manifest(
        [_revision_owner("U1", ["1nF"]), _revision_owner("U2", ["2nF"])],
        include_auto_bypass=False,
    )
    after = build_assembly_manifest(
        [_revision_owner("U1", ["1nF", "10nF"]), _revision_owner("U2", ["2nF"])],
        include_auto_bypass=False,
        previous_manifest=before,
    )

    support = {(item.owner_ref, item.value): item.reference for item in after.items if item.source_kind == "bypass"}
    assert support[("U1", "1nF")] == "C1"
    assert support[("U2", "2nF")] == "C2"
    assert support[("U1", "10nF")] == "C3"


def test_retired_support_reference_is_never_reused_for_different_part() -> None:
    first = build_assembly_manifest(
        [_revision_owner("U1", ["1nF"]), _revision_owner("U2", ["2nF"])],
        include_auto_bypass=False,
    )
    removed = build_assembly_manifest(
        [_revision_owner("U1", []), _revision_owner("U2", ["2nF"])],
        include_auto_bypass=False,
        previous_manifest=first,
    )
    replaced = build_assembly_manifest(
        [_revision_owner("U1", ["47nF"]), _revision_owner("U2", ["2nF"])],
        include_auto_bypass=False,
        previous_manifest=removed,
    )

    new_item = next(item for item in replaced.items if item.value == "47nF")
    assert "C1" in replaced.retired_references
    assert new_item.reference == "C3"


def test_manifest_propagates_only_caller_supplied_evidence_ids() -> None:
    component = ComponentDef(mpn="SENSOR", source_ref="U1", ref_prefix="U", footprint="Package:QFN")

    manifest = build_assembly_manifest(
        [component],
        include_auto_bypass=False,
        evidence_ids_by_reference={"U1": ["EV-datasheet-0123456789ab"]},
        evidence_ids=["EV-tool_result-fedcba987654"],
        evidence_manifest="evidence_manifest.json",
    )

    payload = manifest.to_dict()
    assert payload["evidence_ids"] == ["EV-tool_result-fedcba987654"]
    assert payload["evidence_manifest"] == "evidence_manifest.json"
    assert payload["items"][0]["evidence_ids"] == ["EV-datasheet-0123456789ab"]


def test_manifest_rejects_absolute_evidence_manifest_reference() -> None:
    component = ComponentDef(mpn="SENSOR", source_ref="U1", ref_prefix="U", footprint="Package:QFN")

    with pytest.raises(ValueError, match="output-relative"):
        build_assembly_manifest([component], include_auto_bypass=False, evidence_manifest="C:\\private.json")
