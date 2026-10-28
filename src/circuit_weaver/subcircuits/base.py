"""Subcircuit template base class and value calculators.

SubcircuitTemplate is the abstract base for all circuit block templates.
Each template takes design parameters and produces a SubcircuitResult
containing ComponentDefs, local wire segments, and boundary labels.

Value calculators snap computed resistor/capacitor values to standard
E-series and provide common EE equations (feedback dividers, filters, etc.).
"""

from __future__ import annotations

import math
import threading as _threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .. import calc
from ..component_db import ComponentDef

# ================================================================
# Shared net-name constants (used by topology_builders,
# placement_readiness, generational_repair — single source of truth)
# ================================================================
POWER_NET_PREFIXES = (
    "VDD",
    "VCC",
    "VBUS",
    "VIN",
    "VDDA",
    "MGT",
    "VCCO",
    "VBAT",
    "VSYS",
    "VAUX",
    "VS",
    "VM",
    "VB",
    "VCP",
    "VOUT",
    "AVDD",
    "DVDD",
    "AVCC",
    "DVCC",
    "VDDIO",
    "VCCIO",
    "VDDCORE",
    "VUSB",
)
GROUND_NET_PREFIXES = ("GND", "AGND", "DGND", "PGND", "VSS", "GNDA", "GNDD")


def _is_power_net(net: str) -> bool:
    """Return True if *net* is a power or ground net by name convention."""
    upper = (net or "").upper()
    for p in (*POWER_NET_PREFIXES, *GROUND_NET_PREFIXES):
        if upper == p or upper.startswith(f"{p}_"):
            return True
    return False


# ================================================================
# E-series standard resistor/capacitor values
# ================================================================

# E24 (5% tolerance) — 24 values per decade
E24_VALUES = [
    1.0,
    1.1,
    1.2,
    1.3,
    1.5,
    1.6,
    1.8,
    2.0,
    2.2,
    2.4,
    2.7,
    3.0,
    3.3,
    3.6,
    3.9,
    4.3,
    4.7,
    5.1,
    5.6,
    6.2,
    6.8,
    7.5,
    8.2,
    9.1,
]

# E96 (1% tolerance) — 96 values per decade
E96_VALUES = [
    1.00,
    1.02,
    1.05,
    1.07,
    1.10,
    1.13,
    1.15,
    1.18,
    1.21,
    1.24,
    1.27,
    1.30,
    1.33,
    1.37,
    1.40,
    1.43,
    1.47,
    1.50,
    1.54,
    1.58,
    1.62,
    1.65,
    1.69,
    1.74,
    1.78,
    1.82,
    1.87,
    1.91,
    1.96,
    2.00,
    2.05,
    2.10,
    2.15,
    2.21,
    2.26,
    2.32,
    2.37,
    2.43,
    2.49,
    2.55,
    2.61,
    2.67,
    2.74,
    2.80,
    2.87,
    2.94,
    3.01,
    3.09,
    3.16,
    3.24,
    3.32,
    3.40,
    3.48,
    3.57,
    3.65,
    3.74,
    3.83,
    3.92,
    4.02,
    4.12,
    4.22,
    4.32,
    4.42,
    4.53,
    4.64,
    4.75,
    4.87,
    4.99,
    5.11,
    5.23,
    5.36,
    5.49,
    5.62,
    5.76,
    5.90,
    6.04,
    6.19,
    6.34,
    6.49,
    6.65,
    6.81,
    6.98,
    7.15,
    7.32,
    7.50,
    7.68,
    7.87,
    8.06,
    8.25,
    8.45,
    8.66,
    8.87,
    9.09,
    9.31,
    9.53,
    9.76,
]

# Standard capacitor values (common subset across all series)
CAP_STANDARD = [
    1.0,
    1.5,
    2.2,
    3.3,
    4.7,
    6.8,
    10,
    15,
    22,
    33,
    47,
    68,
    100,
    150,
    220,
    330,
    470,
    680,
]

