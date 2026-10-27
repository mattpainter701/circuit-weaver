import hashlib
from pathlib import Path

import pytest

from circuit_weaver.drc_runner import DrcResult
from circuit_weaver.routing_recovery import recover_routing
from circuit_weaver.validator import ValidationIssue


def _drc(path: Path, opens: int, *, status="ok", other_blocker=False, unknown=False, raw_override=None):
    data = path.read_bytes()
    findings = [ValidationIssue("drc", rule_id="CW-DRC-002", severity="blocker") for _ in range(opens)]
    if unknown:
        findings[0] = ValidationIssue("drc", rule_id="CW-DRC-999", severity="blocker")
    if other_blocker:
        findings.append(ValidationIssue("drc", rule_id="CW-DRC-001", severity="blocker"))
    raw = {"violations": [], "unconnected_items": [{} for _ in range(opens)], "schematic_parity": []}
    if raw_override is not None:
        raw = raw_override
    return DrcResult(
        status=status,
        board=str(path),
        board_sha256=hashlib.sha256(data).hexdigest(),
        findings=tuple(findings),
        raw_report=raw,
    )


def _wire_files(tmp_path, monkeypatch):
    source = tmp_path / "input.kicad_pcb"
    source.write_bytes(b"partial board")
    destination = tmp_path / "recovered.kicad_pcb"
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery._prepare_settings",
        lambda src, staged, **_: {src: hashlib.sha256(src.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr("circuit_weaver.routing_recovery._restore_settings", lambda *_: None)
    return source, destination


def _worker_report():
    return {
        "status": "candidate_only_requires_full_drc",
        "source_identity_preserved": True,
        "original_footprints_and_text_preserved": True,
        "original_board_geometry_preserved": True,
        "existing_copper_preserved": True,
        "net": "N1",
        "from_uuid": "pad-a",
        "to_uuid": "pad-b",
        "added_tracks": 1,
        "added_vias": 0,
        "expanded_states": 12,
    }


def test_recovery_tries_next_pair_after_nonprogress_then_resets_pair_index(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    counts = iter((2, 2, 1, 0, 0))

    def drc(path, **_):
        return _drc(Path(path), next(counts))

    pair_indices = []

    def worker(operation, **kwargs):
        assert operation == "repair_open"
        assert kwargs["drc_report"] is not None
        pair_indices.append(kwargs["pair_index"])
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b" routed")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    evidence = tmp_path / "evidence"
    result = recover_routing(source, destination, evidence_dir=evidence, max_pair_attempts=2)

    assert result["status"] == "ok"
    assert result["initial_open_count"] == 2
    assert result["remaining_open_count"] == 0
    assert result["attempts"] == 3
    assert result["links_repaired"] == 2
    assert pair_indices == [0, 1, 0]
    assert destination.is_file()
    assert len(list(evidence.glob("routing-recovery-*/partial-progress.kicad_pcb"))) == 1
    run_evidence = next(evidence.glob("routing-recovery-*"))
    assert (run_evidence / "history.json").is_file()
    assert (run_evidence / "attempt-0003" / "attempt.json").is_file()
    assert result["fabrication_ready"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"opens": 2, "status": "skipped"},
        {"opens": 2, "other_blocker": True},
        {"opens": 2, "unknown": True},
        {
            "opens": 2,
            "raw_override": {
                "violations": [],
                "unconnected_items": [{}, {}],
                "schematic_parity": [],
                "truncated": True,
            },
        },
    ],
)
def test_recovery_rejects_invalid_or_ambiguous_initial_drc(tmp_path, monkeypatch, bad):
    source, destination = _wire_files(tmp_path, monkeypatch)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), **bad))
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery.run_kicad", lambda *_a, **_k: pytest.fail("invalid DRC reached the worker")
    )
    result = recover_routing(source, destination)
    assert result["status"] == "error"
    assert not destination.exists()


