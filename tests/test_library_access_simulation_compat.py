from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.library_access import _spice_binding
from circuit_weaver.simulation import SimulationPlan, run_design_simulations
from circuit_weaver.spice_fetcher import resolve_spice_models
from circuit_weaver.spice_runner import SimulationResult


@pytest.mark.parametrize("with_contract", [False, True], ids=["legacy-path-only", "explicit-contract"])
def test_audit_contract_keeps_simulator_model_manifest_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_contract: bool
) -> None:
    model_dir = tmp_path / "spice_models"
    model_dir.mkdir()
    model_path = model_dir / "model.lib"
    model_path.write_text(".subckt MODEL_1 vin vout\n.ends MODEL_1\n", encoding="utf-8")
    entry = {"spice_status": "ok", "spice_file": "model.lib"}
    if with_contract:
        entry.update(subckt="MODEL_1", pin_order=["1", "2"])
    manifest_path = model_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"MODEL-1": entry}), encoding="utf-8")

    sim_manifest = resolve_spice_models(manifest_path)
    assert sim_manifest == {"MODEL-1": str(model_path)}
    assert all(isinstance(path, str) for path in sim_manifest.values())
    assert resolve_spice_models(manifest_path, include_contracts=True)["MODEL-1"] == (
        {"path": str(model_path), "subckt": "MODEL_1", "pin_order": ["1", "2"]}
        if with_contract
        else str(model_path)
    )

    binding = _spice_binding("MODEL-1", model_dir)
    assert binding["file_available"] is True
    assert binding["explicit_pin_contract_available"] is with_contract

    from circuit_weaver import spice_runner

    monkeypatch.setattr(
        spice_runner,
        "run_simulation",
        lambda path, sim_type="tran", **kwargs: SimulationResult(status="skipped", sim_type=sim_type),
    )
    component = ComponentDef(
        mpn="MODEL-1",
        source_ref="U1",
        pin_nets={"1": "VIN", "2": "VOUT"} if with_contract else {"1": "VOUT", "2": "VIN"},
    )
    report = run_design_simulations(
        [component],
        plan=SimulationPlan(power_sims=[{"ref": "U1", "analyses": ["tran"]}]),
        output_dir=tmp_path / "sim-output",
        model_dir=model_dir,
    )

    netlist_path = tmp_path / "sim-output" / "U1_tran.cir"
    netlist = netlist_path.read_text(encoding="utf-8")
    assert netlist_path.is_file()
    include_lines = [line for line in netlist.splitlines() if line.strip().lower().startswith(".include ")]
    assert len(include_lines) == 1
    include_value = include_lines[0].split(None, 1)[1].strip()
    assert include_value[1:-1] == str(model_path) if include_value.startswith('"') else include_value == str(model_path)
    assert "XU1 VIN VOUT MODEL_1" in netlist
    assert report.results[0].status == "skipped"


def test_legacy_path_only_contract_is_not_guessed_when_ports_do_not_match_nets(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "ambiguous_models"
    model_dir.mkdir()
    model_path = model_dir / "model.lib"
    model_path.write_text(".subckt MODEL_1 pin_a pin_b\n.ends MODEL_1\n", encoding="utf-8")
    (model_dir / "manifest.json").write_text(
        json.dumps({"MODEL-1": {"spice_status": "ok", "spice_file": "model.lib"}}), encoding="utf-8"
    )
    from circuit_weaver import spice_runner

    monkeypatch.setattr(
        spice_runner,
        "run_simulation",
        lambda path, sim_type="tran", **kwargs: SimulationResult(status="skipped", sim_type=sim_type),
    )
    component = ComponentDef(mpn="MODEL-1", source_ref="U1", pin_nets={"1": "VIN", "2": "VOUT"})
    run_design_simulations(
        [component],
        plan=SimulationPlan(power_sims=[{"ref": "U1", "analyses": ["tran"]}]),
        output_dir=tmp_path / "ambiguous-output",
        model_dir=model_dir,
    )
    netlist = (tmp_path / "ambiguous-output" / "U1_tran.cir").read_text(encoding="utf-8")
    assert ".include" not in netlist
    assert "XU1" not in netlist
    assert "legacy model ports do not map uniquely" in netlist


def test_malformed_explicit_contract_does_not_fall_back_to_legacy_inference(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "malformed_contract"
    model_dir.mkdir()
    model_path = model_dir / "model.lib"
    model_path.write_text(".subckt MODEL_1 VIN VOUT\n.ends MODEL_1\n", encoding="utf-8")
    (model_dir / "manifest.json").write_text(
        json.dumps({
            "MODEL-1": {
                "spice_status": "ok", "spice_file": "model.lib", "subckt": "MODEL_1", "pin_order": ["1", "1"]
            }
        }),
        encoding="utf-8",
    )
    from circuit_weaver import spice_runner

    monkeypatch.setattr(
        spice_runner,
        "run_simulation",
        lambda path, sim_type="tran", **kwargs: SimulationResult(status="skipped", sim_type=sim_type),
    )
    component = ComponentDef(mpn="MODEL-1", source_ref="U1", pin_nets={"1": "VIN", "2": "VOUT"})
    run_design_simulations(
        [component],
        plan=SimulationPlan(power_sims=[{"ref": "U1", "analyses": ["tran"]}]),
        output_dir=tmp_path / "malformed-output",
        model_dir=model_dir,
    )
    netlist = (tmp_path / "malformed-output" / "U1_tran.cir").read_text(encoding="utf-8")
    assert ".include" not in netlist
    assert "XU1" not in netlist
    assert "invalid explicit pin contract" in netlist
