# Run the automated prototype workflow

From a source checkout with Python dependencies installed, install KiCad with
its Python API and standard libraries, plus Freerouting and its Java runtime.
`python -m circuit_weaver doctor` checks the tool discovery. The native PCB
guide in [placement and routing](routing-placement-search.md) describes runtime
overrides, including portable Freerouting installations.

Run a fresh, self-contained sample through the actual application commands:

```sh
python scripts/validate_app_workflow.py --output /path/to/new-demo
```

The default sample is the bundled LDO/USB power indicator. Use `--spec` with
another self-contained YAML spec and a new output directory for each run.
The runner copies the spec before running commands, preserves the source,
and refuses an existing evidence directory. No interactive answers are needed.
An agent such as Codex or Claude can execute the same command and inspect the
saved JSON and stderr logs without parsing a terminal transcript.

To validate a built wheel, install it in a separate environment and run the
script with that environment's Python. Pass `--installed --expected-wheel`
with the wheel's path to check the imported package against the actual wheel
payload before application commands run:

```sh
python -m venv /path/to/validation-env
/path/to/validation-env/bin/python -m pip install --force-reinstall /path/to/circuit_weaver-0.35.0-py3-none-any.whl
/path/to/validation-env/bin/python scripts/validate_app_workflow.py --installed --expected-wheel /path/to/circuit_weaver-0.35.0-py3-none-any.whl --output /path/to/new-wheel-demo
```

On Windows, use the environment's `Scripts/python.exe`. If the environment
shares global site packages, add `--ignore-installed` when installing the wheel
so a global package with the same version cannot satisfy the install. Install
the optional MCP dependencies as well when using `--mcp-smoke`.

Installed mode removes the source-path override from application subprocesses.
The expected-wheel check compares code and packaged resource bytes, rejects
missing or unexpected payload files, and records the wheel hash and matched
content evidence. `--installed` alone records the import path and content hash;
it cannot establish which wheel supplied that package. Evidence also records
the source revision and whether the checkout had uncommitted changes.

To exercise the real MCP stdio server as well, install the optional runtime
and enable its smoke test:

```sh
python -m pip install ".[mcp]"
python scripts/validate_app_workflow.py --output /path/to/new-mcp-demo --mcp-smoke
```

For a physical assembly preview with the board's installed library models, add
`--render-3d` (requires KiCad 9+ and resolvable model files). In this mode,
`board-preview.svg` is the assembled front view: a self-contained SVG embedding
KiCad's native model render at its original aspect ratio. It needs no external
PNG or model files to display. `board-layers.svg` retains the front copper/mask,
silkscreen, footprint body geometry and native outline, including cutouts.
The report opens the assembled view first and provides a separate layer control.
`board-preview-back.svg` remains the mirrored native back-layer view.
Without `--render-3d`, the portable default produces front and mirrored-back
layer SVG views on KiCad 8–10.