# Standard inductor values (common)
IND_STANDARD = [
    1.0,
    1.2,
    1.5,
    1.8,
    2.2,
    2.7,
    3.3,
    3.9,
    4.7,
    5.6,
    6.8,
    8.2,
    10,
    12,
    15,
    18,
    22,
    27,
    33,
    39,
    47,
    56,
    68,
    82,
    100,
    120,
    150,
    180,
    220,
]


def _snap_to_series(value: float, series: list[float]) -> float:
    """Snap a value to the nearest standard series value across all decades."""
    if value <= 0:
        return series[0]

    decade = 10 ** math.floor(math.log10(value))
    normalized = value / decade

    best = None
    best_dist = float("inf")
    # Check current decade and one above (to handle 9.76 -> 10.0 boundary)
    for mult in [1.0, 10.0, 0.1]:
        for sv in series:
            candidate = sv * mult
            dist = abs(math.log(candidate / normalized)) if candidate > 0 else float("inf")
            if dist < best_dist:
                best_dist = dist
                best = candidate
    return round(best * decade, 6) if best else value


def snap_to_e96(value: float) -> float:
    """Snap a resistance value to the nearest E96 standard value."""
    return _snap_to_series(value, E96_VALUES)


def snap_to_e24(value: float) -> float:
    """Snap a resistance value to the nearest E24 standard value."""
    return _snap_to_series(value, E24_VALUES)


def snap_cap(value_f: float) -> float:
    """Snap a capacitance (in Farads) to nearest standard capacitor value."""
    if value_f <= 0:
        return 1e-9
    # Normalize to pF range for comparison
    pf = value_f * 1e12
    return _snap_to_series(pf, CAP_STANDARD) * 1e-12


def snap_ind(value_h: float) -> float:
    """Snap an inductance (in Henries) to nearest standard inductor value."""
    if value_h <= 0:
        return 1e-6
    uh = value_h * 1e6
    return _snap_to_series(uh, IND_STANDARD) * 1e-6


def format_resistance(ohms: float) -> str:
    """Format resistance for schematic display: 100 -> '100R', 10000 -> '10k', etc."""
    if ohms >= 1e6:
        v = ohms / 1e6
        return f"{v:g}M" if v != int(v) else f"{int(v)}M"
    if ohms >= 1e3:
        v = ohms / 1e3
        return f"{v:g}k" if v != int(v) else f"{int(v)}k"
    v = ohms
    return f"{v:g}R" if v != int(v) else f"{int(v)}R"


def format_capacitance(farads: float) -> str:
    """Format capacitance for schematic display: 1e-9 -> '1nF', 1e-6 -> '1uF', etc."""
    if farads >= 1e-3:
        return f"{farads * 1e3:g}mF"
    if farads >= 1e-6:
        return f"{farads * 1e6:g}uF"
    if farads >= 1e-9:
        return f"{farads * 1e9:g}nF"
    return f"{farads * 1e12:g}pF"


def format_inductance(henries: float) -> str:
    """Format inductance for schematic display."""
    if henries >= 1e-3:
        return f"{henries * 1e3:g}mH"
    if henries >= 1e-6:
        return f"{henries * 1e6:g}uH"
    return f"{henries * 1e9:g}nH"


# ================================================================
# Common EE calculations
# ================================================================


def feedback_divider_top(vout: float, vref: float, r_bottom: float) -> float:
    """Calculate top feedback resistor through the traceable calc API."""
    return calc.feedback_divider_top(
        target="param:CALC.feedback.r_top",
        vout_v=vout,
        vref_v=vref,
        r_bottom_ohm=r_bottom,
    ).raw_result.value


def feedback_divider_vout(r_top: float, r_bottom: float, vref: float) -> float:
    """Calculate divider output voltage through the traceable calc API."""
    return calc.feedback_divider_vout(
        target="param:CALC.feedback.vout",
        r_top_ohm=r_top,
        r_bottom_ohm=r_bottom,
        vref_v=vref,
    ).raw_result.value


