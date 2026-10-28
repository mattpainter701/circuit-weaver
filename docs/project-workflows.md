# Project lifecycle workflows

Circuit Weaver exposes one durable project workflow API to its skill, Python
package, CLI, and optional MCP server. It discovers or creates a project,
records completed stages and artifact hashes, and uses that state to plan safe
continuation after interruption or changed inputs.

## Runtime and project selection

Start with `circuit-weaver runtime-info --json` or, with a chosen interpreter,
`python -m circuit_weaver runtime-info --json`. The response reports the
installed package and interpreter, capability checks, skill installation
state, and the MCP launch command. The Python command form is useful when the
global executable may belong to another environment. Discovery accepts an
explicit root:

```text
circuit-weaver discover --root <working-directory> --json
circuit-weaver open <absolute-project-or-source-path> --json
```

`start` creates a draft in an empty selected directory, optionally from
a JSON or YAML specification:

```text
circuit-weaver start <absolute-project-dir> --name sensor --json
circuit-weaver start <absolute-project-dir> --spec <spec.yaml> --json
```

`open` inspects an existing untracked circuit without writing. Use
`circuit-weaver adopt <existing-circuit-dir> --json` (Python/MCP
`adopt_project`) to explicitly record source baselines before planning native stages.
Adoption preserves source bytes and records their hashes; it does not claim
that a previously created board has passed build, placement, routing or DRC.
The native runner can also initialize state when invoked directly; adoption
provides a deliberate, separately reviewable source inventory.

An explicitly selected circuit directory takes precedence over unrelated
ancestor manifests or project files. Recorded source/artifact/checkpoint
ownership still identifies files belonging to a parent project. The canonical
`output/` directory inherits its managed parent unless it has its own project
state or `design.yaml`. Separate native output directories receive their own
checkpoints instead of changing unrelated ancestor projects.

Interactive CLI use begins by running `circuit-weaver` with no subcommand. It
shows the runtime and discovered project choices before asking which workflow
to enter. Automation should pass an explicit project path so a server's working
directory cannot select the wrong project.

## Planning, execution, and continuation

The canonical stages are `validate`, `generate`, `build`, `place`, `route`, and
`preview`. Planning stores the selected ordered stages, options, input snapshot,
and plan hash; it does not execute stages or preflight tool availability. The
workflow runner records stage boundaries and input/output hashes. On restart it
checks those hashes and continues at the first invalid or incomplete stage.
Generation tracks project-local CAD tables, symbols and footprints; previews
also track contained project 3D assets. Changes invalidate the affected saved
plan. External/global model files are checked during rendering but are not
part of this local-asset resume inventory.
Cancellation is requested immediately and observed at the next stage boundary.

```text
circuit-weaver workflow plan <project> --json
circuit-weaver workflow run <project> --stages validate generate --json
circuit-weaver workflow run <project> --stages build place route preview --options-json '{"board_width_mm":50,"board_height_mm":40,"clearance_mm":0.127,"minimum_drill_mm":0.2,"iterations":5000,"route_timeout":300,"appearance":"assembly","kicad_python_path":"C:/KiCad/9.0/bin/python.exe","kicad_cli_path":"C:/KiCad/9.0/bin/kicad-cli.exe","freerouting_path":"C:/tools/freerouting.jar"}' --json
circuit-weaver workflow status <project> --json
circuit-weaver workflow resume <project> --json
circuit-weaver resume <project> --json
circuit-weaver resume <project> --execute --json
circuit-weaver workflow cancel <project> --json
```

`workflow resume` executes the verified saved plan from its first incomplete
stage. The legacy top-level `resume` command prints a plan by default and runs
it only with `--execute`. Call `workflow plan` for a non-executing plan. A
reported successful preview or generated placement is not evidence of a native KiCad build, a
completed routed board, or manufacturing readiness. Native or assembled stages
must report their actual evidence and remain incomplete when required tools or
artifacts are absent.

