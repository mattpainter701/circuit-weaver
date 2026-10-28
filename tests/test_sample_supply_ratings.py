"""Every shipped sample keeps each rated part inside its supply rating.

Two samples once fed 24 V into a regulator rated 20 V absolute maximum. The
supply-rating check now catches that; this test keeps the corpus honest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from circuit_weaver.dispatcher import validate_design

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = sorted(
    path
    for path in (ROOT / "samples").glob("*/*.yaml")
    if path.name != "canonical_spec.yaml" and path.stem == path.parent.name
)


@pytest.mark.parametrize("spec_path", SAMPLES, ids=[path.parent.name for path in SAMPLES])
def test_sample_parts_run_inside_their_supply_ratings(spec_path):
    report = validate_design(
        yaml.safe_load(spec_path.read_text(encoding="utf-8")), check_determinism=False, use_kicad=False
    )
    violations = [
        f"{message.level} {message.code}: {message.message}"
        for message in report.categories["electrical"]
        if message.code in {"power-over-voltage", "power-under-voltage", "power-domain-voltage-conflict"}
    ]
    assert violations == []
    assert report.valid, report.summary


def test_every_sample_is_covered():
    assert len(SAMPLES) == 13
