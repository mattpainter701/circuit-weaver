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

`start` creates or initializes a selected project directory, optionally from
a JSON or YAML specification:

```text
circuit-weaver start <absolute-project-dir> --name sensor --json
circuit-weaver start <absolute-project-dir> --spec <spec.yaml> --json
```

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

`clearance_mm` and `minimum_drill_mm` are explicit prototype assumptions for
native board construction. They do not weaken authored project rules or bypass
physical DRC gates; use them only when those assumptions are appropriate for
the prototype.

## Python API

The same functions are available from `circuit_weaver.project_workflow`:

```python
from circuit_weaver.project_workflow import (
    cancel_workflow,
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

## MCP

Install the optional dependency with the same interpreter that will launch the
server, then configure the MCP client separately:

```text
<absolute-python> -m pip install "circuit-weaver[mcp]"
<absolute-python> -m circuit_weaver.mcp_server
```

The MCP server exposes `runtime_info`, the read-only `library_access` audit,
`start_project`, `open_project`,
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

The source contract supports only structured voltage/current sources with
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