The shared runner accepts these routing controls in `options_json` through
CLI, Python and MCP. They are included in checkpoint option hashes.

`copper_layers` selects the native board's copper count during `build`: use the
integer `2` (default) or `4`. The four-layer order is `F.Cu`, `In1.Cu`,
`In2.Cu`, `B.Cu`. A route-stage `ground_plane` object uses the same strict plan
fields as `plane-pcb` and selects specific enabled copper layers for the named
net's zones. For example, a four-layer board can add inner-layer GND zones:

```json
{
  "copper_layers": 4,
  "ground_plane": {
    "net": "GND",
    "layers": ["In1.Cu", "In2.Cu"],
    "edge_clearance_mm": 0.5,
    "clearance_mm": 0.25,
    "min_thickness_mm": 0.2,
    "thermal_gap_mm": 0.25,
    "thermal_spoke_width_mm": 0.3,
    "protected_connections": []
  }
}
```

Pass those options through the shared CLI workflow like this:

```text
circuit-weaver workflow run <project> --stages build place route --options-json '{"copper_layers":4,"ground_plane":{"net":"GND","layers":["In1.Cu","In2.Cu"],"edge_clearance_mm":0.5,"clearance_mm":0.25,"min_thickness_mm":0.2,"thermal_gap_mm":0.25,"thermal_spoke_width_mm":0.3,"protected_connections":[]}}' --json
```

Build evidence verifies the native copper count and layer order. Route evidence
verifies selected zone layers, saved fill geometry and connectivity. These
reports record copper count/order and zone results only; they do not define
dielectric materials or thicknesses, or qualify impedance.

Pre-route plane reports list pads whose native connectivity cluster does not
reach a filled zone on that net; opens remain pending work. After routing,
the runner requires native DRC with zero opens, fresh saved plane fills,
positive copper anchors, and no pending plane-net pad attachments. Plane
creation currently requires a rectangular outline and no existing zones;
saved-fill verification accepts single-layer copper zones.

The generated `ground-plane.json` and `route-quality.json` files remain project
reports when opening or resuming a native PCB directory. They are excluded from
design-spec selection; `design.yaml` still takes precedence over other design
files, and differently named JSON designs remain supported.

| Option | Native routing flag | Accepted values |
| --- | --- | --- |
| `copper_layers` | `build-pcb --copper-layers` | Integer `2` or `4`; default `2` |
| `ground_plane` | route-stage zone plan | Object with `net`, `layers`, `edge_clearance_mm`, `clearance_mm`, `min_thickness_mm`, `thermal_gap_mm`, `thermal_spoke_width_mm`, and `protected_connections` |
| `route_timeout` | `--timeout` | Positive seconds; workflow clamps the router budget to 1–1,800 seconds |
| `route_max_passes` | `--max-passes` | Integer 0–9,999; 0 leaves the pass count unlimited |
| `route_optimization_threads` | `--optimization-threads` | Integer 0–1,024; 0 disables optimization threads |
| `route_attempts` | `--attempts` | Integer 1–32 |
| `route_effort` | `--effort` | `fast`, `medium` or `high` |

For a bounded exploratory run, use `route_timeout: 300`,
`route_max_passes: 8` and `route_optimization_threads: 0`. Completion still
requires the native connectivity and DRC gates; limits can leave a board
incomplete.

The route-stage parent reserves up to 600 additional seconds for native session
import, zone refill and DRC, with a total cap of 2,400 seconds. This allowance
does not extend the router's budget or relax any acceptance gate.

A timed-out CLI also terminates its native descendants. Windows workflows
assign a suspended child to a private Job Object before it can launch tools;
POSIX workflows use a separate process session for timeout cleanup. Failure to
establish Windows ownership stops the child before execution. Unrelated
processes are outside this cleanup scope.

`clearance_mm` and `minimum_drill_mm` are explicit prototype assumptions for
native board construction. They do not weaken authored project rules or bypass
physical DRC gates; use them only when those assumptions are appropriate for
the prototype.