def test_recovery_keeps_existing_output_when_candidate_drc_regresses(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    destination.write_bytes(b"prior published board")
    calls = 0

    def drc(path, **_):
        nonlocal calls
        calls += 1
        return _drc(Path(path), 2 if calls == 1 else 1, other_blocker=calls == 2)

    def worker(_operation, **kwargs):
        Path(kwargs["output"]).write_bytes(b"bad candidate")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, overwrite=True)
    assert result["status"] == "partial"
    assert destination.read_bytes() == b"prior published board"


def test_recovery_rejects_unknown_worker_status_and_preserves_source(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    source_bytes = source.read_bytes()
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", lambda path, **_: _drc(Path(path), 1))
    monkeypatch.setattr(
        "circuit_weaver.routing_recovery.run_kicad",
        lambda *_a, **_k: {"status": "mystery", "route_repair": _worker_report()},
    )
    result = recover_routing(source, destination)
    assert result["status"] == "partial"
    assert source.read_bytes() == source_bytes
    assert not destination.exists()


def test_recovery_stages_and_restores_project_settings_and_local_library_context(tmp_path, monkeypatch):
    project = tmp_path / "project"
    library = project / "libraries" / "Local.pretty"
    library.mkdir(parents=True)
    (library / "Part.kicad_mod").write_text("footprint", encoding="utf-8")
    (project / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/libraries/Local.pretty")))\n',
        encoding="utf-8",
    )
    source = project / "input.kicad_pcb"
    source.write_bytes(b"partial board")
    source.with_suffix(".kicad_pro").write_bytes(b"authored project")
    source.with_suffix(".kicad_dru").write_bytes(b"authored rules")
    destination = project / "recovered.kicad_pcb"
    counts = iter((1, 0, 0))

    def drc(path, **_):
        board = Path(path)
        assert board.with_suffix(".kicad_pro").read_bytes() == b"authored project"
        assert board.with_suffix(".kicad_dru").read_bytes() == b"authored rules"
        assert (board.parent / "fp-lib-table").is_file()
        assert (board.parent / "libraries" / "Local.pretty" / "Part.kicad_mod").is_file()
        return _drc(board, next(counts))

    def worker(_operation, **kwargs):
        candidate = Path(kwargs["output"])
        assert candidate.with_suffix(".kicad_pro").read_bytes() == b"authored project"
        assert candidate.with_suffix(".kicad_dru").read_bytes() == b"authored rules"
        assert (candidate.parent / "fp-lib-table").is_file()
        Path(kwargs["output"]).write_bytes(b"candidate")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination)
    assert result["status"] == "ok"
    assert destination.with_suffix(".kicad_pro").read_bytes() == b"authored project"
    assert destination.with_suffix(".kicad_dru").read_bytes() == b"authored rules"


def test_recovery_accepts_more_than_twelve_links_with_configured_link_budget(tmp_path, monkeypatch):
    source, destination = _wire_files(tmp_path, monkeypatch)
    counts = iter([15, *range(14, 1, -1)])

    def drc(path, **_):
        return _drc(Path(path), next(counts))

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_drc", drc)
    calls = 0

    def worker(_operation, **kwargs):
        nonlocal calls
        calls += 1
        Path(kwargs["output"]).write_bytes(Path(kwargs["board"]).read_bytes() + b"x")
        return {"status": "ok", "route_repair": _worker_report()}

    monkeypatch.setattr("circuit_weaver.routing_recovery.run_kicad", worker)
    result = recover_routing(source, destination, max_links=13, max_pair_attempts=1, evidence_dir=tmp_path / "evidence")
    assert result["status"] == "partial"
    assert calls == 13
    assert result["links_repaired"] == 13
    run_evidence = next((tmp_path / "evidence").glob("routing-recovery-*"))
    assert (run_evidence / "partial-progress.kicad_pcb").is_file()
    assert len(list(run_evidence.glob("attempt-*/attempt.json"))) == 13
    assert not destination.exists()
