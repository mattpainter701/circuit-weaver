"""DDR inference boundaries and PCB rule-rendering accounting."""

from __future__ import annotations

import json

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.pcb_constraints import ConstraintCompilation, render_kicad_dru, render_kicad_dru_report
from circuit_weaver.pcb_contracts import PcbConstraint
from circuit_weaver.si_constraints import analyze_si_constraints


def _memory(ref: str, value: str, nets: dict[str, str]) -> ComponentDef:
    return ComponentDef(mpn=value, source_ref=ref, description=value, pin_nets=nets)


def test_explicit_ddr3_memory_does_not_receive_guessed_hard_constraints() -> None:
    comp = _memory("U2", "K4B4G1646Q DDR3-1600", {
        "1": "/S0SDQ0", "2": "/S0SDQ1", "3": "/S0SDQS0_P", "4": "/S0SDQS0_N",
        "5": "/S0SDQM0", "6": "/S0SCK_P", "7": "/S0SCK_N", "8": "/S0SCAS",
    })
    result = analyze_si_constraints([comp])
    assert result["buses_detected"][0]["bus_type"] == "ddr3"
    assert result["buses_detected"][0]["generation_status"] == "explicit"
    assert result["impedance_constraints"] == []
    assert result["diff_pairs"] == []
    assert result["length_groups"] == []
    assert result["unverified_constraints"][0]["generated_hard_constraints"] == []
    assert any("authoritative device/topology evidence" in item for item in result["warnings"])


def test_generic_dq_labels_do_not_choose_a_ddr_generation() -> None:
    result = analyze_si_constraints([_memory("U?", "memory device", {"1": "DQ0", "2": "DQ1"})])
    assert result["buses_detected"][0]["bus_type"] == "ddr_unknown"
    assert result["buses_detected"][0]["generation_status"] == "unknown"
    assert result["impedance_constraints"] == []
    assert result["length_groups"] == []
    assert result["unverified_constraints"][0]["status"] == "unknown"


def test_conflicting_explicit_device_descriptions_are_not_silently_resolved() -> None:
    result = analyze_si_constraints([_memory("U?", "DDR3 DDR4 memory", {"1": "DQ0"})])
    assert result["buses_detected"][0]["bus_type"] == "ddr_conflict"
    assert result["buses_detected"][0]["generation_status"] == "conflict"
    assert result["impedance_constraints"] == []


def test_ddr3_description_without_labeled_nets_remains_an_unverified_requirement() -> None:
    result = analyze_si_constraints([_memory("U3", "DDR3 memory", {})])
    assert result["buses_detected"] == [{
        "bus_type": "ddr3",
        "component": "U3",
        "net_count": 0,
        "description": "DDR3 memory interface",
        "generation_status": "explicit",
        "constraint_status": "unverified_device_specific_requirements",
    }]
    assert result["unverified_constraints"][0]["nets"] == []
    assert result["impedance_constraints"] == []


def test_conflicting_ddr_description_without_nets_is_not_dropped() -> None:
    result = analyze_si_constraints([_memory("U4", "DDR3 DDR4 memory", {})])
    assert result["buses_detected"][0]["bus_type"] == "ddr_conflict"
    assert result["buses_detected"][0]["net_count"] == 0
    assert result["unverified_constraints"][0]["status"] == "conflict"


def test_explicit_device_and_net_generation_conflict_is_not_silently_resolved() -> None:
    result = analyze_si_constraints([_memory("U?", "DDR3 memory", {"1": "DDR4_DQ0", "2": "DDR4_DQ1"})])
    assert result["buses_detected"][0]["bus_type"] == "ddr_conflict"
    assert result["buses_detected"][0]["generation_status"] == "conflict"
    assert result["impedance_constraints"] == []
    assert result["length_groups"] == []
    assert any("evidence conflicts" in item for item in result["warnings"])


def test_ddr4_device_generation_is_known_but_numeric_limits_remain_unverified() -> None:
    result = analyze_si_constraints([_memory("U?", "LPDDR4 memory", {"1": "DQ0", "2": "DQ1"})])
    assert result["buses_detected"][0]["bus_type"] == "ddr4"
    assert result["impedance_constraints"] == []
    assert result["length_groups"] == []