## Python API

The same functions are available from `circuit_weaver.project_workflow`:

```python
from circuit_weaver.project_workflow import (
    cancel_workflow,
    adopt_project,
    open_project,
    plan_workflow,
    resume_workflow,
    run_workflow,
    start_project,
    workflow_status,
)

started = start_project("/absolute/path/to/project", spec=None, name="sensor")
plan = plan_workflow("/absolute/path/to/project")
run = run_workflow("/absolute/path/to/project", stages=["validate", "generate"])
status = workflow_status("/absolute/path/to/project")
resume_result = resume_workflow("/absolute/path/to/project")  # executes verified saved stages
```

Each function returns a JSON-safe dictionary with `ok` and `status`, and stable
`error.code` / `error.message` fields on failure. `run_workflow` uses the
persisted checkpoints when called with `resume=True`; `resume_workflow`
executes the verified saved plan from its first incomplete stage. Use
`plan_workflow` to save a plan without execution.

`open_project` and `workflow_status` report live ownership in
`workflow_activity`. A persisted running workflow with a held advisory lock is
`active`; if the existing lock is free or missing, the outward status becomes
`interrupted` and `workflow.persisted_status` retains `running`. Status does not
rewrite the saved checkpoints or lock metadata. Permission or probe failures
produce `unknown` activity rather than declaring the run interrupted. Guarded
resume still acquires the workflow lock and verifies the saved plan, inputs and
completed outputs before continuing.

## MCP

Install the optional dependency with the same interpreter that will launch the
server, then configure the MCP client separately:

```text
<absolute-python> -m pip install "circuit-weaver[mcp]"
<absolute-python> -m circuit_weaver.mcp_server
```

The MCP server exposes `runtime_info`, the read-only `library_access` audit,
`start_project`, `open_project`, `adopt_project`,
`plan_workflow`, `run_workflow`, `workflow_status`, `resume_workflow`, and
`cancel_workflow`, in addition to the original validation, generation,
discovery, research, and manufacturing-readiness tools. `resume_workflow`
executes the saved verified plan; use `plan_workflow` to save a plan without
execution. Each run/resume request returns after the selected stages finish,
fail, or observe cancellation. The server runs those operations off its event
loop, so a separate status or cancel request can be handled while a stage is
running; cancellation takes effect at the next stage boundary. All project
workflow tools require explicit absolute paths. `spec_json` and `options_json` must be
JSON objects; `stages_json` must be an array of stage-name strings. Tool errors
are returned as MCP errors, and workflow progress printed by engine functions
is suppressed so it cannot corrupt the JSON-RPC stream.

`library_access(query, project_root)` accepts an optional query and requires
`project_root` to be absolute when provided. It preserves the distinction
between local symbol/footprint/model assets, curated functional templates, and
project SPICE bindings. A resolved asset does not imply package fit, a working
functional builder, a validated SPICE contract, or RF qualification.

Installing the package, installing its agent skill, and registering an MCP
client entry are separate operations. Use the interpreter reported by
`runtime_info` for both the CLI and server, then configure that absolute
interpreter path in the chosen MCP client's own settings.

## Explicit SPICE testbenches

`circuit-weaver simulate <spec.yaml> --json` accepts optional explicit
analyses under `simulation.analyses`. This is useful for passive circuits and
other designs where the engineer must provide the stimulus and acceptance
limits; the simulator never invents source drive or target limits. For example,
this 1 kΩ / 100 nF low-pass testbench uses a 1 V AC input and a 1.58–1.605 kHz
bandwidth window:

