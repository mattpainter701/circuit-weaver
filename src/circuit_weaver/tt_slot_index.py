"""Read and validate Tiny Tapeout shuttle slot indexes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_PROVIDER_REPO = "https://github.com/TinyTapeout/tinytapeout-sky-26d"
_INDEX_VERSION = 3
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TILES_RE = re.compile(r"^[1-9][0-9]*x[1-9][0-9]*$")
_PROJECT_FIELDS = {"macro", "address", "x", "y", "tiles", "repo", "commit"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_http_url(value: Any, field: str) -> None:
    _require(isinstance(value, str), f"{field} must be a string")
    parsed = urlparse(value)
    _require(parsed.scheme == "https" and bool(parsed.netloc), f"{field} must be an HTTPS URL")


def lookup_tt_slot(index: str | Path, *, shuttle: str, macro: str) -> dict[str, Any]:
    """Look up an exact macro name in a validated SKY 26d shuttle index.

    ``shuttle`` is matched against the index's top-level ``name`` field. It is
    deliberately not matched against the nested ``layout`` object.
    """
    _require(isinstance(shuttle, str) and bool(shuttle.strip()), "shuttle must be a non-empty string")
    _require(isinstance(macro, str) and bool(macro.strip()), "macro must be a non-empty string")
    path = Path(index)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read shuttle index: {exc}") from exc
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shuttle index is not valid UTF-8 JSON") from exc

    _require(isinstance(data, dict), "shuttle index must be a JSON object")
    _require(data.get("name") == shuttle, "shuttle name does not match index")
    _require(data.get("repo") == _PROVIDER_REPO, "index is not the supported Tiny Tapeout SKY 26d provider")
    _require(data.get("version") == _INDEX_VERSION, "unsupported shuttle index version")
    _require(isinstance(data.get("commit"), str) and _COMMIT_RE.fullmatch(data["commit"]) is not None,
             "index commit must be a full lowercase git SHA")
    _require(
        _is_int(data.get("commit_date")) and data["commit_date"] >= 0,
        "commit_date must be a non-negative integer",
    )
    layout = data.get("layout")
    _require(isinstance(layout, dict) and isinstance(layout.get("muxes"), list), "layout.muxes must be an array")
    _require(all(isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in layout["muxes"]),
             "layout.muxes must contain arrays of strings")
    projects = data.get("projects")
    _require(isinstance(projects, list), "projects must be an array")

    seen_macros: set[str] = set()
    seen_addresses: set[int] = set()
    found: dict[str, Any] | None = None
    for entry in projects:
        _require(isinstance(entry, dict), "each project must be an object")
        _require(_PROJECT_FIELDS <= entry.keys(), "project is missing required fields")
        _require(set(entry) <= _PROJECT_FIELDS | {"analog_pins"}, "project has unsupported fields")
        name = entry["macro"]
        address, x, y = entry["address"], entry["x"], entry["y"]
        _require(isinstance(name, str) and bool(name.strip()), "project macro must be a non-empty string")
        _require(_is_int(address) and address >= 0, "project address must be a non-negative integer")
        _require(_is_int(x) and x >= 0 and _is_int(y) and y >= 0, "project coordinates must be non-negative integers")
        _require(isinstance(entry["tiles"], str) and _TILES_RE.fullmatch(entry["tiles"]) is not None,
                 "project tiles must use NxM dimensions")
        _validate_http_url(entry["repo"], "project repo")
        _require(isinstance(entry["commit"], str) and _COMMIT_RE.fullmatch(entry["commit"]) is not None,
                 "project commit must be a full lowercase git SHA")
        if "analog_pins" in entry:
            pins = entry["analog_pins"]
            _require(
                isinstance(pins, list) and all(_is_int(pin) and 0 <= pin <= 11 for pin in pins),
                "analog_pins must be an array of pin numbers from 0 through 11",
            )
            _require(len(pins) == len(set(pins)), "analog_pins must not contain duplicates")
        _require(name not in seen_macros, "duplicate project macro in shuttle index")
        _require(address not in seen_addresses, "duplicate project address in shuttle index")
        seen_macros.add(name)
        seen_addresses.add(address)
        if name == macro:
            found = entry

    result: dict[str, Any] = {
        "status": "assigned" if found is not None else "unassigned",
        "shuttle": shuttle,
        "macro": macro,
        "index_sha256": hashlib.sha256(raw).hexdigest(),
        "provider": {"name": data["name"], "repo": data["repo"], "commit": data["commit"], "version": data["version"]},
        "assignment": None,
    }
    if found is not None:
        result["assignment"] = {
            "address": found["address"], "x": found["x"], "y": found["y"], "tiles": found["tiles"],
            "project_commit": found["commit"], "repo": found["repo"],
        }
    return result
