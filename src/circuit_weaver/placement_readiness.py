"""Sprint 41 — placement-readiness gate.

Promotes the subset of existing validator issues that block PCB placement
from warnings in the ``electrical`` category into hard errors in a new
``placement_readiness`` category. ``dispatcher.generate_artifacts`` treats
this category as non-bypassable (like ``structural`` / ``implementation``),
so any design that emits a ``.kicad_sch`` file is guaranteed to be wired
end-to-end with no dangling buses, no floating enables, and no
unverified-stub ICs.

The pure function :func:`placement_readiness_issues` re-reads the already
computed :class:`~circuit_weaver.validator.ValidationCheckResult` list plus
the compiled design IR so no check is re-run twice. It does not mutate
anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .design_ir import DesignIR
    from .validator import ValidationCheckResult, ValidationIssue


# ---------------------------------------------------------------------------
# Codes from the existing validator that block PCB placement.
#
# Format: { existing_check_code: (promote, fallback_reason) } where
#   promote  — emit under placement_readiness even if level==warning
#   fallback — short text used when the check didn't give a suggestion.
#
# The list below is intentional: every entry represents a condition where
# the schematic cannot be forward-annotated to a clean PCB. A ``--strict``
# user already treats these as failures; the new category makes that the
# default.
# ---------------------------------------------------------------------------
_PROMOTE_CODES: dict[str, str] = {
    "single-pin-net": "Connect the dangling net or mark it as an explicit no-connect.",
    "undriven-net": "Add an output driver or passive pull for this net.",
    "i2c-missing-pullup": "Add an i2c_bus PULLUPS_ONLY block, or declare external pull-ups.",
    "spi-floating-cs": "Drive this chip-select pin from an MCU GPIO or tie it through a strap.",
    "uart-unpaired": "Add the matching RX/TX net or declare the unused side an explicit no-connect.",
    "floating-enable": "Tie the enable pin to VIN via a pull-up, or add it to explicit_no_connects.",
    "floating-power-pin": "Route every power pin to its intended rail before placement.",
    "unverified-pinout": "Provide an explicit pin_map or set pinout_verified: true.",
    "vdd-to-gnd-short": "A power net is shorted to ground — check your net assignments.",
}

_VALIDATOR_NET_RE = re.compile(r"Net '([^']+)'")
_SINGLE_CONSUMER_ALLOW_NET_PREFIXES = (
    "TP",
    "TEST",
    "DEBUG",
    "DBG",
    "SWD",
    "JTAG",
)
_CONNECTOR_LOCAL_ALLOW_NET_PREFIXES = (
    "USB_CC",
    "CC",
    "SHIELD",
    "SHELL",
)


@dataclass
class PlacementReadinessReport:
    """Structured output of the placement-readiness gate.

    Consumed by :func:`dispatcher.generate_artifacts`, serialized as
    ``placement_readiness.json`` next to ``validation_report.json``.
    """

    ready: bool
    blocking: list[dict] = field(default_factory=list)
    auto_repaired: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "blocking": list(self.blocking),
            "auto_repaired": list(self.auto_repaired),
            "summary": dict(self.summary),
        }


def categorize_for_placement(code: str) -> bool:
    """Return True if a validator check code blocks placement readiness."""
    return code in _PROMOTE_CODES


def _component_ref(comp) -> str:
    return comp.source_ref or comp.ref_prefix or ""


def _build_net_to_refs(components) -> tuple[dict[str, set[str]], dict[str, object]]:
    """Map every pin/power net to the set of component refs that consume it."""
    net_to_refs: dict[str, set[str]] = {}
    comp_by_ref: dict[str, object] = {}
    for comp in components:
        ref = _component_ref(comp)
        if not ref:
            continue
        comp_by_ref[ref] = comp
        for net in comp.pin_nets.values():
            if net:
                net_to_refs.setdefault(net, set()).add(ref)
        for net in comp.power_pins.values():
            if net:
                net_to_refs.setdefault(net, set()).add(ref)
    return net_to_refs, comp_by_ref


def _orphan_interface_issues(compiled_ir: "DesignIR", components) -> list[tuple[str, str, str]]:
    """Detect block-declared interfaces whose net name is never consumed.

    An ``interface`` on a block means "this block publishes a signal called
    N, and another block must consume it". If no other block's
    ``pin_nets`` / ``power_pins`` reference N, the interface is orphaned —
    any downstream placement pass that
    relies on hierarchical labels will leave the net dangling.

    Passive support elements (for example a pull strap or bypass capacitor)
    do **not** count as another block consuming the signal. They may support
    the net electrically, but they do not satisfy the higher-level contract
    that another functional block is attached to the interface.

    Power / ground interfaces are excluded: those are driven by the rail
    infrastructure, not another block.

    Returns a list of (block_ref, net_name, direction) tuples.
    """
    net_to_refs, _ = _build_net_to_refs(components)

    # A template's explicit series output filter is part of its functional
    # signal path. Count its far endpoint as that block's published output so
    # a downstream connector or op-amp satisfies the interface contract. Other
    # support passives (pull straps, bypass capacitors, and sensing networks)
    # remain excluded as they do not publish a functional signal interface.
    for comp in components:
        ref = str(getattr(comp, "source_ref", "") or "").strip()
        if not ref:
            continue
        for strap in getattr(comp, "straps", []) or []:
            if getattr(strap, "role", "") != "output_filter":
                continue
            output_net = str(getattr(strap, "rail", "") or "").strip()
            if output_net:
                net_to_refs.setdefault(output_net, set()).add(ref)

    from .subcircuits.base import _is_power_net

    orphans: list[tuple[str, str, str]] = []
    for block in compiled_ir.blocks:
        block_ref = block.ref or block.id
        for iface in block.interfaces:
            net = (iface.name or "").strip()
            if not net:
                continue
            if _is_power_net(net):
                continue
            consumers = net_to_refs.get(net, set())
            # The block itself is allowed to appear in consumers; we need
            # at least one *other* block to reference the net.
            others = {r for r in consumers if r != block_ref}
            if not others:
                orphans.append((block_ref, net, iface.direction or "bidirectional"))
    return orphans


def _is_allowlisted_single_consumer_net(net: str, consumers: set[str], comp_by_ref: dict[str, object]) -> bool:
    """Allow intentional one-ended nets used for test/debug exposure."""
    if len(consumers) != 1:
        return False
    upper = net.strip().upper()
    if upper.startswith(_SINGLE_CONSUMER_ALLOW_NET_PREFIXES):
        return True
    ref = next(iter(consumers))
    comp = comp_by_ref.get(ref)
    if comp is None:
        return False
    if getattr(comp, "category", "") == "debug":
        return True
    if getattr(comp, "category", "") == "connector" and upper.startswith(_CONNECTOR_LOCAL_ALLOW_NET_PREFIXES):
        return True
    ref_prefix = str(getattr(comp, "ref_prefix", "") or "").upper()
    source_ref = str(getattr(comp, "source_ref", "") or "").upper()
    return ref_prefix == "TP" or source_ref.startswith("TP")


def _support_passive_nets(components) -> set[str]:
    """Nets terminated by a support passive (strap, bypass, inductor, divider).

    Unlike the ``orphan-interface`` contract — where a pull strap does not
    satisfy "another functional block consumes this interface" — the
    ``orphan-net`` check is about physical connectivity. A regulator's SW
    node ending at its inductor, an FB node at its feedback divider, or an
    enable pin at its RC strap all have a real second component on the net
    and must not be flagged as one-ended.
    """
    nets: set[str] = set()
    for comp in components:
        for cap in getattr(comp, "bypass_caps", []) or []:
            for net in (getattr(cap, "net", ""), getattr(cap, "gnd_net", "")):
                if net:
                    nets.add(net)
        for strap in getattr(comp, "straps", []) or []:
            for net in (getattr(strap, "net", ""), getattr(strap, "rail", "")):
                if net:
                    nets.add(net)
    return nets


def _single_consumer_net_issues(
    compiled_ir: "DesignIR",
    components,
    existing_validator_nets: set[str],
) -> list[tuple[str, str]]:
    """Detect non-power nets consumed by only one block even without a declared interface.

    This closes the gap where a synthesized or mis-modeled net never appears in
    ``compiled_ir.blocks[].interfaces`` and therefore bypasses the
    ``orphan-interface`` check entirely. We skip nets the validator already
    promoted (for example ``single-pin-net`` / ``undriven-net``) to avoid
    duplicate placement-readiness errors for the same underlying problem.
    """
    from .subcircuits.base import _is_power_net

    net_to_refs, comp_by_ref = _build_net_to_refs(components)
    support_nets = _support_passive_nets(components)
    declared_interfaces = {
        (iface.name or "").strip()
        for block in compiled_ir.blocks
        for iface in block.interfaces
        if (iface.name or "").strip()
    }
    out: list[tuple[str, str]] = []
    for net, consumers in net_to_refs.items():
        if not net or _is_power_net(net):
            continue
        if len(consumers) > 1:
            continue
        if net in declared_interfaces or net in existing_validator_nets:
            continue
        if net in support_nets:
            continue
        if _is_allowlisted_single_consumer_net(net, consumers, comp_by_ref):
            continue
        out.append((next(iter(consumers)), net))
    return out


def placement_readiness_issues(
    validator_results: list["ValidationCheckResult"],
    compiled_ir: "DesignIR",
    components,
) -> list["ValidationIssue"]:
    """Return the subset of validator issues that block PCB placement.

    Issues are returned as ``ValidationIssue`` instances with
    ``level='error'`` so callers can splat them straight into the
    ``placement_readiness`` category of a ``ValidationReport``. Each
    issue carries a ``suggestion`` when the original check didn't
    supply one.

    Also appends:
    - ``orphan-interface`` issues derived from the compiled IR — a block
      declared a signal interface but no other block consumes the net.
    - ``orphan-net`` issues for non-power nets consumed by only one block
      even when no interface was declared.
    """
    from .validator import ValidationIssue  # local import avoids cycle

    out: list[ValidationIssue] = []
    existing_validator_nets: set[str] = set()

    for result in validator_results:
        if result.code not in _PROMOTE_CODES and not any(
            issue.code in _PROMOTE_CODES for issue in result.issues
        ):
            # Some checks have a code like "bus-completeness" that fans out
            # into sub-codes like "i2c-missing-pullup". We need to inspect
            # the issues themselves.
            continue
        for issue in result.issues:
            # Fast path: a check code registered in _PROMOTE_CODES applies
            # to every issue it produces. Otherwise, match on the issue's
            # individual code (for checks like bus-completeness, enable-
            # pins, net-connectivity that emit multiple sub-codes).
            promote = result.code in _PROMOTE_CODES or issue.code in _PROMOTE_CODES
            if not promote:
                continue
            fallback = _PROMOTE_CODES.get(issue.code) or _PROMOTE_CODES.get(result.code, "")
            suggestion = issue.suggestion or fallback
            match = _VALIDATOR_NET_RE.search(issue.message or "")
            if match:
                existing_validator_nets.add(match.group(1))
            out.append(
                ValidationIssue(
                    code=issue.code,
                    level="error",
                    ref=issue.ref,
                    mpn=issue.mpn,
                    message=issue.message,
                    suggestion=suggestion,
                )
            )

    # Orphan interfaces — emitted directly (not from the validator).
    for block_ref, net, direction in _orphan_interface_issues(compiled_ir, components):
        out.append(
            ValidationIssue(
                code="orphan-interface",
                level="error",
                ref=block_ref,
                mpn="",
                message=(
                    f"Block '{block_ref}' declared interface '{net}' ({direction}) "
                    "but no other block connects to that net"
                ),
                suggestion=(
                    f"Wire another block's pin_nets to '{net}', drop the interface, "
                    "or mark the block as terminal."
                ),
            )
        )

    for block_ref, net in _single_consumer_net_issues(compiled_ir, components, existing_validator_nets):
        out.append(
            ValidationIssue(
                code="orphan-net",
                level="error",
                ref=block_ref,
                mpn="",
                message=f"Net '{net}' is only consumed by block '{block_ref}' and has no external peer",
                suggestion=(
                    f"Connect another block to '{net}', declare it as a deliberate debug/test net, "
                    "or mark the pin as an explicit no-connect."
                ),
            )
        )

    return out


__all__ = [
    "PlacementReadinessReport",
    "categorize_for_placement",
    "placement_readiness_issues",
]