```yaml
project: Explicit_RC_Testbench
passives:
  - ref: R1
    value: 1k
    footprint: Resistor_SMD:R_0603_1608Metric
    pin_nets_extra: {"1": VIN, "2": VOUT}
  - ref: C1
    value: 100nF
    footprint: Capacitor_SMD:C_0603_1608Metric
    pin_nets_extra: {"1": VOUT, "2": GND}
simulation:
  analyses:
    - ref: RC_LOWPASS
      kind: ac
      analysis_params:
        variation: dec
        points: 100
        fstart: 1
        fstop: 1000000
        input_trace: v(VIN)
        output_trace: v(VOUT)
      sources:
        - {ref: VTEST, type: V, positive: VIN, negative: GND, dc: 0, ac: 1}
      target_metrics:
        bandwidth_hz_min: 1580
        bandwidth_hz_max: 1605
```

The source contract supports structured voltage/current sources with
finite numeric `dc` and/or `ac` values and endpoints on named, connected nets
(or ground aliases). Analysis kinds are `ac`, `tran`, `op`, and `dc`; parameter
and trace selectors are validated before SPICE execution. Bandwidth targets use
`bandwidth_hz_min` / `bandwidth_min_hz` and
`bandwidth_hz_max` / `bandwidth_max_hz`; trace selectors must name connected
voltage nodes such as `v(VIN)`. `target_metrics` may be omitted, but then the
run remains unverified because no acceptance limit was checked. Unknown,
ambiguous, or unsupported targets also remain unverified. A verified passive
AC result establishes only that the supplied netlist generated fresh parsed
results and its stated bandwidth bounds passed; it does not validate an IC
model, package fit, board layout, RF behavior, or manufacturing readiness.

Structured voltage sources also accept `pulse: {v1, v2, delay, rise, fall,
width, period}` with finite numeric values and nonnegative timing. The period
must be positive and at least the width. A PULSE source cannot also specify
DC/AC values. Transient `output_trace` explicitly selects the node for the
steady-window ripple metric.

Exact trace targets can declare a physical unit and comparator:

```yaml
target_metrics:
  output_rail:
    metric: voltage
    trace: v(VOUT)
    unit: V
    comparator: between
    min: 3.2
    max: 3.4
```

Supported metrics are voltage, current and ripple; use `V`, `A` and `mV`
respectively. `between` uses `min`/`max`; `le` and `ge` use `value`. The
`abs_between`, `abs_le` and `abs_ge` comparators check magnitude. Matching uses
the exact trace name, ignoring case. Missing/ambiguous traces, unsupported
units, malformed limits and nonfinite values cannot verify a target.
Operating-point targets check their sole sample. DC sweep targets must state
`sample: endpoint`; this checks the final sample only and does not qualify the
entire sweep. Other DC sample policies remain unverified.

Run `python -m benchmarks.diverse_systems.active_suite --output <new-dir>`
from the repository for the authored generic op-amp/diode benchmarks. These
exercise supply/load cases and bad pin-map/limit controls; they do not establish
manufacturer model fidelity, RF performance, stability or hardware operation.

## Local library onboarding

Use `library-access` first to distinguish missing assets from missing functional
support. For local authored or vendor-supplied assets, the bundle API plans
contained copies and project table additions before applying them:

```text
circuit-weaver library-bundle plan manifest.json --project-root <project> --json
circuit-weaver library-bundle apply saved-plan.json --project-root <project> --json
```

Save the complete first response as `saved-plan.json`. The corresponding Python
functions in `circuit_weaver.library_bundle` are
`plan_library_bundle(project_root, manifest)` and
`apply_library_bundle(project_root, plan)`. MCP takes explicit absolute project
paths: `plan_library_bundle(project_root, manifest_json)` and
`apply_library_bundle(project_root, plan_json)`. JSON arguments are objects
encoded as strings.

```json
{"version":1,"assets":[
  {"kind":"symbol_library","source":"/absolute/vendor/parts.kicad_sym","nickname":"VendorParts","provenance":"vendor download, revision/date recorded by caller","license":"vendor terms reviewed by caller"},
  {"kind":"footprint_library","source":"/absolute/vendor/Packages.pretty","nickname":"VendorPackages","provenance":"vendor download, revision/date recorded by caller","license":"vendor terms reviewed by caller"}
]}
```