To apply an explicit local-return connection plan after routing, add
`--return-paths /path/to/return-paths.json`. The plan format is documented under
[`refine-pcb`](cli-reference.md#refine-pcb). This adds one checked workflow stage:
the original route is retained as `routed-base.kicad_pcb`, and the refined board
becomes `routed.kicad_pcb` for the delivery gates and previews. The plan is copied
into the evidence folder, and its input and copied hashes must remain unchanged.
Refinement requires a fully routed, DRC-passing input and publishes only after
full final DRC and reference verification. Component positions, labels, pad nets
and original copper are preserved; blocked connections fail the run.

To remove redundant selected-net copper and try shared vias, add
`--cleanup-plan /path/to/cleanup.json`; see
[`cleanup-pcb`](cli-reference.md#cleanup-pcb). This adds one checked stage and
keeps protected pad paths at or below their original lengths. With both plans,
the workflow retains `routed-base.kicad_pcb` and `routed-refined.kicad_pcb` before
the final cleaned `routed.kicad_pcb`; the assembled/MCP run has 18 stages.
All final previews and delivery checks use the cleaned board. The cleanup plan,
board/project/rule snapshots, native DRC and reference checks remain bound to
the final evidence. Existing workflows stay at 16 stages, or 17 with one plan.

To add filled native ground pours after routing, refinement and cleanup, pass
`--ground-plane /path/to/ground-plane.json`; see
[`plane-pcb`](cli-reference.md#plane-pcb). This adds one checked stage. With all
three plans and assembled/MCP checks, the workflow has 19 stages and retains
`routed-base.kicad_pcb`, `routed-refined.kicad_pcb` and `routed-cleaned.kicad_pcb`.
The final `routed.kicad_pcb` contains the filled planes and supplies all preview
and delivery checks. Plan hashes, actual native filled polygons and connectivity,
unchanged original tracks/vias, protected trace-only paths, final DRC and label
checks must pass. All intermediate evidence is rechecked before success.

The MCP pass lists all five tools, validates and generates the same design,
discovers projects, and verifies that malformed input, an empty research query,
and missing readiness evidence are returned as MCP tool errors.
It checks native KiCad verification on the generated schematic. It does not
invoke a second language model; the calling Codex/Claude session orchestrates
the application and retains its evidence.

## What is verified

The runner executes static validation, `generate --require-kicad`, confidence
reporting, `build-pcb`, native placement, routing/session import, and native
KiCad DRC on the final PCB. It checks BOM/CPL export and its identity/sourcing
blockers, checks source preservation, exercises status/resume, and exports a
front/back board previews. Preview evidence is bound to the routed board and
output hashes. Layer geometry must include a real outline and layer inventory.
Assembly SVG evidence must include a valid embedded PNG whose bytes and dimensions
match the native render, complete model-file and populated-component coverage,
and the same source-board hash. An unsupported renderer, missing or hidden-only
component model, or mismatched embedded image fails
the requested assembly preview.
Routing must be complete, DRC must have zero blockers,
and its SHA-256 must match the published board bytes. An unavailable router,
failed command, incomplete route, or mismatched DRC hash fails the run.
The demo explicitly disables Freerouting's post-route optimizer: the tested
2.4.1 optimizer narrowed fine-pitch traces below the imported track-width rule
despite disabled neckdown. Native routing without that optimizer preserves
the configured width and still passes final KiCad DRC.
Native routing varies stable placement declaration order in isolated DSN copies.
This addresses a reproduced KiCad 9 export that left one USB supply connection
unfinished in three identical primary-routing attempts: sorting placement
declarations completed the connection without changing geometry or rules.
Optimizer strategies alone provide no variation when optimization is disabled.
Every accepted result still requires complete routing and final native DRC.
Native-board autorouting also adds a 0.01 mm clearance margin to generic DSN
clearance rules, preserving typed exceptions. A reproduced KiCad 10 case routed
at exactly 0.127 mm measured 0.1263 mm after session import; the margin prevents
that observed discrepancy.
The source board and project rules remain unchanged, caller-provided DSN files
are routed unchanged, and final native DRC remains mandatory.

`index.html` links the visual artifacts and every command's stdout/stderr;
`evidence.json` records arguments, durations, exit codes, source revision,
final DRC, and board hash. Optional MCP results live in `mcp-transcript.json`.
Confidence reporting records skipped optional simulations; it cannot prove
SPICE behavior when ngspice or device models are missing.

## Prototype board handoff

The missing schematic-to-board step can also be called directly:

```sh
circuit-weaver build-pcb generated/Project.kicad_sch -o prototype.kicad_pcb --width 36 --height 30 --clearance 0.127
circuit-weaver place-pcb prototype.kicad_pcb -o placed.kicad_pcb --seed 7 --design design.yaml --tidy-references
circuit-weaver autoroute placed.kicad_pcb --routed-board routed.kicad_pcb --attempts 3
```

Resolve the root schematic from the generation result's `root_schematic` field;
do not guess its filename. `build-pcb` exports the schematic's netlist through
KiCad, loads the named local library footprints, and requires exact pin/pad
number reconciliation. It never invents missing footprints or infers pin swaps.
The initial rectangular board uses `--width`/`--height` in millimeters and is
unrouted. The demo explicitly requests 0.127 mm default clearance because the
USB connector has 0.15 mm pad gaps; the native 0.2 mm default would reject it.
This is a declared prototype assumption, not a validated fabrication profile.
The default sample declares a 36 × 30 mm review outline and a bottom-facing J1
edge/orientation constraint. The runner uses those dimensions (or explicit
`--width`/`--height`) and passes the matching design into native placement.
Explicit dimension overrides update only the copied demo design, including its
placement constraints, so generation, board construction, and placement use
the same outline. Evidence retains the original and effective sizes, overrides,
and copied-design hash. A single-axis override keeps the other declared dimension.
Functional ownership and edge intent survive placement, while native reference
cleanup is measured after saving and reloading. Layout review warnings and
unresolved labels are retained in the report; successful DRC alone does not
establish connector access or readable silkscreen.
Omitting `--clearance` preserves native defaults, and existing source board
rules cannot be weakened by this option. Outputs remain separate from the input; replacement requires
`--overwrite`. The prototype path supports direct sibling schematic sheets;
missing or nested sheet files are rejected. Footprints must resolve through
the local library resolver, and failures identify the mismatch.
For the builtin USB4085 power connector, generation selects the installed
library's `S1` or `SH` spelling for the unused shield only when the complete
pad-number set matches the known variant. Connected pins are never implicitly
renumbered; board import still requires exact pin/pad numbers.

The motor-controller sample also exercises a complete 48-pad STM32F103C8T6
definition. Its pin numbering and supply connections follow the manufacturer's
[STM32F103x8/xB datasheet](https://www.st.com/resource/en/datasheet/stm32f103c8.pdf).
Unused GPIOs are explicit no-connects in the sample, rather than omitted symbol
pins. It declares a compact 45 × 35 mm engineering-review outline. This is a
minimal power/MCU automation example; external motor drivers,
programming access, and application-specific electrical review remain required.

## Manufacturing remains a separate gate

These are prototype artifacts for engineering review. Successful routing and
DRC do not verify manufacturer package identity, approve placement, validate
switching-loop geometry, or establish high-speed, thermal, safety, or fabrication
constraints. BOM/CPL reconciliation does not establish part availability.

The default sample's `export-jlcpcb` delivery is blocked because verified identity
bundles and LCSC sourcing numbers are absent. The report retains the exhaustive
BOM and blocking reasons, and exports a separate native prototype position CSV
for review. That position file is not an approved assembly CPL.

The demo deliberately verifies that `export-gerbers` refuses manufacturing
publication without canonical readiness evidence. This refusal is a successful
gate check, clearly recorded in the report. It does not create readiness
overrides or assert `fabrication_ready`. Complete the authoritative identity,
placement approval, board constraints, and manufacturing evidence workflow
before ordering a board.

CI runs the generated-design path in official KiCad 8, 9, and 10 containers
pinned by digest, plus pinned Freerouting. It checks the CLI/Python versions
and installed libraries before testing. Native artifacts and subprocess
failure diagnostics are retained with each CI run.
