"""Tests for safely resolving SPICE model manifest entries."""

import json

from circuit_weaver.component_db import ComponentDef
from circuit_weaver.simulation import SimulationPlan, run_design_simulations
from circuit_weaver.spice_fetcher import resolve_spice_models
from circuit_weaver.spice_runner import SimulationResult


def _write_manifest(model_dir, entry):
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = model_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"MODEL-1": entry}), encoding="utf-8")
    return manifest_path


def test_resolves_explicit_subcircuit_contract(tmp_path):
    model_dir = tmp_path / "spice_models"
    model_dir.mkdir()
    model_file = model_dir / "model.lib"
    model_file.write_text(".subckt regulator vin vout params: gain=1\nR1 vin vout 1k\n.ends\n")
    manifest = _write_manifest(
        model_dir,
        {"spice_status": "ok", "spice_file": "model.lib", "subckt": "regulator", "pin_order": ["1", "2"]},
    )

    result = resolve_spice_models(manifest)

    assert result == {"MODEL-1": {"path": str(model_file.resolve()), "subckt": "regulator", "pin_order": ["1", "2"]}}


def test_legacy_path_only_download_remains_path_only(tmp_path):
    model_dir = tmp_path / "spice_models"
    model_dir.mkdir()
    model_file = model_dir / "legacy.lib"
    model_file.write_text(".subckt model a b\n.ends\n")
    manifest = _write_manifest(model_dir, {"spice_status": "ok", "spice_file": "legacy.lib"})

    result = resolve_spice_models(manifest)

    assert result == {"MODEL-1": str(model_file.resolve())}


def test_omits_missing_or_escaping_model_paths(tmp_path):
    model_dir = tmp_path / "spice_models"
    outside = tmp_path / "outside.lib"
    outside.write_text(".subckt model a b\n.ends\n")
    manifest = _write_manifest(
        model_dir,
        {"spice_status": "ok", "spice_file": "../outside.lib", "subckt": "model", "pin_order": ["1", "2"]},
    )
    result = resolve_spice_models(manifest)
    assert result == {}

    manifest = _write_manifest(
        model_dir,
        {"spice_status": "ok", "spice_file": "missing.lib", "subckt": "model", "pin_order": ["1", "2"]},
    )
    assert resolve_spice_models(manifest) == {}


def test_contract_with_wrong_subcircuit_pin_count_stays_unsupported(tmp_path):
    model_dir = tmp_path / "spice_models"
    model_dir.mkdir()
    model_file = model_dir / "model.lib"
    model_file.write_text(".subckt regulator vin vout en\n.ends regulator\n")
    manifest = _write_manifest(
        model_dir,
        {"spice_status": "ok", "spice_file": "model.lib", "subckt": "regulator", "pin_order": ["1", "2"]},
    )

    assert resolve_spice_models(manifest) == {"MODEL-1": str(model_file.resolve())}


def test_high_level_simulation_passes_resolved_contract_to_exporter(tmp_path, monkeypatch):
    model_dir = tmp_path / "spice_models"
    model_dir.mkdir()
    model_file = model_dir / "model.lib"
    model_file.write_text(".subckt regulator vin vout\nR1 vin vout 1k\n.ends regulator\n")
    _write_manifest(
        model_dir,
        {"spice_status": "ok", "spice_file": "model.lib", "subckt": "regulator", "pin_order": ["1", "2"]},
    )
    component = ComponentDef(mpn="MODEL-1", source_ref="U1", category="buck", pin_nets={"1": "VIN", "2": "VOUT"})
    plan = SimulationPlan(power_sims=[{"ref": "U1", "analyses": ["op"], "target_metrics": {}}])
    monkeypatch.setattr(
        "circuit_weaver.spice_runner.run_simulation",
        lambda *args, **kwargs: SimulationResult(status="failed", sim_type="op"),
    )

    run_design_simulations([component], plan, output_dir=tmp_path / "simulations", model_dir=model_dir)

    netlist = (tmp_path / "simulations" / "U1_op.cir").read_text()
    assert f'.include "{model_file.resolve()}"' in netlist
    assert "XU1 VIN VOUT regulator" in netlist
    assert "Skipped U1" not in netlist
