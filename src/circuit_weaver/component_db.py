"""Component database — stores IC definitions, pin maps, bypass/strap requirements.

Each component definition contains everything needed to place and wire it
in a schematic: pins, power requirements, bypass caps, strap resistors.
Keyed by MPN (manufacturer part number).
"""

import math
import re
from dataclasses import dataclass, field

from .evidence_policy import EVIDENCE_ID_PATTERN, validate_evidence_text

SUPPORT_PASSIVE_PRESENTATIONS = {"inherit", "literal_local", "symbolic", "topology_local"}
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_POWER_DIRECTIONS = {"source", "load", "bidirectional"}
_PASSIVE_RECOMMENDATION_FAMILIES = {
    "regulator_io_cap",
    "crystal_cap",
    "reset_enable_strap",
    "interface_termination",
    "protection",
}
_PASSIVE_RECOMMENDATION_POLICIES = {"datasheet", "equation", "bounded_fallback"}
_PASSIVE_RECOMMENDATION_CONFIDENCES = {"verified", "corroborated", "single_source", "heuristic", "stub"}
_PASSIVE_RECOMMENDATION_UNITS = {
    "regulator_io_cap": {"F"},
    "crystal_cap": {"F"},
    "reset_enable_strap": {"ohm"},
    "interface_termination": {"ohm"},
    "protection": {"F", "ohm", "V", "A"},
}
_CALCULATION_ID_RE = re.compile(r"^CALC-[A-Z0-9_]+-[a-f0-9]{12}$")
_WITHHELD_FINDING_ID_RE = re.compile(r"^CW-PSV-[0-9]{3}-[a-f0-9]{12}$")
_RECOMMENDATION_PRECEDENCE = ("datasheet", "equation", "bounded_fallback")
_PASSIVE_ELIGIBILITY = {"eligible", "ineligible", "withheld"}
_GENERIC_PURPOSE_BY_CATEGORY = {
    "power": "Power conversion and rail conditioning",
    "transceiver": "RF conversion and high-speed signal processing",
    "fpga": "Programmable processing and high-speed digital interfacing",
    "clock": "Clock generation, cleanup, and distribution",
    "usb": "USB interface control and protocol bridging",
    "ethernet": "Ethernet / PoE interface and physical-layer control",
    "storage": "Boot and nonvolatile storage",
    "connector": "External interface and field connectivity",
    "debug": "Bring-up, test, and service access",
    "sensor": "Measurement and sensing interface",
}


def normalize_support_passive_presentation(value: str | None, default: str = "literal_local") -> str:
    """Return a validated support-passive presentation mode."""
    normalized = (value or default or "literal_local").strip().lower()
    if normalized not in SUPPORT_PASSIVE_PRESENTATIONS:
        valid = ", ".join(sorted(SUPPORT_PASSIVE_PRESENTATIONS))
        raise ValueError(f"Unknown support passive presentation '{value}'. Expected one of: {valid}")
    if normalized == "inherit":
        return normalize_support_passive_presentation(default, default="literal_local")
    return normalized


@dataclass(frozen=True)
class PresentationWiringPolicy:
    """Engine-level presentation defaults for generated schematic connectivity."""

    support_passives: str = "literal_local"

    def normalized(self) -> "PresentationWiringPolicy":
        return PresentationWiringPolicy(support_passives=normalize_support_passive_presentation(self.support_passives))


def normalize_presentation_wiring_policy(
    policy: PresentationWiringPolicy | dict | None,
) -> PresentationWiringPolicy:
    """Return a normalized presentation wiring policy from user/config input."""
    if policy is None:
        return PresentationWiringPolicy().normalized()
    if isinstance(policy, PresentationWiringPolicy):
        return policy.normalized()
    if isinstance(policy, dict):
        return PresentationWiringPolicy(
            support_passives=str(policy.get("support_passives", "literal_local"))
        ).normalized()
    raise TypeError(f"Unsupported presentation wiring policy type: {type(policy)!r}")


@dataclass
class PinDef:
    """A single pin on a component."""

    number: str  # "1", "2", "A3", "EP"
    name: str  # "VIN", "GND", "GPIO0"
    electrical_type: str  # input, output, bidirectional, passive, power_in, power_out
    side: str  # L, R, T, B — which side of the symbol box

    def as_tuple(self):
        return (self.number, self.name, self.electrical_type, self.side)


_PIN_ROLE_ALIASES = {
    "sda": "sda",
    "sda1": "sda1",
    "sda2": "sda2",
    "scl": "scl",
    "scl1": "scl1",
    "scl2": "scl2",
    "mosi": "mosi",
    "sdi": "mosi",
    "copi": "mosi",
    "miso": "miso",
    "sdo": "miso",
    "cipo": "miso",
    "sclk": "sclk",
    "sck": "sclk",
    "cs": "cs",
    "csn": "cs",
    "cs_n": "cs",
    "csb": "cs",
    "ss": "cs",
    "ssn": "cs",
    "nss": "cs",
    "tx": "txd",
    "txd": "txd",
    "txd0": "txd",
    "tx0": "txd",
    "rx": "rxd",
    "rxd": "rxd",
    "rxd0": "rxd",
    "rx0": "rxd",
    "cts": "cts",
    "rts": "rts",
    "dp": "dp",
    "dp1": "dp1",
    "dp2": "dp2",
    "usb_dp": "dp",
    "dm": "dm",
    "dm1": "dm1",
    "dm2": "dm2",
    "usb_dm": "dm",
    "xtal1": "xtal_in",
    "xin": "xtal_in",
    "xi": "xtal_in",
    "osc_in": "xtal_in",
    "xtal_in": "xtal_in",
    "xtal2": "xtal_out",
    "xout": "xtal_out",
    "xo": "xtal_out",
    "osc_out": "xtal_out",
    "xtal_out": "xtal_out",
    "vcc": "vcc",
    "vdd": "vdd",
    "gnd": "gnd",
}


def normalize_pin_role_name(name: str) -> str | None:
    """Return the canonical role name for a raw role/pin label."""
    raw = str(name or "").strip().lower()
    if not raw:
        return None
    if raw.startswith("pin_"):
        raw = raw[4:]
    raw = raw.replace("+", "p").replace("-", "m")
    raw = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return _PIN_ROLE_ALIASES.get(raw)


def normalize_pin_roles(raw_roles: dict | None) -> dict[str, str]:
    """Normalize a raw pin-role mapping to canonical role -> pin-number."""
    out: dict[str, str] = {}
    if not isinstance(raw_roles, dict):
        return out
    for raw_role, raw_pin in raw_roles.items():
        role = normalize_pin_role_name(str(raw_role or ""))
        pin = str(raw_pin or "").strip()
        if role and pin and role not in out:
            out[role] = pin
    return out


def infer_pin_roles_from_pins(pins: list["PinDef"]) -> dict[str, str]:
    """Infer canonical interface roles from pin names when metadata is absent."""
    out: dict[str, str] = {}
    for pin in pins or []:
        role = normalize_pin_role_name(pin.name)
        if role and pin.number and role not in out:
            out[role] = str(pin.number)
    return out


@dataclass
class BypassCap:
    """A bypass/decoupling capacitor required by an IC."""

    pin: str  # pin number or power net name this cap bypasses
    net: str  # power net (e.g. "VDD_3P3")
    gnd_net: str  # ground net (e.g. "GND")
    value: str  # "100nF", "10uF"
    footprint: str  # e.g. FP_0402C
    role: str = "decoupling"
    presentation: str = "topology_local"
    selection_policy: str | None = None
    confidence: str | None = None
    calculation_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    withheld_finding_id: str | None = None
    eligibility: str = "eligible"

    def __post_init__(self) -> None:
        if self.role and not _ROLE_RE.match(self.role):
            raise ValueError(f"Invalid BypassCap role: {self.role!r} (must be lowercase identifier)")
        _validate_passive_traceability(self)


@dataclass
class StrapConfig:
    """A pull-up or pull-down strap resistor."""

    pin: str  # pin number
    net: str  # signal net name
    rail: str  # pull to this rail ("VDD_3P3" or "GND")
    value: str  # "10k", "4.7k"
    footprint: str
    role: str = "strap"
    presentation: str = "topology_local"
    selection_policy: str | None = None
    confidence: str | None = None
    calculation_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    withheld_finding_id: str | None = None
    eligibility: str = "eligible"

    def __post_init__(self) -> None:
        if self.role and not _ROLE_RE.match(self.role):
            raise ValueError(f"Invalid StrapConfig role: {self.role!r} (must be lowercase identifier)")
        _validate_passive_traceability(self)


@dataclass
class PowerReq:
    """A component-level power operating envelope.

    ``voltage`` and ``max_current_ma`` remain compatibility aliases for the
    historic three positional arguments. ``None`` means the source did not
    state a value; it is deliberately distinct from zero.
    """

    net: str  # "VDD_3P3", "VCCINT"
    voltage: float | None = None  # legacy alias for v_nominal
    max_current_ma: float | None = None  # legacy alias for i_peak_ma
    v_min: float | None = None
    v_nominal: float | None = None
    v_max: float | None = None
    direction: str | None = None
    i_peak_ma: float | None = None
    i_steady_ma: float | None = None
    sequence_order: int | None = None
    sequence_dependency: str | None = None
    tolerance: float | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if self.v_nominal is None and self.voltage is not None:
            self.v_nominal = self.voltage
        elif self.voltage is None and self.v_nominal is not None:
            self.voltage = self.v_nominal
        if self.i_peak_ma is None and self.max_current_ma is not None:
            self.i_peak_ma = self.max_current_ma
        elif self.max_current_ma is None and self.i_peak_ma is not None:
            self.max_current_ma = self.i_peak_ma
        _validate_power_envelope(self)


@dataclass
class PowerPin:
    """A typed power-pin connection and its declared operating envelope."""

    pin: str
    net: str
    v_min: float | None = None
    v_nominal: float | None = None
    v_max: float | None = None
    direction: str | None = None
    i_peak_ma: float | None = None
    i_steady_ma: float | None = None
    sequence_order: int | None = None
    sequence_dependency: str | None = None
    tolerance: float | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        _validate_power_envelope(self)


@dataclass(frozen=True)
class PassiveRecommendation:
    """A normalized support-passive recommendation with explicit authority.

    Values are SI quantities.  A recommendation carries either one selected
    value plus unit, or a bounded range plus unit.  Fallback bounds are always
    explicit so generic values cannot silently masquerade as datasheet facts.
    """

    family: str
    role: str
    precedence_policy: str
    confidence: str
    provenance: str | None = None
    evidence_id: str | None = None
    value: float | None = None
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    fallback_min: float | None = None
    fallback_max: float | None = None
    net: str | None = None
    count: int = 1
    gnd_net: str | None = None
    footprint: str | None = None

    def __post_init__(self) -> None:
        if self.family not in _PASSIVE_RECOMMENDATION_FAMILIES:
            raise ValueError(f"unknown passive recommendation family: {self.family!r}")
        if not self.role or not _ROLE_RE.fullmatch(self.role):
            raise ValueError("passive recommendation role must be a lowercase identifier")
        if self.precedence_policy not in _PASSIVE_RECOMMENDATION_POLICIES:
            raise ValueError(f"unknown recommendation precedence policy: {self.precedence_policy!r}")
        if self.confidence not in _PASSIVE_RECOMMENDATION_CONFIDENCES:
            raise ValueError(f"unknown recommendation confidence: {self.confidence!r}")
        if self.unit not in _PASSIVE_RECOMMENDATION_UNITS[self.family]:
            allowed_units = ", ".join(sorted(_PASSIVE_RECOMMENDATION_UNITS[self.family]))
            raise ValueError(f"unsupported unit {self.unit!r} for {self.family}; expected one of: {allowed_units}")
        if self.precedence_policy == "bounded_fallback" and self.confidence != "heuristic":
            raise ValueError("bounded fallback recommendations must be heuristic")
        has_value = self.value is not None
        has_bounds = self.min_value is not None or self.max_value is not None
        if has_value == has_bounds:
            raise ValueError("recommendation requires either value+unit or min/max bounds+unit")
        for field_name in ("value", "min_value", "max_value", "fallback_min", "fallback_max"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            ):
                raise ValueError(f"recommendation {field_name} must be a finite number")
        if has_value and self.value <= 0:
            raise ValueError("recommendation value must be positive")
        if has_bounds:
            if (
                self.min_value is None
                or self.max_value is None
                or self.min_value <= 0
                or self.min_value > self.max_value
            ):
                raise ValueError("recommendation bounds must be positive and ordered")
        if (self.fallback_min is None) != (self.fallback_max is None):
            raise ValueError("fallback bounds require both minimum and maximum")
        if self.fallback_min is not None and (self.fallback_min <= 0 or self.fallback_min > self.fallback_max):
            raise ValueError("fallback bounds must be positive and ordered")
        if self.precedence_policy == "bounded_fallback" and self.fallback_min is None:
            raise ValueError("bounded fallback recommendations require declared fallback bounds")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise ValueError("recommendation count must be a positive integer")
        provenance = self.provenance.strip() if isinstance(self.provenance, str) else ""
        evidence_id = self.evidence_id.strip() if isinstance(self.evidence_id, str) else ""
        if self.provenance is not None and not provenance:
            raise ValueError("recommendation provenance must be non-empty text")
        if provenance:
            validate_evidence_text(provenance)
        if self.evidence_id is not None and not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            raise ValueError("recommendation evidence_id is malformed")
        if self.precedence_policy == "datasheet" and not (provenance or evidence_id):
            raise ValueError("datasheet recommendations require source provenance or evidence_id")
        if self.precedence_policy == "equation" and (
            not evidence_id or not evidence_id.upper().startswith("EV-CALCULATION-")
        ):
            raise ValueError("equation recommendations require a calculation evidence_id")
        if self.precedence_policy == "bounded_fallback" and not (provenance or evidence_id):
            raise ValueError("bounded fallback recommendations require declared provenance or evidence_id")


