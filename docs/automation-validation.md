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
/path/to/validation-env/bin/python -m pip install --force-reinstall /path/to/circuit_weaver-0.37.0-py3-none-any.whl
/path/to/validation-env/bin/python scripts/validate_app_workflow.py --installed --expected-wheel /path/to/circuit_weaver-0.37.0-py3-none-any.whl --output /path/to/new-wheel-demo
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

To remove traces replaced by these planes, add
`--plane-cleanup /path/to/plane-cleanup.json` together with `--ground-plane`;
see [`plane-cleanup-pcb`](cli-reference.md#plane-cleanup-pcb). With all four
plans and assembled/MCP checks the run has 20 stages. It additionally retains
`routed-grounded.kicad_pcb`; the cleaned `routed.kicad_pcb` supplies every final
preview and delivery check. Full-shape native plane coverage, unchanged protected
trace paths and vias, zone refill, saved connectivity, DRC, labels and all input
and intermediate hashes are checked before success.

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
An explicitly unconnected `Mechanical:MountingHole` or `Mechanical:Fiducial`
with no symbol pins may retain its physical footprint. Every native pad,
including unnumbered pads, must have an empty net name and net code zero after
loading and after save/reload. The result lists these physical-only references
separately; electrical symbols still require exact pin/pad reconciliation.
Some legacy footprints assign bookkeeping numbers to non-plated holes that
KiCad clears on load. These qualify only when the symbol pin is typed
`no_connect` and has an explicit singleton no-connect node, or belongs to the
pinless physical-only case above. The importer retains the source pin numbers
in its report and verifies the complete hole inventory, positions, sizes,
drills, offsets and orientation through save/reload. It never converts a hole
into a copper pad or drops a connected pin. Ordinary unnumbered holes retain
their existing import behavior.
Native board creation can write a fresh project file. The importer restores
authored project/rule settings before DRC, including their rule severities,
and checks those bytes through validation. Explicit prototype clearance/drill
assumptions are merged only when source-owned rules permit them. A board with
authored silkscreen errors therefore remains blocked; a native default must
not silently downgrade those errors to warnings.
Project-local footprint nicknames may map to a different library directory using
literal `${KIPRJMOD}/relative/path.pretty` entries in the source project's
`fp-lib-table`. Explicit mappings take precedence over global libraries; missing,
duplicate, unsupported or escaping mappings fail rather than selecting a global
lookalike. Other URI variables and remote library types are unsupported here.

Forward generation currently creates a two-copper-layer rectangular prototype
with front-side footprints. Importing a multilayer schematic does not reproduce
its vendor PCB stackup, BGA escape routing, DDR timing or controlled impedance.
These require separate layout capabilities and engineering verification.

Outline dimensions and placement keepout rectangles are distinct constraints.
The native handoff checks the board dimensions against outline declarations
without interpreting keepout width or height as the size of the board.

The initial rectangular board uses `--width`/`--height` in millimeters and is
unrouted. The demo explicitly requests 0.127 mm default clearance because the
USB connector has 0.15 mm pad gaps; the native 0.2 mm default would reject it.
This is a declared prototype assumption, not a validated fabrication profile.
The default sample declares a 36 × 30 mm review outline and a bottom-facing J1
edge/orientation constraint. The runner uses those dimensions (or explicit
`--width`/`--height`) and passes the matching design into native placement.
Explicit dimension overrides update only the copied demo design, including its
placement constraints, so generation, board construction, and placement use
the same outline. Keepout rectangles retain their authored dimensions and do
not define the board outline. Evidence retains the original and effective sizes, overrides,
and copied-design hash. A single-axis override keeps the other declared dimension.
Functional ownership and edge intent survive placement, while native reference
cleanup is measured after saving and reloading. Layout review warnings and
unresolved labels are retained in the report; successful DRC alone does not
establish connector access or readable silkscreen.
With `--tidy-references`, a complete native report showing label conflicts can
trigger another placement attempt with more spacing, derived from the native
reference boxes. The first attempt retains the authored placement policy;
later attempts preserve its minimum gap, outline, locked parts and font sizes.
The search remains limited to three seeds and publishes only after physical
DRC and all native reference checks pass. Missing or invalid reports stop the
search instead of counting as label feedback.
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

KiCad 10 uses the official `10.0.6-full` image so the assembled-preview corpus
has the stock 3D library. Setup requires representative STEP files before
running the suite; every preview still verifies its complete per-component
model inventory and hashes.
The native CI job installs the MCP extra and imports its real stdio client
before running the corpus, which requires an actual MCP session for each case.
The harness preserves the selected virtual-environment executable path rather
than resolving it to the base Python binary, and checks MCP in that interpreter
before starting workflows.

## Physical inventory regression

`tests/test_native_sample_footprints.py` generates all 13 shipped sample specs
with the normal validation gates enabled, builds each native board, and compares
its saved footprint references with the physical assembly manifest. KiCad must
accept every symbol pin number against the actual local footprint pad numbers.
The test also checks real support-resistor nets, source preservation, and the
saved board hash. CI requires this check in each supported KiCad container.

Logical I²C pull-up containers have an explicit `is_virtual_container` identity.
Their schematic symbols are excluded from the BOM and board; their two actual
pull-up resistors remain in the schematic, physical manifest and PCB. A missing
footprint on an ordinary physical component still fails validation/import.

The two ESP32 samples use the native module footprint's 0.2 mm plated thermal
holes. Their corpus runs explicitly request `--minimum-drill 0.2`; the workflow
records this prototype assumption and passes it to `build-pcb`. Omitting the
option retains KiCad's default. The option cannot replace existing project
rules, does not ignore hole-size DRC findings, and does not establish a
fabrication process. The board importer also has a deterministic free-rectangle
packing fallback to use space beside a large module without reducing its
native courtyard or changing the requested outline.

The MAX17048 binding uses its 2 × 2 mm TDFN package including the grounded
exposed pad. The nRF52840-QIAA binding uses the Nordic aQFN73 alphanumeric pad
IDs throughout, including its supplies and DEC support. The RF and wearable
samples include a prototype 32 MHz crystal and declared load-cap assumptions;
production crystal selection and RF layout review remain required. These sample
corrections exercise physical automation and do not establish complete product
designs from their project names.

The MAX17048 footprint's exact TDFN model is absent from the standard KiCad
model library. Assembly previews can resolve this one file from a bundled,
dimensioned STEP generated by the official KiCad generator. Its body, pitch,
lead count, exposed pad, generating sources, hash and license are recorded in
`circuit_weaver/model_assets/inventory.json`. The native renderer includes the
asset and provenance hashes in its evidence. Installed standard models take
precedence; unrelated or incomplete custom libraries remain authoritative.
This package envelope is not a manufacturer CAD solid, and the preview does
not rewrite model references in the source board.

The ESP32 thermal pad has intentionally overlapping same-number GND lands.
Freerouting 2.4.1 reports 24 clearance findings on those fixed lands even before
routing, while native KiCad accepts their geometry. For native-board output,
the router wrapper compares structured baseline and final DRC reports in
millimeters. Every classified pair must retain its exact geometry, reference,
physical pad number and net, verified against both the DSN and native board.
The result retains the raw count and both pair tables. Any new, changed,
unmapped or foreign-net finding fails. DSN-only routing does not receive this
classification; imported native boards must still pass full KiCad DRC and
connectivity checks before publication.

## Repeat the sample corpus and scale benchmark

Run the complete sample corpus, then the three larger connected compositions:

```sh
python benchmarks/general_scale/run_general_scale.py --suite samples --require-clean --workers 2 --iterations 6000 --attempts 3 --route-timeout 600 --output /path/to/new-sample-evidence
python benchmarks/general_scale/run_general_scale.py --require-clean --workers 2 --iterations 6000 --attempts 3 --route-timeout 600 --output /path/to/new-scale-evidence
```

Each case requires all 16 stages, including native assembled previews and MCP.
The general suite additionally requires at least 100 physical populated parts
and verifies its declared analog channel endpoints on the saved native PCB.
It checks input connectors, amplifier/reference pins, any buffer, ADC filter
resistors and ADC pins. Fixture topology and physical count are separate gates.
The suite definitions and bounded signal assumptions live in
[`benchmarks/general_scale`](../benchmarks/general_scale/README.md).

Use `--case <id>` for a focused replay, and use `--python`, `--installed` and
`--expected-wheel` together for an exact installed artifact. The runner copies
inputs, retains failed runs, binds board/DRC/model hashes and detects changes to
the source revision, package bytes and input specs. Each workflow initializes
private project state so cases below a shared project do not update an ancestor
manifest. `--route-timeout` is the total budget shared across attempts.

`--require-clean` is required for release evidence. Omitting it permits recorded
exploratory runs on a stable dirty tree; those runs do not prove a committed
release. Parallel timings share host resources. These finite corpus results
measure the supported prototype path and do not establish arbitrary-circuit,
electrical, thermal, RF or fabrication correctness.
