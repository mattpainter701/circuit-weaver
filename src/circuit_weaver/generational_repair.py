"""Sprint 41 — auto-repair trivially-fixable bus issues before validation.

Runs inside :func:`dispatcher.compile_design_ir` after the primary
component resolution step. Detects buses whose conditioning passives are
obviously missing (I2C without pull-ups, SPI CS without a pull) and
synthesizes them from the already-shipping subcircuit templates
(:mod:`subcircuits.i2c_bus`).

The pass is intentionally conservative — it only adds what the validator
would otherwise flag as placement-blocking, and only when the missing
passive is unambiguous (e.g. a single VDD rail on every participant). It
never remaps MCU pins, adds missing sensors, or guesses at undeclared
signal names. Those remain user errors and surface as
``placement_readiness / orphan-interface`` from :mod:`placement_readiness`.

Users can disable the whole pass via top-level spec key
``auto_repair: false``. A single repair is suppressed automatically when
the design already contains a matching bus-conditioning block —
e.g. declaring an ``i2c_bus`` block on ``I2C0_SDA/I2C0_SCL`` inhibits
pull-up repair for that bus, but not for unrelated I2C nets elsewhere
in the design.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field

from .component_db import ComponentDef
from .design_ir import DesignBlock, DesignIR

_logger = logging.getLogger(__name__)

_I2C_SDA_RE = re.compile(r"(?:^|_)(SDA|I2C.*DAT|I2C\d*SDA)(?:$|_)", re.IGNORECASE)
_I2C_SCL_RE = re.compile(r"(?:^|_)(SCL|I2C.*CLK|I2C\d*SCL)(?:$|_)", re.IGNORECASE)


def _is_ground(net: str) -> bool:
    """Return True if *net* is a ground net by name convention."""
    upper = (net or "").upper()
    from .subcircuits.base import GROUND_NET_PREFIXES
    return any(upper == p or upper.startswith(f"{p}_") for p in GROUND_NET_PREFIXES)


@dataclass
class RepairAction:
    """One auto-repair decision, logged for audit and written into the
    placement_readiness.json report."""

    kind: str  # e.g. "i2c_pullups"
    rationale: str
    synthetic_block_id: str = ""
    nets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "rationale": self.rationale,
            "synthetic_block_id": self.synthetic_block_id,
            "nets": list(self.nets),
        }


@dataclass
class ComponentRepair:
    """One component-local auto-repair edit applied after resolution."""

    ref: str
    pin_nets: dict[str, str] = field(default_factory=dict)
    explicit_no_connects: set[str] = field(default_factory=set)


def _bus_pairs(components: list[ComponentDef]) -> list[tuple[str, str, set[str]]]:
    """Return (sda_net, scl_net, participating_power_nets) triplets.

    A bus is formed as soon as *any* named I2C_SDA-ish net and a matching
    I2C_SCL-ish net both appear anywhere in the design. Number of
    participants is not required — a lone sensor declaring I2C_SDA /
    I2C_SCL still needs pull-ups, and the user will wire the MCU side
    later (typically via forward-annotation edits or an explicit
    ``pin_map`` in the YAML). ``participating_power_nets`` is the union
    of each participant's non-ground ``power_pins`` values — used to
    pick the pull-up rail when the bus doesn't name one explicitly.

    Matching is one-to-one: each SDA net pairs with at most one SCL net
    (the one with the largest ref overlap, then alphabetical as
    tiebreaker for determinism). Zero-overlap pairs are rejected so
    unrelated debug or placeholder nets do not synthesize false buses.
    """
    sda_to_refs: dict[str, set[str]] = {}
    scl_to_refs: dict[str, set[str]] = {}
    ref_to_power_nets: dict[str, set[str]] = {}

    for comp in components:
        ref = comp.source_ref or comp.ref_prefix
        if not ref:
            continue
        for pin_num, net in comp.pin_nets.items():
            if not net:
                continue
            if _I2C_SDA_RE.search(net):
                sda_to_refs.setdefault(net, set()).add(ref)
            if _I2C_SCL_RE.search(net):
                scl_to_refs.setdefault(net, set()).add(ref)
        for net in comp.power_pins.values():
            if net and not _is_ground(net):
                ref_to_power_nets.setdefault(ref, set()).add(net)

    pairs: list[tuple[str, str, set[str]]] = []
    used_scl: set[str] = set()
    for sda_net in sorted(sda_to_refs):
        sda_refs = sda_to_refs[sda_net]
        # Pick the SCL whose refs overlap most with this SDA; fall back
        # to alphabetical ordering for determinism. A lone named bus
        # with one participant on each line is still a bus that needs
        # pull-ups.
        best_scl = None
        best_overlap = 0
        for scl_net in sorted(scl_to_refs):
            if scl_net in used_scl:
                continue
            overlap = len(sda_refs & scl_to_refs[scl_net])
            if overlap > best_overlap:
                best_overlap = overlap
                best_scl = scl_net
        if best_scl is None:
            continue
        used_scl.add(best_scl)
        power_nets: set[str] = set()
        for ref in sda_refs | scl_to_refs.get(best_scl, set()):
            power_nets |= ref_to_power_nets.get(ref, set())
        pairs.append((sda_net, best_scl, power_nets))
    return pairs


def _has_pullup_on(components: list[ComponentDef], net: str) -> bool:
    """True iff any component already straps ``net`` to a non-ground rail."""
    for comp in components:
        for strap in comp.straps:
            if strap.net == net and strap.rail and not _is_ground(strap.rail):
                return True
    return False


def _existing_template_types(ir: DesignIR) -> set[str]:
    return {(block.template_type or "").strip().lower() for block in ir.blocks if block.template_type}


def _existing_i2c_bus_pairs(ir: DesignIR) -> set[tuple[str, str]]:
    """Return the explicitly declared I2C bus net pairs in the design IR.

    Covers the standard pull-up topology (``sda_net`` / ``scl_net``) plus
    level-shifter forms that declare low-side or high-side nets.
    """
    pairs: set[tuple[str, str]] = set()
    for block in ir.blocks:
        if (block.template_type or "").strip().lower() != "i2c_bus":
            continue
        raw_pairs = [
            (block.params.get("sda_net"), block.params.get("scl_net")),
            (block.params.get("sda_low_net"), block.params.get("scl_low_net")),
            (block.params.get("sda_high_net"), block.params.get("scl_high_net")),
        ]
        for sda_net, scl_net in raw_pairs:
            sda = str(sda_net or "").strip()
            scl = str(scl_net or "").strip()
            if sda and scl:
                pairs.add((sda, scl))
    return pairs


def _handled_pins(comp: ComponentDef) -> set[str]:
    handled = set(comp.pin_nets) | set(comp.power_pins) | set(comp.explicit_no_connects)
    for strap in comp.straps:
        handled.add(str(strap.pin))
    return handled


def _component_roles(comp: ComponentDef) -> dict[str, str]:
    if hasattr(comp, "resolved_pin_roles"):
        return comp.resolved_pin_roles()
    return {}


def _component_ref(comp: ComponentDef) -> str:
    return str(comp.source_ref or comp.ref_prefix or "").strip()


def _match_case(template: str, sample: str) -> str:
    if sample.isupper():
        return template.upper()
    if sample.islower():
        return template.lower()
    if sample[:1].isupper():
        return template.capitalize()
    return template


def _swap_uart_direction(net_name: str) -> str | None:
    raw = str(net_name or "").strip()
    if not raw:
        return None
    patterns = (
        (re.compile(r"(?<![A-Za-z0-9])(TXD)(?![A-Za-z0-9])", re.IGNORECASE), "RXD"),
        (re.compile(r"(?<![A-Za-z0-9])(RXD)(?![A-Za-z0-9])", re.IGNORECASE), "TXD"),
        (re.compile(r"(?<![A-Za-z0-9])(TX)(?![A-Za-z0-9])", re.IGNORECASE), "RX"),
        (re.compile(r"(?<![A-Za-z0-9])(RX)(?![A-Za-z0-9])", re.IGNORECASE), "TX"),
    )
    for pattern, replacement in patterns:
        if pattern.search(raw):
            return pattern.sub(lambda m: _match_case(replacement, m.group(1)), raw, count=1)
    return None


def _swap_uart_flow_direction(net_name: str) -> str | None:
    """Return the sibling flow-control net name (RTS <-> CTS), if derivable."""
    raw = str(net_name or "").strip()
    if not raw:
        return None
    patterns = (
        (re.compile(r"(?<![A-Za-z0-9])(RTS)(?![A-Za-z0-9])", re.IGNORECASE), "CTS"),
        (re.compile(r"(?<![A-Za-z0-9])(CTS)(?![A-Za-z0-9])", re.IGNORECASE), "RTS"),
    )
    for pattern, replacement in patterns:
        if pattern.search(raw):
            return pattern.sub(lambda m: _match_case(replacement, m.group(1)), raw, count=1)
    return None


def _merge_component_pin_net(
    repairs: dict[str, ComponentRepair],
    *,
    ref: str,
    pin: str,
    net_name: str,
) -> bool:
    if not ref or not pin or not net_name:
        return False
    repair = repairs.setdefault(ref, ComponentRepair(ref=ref))
    if repair.pin_nets.get(pin) == net_name:
        return False
    if pin in repair.pin_nets and repair.pin_nets[pin] != net_name:
        return False
    repair.pin_nets[pin] = net_name
    return True


def _repair_spi_chip_selects(components: list[ComponentDef]) -> tuple[list[ComponentRepair], list[RepairAction]]:
    """Route floating SPI CS pins onto an existing unique bus chip-select net."""
    repairs: dict[str, ComponentRepair] = {}
    actions: list[RepairAction] = []
    role_cache = {_component_ref(comp): _component_roles(comp) for comp in components}
    spi_bus_nets: dict[str, set[str]] = {}
    cs_nets: dict[str, set[str]] = {}
    for comp in components:
        ref = _component_ref(comp)
        roles = role_cache.get(ref, {})
        bus_nets = {
            comp.pin_nets[pin]
            for role in ("mosi", "miso", "sclk")
            for pin in [roles.get(role)]
            if pin and pin in comp.pin_nets and comp.pin_nets[pin]
        }
        spi_bus_nets[ref] = bus_nets
        cs_pin = roles.get("cs")
        if cs_pin and cs_pin in comp.pin_nets and comp.pin_nets[cs_pin]:
            cs_nets.setdefault(ref, set()).add(comp.pin_nets[cs_pin])

    for comp in components:
        ref = _component_ref(comp)
        roles = role_cache.get(ref, {})
        cs_pin = roles.get("cs")
        if not ref or not cs_pin or cs_pin in _handled_pins(comp):
            continue
        bus_nets = spi_bus_nets.get(ref, set())
        if len(bus_nets) < 2:
            continue
        candidates: set[str] = set()
        for peer in components:
            peer_ref = _component_ref(peer)
            if not peer_ref or peer_ref == ref:
                continue
            if len(bus_nets & spi_bus_nets.get(peer_ref, set())) < 2:
                continue
            candidates.update(cs_nets.get(peer_ref, set()))
        if len(candidates) != 1:
            continue
        cs_net = next(iter(candidates))
        if _merge_component_pin_net(repairs, ref=ref, pin=cs_pin, net_name=cs_net):
            actions.append(
                RepairAction(
                    kind="spi_cs",
                    rationale=(
                        f"SPI participant {ref} shared MOSI/MISO/SCLK with an existing bus but left "
                        f"chip-select pin {cs_pin} floating; connected it to existing CS net {cs_net}."
                    ),
                    nets=sorted(bus_nets | {cs_net}),
                )
            )
    return list(repairs.values()), actions


def _repair_uart_pairs(components: list[ComponentDef]) -> tuple[list[ComponentRepair], list[RepairAction]]:
    """Complete missing UART TX/RX pair nets only when the peer net already exists."""
    repairs: dict[str, ComponentRepair] = {}
    actions: list[RepairAction] = []
    existing_nets = {
        str(net).strip()
        for comp in components
        for net in comp.pin_nets.values()
        if str(net or "").strip()
    }
    for comp in components:
        ref = _component_ref(comp)
        roles = _component_roles(comp)
        if not ref:
            continue
        handled = _handled_pins(comp)
        tx_pin = roles.get("txd")
        rx_pin = roles.get("rxd")
        tx_net = comp.pin_nets.get(tx_pin) if tx_pin else None
        rx_net = comp.pin_nets.get(rx_pin) if rx_pin else None

        if tx_pin and tx_net and rx_pin and rx_pin not in handled:
            candidate = _swap_uart_direction(tx_net)
            if candidate and candidate in existing_nets:
                if _merge_component_pin_net(repairs, ref=ref, pin=rx_pin, net_name=candidate):
                    actions.append(
                        RepairAction(
                            kind="uart_pair",
                            rationale=(
                                f"UART participant {ref} already drove {tx_net} on TX pin {tx_pin} but left "
                                f"RX pin {rx_pin} unmapped; connected it to existing peer net {candidate}."
                            ),
                            nets=[tx_net, candidate],
                        )
                    )

        if rx_pin and rx_net and tx_pin and tx_pin not in handled:
            candidate = _swap_uart_direction(rx_net)
            if candidate and candidate in existing_nets:
                if _merge_component_pin_net(repairs, ref=ref, pin=tx_pin, net_name=candidate):
                    actions.append(
                        RepairAction(
                            kind="uart_pair",
                            rationale=(
                                f"UART participant {ref} already received on {rx_net} via RX pin {rx_pin} but left "
                                f"TX pin {tx_pin} unmapped; connected it to existing peer net {candidate}."
                            ),
                            nets=[rx_net, candidate],
                        )
                    )
    return list(repairs.values()), actions


def _repair_uart_flow_control(components: list[ComponentDef]) -> tuple[list[ComponentRepair], list[RepairAction]]:
    """Resolve metadata-declared UART handshake pins (RTS/CTS) on active UARTs.

    T233 — when a component's normalized roles declare flow-control pins but
    the design leaves them unmapped while TX/RX are wired, either:

    - complete a handshake pin onto the sibling flow-control net when that
      net already exists (own CTS on ``UART0_CTS`` implies RTS belongs on an
      existing ``UART0_RTS``), or
    - declare the pin an explicit no-connect — flow control is intentionally
      unused on this UART, and the pin must not surface as a floating-input
      or unmapped-required generation failure.

    Components with no active UART (neither TX nor RX mapped) are skipped so
    the pass never invents handshake behavior for parts that merely expose
    the pins in metadata.
    """
    repairs: dict[str, ComponentRepair] = {}
    actions: list[RepairAction] = []
    existing_nets = {
        str(net).strip()
        for comp in components
        for net in comp.pin_nets.values()
        if str(net or "").strip()
    }
    for comp in components:
        ref = _component_ref(comp)
        roles = _component_roles(comp)
        if not ref:
            continue
        handled = _handled_pins(comp)
        tx_pin = roles.get("txd")
        rx_pin = roles.get("rxd")
        uart_active = bool((tx_pin and comp.pin_nets.get(tx_pin)) or (rx_pin and comp.pin_nets.get(rx_pin)))
        if not uart_active:
            continue
        for own_role, peer_role in (("rts", "cts"), ("cts", "rts")):
            pin = roles.get(own_role)
            if not pin or pin in handled:
                continue
            sibling_pin = roles.get(peer_role)
            sibling_net = comp.pin_nets.get(sibling_pin) if sibling_pin else None
            candidate = _swap_uart_flow_direction(sibling_net) if sibling_net else None
            if candidate and candidate in existing_nets:
                if _merge_component_pin_net(repairs, ref=ref, pin=pin, net_name=candidate):
                    actions.append(
                        RepairAction(
                            kind="uart_flow_control",
                            rationale=(
                                f"UART participant {ref} wired {peer_role.upper()} to {sibling_net} but left "
                                f"{own_role.upper()} pin {pin} unmapped; connected it to existing sibling "
                                f"net {candidate}."
                            ),
                            nets=[sibling_net, candidate],
                        )
                    )
                continue
            repair = repairs.setdefault(ref, ComponentRepair(ref=ref))
            if pin not in repair.explicit_no_connects:
                repair.explicit_no_connects.add(pin)
                actions.append(
                    RepairAction(
                        kind="uart_handshake_nc",
                        rationale=(
                            f"UART participant {ref} has an active TX/RX pair but no flow-control wiring; "
                            f"declared unused {own_role.upper()} pin {pin} an explicit no-connect."
                        ),
                        nets=[],
                    )
                )
    return list(repairs.values()), actions


def apply_component_repairs(components: list[ComponentDef], repairs: list[ComponentRepair]) -> None:
    """Apply component-local repairs in-place after resolution."""
    if not repairs:
        return
    by_ref = {_component_ref(comp): comp for comp in components if _component_ref(comp)}
    for repair in repairs:
        comp = by_ref.get(repair.ref)
        if comp is None:
            continue
        for pin, net_name in repair.pin_nets.items():
            if pin not in comp.pin_nets:
                comp.pin_nets[pin] = net_name
        comp.explicit_no_connects.update(repair.explicit_no_connects)
        # A repaired pin is no longer unmapped — drop it from the T228
        # fail-closed marker so generation doesn't hard-fail on a pin the
        # repair pass just resolved.
        resolved = set(repair.pin_nets) | repair.explicit_no_connects
        unmapped = getattr(comp, "unmapped_required_pins", None)
        if unmapped:
            for pin in resolved:
                unmapped.pop(pin, None)


def _pick_vdd_rail(power_nets: set[str]) -> str | None:
    """Pick a sensible pull-up rail from a set of candidate VDDs.

    Preference: lowest-voltage rail that looks like VDD (so I2C pulls to
    the logic rail, not VBUS). Falls back to alphabetical ordering to
    keep the decision deterministic.
    """
    if not power_nets:
        return None
    preferred = [n for n in power_nets if n.upper().startswith("VDD")]
    pool = preferred or list(power_nets)

    def _rank(name: str) -> tuple:
        # Extract numeric voltage if any (e.g. VDD_3P3 -> 3.3, VDD_1P8 -> 1.8)
        match = re.search(r"(\d+)P(\d+)", name.upper())
        if match:
            return (0, float(f"{match.group(1)}.{match.group(2)}"), name)
        match = re.search(r"(\d+)V", name.upper())
        if match:
            return (0, float(match.group(1)), name)
        return (1, 99.0, name)

    return sorted(pool, key=_rank)[0]


def _stable_block_id(section: str, ref: str, tag: str) -> str:
    """Build a stable synthetic block id. Mirrors design_ir._stable_token."""
    import zlib

    token = f"{zlib.crc32(f'{section}|{ref}|{tag}'.encode('utf-8')) & 0xFFFFFFFF:08x}"
    return f"{section}:{ref}:{token}"


def _reserved_refs(ir: DesignIR) -> set[str]:
    return {block.ref for block in ir.blocks if block.ref}


def _next_ref(prefix: str, reserved: set[str]) -> str:
    """Allocate the next free ref with ``prefix`` given reserved refs."""
    idx = 1
    while True:
        candidate = f"{prefix}{idx}"
        if candidate not in reserved:
            reserved.add(candidate)
            return candidate
        idx += 1


def _build_i2c_pullup_block(
    sda_net: str,
    scl_net: str,
    vdd_net: str,
    ref: str,
) -> DesignBlock:
    """Construct a synthetic ``i2c_bus`` PULLUPS_ONLY block."""
    params = {
        "ic": "PULLUPS_ONLY",
        "vdd_net": vdd_net,
        "sda_net": sda_net,
        "scl_net": scl_net,
        "ref": ref,
    }
    return DesignBlock(
        id=_stable_block_id("buses", ref, f"i2c:{sda_net}:{scl_net}"),
        section="buses",
        kind="template",
        ref=ref,
        template_type="i2c_bus",
        ic="PULLUPS_ONLY",
        params=params,
        description=f"Auto-repair: I2C pull-ups on {sda_net}/{scl_net} to {vdd_net}",
    ).normalized()


def auto_repair_design(
    ir: DesignIR,
    components: list[ComponentDef],
    *,
    enabled: bool = True,
) -> tuple[DesignIR, list[ComponentRepair], list[RepairAction]]:
    """Return ``(patched_ir, component_repairs, repair_actions)`` with auto-fixes applied.

    ``ir`` is returned unchanged if ``enabled=False`` or no repairs
    apply. This function never mutates its inputs — it constructs a new
    :class:`DesignIR` with any synthetic blocks appended.

    Parameters
    ----------
    ir:
        Canonical design IR as produced by
        :func:`circuit_weaver.design_ir.normalize_design_spec`.
    components:
        Resolved component list produced by
        :func:`circuit_weaver.project_spec.resolve_project_spec` for the
        *same* IR. Used to detect bus participants.
    enabled:
        When False, skip every repair. Exposed so callers
        (``auto_repair: false`` in the spec, or opt-out env var) can
        disable the pass without branching elsewhere.
    """
    if not enabled:
        return ir, [], []

    actions: list[RepairAction] = []
    new_blocks: list[DesignBlock] = []
    component_repairs: list[ComponentRepair] = []

    existing_types = _existing_template_types(ir)
    existing_i2c_pairs = _existing_i2c_bus_pairs(ir)
    reserved = _reserved_refs(ir)

    # --- I2C pull-ups ----------------------------------------------------
    if "i2c_bus" not in existing_types:
        for sda_net, scl_net, power_nets in _bus_pairs(components):
            if _has_pullup_on(components, sda_net) and _has_pullup_on(components, scl_net):
                continue
            vdd = _pick_vdd_rail(power_nets)
            if not vdd:
                _logger.info(
                    "auto_repair: skipping i2c pull-ups on %s/%s — no candidate VDD rail",
                    sda_net,
                    scl_net,
                )
                continue
            ref = _next_ref("RP", reserved)
            block = _build_i2c_pullup_block(sda_net, scl_net, vdd, ref)
            new_blocks.append(block)
            actions.append(
                RepairAction(
                    kind="i2c_pullups",
                    rationale=(
                        f"I2C bus {sda_net}/{scl_net} declared but no pull-up "
                        f"straps found; inserted PULLUPS_ONLY block {ref} to {vdd}."
                    ),
                    synthetic_block_id=block.id,
                    nets=[sda_net, scl_net, vdd],
                )
            )
            _logger.info(
                "auto_repair: synthesizing I2C pull-ups %s on %s/%s -> %s",
                ref,
                sda_net,
                scl_net,
                vdd,
            )
    else:
        for sda_net, scl_net, power_nets in _bus_pairs(components):
            if (sda_net, scl_net) in existing_i2c_pairs:
                continue
            if _has_pullup_on(components, sda_net) and _has_pullup_on(components, scl_net):
                continue
            vdd = _pick_vdd_rail(power_nets)
            if not vdd:
                _logger.info(
                    "auto_repair: skipping i2c pull-ups on %s/%s — no candidate VDD rail",
                    sda_net,
                    scl_net,
                )
                continue
            ref = _next_ref("RP", reserved)
            block = _build_i2c_pullup_block(sda_net, scl_net, vdd, ref)
            new_blocks.append(block)
            actions.append(
                RepairAction(
                    kind="i2c_pullups",
                    rationale=(
                        f"I2C bus {sda_net}/{scl_net} declared but no pull-up "
                        f"straps found; inserted PULLUPS_ONLY block {ref} to {vdd}."
                    ),
                    synthetic_block_id=block.id,
                    nets=[sda_net, scl_net, vdd],
                )
            )
            _logger.info(
                "auto_repair: synthesizing I2C pull-ups %s on %s/%s -> %s",
                ref,
                sda_net,
                scl_net,
                vdd,
            )

    # --- SPI CS completion -----------------------------------------------
    spi_repairs, spi_actions = _repair_spi_chip_selects(components)
    component_repairs.extend(spi_repairs)
    actions.extend(spi_actions)

    # --- UART TX/RX completion -------------------------------------------
    uart_repairs, uart_actions = _repair_uart_pairs(components)
    component_repairs.extend(uart_repairs)
    actions.extend(uart_actions)

    # --- UART flow-control completion / explicit NC ------------------------
    flow_repairs, flow_actions = _repair_uart_flow_control(components)
    component_repairs.extend(flow_repairs)
    actions.extend(flow_actions)

    if not new_blocks:
        return ir, component_repairs, actions

    # Assemble the patched IR. Keep ordering stable: user blocks first,
    # synthetic blocks appended (matches how a user would have added them
    # at the end of the YAML spec).
    patched_blocks = list(ir.blocks) + new_blocks
    all_interfaces = []
    for block in patched_blocks:
        all_interfaces.extend(block.interfaces)

    patched = DesignIR(
        metadata=dict(ir.metadata),
        blocks=patched_blocks,
        interfaces=all_interfaces,
        power_domains=list(ir.power_domains),
        approved_overrides=list(ir.approved_overrides),
        pcb_constraints=list(ir.pcb_constraints),
        operating_cases=copy.deepcopy(ir.operating_cases),
    )
    return patched, component_repairs, actions


__all__ = ["ComponentRepair", "RepairAction", "apply_component_repairs", "auto_repair_design"]