@dataclass(frozen=True)
class RecommendationSelection:
    """Deterministic selection result for one passive recommendation key."""

    outcome: str
    family: str
    role: str
    net: str | None
    recommendation: PassiveRecommendation | None = None
    detail: str = ""

    @property
    def selected(self) -> bool:
        return self.outcome == "selected" and self.recommendation is not None


def _validate_passive_traceability(passive: BypassCap | StrapConfig) -> None:
    """Validate optional provenance carried by emitted support passives."""
    if passive.selection_policy is not None and passive.selection_policy not in _PASSIVE_RECOMMENDATION_POLICIES:
        raise ValueError(f"unknown passive selection policy: {passive.selection_policy!r}")
    if passive.confidence is not None and passive.confidence not in _PASSIVE_RECOMMENDATION_CONFIDENCES:
        raise ValueError(f"unknown passive confidence: {passive.confidence!r}")
    if passive.calculation_id is not None:
        if not isinstance(passive.calculation_id, str) or not _CALCULATION_ID_RE.fullmatch(passive.calculation_id):
            raise ValueError("calculation_id is malformed")
        validate_evidence_text(passive.calculation_id)
    evidence_ids = tuple(passive.evidence_ids)
    if any(not isinstance(item, str) or not EVIDENCE_ID_PATTERN.fullmatch(item) for item in evidence_ids):
        raise ValueError("evidence_ids must contain valid evidence IDs")
    passive.evidence_ids = evidence_ids
    if passive.withheld_finding_id is not None:
        if not isinstance(passive.withheld_finding_id, str) or not _WITHHELD_FINDING_ID_RE.fullmatch(
            passive.withheld_finding_id
        ):
            raise ValueError("withheld_finding_id is malformed")
        validate_evidence_text(passive.withheld_finding_id)
        if passive.value:
            raise ValueError("an emitted passive value cannot be marked withheld")
    if passive.eligibility not in _PASSIVE_ELIGIBILITY:
        raise ValueError(f"unknown passive eligibility: {passive.eligibility!r}")
    if passive.eligibility == "withheld" and not passive.withheld_finding_id:
        raise ValueError("withheld eligibility requires withheld_finding_id")
    if passive.withheld_finding_id and passive.eligibility != "withheld":
        raise ValueError("withheld_finding_id requires withheld eligibility")
    trace_declared = any(
        (
            passive.selection_policy is not None,
            passive.confidence is not None,
            passive.calculation_id is not None,
            bool(passive.evidence_ids),
            passive.eligibility != "eligible",
        )
    )
    if trace_declared and (
        passive.selection_policy is None or passive.confidence is None or passive.calculation_id is None
    ):
        raise ValueError("passive traceability requires policy, confidence, and calculation_id together")
    if passive.selection_policy == "bounded_fallback" and passive.confidence != "heuristic":
        raise ValueError("bounded fallback passive traceability must remain heuristic")
    if trace_declared and passive.eligibility == "eligible" and not passive.evidence_ids:
        raise ValueError("an eligible synthesized passive requires emitted evidence_ids")