def buck_inductor(vin: float, vout: float, fsw: float, iout: float, ripple_ratio: float = 0.3) -> float:
    """Calculate buck inductance through the traceable ideal-CCM equation API."""
    return calc.buck_inductor(
        target="param:CALC.switching.buck_inductor",
        vin_v=vin,
        vout_v=vout,
        switching_frequency_hz=fsw,
        output_current_a=iout,
        ripple_ratio=ripple_ratio,
    ).raw_result.value


def buck_output_cap(delta_il: float, fsw: float, delta_vout: float = 0.020) -> float:
    """Calculate ideal ripple capacitance through the traceable equation API."""
    return calc.buck_output_cap(
        target="param:CALC.switching.buck_output_cap",
        ripple_current_a=delta_il,
        switching_frequency_hz=fsw,
        output_ripple_v=delta_vout,
    ).raw_result.value


def boost_inductor(vin: float, vout: float, fsw: float, iout: float, ripple_ratio: float = 0.3) -> float:
    """Calculate boost inductance through the traceable ideal-CCM equation API."""
    return calc.boost_inductor(
        target="param:CALC.switching.boost_inductor",
        vin_v=vin,
        vout_v=vout,
        switching_frequency_hz=fsw,
        output_current_a=iout,
        ripple_ratio=ripple_ratio,
    ).raw_result.value


def buck_boost_inductor(vin_min: float, vout: float, fsw: float, iout: float, ripple_ratio: float = 0.3) -> float:
    """Calculate buck-boost inductance for worst-case boost mode through calc."""
    return calc.buck_boost_inductor(
        target="param:CALC.switching.buck_boost_inductor",
        vin_min_v=vin_min,
        vout_v=vout,
        switching_frequency_hz=fsw,
        output_current_a=iout,
        ripple_ratio=ripple_ratio,
    ).raw_result.value


def crystal_load_caps(cl_spec: float, c_stray: float = 4e-12) -> float:
    """Calculate external load capacitors for a crystal.

    CL_ext = 2 * (CL_spec - Cstray) (each cap).

    The legacy 1 pF floor remains here for compatibility with callers that
    expect a selectable capacitor even when an invalid or over-large stray
    capacitance makes the ideal result non-positive.
    """
    try:
        c_external = calc.crystal_external_load_cap(
            target="param:CALC.crystal.load_cap",
            load_capacitance_f=cl_spec,
            stray_capacitance_f=c_stray,
        ).raw_result.value
    except ValueError:
        return 1e-12
    return max(1e-12, c_external)


def rc_filter_cutoff(r: float, c: float) -> float:
    """Calculate RC low-pass cutoff through the traceable calc API."""
    try:
        return calc.rc_cutoff(
            target="param:CALC.filter.cutoff",
            resistance_ohm=r,
            capacitance_f=c,
        ).raw_result.value
    except ValueError:
        # Preserve the historic scalar helper contract while the calculation
        # service itself remains fail-closed for invalid inputs.
        return 0.0


def rc_capacitance_for_cutoff(r: float, fc: float) -> float:
    """Return the capacitor required for a target RC cutoff."""
    return calc.rc_capacitance_for_cutoff(
        target="param:CALC.filter.capacitance",
        resistance_ohm=r,
        cutoff_hz=fc,
    ).raw_result.value


def rc_resistance_for_cutoff(c: float, fc: float) -> float:
    """Return the resistor required for a target RC cutoff."""
    return calc.rc_resistance_for_cutoff(
        target="param:CALC.filter.resistance",
        capacitance_f=c,
        cutoff_hz=fc,
    ).raw_result.value


# ================================================================
# Standard footprints
# ================================================================

FP_0402C = "Capacitor_SMD:C_0402_1005Metric"
FP_0603C = "Capacitor_SMD:C_0603_1608Metric"
FP_0805C = "Capacitor_SMD:C_0805_2012Metric"
FP_1206C = "Capacitor_SMD:C_1206_3216Metric"
FP_0402R = "Resistor_SMD:R_0402_1005Metric"
FP_0603R = "Resistor_SMD:R_0603_1608Metric"
FP_0805L = "Inductor_SMD:L_0805_2012Metric"
FP_1210L = "Inductor_SMD:L_1210_3225Metric"