def test_usb_and_can_inference_remain_unchanged() -> None:
    usb = ComponentDef(mpn="USB_CONN", source_ref="J1", description="USB Connector",
                       pin_nets={"1": "DP", "2": "DM"})
    can = ComponentDef(mpn="CAN_TRANSCEIVER", source_ref="U2", description="CAN transceiver",
                       pin_nets={"1": "CANH", "2": "CANL"})
    usb_result, can_result = analyze_si_constraints([usb]), analyze_si_constraints([can])
    assert [(x["bus_type"], x["component"], x["net_count"]) for x in usb_result["buses_detected"]] == [
        ("usb2", "J1", 2)
    ]
    assert [(x["bus_type"], x["target_ohms"]) for x in usb_result["impedance_constraints"]] == [("usb2", 90)]
    assert [(x["bus_type"], x["component"]) for x in can_result["buses_detected"]] == [("can", "U2")]
    assert [(x["bus_type"], x["target_ohms"]) for x in can_result["impedance_constraints"]] == [("can", 120)]


def test_dru_report_partitions_requested_ids_and_only_claims_emitted_rules() -> None:
    evidence = ["EV-DATASHEET-abcdef012345"]
    items = (
        PcbConstraint.create(klass="width", target="net:CLK",
                             params={"minimum": {"value": 0.2, "unit": "mm"}},
                             origin="user", evidence_ids=evidence),
        PcbConstraint.create(klass="impedance", target="net:CLK",
                             params={"target": {"value": 50, "unit": "ohm"}},
                             origin="user", evidence_ids=evidence),
        PcbConstraint.create(klass="diff_pair", target="net:CLK_P",
                             params={"negative_net": "CLK_N", "protocol": "DDR3"},
                             origin="user", evidence_ids=evidence),
        PcbConstraint.create(klass="length", target="net_class:DDR",
                             params={"match_tolerance": {"value": 0.1, "unit": "mm"}},
                             origin="user", evidence_ids=evidence),
    )
    report = render_kicad_dru_report(ConstraintCompilation(items))
    assert report["status"] == "partial"
    assert report["requested_ids"] == [item.id for item in items]
    assert report["emitted_ids"] == [items[0].id]
    assert set(report["emitted_ids"]).isdisjoint(report["unemitted_ids"])
    assert set(report["requested_ids"]) == set(report["emitted_ids"]) | set(report["unemitted_ids"])
    assert {item["id"]: item["reason"] for item in report["unemitted"]} == {
        items[1].id: "constraint_class_impedance_not_supported_by_kicad_dru_renderer",
        items[2].id: "constraint_class_diff_pair_not_supported_by_kicad_dru_renderer",
        items[3].id: "length_match_tolerance_not_supported_by_kicad_dru_renderer",
    }
    assert f'# circuit-weaver-rendered-constraints ["{items[0].id}"]' in report["text"]
    assert render_kicad_dru(ConstraintCompilation(items)) == report["text"]
    requested_line = next(
        line for line in report["text"].splitlines() if line.startswith("# circuit-weaver-constraints ")
    )
    assert json.loads(requested_line.removeprefix("# circuit-weaver-constraints ")) == report["requested_ids"]
    assert "requested intent only; not every item is enforced" in report["text"]
    rules = [line for line in report["text"].splitlines() if line.startswith("(rule ")]
    assert all(item.id not in "\n".join(rules) for item in items[1:])


def test_length_maximum_does_not_hide_unrendered_matching_tolerance() -> None:
    item = PcbConstraint.create(
        klass="length",
        target="net_class:DDR",
        params={
            "maximum": {"value": 15, "unit": "mm"},
            "match_tolerance": {"value": 0.1, "unit": "mm"},
        },
        origin="user",
        evidence_ids=["EV-DATASHEET-abcdef012345"],
    )
    report = render_kicad_dru_report(ConstraintCompilation((item,)))
    assert report["emitted_ids"] == []
    assert report["unemitted_ids"] == [item.id]
    assert report["unemitted"][0]["reason"] == "length_match_tolerance_not_supported_by_kicad_dru_renderer"
    assert f'(rule "{item.id}"' not in report["text"]