Model and SPICE entries use `kind: model` or `spice`, a local `source`, and a
`destination` below `libraries/`. SPICE entries additionally require explicit
`subckt` and `pin_order` metadata. File presence and declared metadata do not
verify a model or pin mapping. Provenance and license strings are user
assertions. The importer performs no downloads, creates no functional builder,
and makes no sourcing, electrical or package-fit claim.

Plans bind source bytes and existing tables with SHA-256, preserve unrelated
table entries and literal `${KIPRJMOD}` paths, reject stale or conflicting
state, and roll back touched assets if apply fails. Native validation and
engineering inspection still follow asset import.

Native worker, placement/import, schematic build and DRC staging preserve
project `fp-lib-table` and its declared literal `${KIPRJMOD}/...pretty`
libraries. Limits are 1 MiB per table, 256 table entries, 4,096 files, 8,192
traversal entries and 64 MiB aggregate library content. Links, reparse points,
escaping paths, ambiguous nicknames and changing assets fail explicitly.
Global library entries remain unchanged; this does not configure missing global
libraries or qualify their geometry. Temporary copies are not published.

Shared builds pass the circuit root even when the schematic is under `output/`.
Standalone callers can use `build-pcb ... --project-root <project>` or Python
`schematic_to_pcb(..., project_root=...)`. Symbol tables and authored schematic
settings still use the schematic directory. Placement/routing checkpoints watch
the optional footprint table, declared library files and tree membership,
including a table or asset appearing during a stage.

Native `review-pcb`, the shared authored routing gate and transient acceptance
windows are documented in [board quality review](board-quality-review.md).

## Bounded routing recovery

Routing recovery is explicit. For a native board, the CLI can retain the
router's partial copper privately, repair remaining same-net connections, then
publish through the ordinary complete-session import and native DRC gate:

```text
circuit-weaver autoroute placed.kicad_pcb --output routed.ses --routed-board routed.kicad_pcb --max-passes 8 --timeout 1200 --optimization-threads 0 --repair-open-connections --recovery-timeout 1800 --max-repair-links 512
```

Python uses `autoroute_pcb(..., repair_open_connections=True,
recovery_timeout_seconds=1800, max_repair_links=512)`. An already-partial PCB can
use `circuit_weaver.routing_recovery.recover_routing(source, destination, ...)`
directly. Inputs, settings, pad identities, footprint/text geometry and existing
copper are preserved. Recovery derives trace/via dimensions from the native
named netclass and project rules, combined with board minimums; missing or
ambiguous rules stop recovery. Every proposed connection needs fresh native DRC
with fewer opens and no other blocker. A final board requires zero blockers.
After native recovery, Freerouting 2.5 or newer writes the full session with
autorouting, fanout and optimization disabled. Session structure and net/design
correlation are checked before the original-board import and fresh native DRC;
this export step does not fabricate unavailable router completeness statistics.
For private recovery attempts using Freerouting 2.5+, a host budget of at least
61 seconds requests a cooperative router deadline 60 seconds before that attempt's
host deadline. This gives the router time to export a partial session. The host
deadline remains authoritative; missing sessions, unknown statistics and invalid
native geometry still fail closed. Portfolio evidence records each attempt's
actual allowance and requested internal deadline, without claiming that a
requested timeout occurred.
Private candidate reports and current progress remain available when the bounded
search cannot finish; they do not constitute completion or manufacturing approval.

The initial implementation supports rectangular repair inputs without zones.
For a workflow-authored plane, use these saved options with `plan_workflow`,
`run_workflow` or the existing MCP `options_json` argument:

```json
{
  "route_repair_open_connections": true,
  "route_recovery_timeout": 1800,
  "route_max_repair_links": 512,
  "route_max_passes": 8,
  "route_timeout": 1200,
  "route_optimization_threads": 0,
  "ground_plane_after_route": true
}
```