def cap_footprint(farads: float) -> str:
    """Pick capacitor footprint by value: small caps -> 0402, bulk -> 0805/1206."""
    if farads <= 100e-9:
        return FP_0402C
    if farads <= 10e-6:
        return FP_0805C
    return FP_1206C


def res_footprint(ohms: float, power_w: float = 0.0625) -> str:
    """Pick resistor footprint: default 0402, larger for high power."""
    if power_w > 0.125:
        return FP_0805C.replace("C_", "R_").replace("Capacitor", "Resistor")
    return FP_0402R


def ind_footprint(henries: float, current_a: float = 1.0) -> str:
    """Pick inductor footprint by current rating."""
    if current_a > 2.0:
        return FP_1210L
    return FP_0805L


# ================================================================
# Subcircuit result
# ================================================================


@dataclass
class LocalWire:
    """A wire segment within a subcircuit (explicit routing, not label-only)."""

    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class BoundaryPort:
    """A named port at the subcircuit boundary (becomes a global label)."""

    name: str  # net name
    direction: str = "bidirectional"  # bidirectional, input, output, passive


@dataclass
class SubcircuitResult:
    """Output of a subcircuit template — everything needed to place it on a sheet."""

    components: list[ComponentDef] = field(default_factory=list)
    local_wires: list[LocalWire] = field(default_factory=list)
    boundary_ports: list[BoundaryPort] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    # The primary IC (first component) — used for sheet allocation
    primary_category: str = "power"

    def validate_contract(self) -> list[str]:
        """Validate the result against basic component contract rules.

        Returns list of error strings.  Empty = contract satisfied.
        """
        errors: list[str] = []
        if not self.components:
            errors.append("SubcircuitResult has no components")
            return errors

        primary = self.components[0]

        # Every IC must have at least one power pin connected
        if primary.ref_prefix.upper() == "U" and not primary.power_pins:
            errors.append(f"{primary.mpn}: no power pins assigned — IC will be unpowered")

        # For IC-based blocks, boundary ports referencing GND must have power_pins
        has_ic = any(c.ref_prefix.upper() in ("U", "IC") for c in self.components)
        if has_ic:
            all_power_nets = set()
            for comp in self.components:
                all_power_nets.update(comp.power_pins.values())
            for port in self.boundary_ports:
                if port.direction == "passive" and port.name.upper() in ("GND", "AGND", "DGND"):
                    if "GND" not in all_power_nets:
                        errors.append(f"Boundary port '{port.name}' declared but no GND power pin assigned")

        # Must have at least one boundary port (otherwise block is isolated)
        if not self.boundary_ports:
            errors.append(f"{primary.mpn}: no boundary ports — block cannot interface with rest of design")

        return errors


# ================================================================
# Template base class
# ================================================================


class LegacyDBProxy:
    """Dict-like view over ic_data entries filtered by topology.

    Subcircuit templates historically kept their IC pin maps,
    footprints, and application data in module-level ``*_IC_DATABASE``
    dicts. Sprint 41 Task 178 migrated all 84 entries to
    ``ic_data/*.json`` so users adding new parts via
    ``circuit-weaver register-ic`` flow into every template without
    touching Python. A template's old ``XYZ_IC_DATABASE`` variable is
    now bound to one of these proxies — method bodies that read
    ``db[key]``, ``db.get(key)``, ``key in db``, or ``db.keys()``
    continue to work unchanged because this class implements
    ``__getitem__``, ``__contains__``, ``keys``, ``items``, ``values``,
    ``get``, and ``__iter__`` against the live merged view returned
    by :func:`ic_data.merge_into_legacy_db`.

    Constructing with a ``topology`` string is all that's required.
    Every access re-reads the merged view so a ``register_ic()`` call
    is visible on the next read without needing an ``importlib.reload``.

    Example::

        BUCK_IC_DATABASE = LegacyDBProxy("buck")
        # Anywhere downstream:
        BUCK_IC_DATABASE["AP62300"]       # -> dict with pins, vref, etc.
        "NEW_MPN" in BUCK_IC_DATABASE     # -> True as soon as register_ic runs
    """

    __slots__ = ("_topology",)

    def __init__(self, topology: str) -> None:
        self._topology = topology

    def _view(self) -> dict[str, dict[str, Any]]:
        from ..ic_data import merge_into_legacy_db

        return merge_into_legacy_db({}, self._topology)

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self._view()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._view()

    def __iter__(self):
        return iter(self._view())

    def __len__(self) -> int:
        return len(self._view())

    def get(self, key: str, default: Any = None) -> Any:
        return self._view().get(key, default)

    def keys(self):
        return self._view().keys()

    def items(self):
        return self._view().items()

    def values(self):
        return self._view().values()

    def __repr__(self) -> str:
        return f"LegacyDBProxy(topology={self._topology!r}, size={len(self._view())})"