def _capacitance_value_f(value: object) -> float:
    """Parse the legacy compact capacitance notation into SI farads."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([pnum]?F)\s*", str(value), re.IGNORECASE)
    if not match:
        raise ValueError(f"unsupported legacy capacitance value: {value!r}")
    magnitude, suffix = match.groups()
    scale = {"pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "mf": 1e-3, "f": 1.0}[suffix.lower()]
    return float(magnitude) * scale


def _format_capacitance_value(farads: float) -> str:
    """Format a normalized capacitance without importing the builder layer."""
    if farads >= 1e-3:
        return f"{farads * 1e3:g}mF"
    if farads >= 1e-6:
        return f"{farads * 1e6:g}uF"
    if farads >= 1e-9:
        return f"{farads * 1e9:g}nF"
    return f"{farads * 1e12:g}pF"


def normalize_passive_recommendation(
    recommendation: PassiveRecommendation | dict,
    *,
    default_provenance: str | None = None,
) -> PassiveRecommendation:
    """Normalize typed or legacy recommendation input at the model boundary."""
    if isinstance(recommendation, PassiveRecommendation):
        return recommendation
    if not isinstance(recommendation, dict):
        raise TypeError("passive recommendation must be a mapping or PassiveRecommendation")
    raw_value = recommendation.get("value")
    unit = recommendation.get("unit")
    if unit is None and raw_value is not None:
        value = _capacitance_value_f(raw_value)
        unit = "F"
    else:
        value = raw_value
    policy = str(recommendation.get("precedence_policy") or recommendation.get("policy") or "datasheet")
    return PassiveRecommendation(
        family=str(recommendation.get("family") or "regulator_io_cap"),
        role=str(recommendation.get("role") or "decoupling"),
        precedence_policy=policy,
        confidence=str(
            recommendation.get("confidence") or ("heuristic" if policy == "bounded_fallback" else "single_source")
        ),
        provenance=(
            str(recommendation["provenance"]).strip()
            if recommendation.get("provenance")
            else (str(default_provenance).strip() if default_provenance else None)
        ),
        evidence_id=(str(recommendation["evidence_id"]) if recommendation.get("evidence_id") else None),
        value=value,
        unit=str(unit) if unit else None,
        min_value=recommendation.get("min_value"),
        max_value=recommendation.get("max_value"),
        fallback_min=recommendation.get("fallback_min"),
        fallback_max=recommendation.get("fallback_max"),
        net=(str(recommendation["net"]) if recommendation.get("net") else None),
        count=recommendation.get("count", 1),
        gnd_net=(str(recommendation["gnd_net"]) if recommendation.get("gnd_net") else None),
        footprint=(str(recommendation["footprint"]) if recommendation.get("footprint") else None),
    )


def _recommendation_configuration(recommendation: PassiveRecommendation) -> tuple[object, ...]:
    """Fields that change a selected recommendation, excluding authority metadata."""
    return (
        recommendation.family,
        recommendation.role,
        recommendation.precedence_policy,
        recommendation.value,
        recommendation.unit,
        recommendation.min_value,
        recommendation.max_value,
        recommendation.fallback_min,
        recommendation.fallback_max,
        recommendation.net,
        recommendation.count,
        recommendation.gnd_net,
        recommendation.footprint,
    )


def _recommendation_within_declared_bounds(recommendation: PassiveRecommendation) -> bool:
    """Return whether selected data remains within any declared fallback range."""
    if recommendation.fallback_min is None:
        return True
    if recommendation.value is not None:
        return recommendation.fallback_min <= recommendation.value <= recommendation.fallback_max
    return (
        recommendation.min_value is not None
        and recommendation.max_value is not None
        and recommendation.fallback_min
        <= recommendation.min_value
        <= recommendation.max_value
        <= recommendation.fallback_max
    )


def select_passive_recommendation(
    recommendations: list[PassiveRecommendation], *, family: str, role: str, net: str | None = None
) -> RecommendationSelection:
    """Select one recommendation without depending on source-list order.

    A requested net first considers exact-net records, then global records.
    Omitted nets select only global records.  Multiple distinct records at the
    winning policy tier are surfaced as a conflict rather than arbitrarily
    selecting one.
    """
    candidates = [item for item in recommendations if item.family == family and item.role == role]
    if net is not None:
        exact = [item for item in candidates if item.net == net]
        candidates = exact or [item for item in candidates if item.net is None]
    else:
        candidates = [item for item in candidates if item.net is None]
    for policy in _RECOMMENDATION_PRECEDENCE:
        tier = [item for item in candidates if item.precedence_policy == policy]
        if not tier:
            continue
        configurations = {_recommendation_configuration(item) for item in tier}
        if len(configurations) != 1:
            return RecommendationSelection(
                "conflict", family, role, net, detail=f"conflicting {policy} recommendations"
            )
        chosen = min(tier, key=lambda item: (item.evidence_id or "", item.provenance or ""))
        if not _recommendation_within_declared_bounds(chosen):
            return RecommendationSelection(
                "out_of_bounds", family, role, net, detail="recommendation is outside declared fallback bounds"
            )
        return RecommendationSelection("selected", family, role, net, recommendation=chosen)
    return RecommendationSelection("missing", family, role, net, detail="no recommendation for key")


def _validate_power_envelope(envelope: object) -> None:
    """Reject contradictory typed power values while allowing unknowns."""
    direction = getattr(envelope, "direction", None)
    if direction is not None:
        normalized = str(direction).strip().lower()
        if normalized not in _POWER_DIRECTIONS:
            valid = ", ".join(sorted(_POWER_DIRECTIONS))
            raise ValueError(f"Unknown power direction '{direction}'. Expected one of: {valid}")
        setattr(envelope, "direction", normalized)
    v_min, v_nominal, v_max = (getattr(envelope, key, None) for key in ("v_min", "v_nominal", "v_max"))
    if v_min is not None and v_max is not None and v_min > v_max:
        raise ValueError("Power envelope v_min cannot exceed v_max")
    if v_min is not None and v_nominal is not None and v_nominal < v_min:
        raise ValueError("Power envelope v_nominal cannot be below v_min")
    if v_max is not None and v_nominal is not None and v_nominal > v_max:
        raise ValueError("Power envelope v_nominal cannot exceed v_max")
    for key in ("i_peak_ma", "i_steady_ma", "max_current_ma", "tolerance", "sequence_order"):
        value = getattr(envelope, key, None)
        if value is not None and value < 0:
            raise ValueError(f"Power envelope {key} cannot be negative")


@dataclass
class ComponentDef:
    """Complete definition of a component for schematic generation.

    Contains everything needed to:
    1. Create the KiCad symbol (pins)
    2. Place it on the correct sheet (category)
    3. Wire it correctly (pin_nets)
    4. Generate supporting passives (bypass_caps, straps)
    """

    mpn: str  # "ESP32-WROOM-32E"
    ref_prefix: str = "U"  # "U", "J", "D", "R", "C", "L"
    value: str = ""  # display value
    footprint: str = ""  # KiCad footprint string
    description: str = ""
    category: str = "digital"  # power, digital, rf, connector, sensor, storage, debug, passive
    source_ref: str = ""  # BOM/design source reference for this specific instance
    source_mpn: str = ""  # original BOM MPN before registry aliasing
    source_value: str = ""  # original BOM value/comment field
    source_description: str = ""  # original BOM description
    source_manufacturer: str = ""  # original BOM manufacturer
    lcsc_pn: str = ""  # LCSC part number (e.g. "C14663") for JLCPCB assembly
    digikey_pn: str = ""  # DigiKey part number for prototype ordering
    features: list[str] = field(default_factory=list)  # optional feature summary tokens
    annotations: list[str] = field(default_factory=list)  # design rationale text near IC
    template_annotations: list[str] = field(default_factory=list)  # sheet-level notes from a template
    template_boundary_ports: list = field(default_factory=list)  # BoundaryPort-like objects from template
    template_local_wires: list = field(default_factory=list)  # LocalWire-like objects from template
    presentation_group: str = ""  # optional review-sheet partition grouping
    # Canonical design ownership metadata.  ``functional_section`` preserves
    # the user's architectural section (power, sensing, communications, ...)
    # through component resolution so the allocator can build professional
    # functional sheets even for small designs.  ``block_id`` keeps generated
    # support parts traceable to the originating design block.
    functional_section: str = ""
    block_id: str = ""
    presentation_wiring_policy: PresentationWiringPolicy | None = None  # optional component-level rendering override

    pins: list[PinDef] = field(default_factory=list)
    pin_nets: dict = field(default_factory=dict)  # {pin_num: net_name} — signal connections
    power_pins: dict = field(default_factory=dict)  # {pin_num: power_net} — power connections
    # Typed metadata complements the legacy mapping so old templates and
    # schematic rendering continue to receive the string-only pin map.
    power_pin_defs: list[PowerPin] = field(default_factory=list)
    pin_roles: dict[str, str] = field(default_factory=dict)  # normalized role -> pin number

    power_reqs: list[PowerReq] = field(default_factory=list)
    bypass_caps: list[BypassCap] = field(default_factory=list)
    straps: list[StrapConfig] = field(default_factory=list)
    recommended_bypass: list[dict] = field(default_factory=list)  # optional datasheet-driven bypass policy
    # Manufacturer-owned evidence surfaced in placement_review_context.json.
    # These fields deliberately live on the component record so custom
    # registries and data-driven topology definitions can provide the same
    # traceable placement guidance as bundled parts without MPN conditionals
    # in the optimizer.
    datasheet_url: str = ""
    reference_layout_url: str = ""
    official_references: list[dict[str, str]] = field(default_factory=list)
    # Regulator input-output headroom requirement, when a source explicitly
    # supplies it.  Unknown dropout stays None rather than a guessed value.
    dropout_voltage: float | None = None

    # Pin numbers intentionally left unconnected (no-connect by design).
    # The generator will place NC markers on these pins without warnings.
    # Pins NOT in this set and not in pin_nets/power_pins are flagged
    # according to their electrical type (error for power_in, warning for input).
    explicit_no_connects: set = field(default_factory=set)

    # Pins that a generic/data-driven builder knows must be routed through
    # an explicit interface or per-part support network. Generation hard-fails
    # if these reach schematic rendering still unmapped.
    unmapped_required_pins: dict[str, str] = field(default_factory=dict)

    # For BGA ICs: callable that returns {ball: net} mapping
    pin_map_builder: object = None

    # Pre-built KiCad symbol S-expression (for custom library symbols).
    # When set, the engine embeds this instead of generating a generic box symbol.
    lib_symbol_sexpr: str = ""

    # Pinout provenance — used by the validator to gate schematic output.
    # "explicit"      — pin_map supplied in YAML spec or from KiCad library (trusted)
    # "kicad_library" — symbol resolved from installed KiCad symbol library (trusted)
    # "stub"          — generated from distributor package data only; pin assignments
    #                   are 1=pin1 … N=pinN placeholders and MUST NOT be routed.
    pinout_source: str = "explicit"

    # Set to True in YAML (pinout_verified: true) to acknowledge a stub pinout
    # has been manually confirmed against the datasheet.  Suppresses the
    # unverified-pinout validator error without requiring a full pin_map entry.
    pinout_verified: bool = False

    # Normalized support-passive contract. ``recommended_bypass`` remains an
    # ingest compatibility field; consumers must use this typed list.
    passive_recommendations: list[PassiveRecommendation] = field(default_factory=list)
    feedback_vref_voltage: float | None = None
    feedback_vref_provenance: str | None = None
    feedback_vref_evidence_id: str | None = None
    # Immutable calc/evidence records produced while synthesizing support
    # passives.  These are retained on the component so a later validation
    # ledger can merge the exact producer records rather than recreating IDs.
    passive_synthesis_calculations: list[object] = field(default_factory=list)
    passive_synthesis_findings: list[object] = field(default_factory=list)
    passive_synthesis_evidence: list[object] = field(default_factory=list)

    def __post_init__(self) -> None:
        provenance = self.datasheet_url or None
        normalized = [
            normalize_passive_recommendation(item, default_provenance=provenance)
            for item in self.passive_recommendations
        ]
        normalized.extend(
            normalize_passive_recommendation(item, default_provenance=provenance) for item in self.recommended_bypass
        )
        seen: set[PassiveRecommendation] = set()
        self.passive_recommendations = [item for item in normalized if not (item in seen or seen.add(item))]
        if self.feedback_vref_voltage is not None:
            if self.feedback_vref_voltage <= 0:
                raise ValueError("feedback_vref_voltage must be positive")
            if not self.feedback_vref_provenance and not self.feedback_vref_evidence_id:
                raise ValueError("feedback Vref requires provenance or evidence_id")
        _dedupe_passive_synthesis_records(self)

    def select_passive_recommendation(self, family: str, role: str, net: str | None = None) -> RecommendationSelection:
        """Return the explicit deterministic outcome for one recommendation key."""
        return select_passive_recommendation(self.passive_recommendations, family=family, role=role, net=net)

    def pin_tuples(self):
        """Return pins as list of (number, name, type, side) tuples for create_generic_symbol."""
        return [p.as_tuple() for p in self.pins]

    def all_signal_nets(self):
        """All signal net names this component connects to."""
        return set(self.pin_nets.values())

    def all_power_nets(self):
        """All power net names this component needs."""
        return set(self.power_pins.values()) | {r.net for r in self.power_reqs}

    def typed_power_pins(self) -> list[PowerPin]:
        """Return typed pins without inventing legacy electrical limits."""
        explicit = {str(pin.pin): pin for pin in self.power_pin_defs}
        mapped = [
            explicit.get(str(pin_num), PowerPin(pin=str(pin_num), net=str(net)))
            for pin_num, net in self.power_pins.items()
        ]
        mapped_pins = {str(pin_num) for pin_num in self.power_pins}
        return mapped + [pin for pin_num, pin in explicit.items() if pin_num not in mapped_pins]

    def resolved_pin_roles(self) -> dict[str, str]:
        """Normalized role mapping, using explicit metadata plus pin-name inference."""
        roles = normalize_pin_roles(self.pin_roles)
        for role, pin_num in infer_pin_roles_from_pins(self.pins).items():
            roles.setdefault(role, pin_num)
        return roles

    def prefer_multi_column_symbol(self) -> bool:
        """Whether this component should use a multi-column generic symbol.

        Dense left/right-only symbols benefit from review-oriented generic
        rendering once a single face becomes tall enough to read as a label
        wall. This is intentionally broader than the original >100-pin BGA
        case so medium-large imported memory/interface symbols can still use
        the generic multi-column path.
        """
        if any(pin.side in ("T", "B") for pin in self.pins):
            return False

        return self.preferred_symbol_column_segments() is not None

    def preferred_symbol_column_segments(self) -> int | None:
        """Preferred review-time generic symbol columns for dense L/R symbols."""
        if any(pin.side in ("T", "B") for pin in self.pins):
            return None

        max_side = max(
            sum(1 for pin in self.pins if pin.side == "L"),
            sum(1 for pin in self.pins if pin.side == "R"),
        )
        if max_side < 44:
            return None

        if max_side >= 180:
            return 4
        if max_side >= 100:
            return 3
        return 2

    def preferred_symbol_pin_pitch_mm(self) -> float | None:
        """Preferred review-time pin pitch for readability-sensitive symbols."""
        max_side = max(
            [
                sum(1 for pin in self.pins if pin.side == "L"),
                sum(1 for pin in self.pins if pin.side == "R"),
                sum(1 for pin in self.pins if pin.side == "T"),
                sum(1 for pin in self.pins if pin.side == "B"),
            ],
            default=0,
        )
        max_name_len = max((len(pin.name or "") for pin in self.pins), default=0)
        columns = self.preferred_symbol_column_segments() or 1

        if columns > 1 and max_side >= 20:
            return 5.08

        if columns == 1 and self.category in {"usb", "power", "fpga"} and max_side >= 3 and max_name_len >= 10:
            return 5.08

        return None


def _compact_text(text: str, limit: int = 88) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def component_purpose_summary(comp: ComponentDef) -> str:
    """Return a short reader-facing purpose summary for a component."""
    desc = " ".join(
        part
        for part in (
            comp.source_description,
            comp.description,
        )
        if part
    ).strip()
    desc_l = desc.lower()

    keyword_purposes = (
        (("usb pd", "type-c pd", "sink controller"), "USB-C power negotiation and sink control"),
        (("buck", "sync buck", "regulator"), "Power rail generation and voltage regulation"),
        (("ldo", "low-noise"), "Quiet local rail regulation and analog supply cleanup"),
        (("clock", "pll", "sysref", "jesd"), "Clock synthesis, cleanup, and timing distribution"),
        (("rf transceiver", "2t2r", "6ghz"), "RF transceiver front end and sample-data interface"),
        (("zynq", "arm", "fpga"), "Application processing, FPGA logic, and system control"),
        (("ddr3", "sdram"), "Working memory for the processing subsystem"),
        (("usb 3", "superspeed", "fx3"), "USB data bridge and host interface control"),
        (("usb 2", "hub"), "USB fanout and downstream-port management"),
        (("ethernet", "rgmii", "phy"), "Ethernet physical-layer interface"),
        (("poe",), "Power-over-Ethernet input and front-end conversion"),
        (("flash", "qspi", "nor"), "Boot image storage and nonvolatile configuration"),
        (("mux", "switch"), "Signal path selection and interface switching"),
    )
    for keywords, purpose in keyword_purposes:
        if any(keyword in desc_l for keyword in keywords):
            return purpose

    if desc:
        return _compact_text(desc, 72)
    return _GENERIC_PURPOSE_BY_CATEGORY.get(comp.category, "Functional support within the design")


def component_needs_explanation(comp: ComponentDef) -> bool:
    """Return True when the component should get a review explanation block."""
    if comp.ref_prefix == "U":
        return True
    return comp.category in {"power", "clock", "transceiver", "fpga", "usb", "ethernet", "storage"}


def component_explanation_lines(comp: ComponentDef, ref: str = "") -> list[str]:
    """Return a compact explanation block for schematic review output."""
    if not component_needs_explanation(comp):
        return []

    display_name = comp.source_mpn or comp.value or comp.mpn
    heading_prefix = f"{ref} " if ref else ""
    heading = _compact_text(
        f"{heading_prefix}{display_name}: {component_purpose_summary(comp)}",
        92,
    )

    lines = [heading]
    detail_lines = [_compact_text(line, 92) for line in comp.annotations if line.strip()]

    if not detail_lines:
        fallback: list[str] = []
        if comp.description:
            fallback.append(_compact_text(comp.description, 92))
        if comp.power_reqs:
            rails = ", ".join(sorted({req.net for req in comp.power_reqs if req.net}))
            if rails:
                fallback.append(_compact_text(f"Primary rails: {rails}", 92))
        elif comp.power_pins:
            rails = ", ".join(sorted({net for net in comp.power_pins.values() if net}))
            if rails:
                fallback.append(_compact_text(f"Key rails: {rails}", 92))
        if comp.pin_nets:
            named_nets = sorted({net for net in comp.pin_nets.values() if net})[:4]
            if named_nets:
                fallback.append(_compact_text(f"Interfaces: {', '.join(named_nets)}", 92))
        detail_lines = fallback

    for line in detail_lines[:3]:
        if line and line != heading:
            lines.append(f"- {line}")

    return lines


def _bind_builtin_connector_shield(comp: ComponentDef) -> ComponentDef:
    """Use the installed USB4085 library's explicit unused shield pin name.

    KiCad 8/9 call the four shield pads S1; newer libraries call them SH.
    Resolve that known library spelling before schematic generation, while
    leaving connected pins and unknown footprint variants unchanged. The PCB
    importer still requires exact symbol-pin/physical-pad reconciliation.
    """
    if (
        comp.mpn != "USB-C-PWR"
        or comp.footprint != "Connector_USB:USB_C_Receptacle_GCT_USB4085"
        or "SH" not in comp.explicit_no_connects
        or "SH" in comp.pin_nets
        or "SH" in comp.power_pins
    ):
        return comp
    from .footprint_lib import KiCadFootprintLibrary

    path = KiCadFootprintLibrary().resolve(comp.footprint)
    if path is None:
        return comp
    numbers = {
        quoted if quoted else unquoted
        for quoted, unquoted in re.findall(r'\(pad\s+(?:"([^"]*)"|([^\s()]+))', path.read_text(encoding="utf-8"))
        if quoted or unquoted
    }
    symbol_numbers = {pin.number for pin in comp.pins}
    if "SH" not in symbol_numbers or numbers != (symbol_numbers - {"SH"}) | {"S1"}:
        return comp

    import copy

    bound = copy.deepcopy(comp)
    for pin in bound.pins:
        if pin.number == "SH":
            pin.number = "S1"
    bound.explicit_no_connects = (bound.explicit_no_connects - {"SH"}) | {"S1"}
    return bound


class ComponentRegistry:
    """Registry of known components, queryable by MPN."""

    def __init__(self):
        self._components = {}  # mpn -> ComponentDef
        self._aliases = {}  # alias_mpn -> canonical_mpn

    def register(self, comp: ComponentDef):
        self._components[comp.mpn] = comp

    def add_alias(self, alias: str, canonical: str):
        """Map an alias MPN to a canonical MPN (e.g. BOM suffix variants)."""
        self._aliases[alias] = canonical

    def get(self, mpn: str) -> ComponentDef | None:
        import re as _re

        comp = self._components.get(mpn)
        if comp:
            return _bind_builtin_connector_shield(comp)
        canonical = self._aliases.get(mpn)
        if canonical:
            comp = self._components.get(canonical)
            return _bind_builtin_connector_shield(comp) if comp else None
        stripped = _re.sub(r"[-/:](?:7|ND|P|TR|REEL7?|NOPB|CT)$", "", mpn)
        if stripped != mpn:
            comp = self._components.get(stripped)
            return _bind_builtin_connector_shield(comp) if comp else None
        return None

    def find_by_category(self, category: str) -> list[ComponentDef]:
        return [_bind_builtin_connector_shield(c) for c in self._components.values() if c.category == category]

    def all_mpns(self):
        return list(self._components.keys())

    def __len__(self):
        return len(self._components)

    def load_json(self, path: str) -> int:
        """Load component definitions from a JSON file.

        The file should contain a list of objects, each with at least ``mpn``
        and ``pins`` fields matching the :class:`ComponentDef` schema.  Returns
        the number of components loaded.

        Example JSON::

            [
              {
                "mpn": "MY_IC",
                "ref_prefix": "U",
                "value": "MY_IC",
                "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                "description": "Custom IC",
                "category": "analog",
                "pins": [
                  {"number": "1", "name": "IN", "electrical_type": "input", "side": "L"},
                  {"number": "2", "name": "GND", "electrical_type": "power_in", "side": "B"}
                ],
                "power_pins": {"2": "GND"},
                "bypass_caps": [
                  {"pin": "auto", "net": "VDD", "gnd_net": "GND", "value": "100nF",
                   "footprint": "Capacitor_SMD:C_0402_1005Metric"}
                ]
              }
            ]
        """
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = [data]
        count = 0
        for entry in data:
            if not isinstance(entry, dict) or "mpn" not in entry:
                continue
            pins = [
                PinDef(
                    number=str(p.get("number", "")),
                    name=str(p.get("name", "~")),
                    electrical_type=str(p.get("electrical_type", "passive")),
                    side=str(p.get("side", "L")),
                )
                for p in entry.get("pins", [])
            ]
            caps = [
                BypassCap(
                    pin=str(c.get("pin", "auto")),
                    net=str(c.get("net", "")),
                    gnd_net=str(c.get("gnd_net", "GND")),
                    value=str(c.get("value", "100nF")),
                    footprint=str(c.get("footprint", "Capacitor_SMD:C_0402_1005Metric")),
                    role=str(c.get("role", "decoupling")),
                    presentation=str(c.get("presentation", "topology_local")),
                )
                for c in entry.get("bypass_caps", [])
            ]
            straps = [
                StrapConfig(
                    pin=str(s.get("pin", "")),
                    net=str(s.get("net", "")),
                    rail=str(s.get("rail", "GND")),
                    value=str(s.get("value", "10k")),
                    footprint=str(s.get("footprint", "Resistor_SMD:R_0402_1005Metric")),
                    role=str(s.get("role", "strap")),
                    presentation=str(s.get("presentation", "topology_local")),
                )
                for s in entry.get("straps", [])
            ]
            power_reqs = [
                PowerReq(
                    net=str(req.get("net", "")),
                    voltage=req.get("voltage"),
                    max_current_ma=req.get("max_current_ma"),
                    v_min=req.get("v_min"),
                    v_nominal=req.get("v_nominal"),
                    v_max=req.get("v_max"),
                    direction=req.get("direction"),
                    i_peak_ma=req.get("i_peak_ma"),
                    i_steady_ma=req.get("i_steady_ma"),
                    sequence_order=req.get("sequence_order"),
                    sequence_dependency=req.get("sequence_dependency"),
                    tolerance=req.get("tolerance"),
                    evidence_id=req.get("evidence_id"),
                )
                for req in entry.get("power_reqs", [])
                if isinstance(req, dict)
            ]
            power_pin_defs = [
                PowerPin(
                    pin=str(pin.get("pin", "")),
                    net=str(pin.get("net", "")),
                    v_min=pin.get("v_min"),
                    v_nominal=pin.get("v_nominal"),
                    v_max=pin.get("v_max"),
                    direction=pin.get("direction"),
                    i_peak_ma=pin.get("i_peak_ma"),
                    i_steady_ma=pin.get("i_steady_ma"),
                    sequence_order=pin.get("sequence_order"),
                    sequence_dependency=pin.get("sequence_dependency"),
                    tolerance=pin.get("tolerance"),
                    evidence_id=pin.get("evidence_id"),
                )
                for pin in entry.get("power_pin_defs", [])
                if isinstance(pin, dict)
            ]
            feedback_vref_provenance = entry.get("feedback_vref_provenance", entry.get("vref_provenance"))
            feedback_vref_evidence_id = entry.get("feedback_vref_evidence_id")
            feedback_vref_voltage = entry.get("feedback_vref_voltage", entry.get("vref"))
            comp = ComponentDef(
                mpn=str(entry["mpn"]),
                ref_prefix=str(entry.get("ref_prefix", "U")),
                value=str(entry.get("value", entry["mpn"])),
                footprint=str(entry.get("footprint", "")),
                description=str(entry.get("description", "")),
                category=str(entry.get("category", "digital")),
                pins=pins,
                pin_nets={str(k): str(v) for k, v in entry.get("pin_nets", {}).items()},
                power_pins={str(k): str(v) for k, v in entry.get("power_pins", {}).items()},
                power_pin_defs=power_pin_defs,
                pin_roles=normalize_pin_roles(entry.get("pin_roles", {})),
                power_reqs=power_reqs,
                recommended_bypass=list(entry.get("recommended_bypass", []) or []),
                passive_recommendations=list(entry.get("passive_recommendations", []) or []),
                bypass_caps=caps,
                straps=straps,
                datasheet_url=str(entry.get("datasheet_url", "")),
                reference_layout_url=str(entry.get("reference_layout_url", "")),
                official_references=[
                    {str(key): str(value) for key, value in item.items()}
                    for item in entry.get("official_references", [])
                    if isinstance(item, dict)
                ],
                dropout_voltage=entry.get("dropout_voltage"),
                feedback_vref_voltage=(
                    feedback_vref_voltage if (feedback_vref_provenance or feedback_vref_evidence_id) else None
                ),
                feedback_vref_provenance=feedback_vref_provenance,
                feedback_vref_evidence_id=feedback_vref_evidence_id,
            )
            self.register(comp)
            count += 1
        return count

    def load_json_dir(self, directory: str) -> int:
        """Load all ``*.json`` component files from a directory."""
        from pathlib import Path

        total = 0
        d = Path(directory)
        if not d.is_dir():
            return 0
        for f in sorted(d.glob("*.json")):
            total += self.load_json(str(f))
        return total


@dataclass
class BomRow:
    """A single parsed BOM line item."""

    ref: str  # "U1", "C3", "R7"
    mpn: str = ""  # manufacturer part number
    value: str = ""  # "100nF", "10k", "ESP32-WROOM-32E"
    footprint: str = ""  # KiCad footprint string
    description: str = ""
    quantity: int = 1
    manufacturer: str = ""
    supplier_pn: str = ""  # DigiKey/LCSC/Mouser PN


# Column name aliases for auto-detection (lowercase → canonical)
_COLUMN_ALIASES = {
    # Reference
    "reference": "ref",
    "ref": "ref",
    "designator": "ref",
    "refdes": "ref",
    "ref des": "ref",
    "part reference": "ref",
    # MPN
    "mpn": "mpn",
    "part number": "mpn",
    "part": "mpn",
    "mfg part": "mpn",
    "manufacturer part": "mpn",
    "mfr. part #": "mpn",
    "mfg part #": "mpn",
    "manufacturer part number": "mpn",
    "mfg p/n": "mpn",
    # Value
    "value": "value",
    "val": "value",
    "comment": "value",
    # Footprint
    "footprint": "footprint",
    "package": "footprint",
    "case/package": "footprint",
    "case": "footprint",
    "pcb footprint": "footprint",
    # Description
    "description": "description",
    "desc": "description",
    "part description": "description",
    # Quantity
    "quantity": "quantity",
    "qty": "quantity",
    "qty.": "quantity",
    "count": "quantity",
    "qty_sdr": "quantity",
    "qty_mini": "quantity",
    # Manufacturer
    "manufacturer": "manufacturer",
    "mfg": "manufacturer",
    "mfr": "manufacturer",
    "mfr.": "manufacturer",
    # Supplier PN
    "digikey_pn": "supplier_pn",
    "digi-key part number": "supplier_pn",
    "lcsc_pn": "supplier_pn",
    "lcsc part": "supplier_pn",
    "lcsc part #": "supplier_pn",
    "mouser_pn": "supplier_pn",
    "mouser part number": "supplier_pn",
    "supplier part number": "supplier_pn",
    # LCSC-specific
    "mfr part": "mpn",
    "mfr part #": "mpn",
    "mfr. part": "mpn",
}


def _detect_delimiter(first_line: str) -> str:
    """Auto-detect CSV delimiter (comma, tab, semicolon, pipe)."""
    counts = {
        ",": first_line.count(","),
        "\t": first_line.count("\t"),
        ";": first_line.count(";"),
        "|": first_line.count("|"),
    }
    return max(counts, key=counts.get) if max(counts.values()) > 0 else ","


def _normalize_columns(header_row: list[str]) -> dict[int, str]:
    """Map column indices to canonical field names."""
    mapping = {}
    for i, col in enumerate(header_row):
        # Aggressively normalize: lowercase, strip whitespace, #, trailing .
        normalized = col.strip().lower().rstrip("#").rstrip(".").rstrip("#").strip()
        canonical = _COLUMN_ALIASES.get(normalized)
        # Also try with trailing # and . included (some aliases have them)
        if not canonical:
            canonical = _COLUMN_ALIASES.get(col.strip().lower())
        if canonical and canonical not in mapping.values():
            mapping[i] = canonical
    return mapping


def _expand_ref_range(token: str) -> list[str]:
    """Expand a single reference token, including simple numeric ranges.

    Examples:
    - R1-R4 -> [R1, R2, R3, R4]
    - C10..C12 -> [C10, C11, C12]
    """
    t = (token or "").strip()
    if not t:
        return []

    # prefix + start + ("-" or "..") + optional prefix + end
    m = re.fullmatch(r"([A-Za-z]+)(\d+)\s*(?:-|\.\.)\s*([A-Za-z]+)?(\d+)", t)
    if not m:
        return [t]

    p1, start_s, p2, end_s = m.groups()
    p2 = p2 or p1
    if p1.upper() != p2.upper():
        return [t]

    start = int(start_s)
    end = int(end_s)
    step = 1 if end >= start else -1
    width = max(len(start_s), len(end_s))
    return [f"{p1}{i:0{width}d}" for i in range(start, end + step, step)]


def _split_refs(ref_field: str) -> list[str]:
    """Split a BOM ref field into individual designators.

    Handles common delimiters like commas/semicolons/whitespace.
    """
    raw = (ref_field or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,\s;]+", raw)
    out = []
    for part in (p.strip() for p in parts if p.strip()):
        out.extend(_expand_ref_range(part))
    return out


def _parse_quantity(qty_str: str) -> tuple[int, bool]:
    """Parse BOM quantity variants like '3', '3.0', '3x', 'x3', '1,000'.

    Returns: (qty, parsed_ok)
    """
    raw = (qty_str or "").strip().lower().replace(",", "")
    if not raw:
        return 1, True

    # Common suffix/prefix notation from distributor exports.
    raw = raw.removesuffix("x").removeprefix("x").strip()

    try:
        qty = int(float(raw))
        return (qty if qty > 0 else 1), True
    except ValueError:
        return 1, False


def parse_bom_csv(csv_path: str) -> list[BomRow]:
    """Parse a BOM CSV/TSV file with auto-detected format.

    Handles:
    - project BOM CSV
    - KiCad BOM export (Reference, Value, Footprint, ...)
    - DigiKey cart export (Digi-Key Part Number, Manufacturer Part Number, ...)
    - LCSC BOM (LCSC Part #, MFR. Part #, Package, ...)
    - Generic CSV/TSV with common column names

    Auto-detects: delimiter (comma/tab/semicolon/pipe), column mapping,
    encoding (UTF-8 with optional BOM).
    """
    import csv
    from pathlib import Path

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"BOM file not found: {csv_path}")

    # Read with BOM-aware encoding
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

    lines = text.splitlines()
    if not lines:
        return []

    # Skip blank/comment lines at the top
    header_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            header_idx = i
            break

    # Detect delimiter
    delimiter = _detect_delimiter(lines[header_idx])

    # Parse header
    reader = csv.reader([lines[header_idx]], delimiter=delimiter)
    header = next(reader)
    col_map = _normalize_columns(header)

    has_ref_column = "ref" in col_map.values()
    if not has_ref_column:
        print("WARNING: BOM has no reference/designator column; using quantity-only expansion mode.")

    # Parse data rows
    rows = []
    data_reader = csv.reader(lines[header_idx + 1 :], delimiter=delimiter)
    for csv_row in data_reader:
        if not csv_row or not any(c.strip() for c in csv_row):
            continue

        # Map columns to fields
        fields = {}
        for col_idx, field_name in col_map.items():
            if col_idx < len(csv_row):
                fields[field_name] = csv_row[col_idx].strip()

        ref = fields.get("ref", "")
        mpn = fields.get("mpn", "")
        value = fields.get("value", "")

        # Skip empty rows
        if not ref and not mpn and not value:
            continue

        # If no MPN, try to use value as MPN (common for KiCad BOMs)
        if not mpn and value:
            mpn = value

        qty_raw = fields.get("quantity", "1")
        qty, qty_ok = _parse_quantity(qty_raw)
        if not qty_ok:
            print(f"WARNING: Could not parse quantity '{qty_raw}' for row ref='{ref}' mpn='{mpn}'; defaulting to 1.")

        refs = _split_refs(ref) if has_ref_column else []

        # If refs are explicitly listed (e.g. "R1,R2,R3"), expand to one row per ref.
        # For a single explicit ref, preserve quantity from the BOM line.
        if len(refs) > 1:
            if qty > 1 and qty != len(refs):
                print(
                    f"WARNING: Ref/Qty mismatch for '{ref}': {len(refs)} refs vs qty={qty}; "
                    "expanding by refs and ignoring qty."
                )
            for ref_item in refs:
                rows.append(
                    BomRow(
                        ref=ref_item,
                        mpn=mpn,
                        value=value,
                        footprint=fields.get("footprint", ""),
                        description=fields.get("description", ""),
                        quantity=1,
                        manufacturer=fields.get("manufacturer", ""),
                        supplier_pn=fields.get("supplier_pn", ""),
                    )
                )
            continue
        elif len(refs) == 1:
            ref = refs[0]
            if qty > 1 and ref:
                print(f"WARNING: Row '{ref}' has quantity={qty} but only 1 ref. Expanding to {qty} instances.")

        rows.append(
            BomRow(
                ref=ref,
                mpn=mpn,
                value=value,
                footprint=fields.get("footprint", ""),
                description=fields.get("description", ""),
                quantity=qty,
                manufacturer=fields.get("manufacturer", ""),
                supplier_pn=fields.get("supplier_pn", ""),
            )
        )

    return rows


# ================================================================
# Generic passive component generator
# ================================================================

# Footprint inference from value string
_PASSIVE_FOOTPRINT_MAP = {
    "0201": {"C": "Capacitor_SMD:C_0201_0603Metric", "R": "Resistor_SMD:R_0201_0603Metric"},
    "0402": {"C": "Capacitor_SMD:C_0402_1005Metric", "R": "Resistor_SMD:R_0402_1005Metric"},
    "0603": {"C": "Capacitor_SMD:C_0603_1608Metric", "R": "Resistor_SMD:R_0603_1608Metric"},
    "0805": {
        "C": "Capacitor_SMD:C_0805_2012Metric",
        "R": "Resistor_SMD:R_0805_2012Metric",
        "L": "Inductor_SMD:L_0805_2012Metric",
    },
    "1206": {"C": "Capacitor_SMD:C_1206_3216Metric", "R": "Resistor_SMD:R_1206_3216Metric"},
}


def _infer_passive_type(ref: str, value: str, footprint: str) -> str | None:
    """Infer passive type code ("C", "R", "L") from ref/value/footprint."""
    prefix = ""
    for ch in ref:
        if ch.isalpha():
            prefix += ch
        else:
            break
    prefix = prefix.upper()
    if prefix in ("C", "R", "L", "FB"):
        return prefix

    fp = (footprint or "").lower()
    if "resistor" in fp:
        return "R"
    if "capacitor" in fp:
        return "C"
    if "inductor" in fp:
        return "L"

    v = (value or "").strip().lower().replace(" ", "")
    if not v:
        return None

    # Common resistor notations: 10k, 4.7R, 1M, 100 ohm
    if "ohm" in v or re.fullmatch(r"\d+([.,]\d+)?[rkm]([ωΩ]|ohm)?", v):
        return "R"

    # IEC resistor notation: 4K7, 2M2, 0R0
    if re.fullmatch(r"\d+[rkm]\d+", v):
        return "R"

    # Common capacitor notations: 100nF, 10uF, 47pF
    if re.fullmatch(r"\d+([.,]\d+)?[pnum]?f", v):
        return "C"

    # Common inductor notations: 4.7uH, 10mH, 220nH
    if re.fullmatch(r"\d+([.,]\d+)?[num]?h", v):
        return "L"

    return None


def infer_passive_component(ref: str, value: str, footprint: str = "") -> ComponentDef | None:
    """Auto-generate a ComponentDef for a passive component from ref + value.

    Handles: C1=100nF, R3=10k, L1=4.7uH, etc.
    Infers footprint from value or footprint string if provided.
    """
    prefix = _infer_passive_type(ref, value, footprint)
    if prefix is None:
        return None

    sym_type = "L" if prefix == "FB" else prefix  # Ferrite beads use inductor symbol
    category = "passive"

    # Infer footprint
    if not footprint:
        # Try to detect package size from value or ref
        # Default to 0402 for C/R, 0805 for L
        pkg = "0402" if sym_type in ("C", "R") else "0805"

        # Check if value mentions a package size
        for size in ("0201", "0402", "0603", "0805", "1206", "1210"):
            if size in value or size in ref:
                pkg = size
                break

        footprint = _PASSIVE_FOOTPRINT_MAP.get(pkg, {}).get(sym_type, "")

    sym_name = {"C": "C_Small", "R": "R_Small", "L": "L_Small"}[sym_type]

    return ComponentDef(
        mpn=f"{sym_name}_{value}",
        ref_prefix=prefix,
        value=value,
        footprint=footprint,
        description=f"Passive {sym_type} {value}",
        category=category,
        pins=[
            PinDef("1", "~", "passive", "L"),
            PinDef("2", "~", "passive", "R"),
        ],
    )


# ================================================================
# Auto-decoupling for ICs with power pins but no explicit bypass caps
# ================================================================

# Default footprints for auto-generated bypass caps
_AUTO_BYPASS_FP_HF = "Capacitor_SMD:C_0402_1005Metric"
_AUTO_BYPASS_FP_BULK = "Capacitor_SMD:C_0805_2012Metric"

# Power nets that are ground (not decoupled, used as gnd_net)
_GROUND_NETS = frozenset({"GND", "AGND", "DGND", "GNDA", "GNDD", "VSS", "VSSA"})

# Minimum pin count for an IC to get auto-decoupling (skip simple passives/connectors)
_AUTO_BYPASS_MIN_PINS = 6


def _bypass_counts(comp: ComponentDef) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for cap in comp.bypass_caps:
        key = (cap.net, cap.value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _bypass_footprint_for_value(value: str) -> str:
    upper = (value or "").strip().upper()
    if "UF" in upper or "MF" in upper:
        return _AUTO_BYPASS_FP_BULK
    return _AUTO_BYPASS_FP_HF


def _append_bypass_caps(
    comp: ComponentDef,
    *,
    net: str,
    gnd_net: str,
    value: str,
    count: int,
    footprint: str | None = None,
    selection_policy: str | None = None,
    confidence: str | None = None,
    calculation_id: str | None = None,
    evidence_ids: tuple[str, ...] = (),
) -> int:
    """Add the requested number of bypass caps without duplicating existing ones."""
    existing = _bypass_counts(comp)
    key = (net, value)
    have = existing.get(key, 0)
    if have >= count:
        return 0
    added = 0
    for _ in range(count - have):
        comp.bypass_caps.append(
            BypassCap(
                pin="auto",
                net=net,
                gnd_net=gnd_net,
                value=value,
                footprint=footprint or _bypass_footprint_for_value(value),
                selection_policy=selection_policy,
                confidence=confidence,
                calculation_id=calculation_id,
                evidence_ids=evidence_ids,
            )
        )
        added += 1
    return added


def _passive_target(comp: ComponentDef, net: str, kind: str) -> str:
    ref = re.sub(r"[^A-Za-z0-9_]", "_", comp.source_ref or comp.ref_prefix or "U").strip("_") or "U"
    if not ref[0].isalpha():
        ref = f"U_{ref}"
    normalized_net = re.sub(r"[^A-Za-z0-9_-]", "_", net).strip("_") or "rail"
    return f"param:{ref}.passive.{kind}_{normalized_net}"


def _dedupe_passive_synthesis_records(comp: ComponentDef) -> None:
    """Keep only unique, self-contained producer records on a component."""
    from . import calc
    from .evidence import EvidenceRecord

    evidence: dict[str, EvidenceRecord] = {}
    for record in comp.passive_synthesis_evidence:
        if not isinstance(record, EvidenceRecord):
            raise TypeError("passive_synthesis_evidence must contain EvidenceRecord values")
        if record.id in evidence and evidence[record.id] != record:
            raise ValueError("conflicting passive synthesis evidence IDs")
        evidence[record.id] = record
    calculations: dict[str, calc.CalculationRecord] = {}
    for record in comp.passive_synthesis_calculations:
        if not isinstance(record, calc.CalculationRecord):
            raise TypeError("passive_synthesis_calculations must contain CalculationRecord values")
        if record.id in calculations and calculations[record.id] != record:
            raise ValueError("conflicting passive synthesis calculation IDs")
        if record.emits_evidence is not None and record.emits_evidence not in evidence:
            raise ValueError("passive synthesis calculation has dangling emitted evidence")
        if any(item.evidence_id and item.evidence_id not in evidence for item in record.inputs):
            raise ValueError("passive synthesis calculation has dangling input evidence")
        calculations[record.id] = record
    findings: dict[str, calc.PassiveSynthesisFinding] = {}
    for finding in comp.passive_synthesis_findings:
        if not isinstance(finding, calc.PassiveSynthesisFinding):
            raise TypeError("passive_synthesis_findings must contain PassiveSynthesisFinding values")
        if finding.calculation_id not in calculations:
            raise ValueError("passive synthesis finding has dangling calculation")
        if any(item not in evidence for item in finding.evidence_ids):
            raise ValueError("passive synthesis finding has dangling evidence")
        if finding.id in findings and findings[finding.id] != finding:
            raise ValueError("conflicting passive synthesis finding IDs")
        findings[finding.id] = finding
    comp.passive_synthesis_evidence = [evidence[key] for key in sorted(evidence)]
    comp.passive_synthesis_calculations = [calculations[key] for key in sorted(calculations)]
    comp.passive_synthesis_findings = [findings[key] for key in sorted(findings)]


def _store_passive_synthesis(
    comp: ComponentDef, calculation: object, finding: object | None, evidence_records: list[object]
) -> None:
    comp.passive_synthesis_calculations.append(calculation)
    if finding is not None:
        comp.passive_synthesis_findings.append(finding)
    comp.passive_synthesis_evidence.extend(evidence_records)
    _dedupe_passive_synthesis_records(comp)


def emit_and_retain_passive_synthesis(
    comp: ComponentDef,
    calculation: object,
    *,
    finding: object | None = None,
    input_evidence: tuple[object, ...] = (),
) -> object:
    """Emit calculation evidence and retain the complete producer trace on ``comp``.

    Topology producers use this boundary instead of independently assembling
    ledgers and component-owned record lists.  Supplied input evidence is
    admitted before the calculation, so a datasheet-backed calculation still
    fails closed when its cited evidence is missing or malformed.
    """
    from . import calc
    from .evidence import EvidenceLedger, EvidenceRecord

    if not isinstance(calculation, calc.CalculationRecord):
        raise TypeError("calculation must be a CalculationRecord")
    if finding is not None and not isinstance(finding, calc.PassiveSynthesisFinding):
        raise TypeError("finding must be a PassiveSynthesisFinding or None")
    ledger = EvidenceLedger()
    retained_evidence: list[object] = []
    for record in input_evidence:
        if not isinstance(record, EvidenceRecord):
            raise TypeError("input_evidence must contain EvidenceRecord values")
        ledger.add(record)
        retained_evidence.append(record)
    emitted = calc.emit_calculation_evidence(calculation, ledger)
    if emitted.emits_evidence is None:
        raise RuntimeError("calculation evidence emission did not return an evidence ID")
    calculation_evidence = ledger.get(emitted.emits_evidence)
    if calculation_evidence is None:
        raise RuntimeError("emitted calculation evidence is missing from its ledger")
    retained_evidence.append(calculation_evidence)
    _store_passive_synthesis(comp, emitted, finding, retained_evidence)
    return emitted


def _withheld_bypass_trace(comp: ComponentDef, *, net: str, reason: str, kind: str = "bypass") -> None:
    """Create a retained fail-closed calculation/finding for a withheld rail."""
    from . import calc
    from .evidence import EvidenceLedger

    value = 2e-6 if reason == "out_of_range" else 100e-9
    decision = calc.bounded_fallback_scalar(
        target=_passive_target(comp, net, kind), value=value, minimum=10e-9, maximum=1e-6, unit="F"
    )
    if reason == "out_of_range":
        withheld, finding = decision.calculation, decision.finding
    else:
        withheld, finding = calc.withhold_calculation(decision.calculation, reason=reason)
    if finding is None:
        raise RuntimeError("withheld bypass trace requires a passive synthesis finding")
    ledger = EvidenceLedger()
    emitted = calc.emit_calculation_evidence(withheld, ledger)
    evidence_records = [ledger.get(emitted.emits_evidence)] if emitted.emits_evidence else []
    _store_passive_synthesis(comp, emitted, finding, [item for item in evidence_records if item is not None])


def auto_generate_bypass_caps(components: list[ComponentDef]) -> int:
    """Add/augment decoupling caps for ICs with power pins.

    Every newly emitted capacitor is backed by a retained calculation and
    evidence record.  The historic integer return remains the number of
    components augmented, not the number of capacitors added.
    """
    count = 0
    _power_categories = {"power", "regulator", "poe"}
    for comp in components:
        if not comp.power_pins:
            continue  # no power pins assigned
        # Power ICs (regulators, etc.) always get decoupling regardless of pin count.
        # Other ICs need at least _AUTO_BYPASS_MIN_PINS to avoid decoupling bare passives/connectors.
        is_power_ic = comp.category in _power_categories or comp.ref_prefix == "U"
        if not is_power_ic and len(comp.pins) < _AUTO_BYPASS_MIN_PINS:
            continue

        # Collect unique non-ground power nets
        power_nets = set()
        gnd_net = "GND"  # default
        for pin_num, net in comp.power_pins.items():
            if net in _GROUND_NETS:
                gnd_net = net
            else:
                power_nets.add(net)

        if not power_nets:
            continue

        added = 0
        for net in sorted(power_nets):
            selection = comp.select_passive_recommendation("regulator_io_cap", "decoupling", net=net)
            if selection.outcome in {"conflict", "out_of_bounds"}:
                reason = "out_of_range" if selection.outcome == "out_of_bounds" else "incompatible_network"
                _withheld_bypass_trace(comp, net=net, reason=reason)
                continue
            recommendation = selection.recommendation
            if recommendation is not None and recommendation.precedence_policy == "equation":
                # An externally supplied EV-CALCULATION ID cannot be safely
                # reconstructed into this ledger without its original record.
                _withheld_bypass_trace(comp, net=net, reason="missing_basis")
                continue
            if recommendation is not None and recommendation.precedence_policy == "datasheet":
                if recommendation.value is None or not recommendation.provenance:
                    _withheld_bypass_trace(comp, net=net, reason="missing_basis")
                    continue
                from . import calc
                from .evidence import EvidenceLedger, EvidenceSource

                target = _passive_target(comp, net, "bypass")
                ledger = EvidenceLedger()
                datasheet_id = ledger.record(
                    subject_ref=target,
                    claim=f"datasheet recommends {recommendation.value:g} {recommendation.unit} for {net}",
                    kind="datasheet",
                    source=EvidenceSource(uri=recommendation.provenance, extraction_method="passive-recommendation"),
                    confidence=recommendation.confidence,
                    freshness="unknown",
                )
                calculation = calc.datasheet_selected_scalar(
                    target=target, value=recommendation.value, unit=recommendation.unit or "F", evidence_id=datasheet_id
                )
                calculation = calc.emit_calculation_evidence(calculation, ledger)
                evidence_ids = tuple(item for item in (datasheet_id, calculation.emits_evidence) if item)
                _store_passive_synthesis(
                    comp,
                    calculation,
                    None,
                    [ledger.get(item) for item in evidence_ids if ledger.get(item) is not None],
                )
                added += _append_bypass_caps(
                    comp,
                    net=net,
                    gnd_net=str(recommendation.gnd_net or gnd_net),
                    value=_format_capacitance_value(recommendation.value),
                    count=recommendation.count,
                    footprint=recommendation.footprint,
                    selection_policy=calculation.policy,
                    confidence=calculation.confidence,
                    calculation_id=calculation.id,
                    evidence_ids=evidence_ids,
                )
                continue

            from . import calc
            from .evidence import EvidenceLedger

            # The central default is itself an explicit, versioned bounded
            # policy: 100nF within 10nF..1uF, never an unlabeled literal.
            value, minimum, maximum, footprint = 100e-9, 10e-9, 1e-6, _AUTO_BYPASS_FP_HF
            if recommendation is not None:
                if recommendation.value is None or recommendation.fallback_min is None:
                    _withheld_bypass_trace(comp, net=net, reason="missing_basis")
                    continue
                value, minimum, maximum = recommendation.value, recommendation.fallback_min, recommendation.fallback_max
                footprint = recommendation.footprint or footprint
            decision = calc.bounded_fallback_scalar(
                target=_passive_target(comp, net, "bypass"),
                value=value,
                minimum=minimum,
                maximum=maximum,
                unit="F",
            )
            ledger = EvidenceLedger()
            calculation = calc.emit_calculation_evidence(decision.calculation, ledger)
            evidence_ids = tuple(item for item in (calculation.emits_evidence,) if item)
            _store_passive_synthesis(
                comp,
                calculation,
                decision.finding,
                [ledger.get(item) for item in evidence_ids if ledger.get(item) is not None],
            )
            if decision.finding is not None:
                continue
            added += _append_bypass_caps(
                comp,
                net=net,
                gnd_net=str(recommendation.gnd_net if recommendation and recommendation.gnd_net else gnd_net),
                value=_format_capacitance_value(calculation.chosen_value.value),  # type: ignore[union-attr]
                count=recommendation.count if recommendation else 1,
                footprint=footprint,
                selection_policy=calculation.policy,
                confidence=calculation.confidence,
                calculation_id=calculation.id,
                evidence_ids=evidence_ids,
            )

        if len(power_nets) >= 3:
            main_rail = sorted(power_nets, key=lambda n: ("5V" in n, "3P3" in n, n), reverse=True)[0]
            from . import calc
            from .evidence import EvidenceLedger, EvidenceSource

            selection = comp.select_passive_recommendation("regulator_io_cap", "bulk", net=main_rail)
            recommendation = selection.recommendation
            if selection.outcome in {"conflict", "out_of_bounds"}:
                reason = "out_of_range" if selection.outcome == "out_of_bounds" else "missing_basis"
                _withheld_bypass_trace(comp, net=main_rail, reason=reason, kind="bulk_bypass")
            elif recommendation is not None and recommendation.precedence_policy == "equation":
                _withheld_bypass_trace(comp, net=main_rail, reason="missing_basis", kind="bulk_bypass")
            elif recommendation is not None and recommendation.precedence_policy == "datasheet":
                if recommendation.value is None or not recommendation.provenance:
                    _withheld_bypass_trace(comp, net=main_rail, reason="missing_basis", kind="bulk_bypass")
                else:
                    target = _passive_target(comp, main_rail, "bulk_bypass")
                    ledger = EvidenceLedger()
                    source_id = ledger.record(
                        subject_ref=target,
                        claim=f"datasheet recommends {recommendation.value:g} {recommendation.unit} bulk capacitance",
                        kind="datasheet",
                        source=EvidenceSource(
                            uri=recommendation.provenance, extraction_method="passive-recommendation"
                        ),
                        confidence=recommendation.confidence,
                        freshness="unknown",
                    )
                    calculation = calc.emit_calculation_evidence(
                        calc.datasheet_selected_scalar(
                            target=target,
                            value=recommendation.value,
                            unit=recommendation.unit or "F",
                            evidence_id=source_id,
                        ),
                        ledger,
                    )
                    evidence_ids = tuple(item for item in (source_id, calculation.emits_evidence) if item)
                    _store_passive_synthesis(
                        comp,
                        calculation,
                        None,
                        [ledger.get(item) for item in evidence_ids if ledger.get(item) is not None],
                    )
                    added += _append_bypass_caps(
                        comp,
                        net=main_rail,
                        gnd_net=str(recommendation.gnd_net or gnd_net),
                        value=_format_capacitance_value(recommendation.value),
                        count=recommendation.count,
                        footprint=recommendation.footprint or _AUTO_BYPASS_FP_BULK,
                        selection_policy=calculation.policy,
                        confidence=calculation.confidence,
                        calculation_id=calculation.id,
                        evidence_ids=evidence_ids,
                    )
            else:
                value, minimum, maximum = 10e-6, 1e-6, 47e-6
                footprint = _AUTO_BYPASS_FP_BULK
                if recommendation is not None:
                    if recommendation.value is None or recommendation.fallback_min is None:
                        _withheld_bypass_trace(comp, net=main_rail, reason="missing_basis", kind="bulk_bypass")
                        continue
                    value, minimum, maximum = (
                        recommendation.value,
                        recommendation.fallback_min,
                        recommendation.fallback_max,
                    )
                    footprint = recommendation.footprint or footprint
                ledger = EvidenceLedger()
                decision = calc.bounded_fallback_scalar(
                    target=_passive_target(comp, main_rail, "bulk_bypass"),
                    value=value,
                    minimum=minimum,
                    maximum=maximum,
                    unit="F",
                )
                calculation = calc.emit_calculation_evidence(decision.calculation, ledger)
                evidence_ids = tuple(item for item in (calculation.emits_evidence,) if item)
                _store_passive_synthesis(
                    comp,
                    calculation,
                    decision.finding,
                    [ledger.get(item) for item in evidence_ids if ledger.get(item) is not None],
                )
                if decision.finding is None:
                    count_value = recommendation.count if recommendation else 1
                    selected_gnd = str(recommendation.gnd_net if recommendation and recommendation.gnd_net else gnd_net)
                    added += _append_bypass_caps(
                        comp,
                        net=main_rail,
                        gnd_net=selected_gnd,
                        value=_format_capacitance_value(calculation.chosen_value.value),  # type: ignore[union-attr]
                        count=count_value,
                        footprint=footprint,
                        selection_policy=calculation.policy,
                        confidence=calculation.confidence,
                        calculation_id=calculation.id,
                        evidence_ids=evidence_ids,
                    )

        if added:
            count += 1
    return count


# ================================================================
# Built-in component library — common parts for proof-of-concept
# ================================================================
def _builtin_components():
    """Return a registry pre-loaded with common components."""
    reg = ComponentRegistry()

    # --- AMS1117-3.3 LDO (SOT-223) ---
    reg.register(
        ComponentDef(
            mpn="AMS1117-3.3",
            ref_prefix="U",
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            description="3.3V 1A LDO Regulator",
            category="power",
            pins=[
                PinDef("1", "GND", "power_in", "B"),
                PinDef("2", "VOUT", "power_out", "R"),
                PinDef("3", "VIN", "power_in", "L"),
            ],
            pin_nets={},
            power_pins={"1": "GND", "2": "VDD_3P3", "3": "VIN"},
            power_reqs=[PowerReq("VIN", 5.0, 1000)],
            bypass_caps=[
                BypassCap("3", "VIN", "GND", "10uF", "Capacitor_SMD:C_0805_2012Metric"),
                BypassCap("2", "VDD_3P3", "GND", "10uF", "Capacitor_SMD:C_0805_2012Metric"),
                BypassCap("2", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ],
        )
    )

    # --- ESP32-WROOM-32E Module (38-pin) ---
    esp_pins = [
        PinDef("1", "GND", "power_in", "B"),
        PinDef("2", "3V3", "power_in", "T"),
        PinDef("3", "EN", "input", "L"),
        PinDef("4", "SENSOR_VP", "input", "L"),
        PinDef("5", "SENSOR_VN", "input", "L"),
        PinDef("6", "IO34", "input", "L"),
        PinDef("7", "IO35", "input", "L"),
        PinDef("8", "IO32", "bidirectional", "L"),
        PinDef("9", "IO33", "bidirectional", "L"),
        PinDef("10", "IO25", "bidirectional", "L"),
        PinDef("11", "IO26", "bidirectional", "L"),
        PinDef("12", "IO27", "bidirectional", "L"),
        PinDef("13", "IO14", "bidirectional", "R"),
        PinDef("14", "IO12", "bidirectional", "R"),
        PinDef("15", "GND2", "power_in", "B"),
        PinDef("16", "IO13", "bidirectional", "R"),
        PinDef("17", "SD2", "bidirectional", "R"),
        PinDef("18", "SD3", "bidirectional", "R"),
        PinDef("19", "CMD", "bidirectional", "R"),
        PinDef("20", "CLK", "output", "R"),
        PinDef("21", "SD0", "bidirectional", "R"),
        PinDef("22", "SD1", "bidirectional", "R"),
        PinDef("23", "IO15", "bidirectional", "R"),
        PinDef("24", "IO2", "bidirectional", "R"),
        PinDef("25", "IO0", "bidirectional", "L"),
        PinDef("26", "IO4", "bidirectional", "R"),
        PinDef("27", "IO16", "bidirectional", "R"),
        PinDef("28", "IO17", "bidirectional", "R"),
        PinDef("29", "IO5", "bidirectional", "R"),
        PinDef("30", "IO18", "bidirectional", "R"),
        PinDef("31", "IO19", "bidirectional", "R"),
        PinDef("32", "NC", "passive", "R"),
        PinDef("33", "IO21", "bidirectional", "R"),
        PinDef("34", "RXD0", "input", "L"),
        PinDef("35", "TXD0", "output", "L"),
        PinDef("36", "IO22", "bidirectional", "R"),
        PinDef("37", "IO23", "bidirectional", "R"),
        PinDef("38", "GND3", "power_in", "B"),
        PinDef("39", "GND_PAD", "power_in", "B"),
    ]
    reg.register(
        ComponentDef(
            mpn="ESP32-WROOM-32E",
            ref_prefix="U",
            value="ESP32-WROOM-32E",
            footprint="RF_Module:ESP32-WROOM-32E",
            description="WiFi+BT Module (ESP32, 4MB Flash)",
            category="digital",
            pins=esp_pins,
            pin_nets={
                "3": "ESP_EN",
                "25": "ESP_IO0",
                "34": "UART0_RX",
                "35": "UART0_TX",
            },
            power_pins={"1": "GND", "2": "VDD_3P3", "15": "GND", "38": "GND", "39": "GND"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 500)],
            bypass_caps=[
                BypassCap("2", "VDD_3P3", "GND", "10uF", "Capacitor_SMD:C_0805_2012Metric"),
                BypassCap("2", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ],
            straps=[
                StrapConfig("3", "ESP_EN", "VDD_3P3", "10k", "Resistor_SMD:R_0402_1005Metric"),
                StrapConfig("25", "ESP_IO0", "VDD_3P3", "10k", "Resistor_SMD:R_0402_1005Metric"),
            ],
            official_references=[
                {
                    "title": "ESP32-WROOM-32E/32UE datasheet",
                    "url": (
                        "https://documentation.espressif.com/esp32-wroom-32e_" "esp32-wroom-32ue_datasheet_en.html"
                    ),
                    "publisher": "Espressif Systems",
                    "why": "Module land pattern, antenna area, dimensions, and operating limits.",
                },
                {
                    "title": "ESP32 PCB layout design guidelines",
                    "url": (
                        "https://docs.espressif.com/projects/esp-hardware-design-guidelines/"
                        "en/latest/esp32/pcb-layout-design.html"
                    ),
                    "publisher": "Espressif Systems",
                    "why": "Antenna-edge placement, keepout, grounding, power, and RF layout guidance.",
                },
            ],
        )
    )

    # --- USB-C power-only connector ---
    reg.register(
        ComponentDef(
            mpn="USB-C-PWR",
            ref_prefix="J",
            value="USB-C",
            footprint="Connector_USB:USB_C_Receptacle_GCT_USB4085",
            description="USB-C Power Input",
            category="connector",
            pins=[
                PinDef("A4", "VBUS", "power_out", "R"),
                PinDef("A1", "GND", "power_in", "B"),
                PinDef("A5", "CC1", "bidirectional", "L"),
                PinDef("A6", "D+1", "bidirectional", "L"),
                PinDef("A7", "D-1", "bidirectional", "L"),
                PinDef("A8", "SBU1", "bidirectional", "L"),
                PinDef("A9", "VBUS2", "passive", "R"),
                PinDef("A12", "GND2", "power_in", "B"),
                PinDef("B1", "GND3", "power_in", "B"),
                PinDef("B4", "VBUS3", "passive", "R"),
                PinDef("B5", "CC2", "bidirectional", "T"),
                PinDef("B6", "D+2", "bidirectional", "L"),
                PinDef("B7", "D-2", "bidirectional", "L"),
                PinDef("B8", "SBU2", "bidirectional", "L"),
                PinDef("B9", "VBUS4", "passive", "R"),
                PinDef("B12", "GND4", "power_in", "B"),
                PinDef("SH", "SHIELD", "passive", "B"),
            ],
            pin_nets={"A5": "USB_CC1", "B5": "USB_CC2"},
            power_pins={
                "A4": "VBUS_5V",
                "A9": "VBUS_5V",
                "B4": "VBUS_5V",
                "B9": "VBUS_5V",
                "A1": "GND",
                "A12": "GND",
                "B1": "GND",
                "B12": "GND",
            },
            explicit_no_connects={"A6", "A7", "A8", "B6", "B7", "B8", "SH"},
            straps=[
                StrapConfig(
                    "A5",
                    "USB_CC1",
                    "GND",
                    "5.1k",
                    "Resistor_SMD:R_0402_1005Metric",
                    role="termination",
                    presentation="topology_local",
                ),
                StrapConfig(
                    "B5",
                    "USB_CC2",
                    "GND",
                    "5.1k",
                    "Resistor_SMD:R_0402_1005Metric",
                    role="termination",
                    presentation="topology_local",
                ),
            ],
        )
    )

    # --- BME280 Environmental Sensor (LGA-8) ---
    reg.register(
        ComponentDef(
            mpn="BME280",
            ref_prefix="U",
            value="BME280",
            footprint="Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
            description="Temperature/Humidity/Pressure Sensor I2C/SPI",
            category="sensor",
            pins=[
                PinDef("1", "GND", "power_in", "B"),
                PinDef("2", "CSB", "input", "L"),
                PinDef("3", "SDI", "bidirectional", "L"),
                PinDef("4", "SCK", "input", "L"),
                # SDO is an SPI output but becomes the sampled I2C address
                # strap in I2C mode.  ``passive`` models the mode-dependent
                # electrical role without a false output-vs-power conflict
                # when a design intentionally straps it to GND or VDDIO.
                PinDef("5", "SDO", "passive", "R"),
                PinDef("6", "VDDIO", "power_in", "T"),
                PinDef("7", "GND2", "power_in", "B"),
                PinDef("8", "VDD", "power_in", "T"),
            ],
            pin_nets={"3": "I2C_SDA", "4": "I2C_SCL", "5": "BME_SDO"},
            power_pins={"1": "GND", "2": "VDD_3P3", "6": "VDD_3P3", "7": "GND", "8": "VDD_3P3"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 4)],
            bypass_caps=[
                BypassCap("8", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ],
            datasheet_url=(
                "https://www.bosch-sensortec.com/media/boschsensortec/downloads/" "datasheets/bst-bme280-ds002.pdf"
            ),
        )
    )

    # --- W25Q128 SPI Flash (SOIC-8) ---
    reg.register(
        ComponentDef(
            mpn="W25Q128JVSIQ",
            ref_prefix="U",
            value="W25Q128JVSIQ",
            footprint="Package_SO:SOIC-8_5.23x5.23mm_P1.27mm",
            description="128Mb SPI NOR Flash",
            category="storage",
            pins=[
                PinDef("1", "CS", "input", "L"),
                PinDef("2", "DO", "output", "R"),
                PinDef("3", "WP", "input", "L"),
                PinDef("4", "GND", "power_in", "B"),
                PinDef("5", "DI", "input", "L"),
                PinDef("6", "CLK", "input", "L"),
                PinDef("7", "HOLD", "input", "R"),
                PinDef("8", "VCC", "power_in", "T"),
            ],
            pin_nets={"1": "FLASH_CS", "2": "SPI_MISO", "5": "SPI_MOSI", "6": "SPI_CLK"},
            power_pins={"3": "VDD_3P3", "4": "GND", "7": "VDD_3P3", "8": "VDD_3P3"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 50)],
            bypass_caps=[
                BypassCap("8", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ],
        )
    )

    # --- ATmega328P (TQFP-32 simplified) ---
    reg.register(
        ComponentDef(
            mpn="ATmega328P-AU",
            ref_prefix="U",
            value="ATmega328P",
            footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm",
            description="8-bit AVR MCU, 32KB Flash",
            category="digital",
            pins=[
                PinDef("4", "VCC", "power_in", "T"),
                PinDef("5", "GND", "power_in", "B"),
                PinDef("6", "XTAL1", "input", "L"),
                PinDef("7", "XTAL2", "output", "L"),
                PinDef("29", "PC6/RESET", "input", "R"),
                PinDef("30", "PD0/RXD", "input", "R"),
                PinDef("31", "PD1/TXD", "output", "R"),
            ],
            pin_nets={"29": "RESET_N", "30": "UART_RX", "31": "UART_TX"},
            power_pins={"4": "VDD_5V", "5": "GND"},
            power_reqs=[PowerReq("VDD_5V", 5.0, 30)],
            bypass_caps=[BypassCap("4", "VDD_5V", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- STM32F103C8T6 (LQFP-48 simplified) ---
    reg.register(
        ComponentDef(
            mpn="STM32F103C8T6",
            ref_prefix="U",
            value="STM32F103C8T6",
            footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
            description="ARM Cortex-M3 MCU, 64KB Flash",
            category="digital",
            pins=[
                PinDef("24", "VSSA", "power_in", "B"),
                PinDef("23", "VDDA", "power_in", "T"),
                PinDef("35", "VSS_1", "power_in", "B"),
                PinDef("36", "VDD_1", "power_in", "T"),
                PinDef("7", "BOOT0", "input", "L"),
                PinDef("44", "NRST", "input", "R"),
                PinDef("37", "PA13/SWDIO", "bidirectional", "R"),
                PinDef("34", "PA14/SWCLK", "input", "R"),
            ],
            pin_nets={"7": "BOOT0", "44": "RESET_N", "37": "SWDIO", "34": "SWCLK"},
            power_pins={"23": "VDD_3P3", "24": "GND", "35": "GND", "36": "VDD_3P3"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 80)],
            bypass_caps=[BypassCap("36", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- RP2040 (QFN-56 simplified) ---
    reg.register(
        ComponentDef(
            mpn="RP2040",
            ref_prefix="U",
            value="RP2040",
            footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
            description="Dual-core Cortex-M0+ MCU",
            category="digital",
            pins=[
                PinDef("49", "RUN", "input", "L"),
                PinDef("46", "SWCLK", "input", "R"),
                PinDef("47", "SWD", "bidirectional", "R"),
                PinDef("44", "USB_DP", "bidirectional", "L"),
                PinDef("45", "USB_DM", "bidirectional", "L"),
                PinDef("50", "ADC_AVDD", "power_in", "T"),
                PinDef("53", "IOVDD", "power_in", "T"),
                PinDef("57", "DVDD", "power_in", "T"),
                PinDef("33", "GND", "power_in", "B"),
            ],
            pin_nets={
                "49": "RESET_N",
                "46": "SWCLK",
                "47": "SWDIO",
                "44": "USB_DP",
                "45": "USB_DM",
            },
            power_pins={"50": "VDD_3P3", "53": "VDD_3P3", "57": "VDD_1P1", "33": "GND"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 120), PowerReq("VDD_1P1", 1.1, 80)],
            bypass_caps=[BypassCap("53", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- nRF52840 (QFN-73 simplified) ---
    reg.register(
        ComponentDef(
            mpn="nRF52840-QIAA",
            ref_prefix="U",
            value="nRF52840",
            footprint="Package_DFN_QFN:QFN-73-1EP_7x7mm_P0.5mm",
            description="BLE/2.4GHz SoC",
            category="digital",
            pins=[
                PinDef("12", "DEC4", "power_out", "T"),
                PinDef("13", "DCC", "power_in", "T"),
                PinDef("18", "SWDCLK", "input", "R"),
                PinDef("19", "SWDIO", "bidirectional", "R"),
                PinDef("20", "nRESET", "input", "R"),
                PinDef("32", "ANT", "output", "R"),
                PinDef("34", "VDD", "power_in", "T"),
                PinDef("35", "GND", "power_in", "B"),
            ],
            pin_nets={"18": "SWCLK", "19": "SWDIO", "20": "RESET_N"},
            power_pins={"34": "VDD_3P3", "35": "GND"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 80)],
            bypass_caps=[BypassCap("34", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- AP2112K-3.3 LDO ---
    reg.register(
        ComponentDef(
            mpn="AP2112K-3.3TRG1",
            ref_prefix="U",
            value="AP2112K-3.3",
            footprint="Package_TO_SOT_SMD:SOT-23-5",
            description="600mA LDO regulator",
            category="power",
            pins=[
                PinDef("1", "VIN", "power_in", "L"),
                PinDef("2", "GND", "power_in", "B"),
                PinDef("3", "EN", "input", "L"),
                PinDef("5", "VOUT", "power_out", "R"),
            ],
            pin_nets={},
            power_pins={"1": "VIN", "2": "GND", "5": "VDD_3P3"},
            power_reqs=[PowerReq("VIN", 5.0, 600)],
            bypass_caps=[BypassCap("5", "VDD_3P3", "GND", "10uF", "Capacitor_SMD:C_0805_2012Metric")],
        )
    )

    # --- MCP1700-3302E ---
    reg.register(
        ComponentDef(
            mpn="MCP1700-3302E",
            ref_prefix="U",
            value="MCP1700-3.3",
            footprint="Package_TO_SOT_SMD:SOT-23",
            description="250mA LDO regulator",
            category="power",
            pins=[
                PinDef("1", "GND", "power_in", "B"),
                PinDef("2", "VIN", "power_in", "L"),
                PinDef("3", "VOUT", "power_out", "R"),
            ],
            power_pins={"1": "GND", "2": "VIN", "3": "VDD_3P3"},
            power_reqs=[PowerReq("VIN", 5.0, 250)],
            bypass_caps=[BypassCap("3", "VDD_3P3", "GND", "1uF", "Capacitor_SMD:C_0603_1608Metric")],
        )
    )

    # --- RT6150 Buck-Boost (simplified) ---
    reg.register(
        ComponentDef(
            mpn="RT6150A",
            ref_prefix="U",
            value="RT6150A",
            footprint="Package_DFN_QFN:WDFN-10-1EP_3x3mm_P0.5mm",
            description="Synchronous buck-boost converter",
            category="power",
            pins=[
                PinDef("1", "VIN", "power_in", "L"),
                PinDef("2", "EN", "input", "L"),
                PinDef("4", "SW", "power_out", "R"),
                PinDef("7", "VOUT", "power_out", "R"),
                PinDef("8", "FB", "input", "R"),
                PinDef("10", "GND", "power_in", "B"),
            ],
            pin_nets={"8": "FB"},
            power_pins={"1": "VIN", "7": "VDD_3P3", "10": "GND"},
            power_reqs=[PowerReq("VIN", 5.0, 2000)],
        )
    )

    # --- TPS63020 Buck-Boost (simplified) ---
    reg.register(
        ComponentDef(
            mpn="TPS63020DSJR",
            ref_prefix="U",
            value="TPS63020",
            footprint="Package_TO_SOT_SMD:WSON-14-1EP_3x4mm_P0.5mm",
            description="Buck-boost converter 4A switch",
            category="power",
            pins=[
                PinDef("1", "L1", "power_out", "L"),
                PinDef("3", "VIN", "power_in", "L"),
                PinDef("5", "EN", "input", "L"),
                PinDef("7", "VOUT", "power_out", "R"),
                PinDef("10", "FB", "input", "R"),
                PinDef("14", "GND", "power_in", "B"),
            ],
            pin_nets={"10": "FB"},
            power_pins={"3": "VIN", "7": "VDD_3P3", "14": "GND"},
            power_reqs=[PowerReq("VIN", 5.0, 2000)],
        )
    )

    # --- microSD Card Slot ---
    reg.register(
        ComponentDef(
            mpn="microSD-slot",
            ref_prefix="J",
            value="microSD",
            footprint="Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5",
            description="microSD Card Slot",
            category="connector",
            pins=[
                PinDef("1", "SD_D2", "bidirectional", "L"),
                PinDef("2", "SD_D3", "bidirectional", "L"),
                PinDef("3", "SD_CMD", "bidirectional", "L"),
                PinDef("4", "VDD", "power_in", "T"),
                PinDef("5", "SD_CLK", "input", "L"),
                PinDef("6", "GND", "power_in", "B"),
                PinDef("7", "SD_D0", "bidirectional", "R"),
                PinDef("8", "SD_D1", "bidirectional", "R"),
                PinDef("9", "SD_CD", "output", "R"),
            ],
            pin_nets={
                "1": "SD_D2",
                "2": "SD_D3",
                "3": "SD_CMD",
                "5": "SD_CLK",
                "7": "SD_D0",
                "8": "SD_D1",
                "9": "SD_CD",
            },
            power_pins={"4": "VDD_3P3", "6": "GND"},
            bypass_caps=[
                BypassCap("4", "VDD_3P3", "GND", "10uF", "Capacitor_SMD:C_0805_2012Metric"),
            ],
        )
    )

    # --- SWD/JTAG Debug Header (10-pin) ---
    reg.register(
        ComponentDef(
            mpn="SWD-10PIN",
            ref_prefix="J",
            value="SWD Header",
            footprint="Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical_SMD",
            description="ARM SWD Debug Header 10-pin",
            category="debug",
            pins=[
                PinDef("1", "VCC", "power_out", "T"),
                PinDef("2", "SWDIO", "bidirectional", "R"),
                PinDef("3", "GND", "power_in", "B"),
                PinDef("4", "SWCLK", "input", "R"),
                PinDef("5", "GND2", "power_in", "B"),
                PinDef("6", "SWO", "output", "R"),
                PinDef("7", "KEY", "passive", "L"),
                PinDef("8", "NC", "passive", "L"),
                PinDef("9", "GND3", "power_in", "B"),
                PinDef("10", "RESET", "output", "R"),
            ],
            pin_nets={"2": "SWDIO", "4": "SWCLK", "6": "SWO", "10": "RESET_N"},
            power_pins={"1": "VDD_3P3", "3": "GND", "5": "GND", "9": "GND"},
        )
    )

    # --- JTAG Header (2x7, 2.54mm simplified) ---
    reg.register(
        ComponentDef(
            mpn="JTAG-14PIN",
            ref_prefix="J",
            value="JTAG Header",
            footprint="Connector_PinHeader_2.54mm:PinHeader_2x07_P2.54mm_Vertical",
            description="ARM 14-pin JTAG header",
            category="debug",
            pins=[
                PinDef("1", "VREF", "power_out", "T"),
                PinDef("3", "nTRST", "input", "L"),
                PinDef("5", "TDI", "input", "L"),
                PinDef("7", "TMS", "input", "L"),
                PinDef("9", "TCK", "input", "R"),
                PinDef("13", "TDO", "output", "R"),
                PinDef("4", "GND", "power_in", "B"),
                PinDef("6", "GND2", "power_in", "B"),
            ],
            pin_nets={
                "3": "JTAG_TRST_N",
                "5": "JTAG_TDI",
                "7": "JTAG_TMS",
                "9": "JTAG_TCK",
                "13": "JTAG_TDO",
            },
            power_pins={"1": "VDD_3P3", "4": "GND", "6": "GND"},
        )
    )

    # --- Generic pin header 1x08 ---
    reg.register(
        ComponentDef(
            mpn="PINHDR-1x08-2.54",
            ref_prefix="J",
            value="PinHeader 1x08",
            footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
            description="Generic 1x08 pin header",
            category="connector",
            pins=[PinDef(str(i), f"PIN{i}", "bidirectional", "L" if i <= 4 else "R") for i in range(1, 9)],
        )
    )

    # --- SMA Connector ---
    reg.register(
        ComponentDef(
            mpn="SMA-EDGE",
            ref_prefix="J",
            value="SMA",
            footprint="Connector_Coaxial:SMA_Amphenol_132134_EdgeMount",
            description="RF SMA edge connector",
            category="connector",
            pins=[
                PinDef("1", "SIG", "passive", "R"),
                PinDef("2", "GND", "power_in", "B"),
            ],
            pin_nets={"1": "RF_IN"},
            power_pins={"2": "GND"},
        )
    )

    # --- RJ45 MagJack (simplified pins) ---
    reg.register(
        ComponentDef(
            mpn="RJ45-MAGJACK",
            ref_prefix="J",
            value="RJ45",
            footprint="Connector_RJ:RJ45_Amphenol_RJMG1BD3B8K1ANR",
            description="RJ45 connector with integrated magnetics",
            category="connector",
            pins=[
                PinDef("1", "TX+", "bidirectional", "L"),
                PinDef("2", "TX-", "bidirectional", "L"),
                PinDef("3", "RX+", "bidirectional", "R"),
                PinDef("6", "RX-", "bidirectional", "R"),
                PinDef("7", "GND", "power_in", "B"),
            ],
            pin_nets={"1": "ETH_TX_P", "2": "ETH_TX_N", "3": "ETH_RX_P", "6": "ETH_RX_N"},
            power_pins={"7": "GND"},
        )
    )

    # --- MPU6050 IMU (QFN-24 simplified) ---
    reg.register(
        ComponentDef(
            mpn="MPU6050",
            ref_prefix="U",
            value="MPU6050",
            footprint="Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm",
            description="6-axis accelerometer/gyroscope",
            category="sensor",
            pins=[
                PinDef("1", "CLKIN", "input", "L"),
                PinDef("8", "VDD", "power_in", "T"),
                PinDef("9", "GND", "power_in", "B"),
                PinDef("23", "SCL", "input", "L"),
                PinDef("24", "SDA", "bidirectional", "R"),
                PinDef("12", "INT", "output", "R"),
            ],
            pin_nets={"23": "I2C_SCL", "24": "I2C_SDA", "12": "IMU_INT"},
            power_pins={"8": "VDD_3P3", "9": "GND"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 10)],
            bypass_caps=[BypassCap("8", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- MAX17048 Fuel Gauge (TDFN-8 simplified) ---
    reg.register(
        ComponentDef(
            mpn="MAX17048G+T10",
            ref_prefix="U",
            value="MAX17048",
            footprint="Package_DFN_QFN:TDFN-8-1EP_2x3mm_P0.5mm_EP1.4x1.6mm",
            description="Li-ion fuel gauge",
            category="sensor",
            pins=[
                PinDef("1", "CELL", "input", "L"),
                PinDef("2", "GND", "power_in", "B"),
                PinDef("3", "SCL", "input", "L"),
                PinDef("4", "SDA", "bidirectional", "R"),
                PinDef("5", "ALRT", "output", "R"),
                PinDef("8", "VDD", "power_in", "T"),
            ],
            pin_nets={"3": "I2C_SCL", "4": "I2C_SDA", "5": "FUEL_ALRT"},
            power_pins={"2": "GND", "8": "VDD_3P3"},
            power_reqs=[PowerReq("VDD_3P3", 3.3, 1)],
            bypass_caps=[BypassCap("8", "VDD_3P3", "GND", "100nF", "Capacitor_SMD:C_0402_1005Metric")],
        )
    )

    # --- Status LED (2-pin) ---
    reg.register(
        ComponentDef(
            mpn="LED-0603",
            ref_prefix="D",
            value="Green LED",
            footprint="LED_SMD:LED_0603_1608Metric",
            description="Status LED Green 0603",
            category="debug",
            pins=[
                PinDef("1", "A", "passive", "L"),
                PinDef("2", "K", "passive", "R"),
            ],
            pin_nets={"1": "LED_A", "2": "LED_K"},
            straps=[
                StrapConfig("1", "LED_A", "VDD_3P3", "330R", "Resistor_SMD:R_0402_1005Metric"),
            ],
        )
    )

    return reg


# Module-level default registry
BUILTIN_REGISTRY = _builtin_components()