Keep the existing `ground_plane` plan and four-layer selection in those options.
Deferral makes all copper layers available during routing; the workflow then
prepares the authored plane definitions on its original input, imports the full
session, refills and independently verifies the saved plane geometry/connectivity.
Existing source zones are not removed. The normal defaults remain unchanged.

For four-layer GND planes whose thermal connections are obstructed by signal
routing, explicitly set `ground_plane_repair_thermals: true` in the same options.
This option requires `copper_layers: 4`, an authored `ground_plane` plan and
`ground_plane_after_route: true`. CLI, Python and MCP use the same saved option
and checkpoint hash. The Python session-import helper also accepts
`repair_plane_thermals=True`.

Repair runs only when fresh native DRC identifies thermal-connection errors
alone, with no open nets or other physical faults. It searches for outside-pad
GND stitching vias joining nearby non-dominant filled components to a dominant
same-net plane on another layer. The initial implementation requires rectangular
four-layer boards, multiple filled GND planes, an unambiguous dominant polygon,
and no other copper zones or keepouts. Search, via count, dimensions, clearances,
holes and silk are bounded; the via count cannot exceed the distinct error-pad
count or 16. Existing tracks, vias, component geometry, labels and authored rules
must survive unchanged. The worker refills and reloads the board, and final full
native DRC must pass before publication. Unsupported geometry or a failed search
leaves the candidate private. This is opt-in CAD repair and supplies no current,
thermal, RF, fabrication or hardware qualification.
The final PCB is authoritative. When stitching runs, the unchanged router SES
predates those native via additions; the result and durable `ground-plane.json`
record `session_includes_plane_stitching: false`, the session scope and the repair
details. Reproducing the final board requires the saved workflow options as well
as the router session. A clean board records repair as `not_needed`.

The parent reserves both router and recovery budgets plus bounded native
postprocessing time. This supplies an automated recovery path, not a guarantee
that every circuit fits the search bounds or meets electrical/impedance targets.

The repository validation runners `scripts/validate_app_workflow.py` and
`benchmarks/general_scale/run_general_scale.py` also accept
`--route-max-passes`, `--repair-open-connections` and `--recovery-timeout`.
Their defaults remain 20 passes with recovery disabled. Route subprocesses
reserve the router budget, active recovery budget and 600 seconds for session
transport, native import and DRC. Other command deadlines remain unchanged.
When recovery is enabled, the general-scale runner rejects a per-case
`--timeout` shorter than that combined route allowance. Evidence records the
effective route deadline and each constituent budget. Case contracts, source
hash checks, native DRC and assembly coverage remain required.

The unseen-scale CI suite uses the same pinned Freerouting 2.5 recovery profile
for all three cases: 20 passes, one 1800-second routing attempt and an optional
1800-second native recovery phase, within a 7200-second case deadline. The
separate basic native tests retain Freerouting 2.4.1 coverage. CI preserves the
private router logs and rejected candidates in its controlled validation-output
artifacts so failures can be investigated; those candidates are not accepted
workflow outputs. Full native DRC, authored path contracts and assembled model
coverage are required after recovery, just as for a direct route.

Rejected router sessions are retained alongside the attempt's private logs as
`rejected.ses`, with a hash, byte count and `published: false`. Attempt evidence
also retains available router-reported statistics and the validation failure.
These diagnostics do not change the error status or populate the requested
session output; reported zero opens cannot override an invalid session.

When the router reports zero opens but native import finds open connections,
opt-in recovery can continue only from the validated private session and a
complete, opens-only native rejection. It reimports into quarantine and requires
a new hash-matched native report before any repair. Missing reports, other
physical faults and ambiguous results remain errors. A failed native import
always reports routing as incomplete, regardless of the router's statistics.

The frozen 438-part DAC completes this eight-pass, post-route-plane recipe from
its zero-copper placement in the isolated installed runtime. It needs no native
repair on that run; all native DRC, 110-path, 144-leg, 72-return and assembly gates
pass. A one-pass trial remains incomplete. See the
[measured completion record](suite-circuit-completion.md#fresh-public-workflow).
