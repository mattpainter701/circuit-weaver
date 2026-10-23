"""Contract tests for the checked-in capability registry."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from circuit_weaver.capabilities import (
    CAPABILITIES,
    NOT_APPLICABLE,
    get_capability,
    get_capability_registry,
    validate_capability_registry,
    validate_runtime_verification_claim,
)


def _dispatcher_top_level_parser_names() -> set[str]:
    """Read top-level parser declarations without importing the CLI or parsing argv."""

    dispatcher = Path(__file__).parents[1] / "src" / "circuit_weaver" / "dispatcher.py"
    tree = ast.parse(dispatcher.read_text(encoding="utf-8"))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subparsers"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_every_dispatcher_parser_command_is_registered():
    registered_commands = {record["surfaces"]["cli"] for record in CAPABILITIES}
    assert _dispatcher_top_level_parser_names() <= registered_commands


def test_capability_registry_is_valid():
    validate_capability_registry(CAPABILITIES)


def test_library_access_advertises_read_only_public_surfaces():
    record = get_capability("library-access")
    assert record["surfaces"]["python"] == "circuit_weaver.library_access:audit_library_access"
    assert record["surfaces"]["mcp"] == "library_access"
    assert record["verification_prereq"] == NOT_APPLICABLE
    assert record["output_guarantee"] == NOT_APPLICABLE


def test_public_registry_accessors_are_json_safe_copies():
    registry = get_capability_registry()
    registry[0]["surfaces"]["cli"] = "mutated"

    assert get_capability_registry()[0]["surfaces"]["cli"] != "mutated"
    assert get_capability("validate")["id"] == "validate"


@pytest.mark.parametrize("invalid_maturity", ["stable", "alpha", "", "SUPPORTED"])
def test_registry_rejects_invalid_maturity_state(invalid_maturity):
    records = deepcopy(CAPABILITIES)
    records[0]["maturity"] = invalid_maturity
    with pytest.raises(ValueError, match="invalid maturity"):
        validate_capability_registry(records)


def test_registry_allows_erc_to_advance_its_kicad_load_prerequisite():
    records = deepcopy(CAPABILITIES)
    records[0]["verification_prereq"] = "static-parse"
    records[0]["output_guarantee"] = "drc"
    validate_capability_registry(records)


def test_runtime_claim_rejects_stronger_claim_without_returned_evidence():
    with pytest.raises(ValueError, match="exceeds returned"):
        validate_runtime_verification_claim("validate", "drc")


def test_runtime_claim_allows_stronger_claim_with_matching_evidence():
    validate_runtime_verification_claim("generate", "erc", evidence_levels=("erc",))


def test_runtime_claim_rejects_evidence_not_declared_by_capability():
    with pytest.raises(ValueError, match="not declared"):
        validate_runtime_verification_claim("validate", "erc", evidence_levels=("erc",))


def test_runtime_claim_preserves_not_applicable_truthfulness():
    validate_runtime_verification_claim("doctor", NOT_APPLICABLE)
    with pytest.raises(ValueError, match="not_applicable cannot mix"):
        validate_runtime_verification_claim("doctor", "static-parse")


@pytest.mark.parametrize(
    ("prerequisite", "guarantee"),
    [(NOT_APPLICABLE, "static-parse"), ("static-parse", NOT_APPLICABLE)],
)
def test_registry_rejects_mixed_not_applicable_verification(prerequisite, guarantee):
    records = deepcopy(CAPABILITIES)
    records[0]["verification_prereq"] = prerequisite
    records[0]["output_guarantee"] = guarantee
    with pytest.raises(ValueError, match="not_applicable must pair"):
        validate_capability_registry(records)