class SubcircuitTemplate(ABC):
    """Abstract base for subcircuit templates.

    Subclasses implement `generate()` which takes design parameters
    and returns a SubcircuitResult with components, wires, and ports.
    """

    # Metadata for registry
    template_type: str = ""  # "buck", "ldo", "usb_controller", etc.
    description: str = ""
    param_schema: list[dict[str, Any]] = []

    @abstractmethod
    def generate(self, params: dict[str, Any]) -> SubcircuitResult:
        """Generate subcircuit from design parameters.

        Params dict is template-specific. Common keys:
          - ref_prefix: str — base ref designator (e.g., "U12")
          - vin: float — input voltage
          - vout: float — output voltage
          - iout: float — output current
          - ic: str — specific IC MPN (optional, template may have default)
        """

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate params against param_schema. Subclasses may extend."""
        errors = self._validate_params_from_schema(params)
        return errors

    def _validate_params_from_schema(self, params: dict[str, Any]) -> list[str]:
        """Auto-validate params against the declared param_schema.

        Checks: required params present, type correctness, options membership.
        """
        errors: list[str] = []
        schema = getattr(self, "param_schema", [])
        if not schema:
            return errors

        for spec in schema:
            name = spec.get("name", "")
            if not name:
                continue

            required = spec.get("required", False)
            has_default = "default" in spec
            value = params.get(name)

            # Required check
            if required and value is None and not has_default:
                errors.append(f"Missing required parameter '{name}'")
                continue

            if value is None:
                continue

            # Type check
            expected_type = spec.get("type", "")
            if expected_type == "number" and not isinstance(value, (int, float)):
                errors.append(f"Parameter '{name}' must be a number, got {type(value).__name__}")
            elif expected_type == "integer" and not isinstance(value, int):
                errors.append(f"Parameter '{name}' must be an integer, got {type(value).__name__}")
            elif expected_type == "string" and not isinstance(value, str):
                errors.append(f"Parameter '{name}' must be a string, got {type(value).__name__}")
            elif expected_type == "boolean" and not isinstance(value, bool):
                errors.append(f"Parameter '{name}' must be a boolean, got {type(value).__name__}")

            # Options/enum check
            options = spec.get("options") or spec.get("enum")
            if options and value not in options:
                errors.append(f"Parameter '{name}' must be one of {options}, got '{value}'")

            # Range check (minimum/maximum)
            if isinstance(value, (int, float)):
                if "minimum" in spec and value < spec["minimum"]:
                    errors.append(f"Parameter '{name}' must be >= {spec['minimum']}, got {value}")
                if "maximum" in spec and value > spec["maximum"]:
                    errors.append(f"Parameter '{name}' must be <= {spec['maximum']}, got {value}")

        return errors

    def _validate_unknown_params(self, params: dict[str, Any]) -> list[str]:
        """Detect params passed to a template that are not in its param_schema."""
        schema = getattr(self, "param_schema", [])
        if not schema:
            return []
        known = {spec.get("name", "") for spec in schema if spec.get("name")}
        framework_passthrough = {
            "ic",
            "ref",
            "type",
            "section",
            "template",
            "pin_map",
            "pinout_verified",
            "power_map",
            "no_connects",
            "pin_nets_extra",
            "power_pins_extra",
            "interfaces",
            "terminal",
            "supply_ratings",
        }
        unknown = [k for k in params if k not in known and k not in framework_passthrough]
        if unknown:
            return ["Unknown parameter(s): " + ", ".join(unknown)]
        return []

    def get_param_schema(self) -> list[dict[str, Any]]:
        """Return a copy of the template's declared parameter schema."""
        return [dict(item) for item in getattr(self, "param_schema", [])]


