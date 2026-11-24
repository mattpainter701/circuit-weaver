import json

import pytest

from circuit_weaver.tt_slot_index import lookup_tt_slot


def _index() -> dict:
    return {
        "name": "Tiny Tapeout SKY 26d",
        "repo": "https://github.com/TinyTapeout/tinytapeout-sky-26d",
        "commit": "a" * 40,
        "commit_date": 1795554687,
        "layout": {"muxes": [["digital", "analog"]]},
        "version": 3,
        "projects": [
            {
                "macro": "tt_um_sample",
                "address": 12,
                "x": 3,
                "y": 4,
                "tiles": "2x1",
                "repo": "https://github.com/example/sample",
                "commit": "b" * 40,
                "analog_pins": [2, 3],
            }
        ],
    }


def _write(tmp_path, data: dict) -> str:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_returns_exact_assignment_and_provenance(tmp_path):
    result = lookup_tt_slot(_write(tmp_path, _index()), shuttle="Tiny Tapeout SKY 26d", macro="tt_um_sample")
    assert result["status"] == "assigned"
    assert result["assignment"] == {
        "address": 12,
        "x": 3,
        "y": 4,
        "tiles": "2x1",
        "project_commit": "b" * 40,
        "repo": "https://github.com/example/sample",
    }
    assert result["provider"]["commit"] == "a" * 40
    assert len(result["index_sha256"]) == 64
    json.dumps(result)


def test_unknown_macro_is_unassigned(tmp_path):
    result = lookup_tt_slot(_write(tmp_path, _index()), shuttle="Tiny Tapeout SKY 26d", macro="tt_um_other")
    assert result["status"] == "unassigned"
    assert result["assignment"] is None


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d.update(name="tiny tapeout sky 26d"), "shuttle name"),
        (lambda d: d.update(repo="https://example.com/fake"), "provider"),
        (lambda d: d.update(version=4), "version"),
        (lambda d: d["projects"].append(dict(d["projects"][0], address=13)), "duplicate project macro"),
        (lambda d: d["projects"].append(dict(d["projects"][0], macro="tt_um_other")), "duplicate project address"),
        (lambda d: d["projects"][0].update(address=True), "address"),
        (lambda d: d["projects"][0].update(commit="bad"), "project commit"),
    ],
)
def test_rejects_invalid_or_ambiguous_indexes(tmp_path, mutate, error):
    data = _index()
    mutate(data)
    with pytest.raises(ValueError, match=error):
        lookup_tt_slot(_write(tmp_path, data), shuttle="Tiny Tapeout SKY 26d", macro="tt_um_sample")


def test_does_not_accept_layout_object_as_shuttle(tmp_path):
    with pytest.raises(ValueError, match="shuttle name"):
        lookup_tt_slot(_write(tmp_path, _index()), shuttle="layout", macro="tt_um_sample")
