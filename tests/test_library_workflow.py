"""Focused tests for the persistent local-library workflow benchmark."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.diverse_systems.library_workflow import run_library_workflow
from circuit_weaver.library_bundle import apply_library_bundle


def _write_symbol_library(path: Path) -> None:
    path.write_text(
        '''(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")
  (symbol "FixtureR"
    (pin_names (offset 0.254))
    (pin_numbers (hide yes))
    (property "Reference" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Value" "FixtureR" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "FixtureR_0_1"
      (rectangle (start -2.54 1.27) (end 2.54 -1.27)
        (stroke (width 0) (type default)) (fill (type background)))
    )
    (symbol "FixtureR_1_1"
      (pin passive line (at -5.08 0 0) (length 2.54)
        (name "A" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 5.08 0 180) (length 2.54)
        (name "B" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
''',
        encoding="utf-8",
    )


def _write_footprint(path: Path) -> None:
    path.write_text(
        '''(footprint "FixtureR" (version 20240108) (generator "pcbnew")
  (layer "F.Cu") (attr smd)
  (fp_rect (start -1.5 -0.75) (end 1.5 0.75) (stroke (width 0.05) (type solid))
    (fill none) (layer "F.CrtYd"))
  (pad "1" smd rect (at -0.75 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 0.75 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))
)
''',
        encoding="utf-8",
    )


def test_workflow_persists_replayable_sources_and_resolves_real_library_assets(tmp_path: Path) -> None:
    symbol_source = tmp_path / "Fixture.kicad_sym"
    footprint_source = tmp_path / "Fixture.pretty" / "FixtureR.kicad_mod"
    footprint_source.parent.mkdir()
    _write_symbol_library(symbol_source)
    _write_footprint(footprint_source)
    output = tmp_path / "project"

    report = run_library_workflow(
        symbol_source=symbol_source,
        symbol_name="FixtureR",
        symbol_provenance="self-authored test fixture",
        symbol_license="CC0 test assertion",
        footprint_source=footprint_source,
        footprint_name="FixtureR",
        footprint_provenance="self-authored test fixture",
        footprint_license="CC0 test assertion",
        output=output,
    )

    assert report["status"] == "resolved_local_assets"
    assert report["native_validation"]["status"] == "not_run"
    assert Path(report["assets"]["symbol"]["destination"]).is_file()
    assert Path(report["assets"]["footprint"]["destination"]).is_dir()
    assert report["assets"]["footprint"]["geometry"]["source"] == "courtyard"
    assert (output / "selected-sources" / "symbols" / "Fixture.kicad_sym").is_file()
    assert (output / "selected-sources" / "footprints" / "Fixture.pretty" / "FixtureR.kicad_mod").is_file()

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    persisted_plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert all(Path(asset["source"]).is_relative_to(output) for asset in manifest["assets"])
    replay = apply_library_bundle(output, persisted_plan)
    assert replay["status"] == "already_applied"