# ================================================================
# Template registry
# ================================================================


class DataDrivenTemplate(SubcircuitTemplate):
    """Template backed by JSON IC data + topology builder function.

    Wraps a topology builder with the SubcircuitTemplate interface so the
    registry, dispatcher, and validator see it as a normal template.
    New ICs are added by writing JSON — no Python code changes needed.
    """

    def __init__(
        self,
        template_type: str,
        topology: str,
        ic_database: dict[str, dict],
        param_schema: list | None = None,
        description: str | None = None,
    ):
        self.template_type = template_type
        self.description = description or f"Data-driven {template_type} template"
        self._topology = topology
        self._ic_database = ic_database
        self.param_schema = param_schema or []

    def generate(self, params: dict[str, Any]) -> SubcircuitResult:
        from .topology_builders import get_builder

        ic_name = str(params.get("ic", "")).strip()
        if ic_name:
            if ic_name not in self._ic_database:
                available = ", ".join(sorted(self._ic_database))
                raise ValueError(f"Unknown {self.template_type} IC '{ic_name}'. Available: {available}")
            ic_data = dict(self._ic_database[ic_name])
        elif self._ic_database:
            ic_name = next(iter(self._ic_database))
            ic_data = dict(self._ic_database[ic_name])
        else:
            raise ValueError(f"No ICs registered for topology '{self._topology}'")

        ic_data["_mpn"] = ic_name
        params = dict(params)
        params.setdefault("ic", ic_name)

        builder = get_builder(self._topology)
        result = builder(ic_data, params)
        # Keep manufacturer evidence attached across every topology-specific
        # builder.  The placement pipeline consumes this metadata later and
        # must not depend on whether an IC used the generic or a specialized
        # electrical builder.
        official_references = [
            {str(key): str(value) for key, value in item.items()}
            for item in (ic_data.get("official_references") or [])
            if isinstance(item, dict)
        ]
        for component in result.components:
            if component.mpn != ic_name:
                continue
            component.datasheet_url = str(ic_data.get("datasheet_url") or "")
            component.reference_layout_url = str(ic_data.get("reference_layout_url") or "")
            component.official_references = [dict(item) for item in official_references]
        return result

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = self._validate_params_from_schema(params)
        ic_name = str(params.get("ic", "")).strip()
        if ic_name and ic_name not in self._ic_database:
            available = ", ".join(sorted(self._ic_database))
            errors.append(f"Unknown {self.template_type} IC '{ic_name}'. Available: {available}")
        return errors


class SubcircuitRegistry:
    """Registry of available subcircuit templates, queryable by type name.

    Resolution order:
    1. Registered template classes (via register()) — preferred when a
        topology has handwritten behavior beyond the generic JSON builder.
    2. Data-driven templates from JSON IC data store — fallback for
        topologies that have no registered template class.
    """

    def __init__(self):
        self._templates: dict[str, SubcircuitTemplate] = {}

    def register(self, template: SubcircuitTemplate):
        """Register a template by its type name."""
        self._templates[template.template_type] = template

    def get(self, type_name: str) -> SubcircuitTemplate | None:
        """Look up a template by type name (e.g., 'buck', 'ldo').

        Registered template classes are preferred so richer handwritten
        behavior (shared bus nets, connector subtype handling, etc.)
        is not shadowed by the generic data-driven builder. JSON-backed
        DataDrivenTemplate remains the fallback for topologies that only
        exist in ic_data.
        """
        legacy = self._templates.get(type_name)
        if legacy is not None:
            return legacy
        return self._get_data_driven(type_name)

    def _get_data_driven(self, type_name: str) -> DataDrivenTemplate | None:
        """Try to build a DataDrivenTemplate from JSON IC data."""
        try:
            from ..ic_data import get_all_ics
        except ImportError:
            return None

        ics = get_all_ics(type_name)
        if not ics:
            return None

        from .topology_builders import TOPOLOGY_TEMPLATE_INFO

        info = TOPOLOGY_TEMPLATE_INFO.get(type_name, {})
        return DataDrivenTemplate(
            template_type=type_name,
            topology=type_name,
            ic_database=ics,
            param_schema=info.get("param_schema"),
            description=info.get("description"),
        )

    def available_types(self) -> list[str]:
        """List all registered template type names (legacy + data-driven)."""
        types = set(self._templates.keys())
        try:
            from ..ic_data import list_topologies
        except ImportError:
            pass
        else:
            types.update(list_topologies())
        return sorted(types)

    def __len__(self):
        return len(self._templates)


