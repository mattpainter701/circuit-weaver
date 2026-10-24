# Authored current operating cases

`check_operating_case` compares an explicitly authored simultaneous-load case
with source, connector and distribution current limits. This advances the
roadmap's operating-case checks; it does not qualify circuit function, heat rise,
device suitability or PCB trace ampacity.

The Python API is exported from `circuit_weaver`:

```python
from circuit_weaver import CurrentLoad, OperatingCase, check_operating_case

case = OperatingCase(
    name="Relay operating case awaiting requirements",
    max_simultaneous=None,
    loads=(CurrentLoad("coil", current_ma=80, max_concurrent=None, evidence=None),),
    source_limit=None,
)
result = check_operating_case(case)
assert result.status == "unverified"
assert result.required_ma is None
```

All currents use mA. `None` represents unknown current or concurrency; booleans,
nonfinite numbers, incorrect units and malformed runtime models are rejected.
A complete case reconciles `max_simultaneous` with the sum of each load's
`max_concurrent`. Use the current required by one active item for each load.
Different types of loads can be combined. A declared zero-concurrency case is
allowed; it still requires explicit evidence before claiming the current-budget
comparison passes.

Use `CurrentLimit` for the common source, every series connector and every
series distribution boundary on that feed. Every boundary is compared against
the complete load sum. Branch-specific limits need separate operating cases;
this API does not derive a power-tree topology or distribute current among
parallel feeds. Other rail currents and startup/fault cases need their own
explicit budgets. It does not convert values between voltage domains.

`CurrentEvidence` binds one value to its subject and unit. Load subjects use
the load name; boundary subjects use `source`, `connector:<name>` or
`distribution:<name>`. The comparison requires a nonempty evidence ID, an exact
subject/value/unit match, current freshness, no conflicts, verified or
corroborated confidence, and a datasheet, calculation or distributor source
kind. Single-source, user, heuristic, stub, stale or mismatched evidence remains
unverified. The caller must establish that those provenance claims are true;
the checker does not authenticate an evidence ID, fetch a datasheet, resolve
the evidence ledger, or turn an arbitrary confidence label into a real rating.
Preserve the underlying evidence and its applicability to the operating case.

Known values with untrusted load evidence may produce a conditional current
sum, but cannot establish a boundary pass or failure. Missing current or
concurrency produces no required-current value. A fully supported load case
exceeding a supported limit fails even if another boundary is unknown. Missing
limits remain visible and unverified. `to_dict()` and `result_hash` provide
deterministic report data; `result_scope` stays `authored_current_budget_only`,
and functionality/thermal qualification stay unverified even after a pass.

Arithmetic and comparisons use the decimal spelling of each authored numeric
value, with bounded derived precision and no engineering tolerance. This avoids
false overloads such as binary `0.1 * 3` against an authored `0.3` limit. The
numeric report uses floats; a failure reason preserves its exact decimal
comparison. The result includes `input_sha256` over the complete case, including
each load and limit evidence record, so equal totals do not hide changed inputs.

The audit below exercises the four-layer corpus and five synthetic checker
controls. It does not route a board or modify a design, and its synthetic
ratings do not apply to real components:

```text
python benchmarks/four_layer_acceptance/operating_case_audit.py --output <new-report.json>
```

Supply the chosen product environment explicitly. In a source checkout use
the repository's normal source Python path; a freshly installed wheel can run
the same audit using the benchmark helper from the checkout. The report records
the actual checker import path and source hash. It leaves all missing authored
cases unverified and displays relay all-active arithmetic separately. This is
a separate Python/audit API; adding `operating_cases` to a design spec does not
automatically invoke it through the CLI, MCP or shared CAD workflow.
