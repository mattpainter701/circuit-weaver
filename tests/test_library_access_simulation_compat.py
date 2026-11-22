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

    binding = _spice_binding("MODEL-1", model_dir)
    assert binding["file_available"] is True
    assert binding["explicit_pin_contract_available"] is with_contract

    from circuit_weaver import spice_runner

    monkeypatch.setattr(
        spice_runner,
        "run_simulation",
        lambda path, sim_type="tran", **kwargs: SimulationResult(status="skipped", sim_type=sim_type),
    )
    component = ComponentDef(mpn="MODEL-1", source_ref="U1", pin_nets={"1": "VIN", "2": "VOUT"})
    report = run_design_simulations(
        [component],
        plan=SimulationPlan(power_sims=[{"ref": "U1", "analyses": ["tran"]}]),
        output_dir=tmp_path / "sim-output",
        model_dir=model_dir,
    )

    netlist_path = tmp_path / "sim-output" / "U1_tran.cir"
    netlist = netlist_path.read_text(encoding="utf-8")
    assert netlist_path.is_file()
    assert f".include {model_path}" in netlist
    assert "XU1 VIN VOUT MODEL_1" in netlist
    assert report.results[0].status == "skipped"