# ================================================================
# Built-in registry with standard templates
# ================================================================


def _build_default_registry() -> SubcircuitRegistry:
    """Build registry with all built-in subcircuit templates."""
    reg = SubcircuitRegistry()
    # Import here to avoid circular imports
    from .adc import ADCTemplate
    from .audio_amplifier import AudioAmplifierTemplate
    from .battery_charger import BatteryChargerTemplate
    from .battery_monitor import BatteryMonitorTemplate
    from .charge_pump import ChargePumpTemplate
    from .clock import ClockSynthTemplate
    from .connector import ConnectorTemplate
    from .crystal_oscillator import CrystalOscillatorTemplate
    from .current_sense import CurrentSenseTemplate
    from .dac import DACTemplate
    from .display_driver import DisplayDriverTemplate
    from .driver import GateDriverTemplate, LevelShifterTemplate
    from .ethernet import EthernetPHYTemplate
    from .i2c_bus import I2CBusTemplate
    from .led_driver import LEDDriverTemplate
    from .mosfet_switch import MOSFETSwitchTemplate
    from .motor_driver import MotorDriverTemplate
    from .opamp import OpAmpTemplate
    from .power_mux import PowerMuxTemplate
    from .relay_driver import RelayDriverTemplate
    from .rs485_transceiver import RS485TransceiverTemplate
    from .rtc import RTCTemplate
    from .sensor_frontend import SensorFrontendTemplate
    from .spi_bus import SPIBusTemplate
    from .usb import USBControllerTemplate, USBHubTemplate
    from .usb_c_connector import USBCConnectorTemplate
    from .voltage_reference import VoltageReferenceTemplate
    from .wireless_module import WirelessModuleTemplate

    for tmpl_cls in [
        ADCTemplate,
        AudioAmplifierTemplate,
        BatteryChargerTemplate,
        BatteryMonitorTemplate,
        ChargePumpTemplate,
        ClockSynthTemplate,
        ConnectorTemplate,
        CrystalOscillatorTemplate,
        CurrentSenseTemplate,
        DACTemplate,
        DisplayDriverTemplate,
        EthernetPHYTemplate,
        GateDriverTemplate,
        I2CBusTemplate,
        LEDDriverTemplate,
        LevelShifterTemplate,
        MOSFETSwitchTemplate,
        MotorDriverTemplate,
        OpAmpTemplate,
        PowerMuxTemplate,
        RelayDriverTemplate,
        RS485TransceiverTemplate,
        RTCTemplate,
        SensorFrontendTemplate,
        SPIBusTemplate,
        USBCConnectorTemplate,
        USBControllerTemplate,
        USBHubTemplate,
        VoltageReferenceTemplate,
        WirelessModuleTemplate,
    ]:
        reg.register(tmpl_cls())
    return reg


_REGISTRY_LOCK = _threading.Lock()
DEFAULT_REGISTRY = None


def get_default_registry() -> SubcircuitRegistry:
    """Get the default subcircuit registry (lazy-loaded, thread-safe)."""
    global DEFAULT_REGISTRY
    if DEFAULT_REGISTRY is None:
        with _REGISTRY_LOCK:
            if DEFAULT_REGISTRY is None:
                DEFAULT_REGISTRY = _build_default_registry()
    return DEFAULT_REGISTRY
