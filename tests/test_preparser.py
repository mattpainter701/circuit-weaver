"""Format detection for supported engine-only YAML sections."""

from __future__ import annotations

import pytest

from circuit_weaver.preparser import extract_engine_spec


def test_extract_engine_spec_accepts_passives_only_design() -> None:
    spec = {
        "project": "Explicit_RC_Testbench",
        "passives": [{"ref": "R1", "value": "1k"}],
        "simulation": {"analyses": [{"kind": "ac"}]},
    }

    assert extract_engine_spec(spec) is spec


def test_extract_engine_spec_recognizes_each_supported_block_section() -> None:
    sections = (
        "power",
        "digital",
        "storage",
        "audio",
        "interface",
        "analog",
        "connectors",
        "sensors",
        "drivers",
        "misc",
        "protection",
        "passives",
        "blocks",
    )

    for section in sections:
        spec = {"project": "section-test", section: []}
        assert extract_engine_spec(spec) is spec, section


def test_extract_engine_spec_still_rejects_unknown_sections() -> None:
    with pytest.raises(ValueError, match="Unknown spec format"):
        extract_engine_spec({"project": "unknown", "simulation": {"analyses": []}})
