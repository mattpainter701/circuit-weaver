"""Single-source capability and maturity declarations.

This module deliberately records what public surfaces *can claim*, rather than
what an individual invocation might happen to accomplish.  In particular, a
successful command does not imply ERC, DRC, or fabrication readiness unless
that verification is explicitly represented in its returned evidence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Final, Iterable, TypedDict

MATURITY_STATES: Final = (
    "supported",
    "beta",
    "experimental",
    "review_only",
    "deprecated",
)
"""The frozen public maturity vocabulary."""

VERIFICATION_LADDER: Final = (
    "static-parse",
    "kicad-load",
    "erc",
    "drc",
    "fabrication-ready",
)
"""Ordered evidence levels; later values are strictly stronger."""

NOT_APPLICABLE: Final = "not_applicable"
"""Verification state for capabilities that do not verify design artifacts.

This is intentionally not a rung in :data:`VERIFICATION_LADDER`: operations
such as environment inspection, cache maintenance, and project-log viewing do
not make a design-artifact claim at all.  A non-design prerequisite and
guarantee must therefore both be ``not_applicable``; mixing it with an ordered
level would falsely imply a comparison that does not exist.
"""

EVIDENCE_KINDS: Final = (
    "command-contract",
    "static-parse",
    "kicad-load",
    "erc",
    "drc",
    "fabrication-ready",
    "external-tool",
    "user-supplied",
)
"""The frozen vocabulary for evidence a capability may name."""


class CapabilitySurfaces(TypedDict):
    """Entrypoints for a capability; ``None`` means that surface is absent."""

    cli: str | None
    python: str | None
    http: str | None
    mcp: str | None
    skill: str | None


class CapabilityRecord(TypedDict):
    """The frozen record shape consumed by all future capability surfaces."""

    id: str
    surfaces: CapabilitySurfaces
    maturity: str
    verification_prereq: str
    output_guarantee: str
    evidence_kinds: list[str]
    since_version: str


def _record(
    capability_id: str,
    cli: str,
    *,
    maturity: str = "beta",
    python: str | None = None,
    http: str | None = None,
    mcp: str | None = None,
    skill: str | None = None,
    evidence_kinds: tuple[str, ...] = ("command-contract", "static-parse"),
    verification_prereq: str = "static-parse",
    output_guarantee: str = "static-parse",
    since_version: str = "0.32.1",
) -> CapabilityRecord:
    """Build a conservative v0.32.1 registry record.

    Python entrypoints are listed only when a stable public function is known;
    the CLI dispatcher itself is intentionally not advertised as an API.  The
    guarantee intentionally remains static parsing until a later task wires
    runtime verification evidence through each public response. Non-design
    operations opt into ``not_applicable`` for both verification fields.
    """

    return {
        "id": capability_id,
        "surfaces": {
            "cli": cli,
            "python": python,
            "http": http,
            "mcp": mcp,
            "skill": skill,
        },
        "maturity": maturity,
        "verification_prereq": verification_prereq,
        "output_guarantee": output_guarantee,
        "evidence_kinds": list(evidence_kinds),
        "since_version": since_version,
    }


# Keep this checked-in inventory explicit.  Do not infer capabilities from a
# help screen: that would hide an unreviewed command until documentation is
# regenerated.  ``cache stats`` and ``cache clear`` are child subcommands;
# ``cache`` remains registered because it is the dispatcher command.
_NON_DESIGN: Final = {
    "evidence_kinds": ("command-contract",),
    "verification_prereq": NOT_APPLICABLE,
    "output_guarantee": NOT_APPLICABLE,
}

CAPABILITIES: Final[tuple[CapabilityRecord, ...]] = (
    _record(
        "validate",
        "validate",
        python="circuit_weaver.dispatcher:validate_design",
        http="POST /validate; POST /mvp/validate",
        mcp="validate_design",
        skill="circuit-weaver",
    ),
    _record(
        "apply-patch",
        "apply-patch",
        python="circuit_weaver.dispatcher:apply_design_patch",
        http="POST /mvp/apply-patch",
    ),
    _record(
        "generate",
        "generate",
        maturity="experimental",
        python="circuit_weaver.dispatcher:generate_artifacts",
        http="POST /generate; POST /mvp/generate",
        mcp="generate_artifacts",
        skill="circuit-weaver",
        evidence_kinds=("command-contract", "static-parse", "kicad-load", "erc"),
    ),
    _record("review-report", "review-report", maturity="experimental", skill="circuit-weaver"),
    _record("diff", "diff", python="circuit_weaver.dispatcher:diff_designs", http="POST /mvp/diff"),
    _record(
        "ingest-pcb-feedback",
        "ingest-pcb-feedback",
        python="circuit_weaver.dispatcher:ingest_pcb_feedback",
        http="POST /mvp/pcb-feedback",
    ),
    _record("import-placement", "import-placement", maturity="review_only", skill="circuit-weaver"),
    _record("list-templates", "list-templates", http="GET /templates", **_NON_DESIGN),
    _record("scaffold", "scaffold", skill="circuit-weaver"),
    _record("register-ic", "register-ic", maturity="experimental"),
    _record("resolve-symbol", "resolve-symbol", maturity="experimental", since_version="0.33.0", **_NON_DESIGN),
    _record("export-jlcpcb", "export-jlcpcb", maturity="review_only", skill="circuit-weaver"),
    _record("export-gerbers", "export-gerbers", maturity="review_only", skill="circuit-weaver"),
    _record("cost-bom", "cost-bom", maturity="experimental", skill="circuit-weaver"),
    _record(
        "import-design",
        "import-design",
        maturity="experimental",
        python="circuit_weaver.design_import:import_design",
        skill="circuit-weaver",
    ),
    _record(
        "analyze-design",
        "analyze-design",
        maturity="experimental",
        python="circuit_weaver.design_import:analyze_design",
        skill="circuit-weaver",
    ),
    _record(
        "status",
        "status",
        python="circuit_weaver.project_state:get_project_state_summary",
        skill="circuit-weaver",
        **_NON_DESIGN,
    ),
    _record(
        "resume",
        "resume",
        maturity="experimental",
        python="circuit_weaver.project_state:resume_project",
        skill="circuit-weaver",
        **_NON_DESIGN,
    ),
    _record("design-wizard", "design-wizard", maturity="experimental", skill="design-wizard"),
    _record(
        "build-pcb", "build-pcb", maturity="experimental", skill="circuit-weaver",
        python="circuit_weaver.schematic_pcb:schematic_to_pcb",
        evidence_kinds=("command-contract", "static-parse", "kicad-load", "external-tool"),
        output_guarantee="kicad-load", since_version="0.35.0",
    ),
    _record("place-pcb", "place-pcb", maturity="experimental", skill="circuit-weaver",
            evidence_kinds=("command-contract", "external-tool", "user-supplied")),
    _record(
        "preview-pcb", "preview-pcb", maturity="review_only", skill="circuit-weaver",
        python="circuit_weaver.pcb_preview:render_board_preview",
        evidence_kinds=("command-contract", "static-parse", "kicad-load", "external-tool"),
        output_guarantee="kicad-load", since_version="0.35.0",
    ),
    _record("log-status", "log-status", **_NON_DESIGN),
    _record("log-view", "log-view", **_NON_DESIGN),
    _record(
        "autoroute",
        "autoroute",
        maturity="review_only",
        skill="circuit-weaver",
        evidence_kinds=("command-contract", "external-tool", "user-supplied"),
    ),
    _record(
        "install-skills",
        "install-skills",
        maturity="experimental",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record(
        "schema",
        "schema",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record(
        "harvest-specs", "harvest-specs", maturity="experimental", evidence_kinds=("command-contract", "external-tool")
    ),
    _record(
        "extract-specs", "extract-specs", maturity="experimental", evidence_kinds=("command-contract", "user-supplied")
    ),
    _record(
        "fetch-spice", "fetch-spice", maturity="experimental", evidence_kinds=("command-contract", "external-tool")
    ),
    _record(
        "cache",
        "cache",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record(
        "cache-stats",
        "cache stats",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record(
        "cache-clear",
        "cache clear",
        maturity="review_only",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record("optimize-placement", "optimize-placement", maturity="review_only", skill="circuit-weaver"),
    _record("placement-viewer", "placement-viewer", maturity="review_only", skill="circuit-weaver"),
    _record("si-constraints", "si-constraints", maturity="experimental"),
    _record("thermal-analysis", "thermal-analysis", maturity="experimental"),
    _record("export-dual-cpl", "export-dual-cpl", maturity="review_only"),
    _record("panelize", "panelize", maturity="experimental"),
    _record("design-enclosure", "design-enclosure", maturity="experimental"),
    _record("check-dfm", "check-dfm", maturity="review_only"),
    _record("generate-docs", "generate-docs", maturity="experimental"),
    _record(
        "erc",
        "erc",
        maturity="review_only",
        evidence_kinds=("command-contract", "kicad-load", "erc"),
        verification_prereq="kicad-load",
        output_guarantee="erc",
    ),
    _record(
        "doctor",
        "doctor",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record(
        "repair",
        "repair",
        maturity="experimental",
        python="circuit_weaver.repair_service:apply_no_connect",
        evidence_kinds=("command-contract", "static-parse"),
        since_version="0.35.0",
    ),
    _record("confidence", "confidence", maturity="review_only", skill="circuit-weaver"),
    _record(
        "manufacturing-readiness",
        "manufacturing-readiness",
        python="circuit_weaver.manufacturing_readiness:read_manufacturing_readiness",
        http="POST /manufacturing-readiness",
        mcp="manufacturing_readiness",
        skill="circuit-weaver",
        evidence_kinds=("command-contract", "fabrication-ready"),
        since_version="0.34.0",
    ),
    _record("simulate", "simulate", maturity="experimental", skill="circuit-weaver"),
    _record(
        "discover",
        "discover",
        mcp="discover_projects",
        skill="circuit-weaver",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
    _record("save-research", "save-research", maturity="experimental", skill="circuit-weaver"),
    _record(
        "log-event",
        "log-event",
        evidence_kinds=("command-contract",),
        verification_prereq=NOT_APPLICABLE,
        output_guarantee=NOT_APPLICABLE,
    ),
)


def get_capability_registry() -> list[CapabilityRecord]:
    """Return a JSON-safe copy of the public capability registry."""

    return deepcopy(list(CAPABILITIES))


def get_capability(capability_id: str) -> CapabilityRecord:
    """Return one JSON-safe registry record or raise ``KeyError`` if unknown."""

    for record in CAPABILITIES:
        if record["id"] == capability_id:
            return deepcopy(record)
    raise KeyError(f"unknown capability: {capability_id!r}")


def validate_runtime_verification_claim(
    capability_id: str,
    claim: str,
    *,
    evidence_levels: Iterable[str] = (),
) -> None:
    """Reject runtime claims that exceed baseline or returned evidence.

    The registered output guarantee is the no-evidence baseline. A response may
    claim more only when it returns an explicit ordered evidence level strong
    enough to support that claim. Non-design operations remain outside the
    ordered ladder and can only claim ``not_applicable``.
    """

    record = get_capability(capability_id)
    guarantee = record["output_guarantee"]
    evidence = tuple(evidence_levels)
    if guarantee == NOT_APPLICABLE:
        if claim != NOT_APPLICABLE or evidence:
            raise ValueError(f"{capability_id}: not_applicable cannot mix with ordered evidence")
        return
    if claim == NOT_APPLICABLE or claim not in VERIFICATION_LADDER:
        raise ValueError(f"{capability_id}: invalid runtime verification claim {claim!r}")
    if any(level == NOT_APPLICABLE or level not in VERIFICATION_LADDER for level in evidence):
        raise ValueError(f"{capability_id}: invalid runtime verification evidence")
    if any(level not in record["evidence_kinds"] for level in evidence):
        raise ValueError(f"{capability_id}: runtime evidence is not declared by this capability")
    strongest = guarantee if not evidence else max(evidence, key=VERIFICATION_LADDER.index)
    if VERIFICATION_LADDER.index(claim) > VERIFICATION_LADDER.index(strongest):
        raise ValueError(f"{capability_id}: runtime claim exceeds returned verification evidence")


def capability_maturity_summary() -> dict[str, int]:
    """Return stable maturity counts for compact diagnostic surfaces."""

    return {state: sum(record["maturity"] == state for record in CAPABILITIES) for state in MATURITY_STATES}


def validate_capability_registry(records: tuple[CapabilityRecord, ...] | list[CapabilityRecord]) -> None:
    """Raise ``ValueError`` when registry data violates the frozen contract."""

    expected_keys = {
        "id",
        "surfaces",
        "maturity",
        "verification_prereq",
        "output_guarantee",
        "evidence_kinds",
        "since_version",
    }
    expected_surface_keys = {"cli", "python", "http", "mcp", "skill"}
    ids: set[str] = set()
    cli_paths: set[str] = set()
    levels = {level: index for index, level in enumerate(VERIFICATION_LADDER)}

    for record in records:
        if set(record) != expected_keys:
            raise ValueError(f"{record.get('id', '<unknown>')}: record shape is not frozen")
        if not record["id"] or record["id"] in ids:
            raise ValueError(f"duplicate or empty capability id: {record['id']!r}")
        ids.add(record["id"])
        if set(record["surfaces"]) != expected_surface_keys:
            raise ValueError(f"{record['id']}: surface shape is not frozen")
        cli = record["surfaces"]["cli"]
        if not cli or cli in cli_paths:
            raise ValueError(f"{record['id']}: CLI entrypoint must be unique and non-empty")
        cli_paths.add(cli)
        if record["maturity"] not in MATURITY_STATES:
            raise ValueError(f"{record['id']}: invalid maturity {record['maturity']!r}")
        prereq = record["verification_prereq"]
        guarantee = record["output_guarantee"]
        if prereq == NOT_APPLICABLE or guarantee == NOT_APPLICABLE:
            if prereq != NOT_APPLICABLE or guarantee != NOT_APPLICABLE:
                raise ValueError(f"{record['id']}: not_applicable must pair with itself")
        elif prereq not in levels or guarantee not in levels:
            raise ValueError(f"{record['id']}: unknown verification ladder value")
        if not record["evidence_kinds"] or any(kind not in EVIDENCE_KINDS for kind in record["evidence_kinds"]):
            raise ValueError(f"{record['id']}: unknown or missing evidence kind")
        if not record["since_version"]:
            raise ValueError(f"{record['id']}: since_version is required")


validate_capability_registry(CAPABILITIES)
