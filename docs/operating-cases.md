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
a separate corpus audit. The spec integration below now invokes the checker
through shared validation, CLI, MCP and the CAD workflow's validation stage.

## Shared spec validation

`validate_design(spec)`, `circuit-weaver validate <spec>` and MCP
`validate_design` recognize this optional top-level document:

```yaml
operating_cases:
  cases:
    - name: Relay
      max_simultaneous: null
      loads:
        - name: coil
          current_ma: 80
          max_concurrent: null
          evidence_id: null
      source_limit: null
      connector_limits: []
      distribution_limits: []
```

This deliberately incomplete example remains **unverified**. The 80 mA value
is an authored assumption, not a component rating. Omit `operating_cases` when
no check is requested; an absent case is reported as `not_requested`.

The document accepts only `cases` and optional `evidence_manifest`. A limit
object has `name`, `current_ma`, optional `unit` (mA only) and `evidence_id`.
Loads additionally have `max_concurrent`; cases have `max_simultaneous`,
`loads`, `source_limit`, `connector_limits` and `distribution_limits`.
Unknown keys, duplicate case names, malformed manifests and invalid numerical
values are validation errors. Names are identifiers of 1–64 characters,
beginning with a letter and then letters, digits or underscores.

`evidence_manifest` uses the existing `EvidenceLedger.to_manifest()` schema.
Create/import real records with the existing ledger APIs; do not invent IDs or
inline confidence labels. Each referenced record must resolve in that manifest
with one of these exact subjects:

| Value | Subject for case `Relay` |
| --- | --- |
| Per-active-item current for load `coil` | `param:Relay.current.coil` |
| Common source limit | `param:Relay.source.source` |
| Series connector `J1` | `param:Relay.connector.J1` |
| Series distribution boundary `feed` | `param:Relay.distribution.feed` |

Its claim is a JSON string with exactly `value` and `unit`, for example
`{"value":80,"unit":"mA"}`. A nonempty underlying source content hash is
required. Existing confidence, freshness, kind and conflict checks still apply;
superseded records and mismatched claims cannot establish a comparison.
Missing IDs leave the comparison unverified. The supplied manifest is validated
and merged into the returned validation evidence ledger; this does not
authenticate sources or prove applicability to a physical component or rail.

`metadata.operating_cases` contains the aggregate state, each boundary result,
resolved evidence IDs, case result hashes and a hash of the complete authored
document, including source metadata. Findings also link to the resolved ledger
records. MCP summaries preserve these results. A supported overload produces an
electrical error and blocks generation/workflow validation. Unknown ratings
produce warnings; `validate --strict` rejects those warnings. A successful
workflow validation checkpoint retains the current-case result rather than
discarding it.

Canonical/engine conversions and automatic repair preserve this document and
its ledger. Semantic diffs expose authored budget changes. Nested `engine`
specs are supported; conflicting outer/nested declarations are validation
errors. This prevents a saved canonical spec from silently losing its current
checks when it is reopened.

The parser bounds cases, loads, series boundaries and ledger records. It checks
an explicitly authored common series feed; it does not infer power-tree paths,
convert current across rails, measure conductor ampacity, or qualify electrical
function and thermal behavior. Those qualifications remain unverified even
when the current-budget comparison passes.
