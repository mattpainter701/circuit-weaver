# CLI Reference

All commands use the `circuit-weaver` entry point (or `python -m circuit_weaver`).

## validate

Validate a canonical or legacy design spec.

```bash
circuit-weaver validate <spec.yaml> [--strict] [--enrich-parts]
```

| Flag | Description |
|-|-|
| `--strict` | Treat warnings as errors (production gate) |
| `--enrich-parts` | Query LCSC/DigiKey to fill missing part data before validation |

**Exit codes:** 0 = valid, 2 = validation errors found.

---

## generate

Generate KiCad artifacts from a validated design spec.

```bash
circuit-weaver generate <spec.yaml> --output <dir> [flags]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Output directory (required) |
| `--no-require-valid` | Bypass soft electrical findings only; structural and implementation errors still fail |
| `--no-readiness-gate` | Debug-only bypass for placement-readiness errors |
| `--no-svg` | Skip SVG export |
| `--presentation-profile` | `default` or `review` (changes schematic layout) |
| `--enrich-parts` | Query distributors for missing part data |
| `--score` | Include electrical quality score in report |
| `--auto-source` | Auto-discover blank MPNs via DigiKey/Mouser APIs; caches results for 30 days |
| `--update-spec` | Write discovered MPNs/LCSC back to the original YAML spec (requires `--auto-source`) |
| `--placement-review` | Generate the exhaustive placement review bundle (enabled by default) |
| `--no-placement-review` | Skip placement review generation |
| `--pinout` | Emit pinout CSV/config stubs even for non-MCU designs |
| `--require-kicad` | Fail unless the exact final schematic passes real `kicad-cli` ERC |

**Outputs:** a root `.kicad_sch`, functional sub-sheets when the spec has
multiple block `section` values, `_report.md`, `assembly_manifest.json`,
`placement_result.json`, `placement_review_context.json`, `placement.svg`,
`placement_editor.html`, `artifact_manifest.json`, and optional firmware
artifacts for a valid reconciled placement inventory. A blocked/empty inventory
publishes only truthful status rather than misleading visuals. Explicit
functional sections remain separate even on small designs,
and generated support passives stay with their owning block. Read paths from
the manifest instead of assuming filenames. Manifest paths are relative to its
directory; verification fields distinguish generated-but-unverified output
from a schematic checked by KiCad.

The placement bundle is an exhaustive heuristic review aid, never a PCB or
fabrication artifact. `placement_result.json` is published only after its
reference inventory reconciles exactly with `assembly_manifest.json`.
`placement_review_context.json` contains constraints, review blockers,
targeted research prompts, and available official/reference-layout links.
Unresolved footprint geometry/dimensions, sourcing, overlaps, board bounds,
support ownership, or constraints keep the review blocked.

**Example:**
```bash
# Auto-discover components and update spec
circuit-weaver generate design.yaml -o /tmp/out --auto-source --update-spec

# Generate and require real KiCad verification of the final hierarchy
circuit-weaver generate design.yaml -o /tmp/out --require-kicad
```

---

## apply-patch

Apply a transactional patch to a design spec. Validates before accepting.

```bash
circuit-weaver apply-patch <spec.yaml> <patch.yaml> [--output <file>] [--enrich-parts]
```

| Flag | Description |
|-|-|
| `--output` | Write updated spec to file (default: stdout) |
| `--enrich-parts` | Enrich parts before validation |

**Exit codes:** 0 = patch accepted, 2 = patch rejected (validation failed).

---

## diff

Compare two design specs structurally.

```bash
circuit-weaver diff <old.yaml> <new.yaml> [--svg] [--output <file>]
```

| Flag | Description |
|-|-|
| `--svg` | Generate visual HTML diff with side-by-side SVG schematics |
| `--output`, `-o` | Write HTML report to file (requires `--svg`) |

**Without flags:** JSON semantic diff to stdout.
**With `--svg`:** HTML report with color-coded block changes.

---

## ingest-pcb-feedback

Merge PCB layout constraints back into the design spec.

```bash
circuit-weaver ingest-pcb-feedback <spec.yaml> <feedback.yaml> [--output <file>]
```

Accepts constraint additions (placement, routing) and approved component substitutions.

---

## import-design

Inventory an existing design without modifying its source files.

```bash
circuit-weaver import-design <source> [--project-dir <dir>] [--analyze] [--force] [--timeout <seconds>]
```

`source` may be a KiCad file, KiCad project directory, Gerber/drill directory,
or ZIP archive. The command writes durable state to
`<project>/.circuit-weaver/project.json`; ZIP contents are safely staged under
that internal directory. A single KiCad file also inventories related sibling
files.

| Flag | Description |
|-|-|
| `--project-dir`, `-o` | State directory (default: source directory; ZIPs use a sibling folder) |
| `--analyze` | Run every applicable bundled analyzer after inventory |
| `--force` | Explicitly replace changed import staging/source identity and rerun analysis |
| `--timeout` | Per-analyzer timeout in seconds (default: 300) |

Netlist-only sources are inventoried but report analysis as unsupported; a
netlist has no schematic presentation or physical-layout evidence.

---

## analyze-design

Analyze all registered schematic, PCB, and Gerber sources for a project.

```bash
circuit-weaver analyze-design <project> [--force] [--timeout <seconds>]
```

Results and fingerprints are stored under `.circuit-weaver/analysis/`; valid
unchanged results are reused unless `--force` is passed. The command exits 0
only for `status: analyzed` and exits 2 for failed or unsupported analysis.

---

## status and resume

Read reconciled durable project state or produce a deterministic restart plan.

```bash
circuit-weaver status <project> [--json]
circuit-weaver resume <project> [--json]
```

Both commands accept a project directory or a file inside it. `status` reports
source/artifact inventory, validation, modified or missing recorded files, and
safe next actions. `resume` does not mutate or automatically execute those
actions; it verifies that the project is resumable and prints the phase and
restart plan. Use these commands for workflow recovery; `log-status` and
`log-view` remain diagnostic views of append-only logs.

---

## cache

Manage the symbol and parts cache (30-day TTL at `~/.cache/circuit-weaver/symbols/`).

```bash
circuit-weaver cache <action> [flags]
```

**Subcommands:**

| Action | Description |
|-|-|
| `stats` | Show cache hit rate, size, and entry count |
| `clear [--stale-only]` | Clear cache (default: all entries; `--stale-only`: older than 30 days) |

**Example:**
```bash
circuit-weaver cache stats         # Show cache statistics
circuit-weaver cache clear --stale-only  # Remove expired entries
```

---

## import-placement

Import SVG placement edits back into .kicad_pcb and CPL files.

```bash
circuit-weaver import-placement <placement.svg> <board.kicad_pcb> [flags]
```

| Flag | Description |
|-|-|
| `--output-pcb`, `-o` | Write updated .kicad_pcb to this path (default: overwrite input) |
| `--output-cpl` | Destination when an existing sibling `<board-stem>_cpl.csv` is updated |
| `--dry-run` | Preview changes without writing files |
| `--allow-partial` | Intentionally update only SVG-listed PCB refs; unknown SVG refs still fail |

**Workflow:**
1. `circuit-weaver generate design.yaml -o /tmp/out` creates the review bundle.
2. Inspect `placement_result.json` and `placement_review_context.json`; resolve blockers.
3. Edit/export `/tmp/out/placement.svg` in the generated HTML editor or a vector editor.
4. Create a real pad-bearing PCB with KiCad **Update PCB from Schematic**.
5. Dry-run and then import the SVG into that real PCB.

By default, the SVG and PCB reference inventories must match exactly. Duplicate,
unknown, missing, malformed, or non-finite placements block the write. Use
`--allow-partial` only for an intentional subset update.

**Example:**
```bash
# Dry-run to preview changes
circuit-weaver import-placement placement.svg design.kicad_pcb --dry-run

# Apply changes
circuit-weaver import-placement placement.svg design.kicad_pcb -o design_updated.kicad_pcb

# Explicit subset update
circuit-weaver import-placement subset.svg design.kicad_pcb \
  -o design_updated.kicad_pcb --allow-partial
```

---

## list-templates

List all available subcircuit templates.

```bash
circuit-weaver list-templates [--json] [--verbose] [--include-data-driven]
```

| Flag | Description |
|-|-|
| `--json` | Machine-readable JSON output |
| `--verbose` | Include full parameter schema with types, defaults, and options |
| `--include-data-driven` | Also list ICs available via JSON data store (`ic_data/`) |

---

## register-ic

Register a new IC in the data-driven template system. Accepts JSON from a file or stdin.

```bash
# Register from a file (dict of MPN → IC data)
circuit-weaver register-ic --file new_ics.json

# Register a single IC from stdin
echo '{"topology": "buck", "vref": 0.6, ...}' | circuit-weaver register-ic --mpn TPS54308
```

| Flag | Description |
|-|-|
| `--file`, `-f` | JSON file with IC data |
| `--mpn` | MPN name (required when input is a single IC object) |

The IC is persisted to `ic_data/custom.json` and is immediately available for use in design specs.

---

## scaffold

Generate a YAML design spec stub from a template.

```bash
circuit-weaver scaffold [--template TYPE] [--ref REF] [--output <file>]
```

| Flag | Description |
|-|-|
| `--template` | Template type (e.g., `buck`, `ldo`, `i2c_bus`) |
| `--ref` | Reference designator (e.g., `U1`) |
| `--output`, `-o` | Write to file (default: stdout) |

**No arguments:** Lists available templates.

---

## cost-bom

Show costed BOM with LCSC pricing at volume breaks.

```bash
circuit-weaver cost-bom <spec.yaml> [--qty 1,10,100,1000] [--json]
```

| Flag | Description |
|-|-|
| `--qty` | Comma-separated build quantities (default: 1,10,100,1000) |
| `--json` | Machine-readable JSON output |

Queries the LCSC/jlcsearch API for real-time pricing. Flags out-of-stock and unresolvable parts.

---

## export-jlcpcb

Export JLCPCB-formatted delivery files for assembly ordering.

```bash
circuit-weaver export-jlcpcb <spec.yaml> --output <dir> [--pcb <board.kicad_pcb>]
```

Without `--pcb`, the command intentionally emits a BOM-only delivery and does
not invent placement coordinates. With `--pcb`, the real board must reconcile
the full assembly reference and footprint inventory before CPL publication.

**Outputs:** `bom_jlcpcb.csv`, optional `cpl_jlcpcb.csv`,
`README_jlcpcb.txt`, and `delivery_manifest.json`. The delivery manifest is the
commit marker and records `ok`, `bom_only`, or a blocking failure.

---

## export-dual-cpl

Export top and bottom CPL files from a real, reference-reconciled PCB.

```bash
circuit-weaver export-dual-cpl <spec.yaml> --pcb <board.kicad_pcb> \
  --output <dir> [--assembly-mode single-sided|dual-sided-simultaneous|dual-sided-sequential]
```

`--pcb` is required. Heuristic placement JSON/SVG is never accepted as
manufacturing CPL evidence. Outputs are `cpl_top.csv` and `cpl_bottom.csv`.

---

## export-gerbers

Export Gerber and drill files from a KiCad PCB.

```bash
circuit-weaver export-gerbers <board.kicad_pcb> --output <dir>
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Output directory (required) |

Requires KiCad CLI (`kicad-cli`) to be installed and on PATH.

---

## design-wizard

Interactive offline design wizard. No agents or APIs required.

```bash
circuit-weaver design-wizard [--output <file>] [--dry-run] [--resume <path>] \
  [--research-backend auto|sonar-pro|standard] [--research-depth fast|normal]
```

**Features:**
- Grouped form-like sections (Basic Info, Power Supply, Components & Interfaces)
- Captures project context, purpose, power requirements, interfaces, MCU, components
- Auto-creates project directory with `design.yaml` and `design.log`
- All input logged for troubleshooting and resuming

**Workflow:**
```
1. Creates project folder
2. Guides through 3 sections of questions (Press Enter to skip/use defaults)
3. Generates design.yaml scaffold
4. Saves design.log with full workflow history
5. Shows log-view and log-status commands for next steps
```

| Flag | Description |
|-|-|
| `--dry-run` | Run wizard with default answers (testing) |
| `--resume` | Resume from existing design.yaml |
| `--output`, `-o` | Save to this exact file |
| `--research-backend` | Select research backend (default: auto) |
| `--research-depth` | Select latency-first `fast` or fuller `normal` research |

---

## build-pcb

Forward-annotate a root schematic into a separate prototype board using actual
local library footprints, exact symbol pin/pad numbering, and native KiCad.

```bash
circuit-weaver build-pcb <root.kicad_sch> --output <prototype.kicad_pcb> \
  [--width 80] [--height 60] [--clearance 0.127] [--overwrite] [--kicad-python-path <python>]
```

Dimensions and optional default clearance are in millimeters. Explicit clearance
is allowed only for a new prototype project without existing board rules or
netclass settings. Missing footprints, pin/pad mismatches, annotation errors,
unsupported hierarchy paths, or physical DRC blockers prevent publication.
Source files remain unchanged; existing output requires `--overwrite`.
Repeated physical pads with the same pin number receive the same net.
The result always reports `fabrication_ready: false`; identity evidence and
manufacturing approval remain separate. See the [real workflow demo](automation-validation.md).

## preview-pcb

Draw a standalone board SVG with either native layer geometry or the assembled
board's actual KiCad component models. The default `--appearance layers` shows
outline/cutouts, copper, mask openings, silkscreen and footprint body outlines.
Layer back views are mirrored to show the physical underside.

```bash
circuit-weaver preview-pcb routed.kicad_pcb -o board-preview.svg
circuit-weaver preview-pcb routed.kicad_pcb -o board-preview-back.svg --side back
circuit-weaver preview-pcb routed.kicad_pcb -o board-preview.svg --appearance assembly
circuit-weaver preview-pcb routed.kicad_pcb -o board-preview-back.svg --appearance assembly --side back
```

The layer view requires KiCad 8–10 CLI and its `pcbnew` Python API.
`--appearance assembly` requires KiCad 9+ with every declared model file resolvable.
It embeds the native transparent PNG inside the SVG, preserving the render's
pixel frame and aspect ratio. Component bodies, pads, board and printed labels
come from KiCad's renderer and the footprint's actual model references. This is
an embedded raster image in a portable SVG container; it does not convert 3D
models into vector paths or register a raster overlay onto plotted layers.
The SVG needs no adjacent PNG or model files to display.

The result records source/output hashes, native tool version, embedded-image
hash, model-file coverage and populated-component coverage. Populated electrical
footprints must each declare a visible model; hidden-only models do not count.
Explicit DNP/board-only and mechanical footprints without electrical pads are
excluded from this component requirement. A missing model or unavailable renderer fails an
assembly request without replacing an existing SVG. With the default layer
appearance, the older `--render-3d` option still creates a separate native PNG
beside the layer SVG. With assembly appearance it uses the same embedded render
and does not create a second PNG.

Use `--overwrite` to replace previews, `--timeout` for native process limits,
`--kicad-cli-path` for an explicit CLI, and `--kicad-python-path` for its Python.
Source board/project/rule files are staged separately. These are engineering
views; rendering does not verify DRC or grant manufacturing approval.

## place-pcb

Place actual footprints on an unrouted rectangular board and check physical DRC.

```bash
circuit-weaver place-pcb board.kicad_pcb -o board_placed.kicad_pcb --iterations 5000 --seed 7
circuit-weaver place-pcb board.kicad_pcb -o board_placed.kicad_pcb --design design.yaml --tidy-references
```

Requires real pads, nets, a closed rectangular Edge.Cuts outline, and KiCad's
`pcbnew` Python API. Copper tracks and zones must be absent. Locked footprints
and board sides are preserved. `--output` is required and must differ from the
source. `--overwrite` allows replacement of a previous output; failures preserve
it. `--kicad-python-path` overrides interpreter discovery. The returned DRC may
include expected unconnected items because placement precedes routing; all other
DRC errors block publication. Project `.kicad_pro` and `.kicad_dru` files are copied.

`--design` binds matching YAML/JSON design intent to the native reference,
footprint and net inventory. It retains functional blocks and generated support
ownership, so bypass capacitors and terminations stay with their owning parts.
Stale or mismatched intent fails. Board dimensions must already match Edge.Cuts;
placement never resizes an existing board. Explicit edge constraints can include
`rotation` while leaving the position along that edge free to optimize:

```yaml
pcb_constraints:
  - {kind: placement, target: J1, edge: bottom, rotation: 0, max_distance_mm: 0.5}
```

The edge applies to the footprint envelope, with the configured board clearance.
Determine the mating direction from the actual footprint drawing; a J reference
alone cannot establish it. `layout_review` reports missing connector intent and
sparse-board warnings separately from DRC and manufacturing approval.
Native placement uses the true courtyard center even when the library footprint
origin is at a pin. Pad offsets and fixed coordinates are transformed between
that center and the native origin; output retains native footprint coordinates.
Support-distance diagnostics identify whether they measured connected non-ground
pad endpoints or used a component-center fallback. These distances are advisory,
not a substitute for part-specific layout limits.
`--tidy-references` relocates native reference fields to readable positions and
reports unresolved collisions; locked footprints retain their reference fields.

## plane-pcb

Add native filled copper zones to an already routed board while retaining every
existing track, via, component pose and reference field.

```bash
circuit-weaver plane-pcb cleaned.kicad_pcb -o grounded.kicad_pcb --plan ground-plane.json
```

The strict JSON plan specifies all zone settings and any protected same-net pad
pairs. Protected pairs may belong to another net, such as a digital supply:

```json
{"net":"GND","layers":["F.Cu","B.Cu"],"edge_clearance_mm":0.5,"clearance_mm":0.25,"min_thickness_mm":0.2,"thermal_gap_mm":0.25,"thermal_spoke_width_mm":0.3,"protected_connections":[{"from_ref":"C1","from_pad":"1","to_ref":"U1","to_pad":"1"}]}
```

The current operation supports two-layer boards with one closed, axis-aligned
rectangular Edge.Cuts outline and no existing zones. It creates one zone on each
outer copper layer, uses thermal pad connections, removes unconnected islands,
and fills with KiCad's native zone filler. Unsupported geometry is rejected.
The report measures actual filled polygons after save/reload and retains
trace-only path lengths independently of the added plane. These path measurements
exclude via barrels and do not estimate impedance or inductance.

Input and final native DRC, fill connectivity, exact original geometry, reference
checks and unchanged board/project/rule snapshots gate atomic publication.
Use a separate output board; `--overwrite` permits replacement only after all
checks pass. `--kicad-python-path` overrides native interpreter discovery.

## plane-cleanup-pcb

Remove redundant unlocked straight traces covered by an existing filled same-net
plane, preserving all vias and the exact copper used by protected pad paths.

```bash
circuit-weaver plane-cleanup-pcb grounded.kicad_pcb -o cleaned.kicad_pcb --plan plane-cleanup.json
```

The strict JSON plan has exactly `net` and `protected_connections`:

```json
{"net":"GND","protected_connections":[{"from_ref":"C1","from_pad":"1","to_ref":"U1","to_pad":"1"}]}
```

The native engine compares each candidate's complete copper shape with the filled
plane polygons on its own layer. It retains uncovered, locked and protected
traces and all vias. It refills the zones and verifies coverage again after
save/reload; plane outlines and settings, other-net copper, components, models,
pad nets and labels stay unchanged. Protected paths may include other nets such
as VDD. Their trace-only geometry and metrics remain independent of the planes.
On KiCad 8, missing curve-conversion bindings require a conservative enclosing
rectangle for each straight trace. The complete rectangle must fit inside the
plane; this can retain extra traces near plane boundaries or holes.

Use a separate output file. Full input/final native DRC, saved filled geometry,
native connectivity, exact permitted deletions, references and unchanged input
board/project/rules gate atomic publication. `--overwrite` and
`--kicad-python-path` have the same meanings as for `plane-pcb`.

## cleanup-pcb

Remove redundant selected-net copper and try shared via pairs without worsening
explicitly protected pad-to-pad paths.

```bash
circuit-weaver cleanup-pcb routed.kicad_pcb -o cleaned.kicad_pcb --plan cleanup.json
```

The strict JSON plan has exactly `nets` and `protected_connections`:

```json
{"nets":["GND"],"protected_connections":[{"from_ref":"J1","from_pad":"1","to_ref":"J2","to_pad":"1"}]}
```

Protected entries can include a positive `max_length_mm`; the effective ceiling
also retains the original measured path length, with a small numerical geometry
tolerance. Unselected nets, locked copper, pad/net identity, component positions
and reference fields remain unchanged. Bounded shared-via proposals may add new
selected-net tracks while replacing several transitions with a common pair.
The output reports before/after copper counts, removed/added item IDs and each
protected path. These are planar centerline/contact proxies, excluding via
barrels, rather than electromagnetic or current-capacity results.

The current search supports two-layer boards without copper zones or copper
arcs. Input and final boards must pass full native DRC, reference checks and
save/reload verification. Board/project/rule snapshots must remain unchanged.
Separate output is mandatory; `--overwrite` permits atomic replacement after
all gates pass. `--kicad-python-path` overrides interpreter discovery.

## refine-pcb

Add short, explicitly requested same-net connections to an already routed board.

```bash
circuit-weaver refine-pcb routed.kicad_pcb -o refined.kicad_pcb --connections return-paths.json
```

The strict JSON plan contains a `connections` list. Each entry names
`from_ref`, `from_pad`, `to_ref`, and `to_pad`, with an optional positive
`max_length_mm` limiting the added planar connection length. Endpoints must be
unique physical pads on the same net. No component coordinates, pad nets,
reference fields or original copper may change. The bounded local search tries
front-side paths and back-side paths through vias; unsupported or blocked
connections fail without replacing existing output.

The current search supports two-layer boards without copper zones and endpoints
accessible on front copper. It can reuse existing same-net through vias. Copper
arcs and unsupported native geometry fail closed.

Both input and final board must pass full native DRC. Native reference checks
also remain enforced. Board/project/rule inputs are preserved, and publication
is atomic. `--overwrite` permits replacing an existing output; a separate output
is always required. `--kicad-python-path` overrides interpreter discovery.
This command adds copper links; it does not claim electromagnetic simulation,
loop inductance or manufacturing readiness.

## autoroute

Route a real KiCad PCB or user-exported Specctra DSN using Freerouting.

```bash
circuit-weaver autoroute <board.kicad_pcb|design.dsn> [--output <file.ses>] \
  [--effort fast|medium|high] [--timeout <seconds>] [--overwrite]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Validated Specctra `.ses` output (default: input stem with `.ses`) |
| `--effort` | Pass preset: `fast=100`, `medium=500` (default), `high=1000` |
| `--max-passes` | Exact pass limit overriding `--effort`; 0 means unlimited |
| `--timeout` | Routing timeout in seconds (default: 300) |
| `--attempts` | 1-32 candidates sharing the routing timeout (default: 1); native placement declaration orders, advertised seeds, or optimizer strategies |
| `--overwrite` | Atomically replace existing DSN/SES outputs |
| `--headless`, `--no-headless` | Enable/disable Freerouting GUI (headless by default) |
| `--optimization-threads` | Optimizer thread count; 0 disables optimization |
| `--optimizer-strategy` | `greedy`, `global`, or `hybrid` |
| `--optimizer-hybrid-ratio` | Required positive `m:n` ratio with hybrid strategy |
| `--optimizer-item-selection` | `sequential`, `random`, or `prioritized` |
| `--optimizer-improvement-threshold` | Optimizer stopping percentage |
| `--seed` | Seed only when the installed router advertises `-random_seed` |
| `--freerouting-path` | Freerouting launcher/JAR path |
| `--kicad-cli-path` | `kicad-cli` used only after a Specctra capability probe |
| `--kicad-python-path` | Native KiCad Python interpreter, also settable with `CIRCUIT_WEAVER_KICAD_PYTHON` |
| `--routed-board` | Separate `.kicad_pcb` output: import the session and require KiCad DRC before publication |

Requires Freerouting to be installed separately.

For a broader search, use `--attempts 3 --timeout 600`. Each attempt
receives an equal share of the remaining routing time; failed attempts do not
discard earlier successes. The selected session minimizes incomplete connections,
then prefers reported-clear routing, fewer vias, and fewer trace segments.
Segment count is a tie-breaker, not a wire-length measurement. Missing measurements
never count as zero. The JSON `search` object records every attempt and its seed.
Native boards first use ascending footprint-reference declaration order, then
descending and original export order in isolated DSN copies. Geometry, nets,
outlines, rules, and the original export stay unchanged. This provides primary
routing variation while `--routed-board` keeps optimization disabled by default.
Direct DSN inputs are routed unchanged; multiple attempts without native board
publication default to one optimizer thread. Builds advertising seed support
use consecutive seeds starting at `--seed` (or zero); other builds cycle through
greedy, global, and hybrid strategies unless an explicit strategy was supplied.
Optimizer strategies provide variation only when optimization is enabled.
Explicit `--seed` still requires advertised support. Tool version and thread settings
affect reproducibility. Input changes during search abort publication.

The board preflight rejects review/preview files, missing pads, missing named
nets, and mismatched pad-net declarations, including KiCad 10's named-pad format.
Native `pcbnew` performs automatic DSN export when `kicad-cli` lacks that command.
You can also export `.dsn` in KiCad PCB Editor and pass it directly.

The command stages output, validates the DSN and SES structure and net
correlation, requires known connection-completeness and clearance statistics,
and publishes only after those checks. `status: partial` exits 2 when the
router truthfully reports incomplete connections. A successful output is still
a Specctra session unless `--routed-board` is supplied with a source PCB. That
option imports routes, checks source/settings hashes, and publishes a separate
PCB only after native KiCad DRC passes. Otherwise import the session in KiCad
and run DRC. Power, switching loops,
differential pairs, RF, clocks, crystals, and other critical nets require
manual engineering rather than blanket autorouting.

---

## optimize-placement

Run simulated annealing placement optimizer on a design spec.

```bash
circuit-weaver optimize-placement <spec.yaml> [flags]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Write placement JSON to file |
| `--board-width` | Board width in mm (default: 100) |
| `--board-height` | Board height in mm (default: 80) |
| `--strategy` | `simple`, `thermal`, `si`, `cost`, `balanced` (default: balanced) |
| `--specs-dir` | Path to specs/ directory with thermal/SI JSON |
| `--iterations` | Total SA iterations shared across restarts (default: 5000) |
| `--restarts` | Independent placement candidates, 1-32 (default: 3) |
| `--refinement-passes` | Fine-placement passes per candidate, 0-20 (default: 2) |
| `--seed` | Random seed for reproducibility |
| `--json` | Machine-readable JSON output |

Multi-objective optimization considering overlap, boundary, thermal proximity, and zone affinity. Reads thermal data from `specs/ic_thermal.json` and SI data from `specs/si_params.json` (generated by `harvest-specs`).

Candidates are legalized and refined before selection. Hard defects (including
fixed positions, keepouts, clearances, and declared placement constraints) outrank
weighted preferences. The legal initial layout remains a fallback. Fixed positions
retain their exact coordinates, and support parts follow owners moved by legalization.
`simple` skips annealing and refinement. JSON output includes `search` diagnostics
and initial/final `routing_estimate` values for optimized layouts. These are
component-center half-perimeter wire-length (HPWL) estimates excluding ground nets;
they are not routed copper lengths, congestion analysis, or pad-level verification.
Fine refinement is bounded separately from the SA iteration budget and adds runtime.
See [search design and measured comparison](routing-placement-search.md).

---

## placement-viewer

Generate an interactive HTML PCB placement viewer.

```bash
circuit-weaver placement-viewer <spec.yaml> --output <file.html> [flags]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Output HTML file path (required) |
| `--board-width` | Board width in mm (default: 100) |
| `--board-height` | Board height in mm (default: 80) |
| `--specs-dir` | Path to specs/ directory for thermal overlay |
| `--strategy` | Placement strategy (default: balanced) |
| `--iterations`, `--restarts`, `--refinement-passes`, `--seed` | Same search controls as `optimize-placement`; viewer seed defaults to 0 |

Runs the placement optimizer, then generates an interactive HTML page with:
- Click to highlight nets (connected components glow, others dim)
- Hover tooltips (MPN, value, footprint, position, power dissipation)
- Thermal heatmap overlay toggle
- CSV export button

---

## harvest-specs

Download datasheets and extract structured specs for all BOM components.

```bash
circuit-weaver harvest-specs <spec.yaml> [--output <dir>] [--skip-download] [--delay 0.5] [--json]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Project output directory (default: current dir) |
| `--skip-download` | Extract specs from API data only, skip PDF downloads |
| `--delay` | Seconds between API calls (default: 0.5) |
| `--json` | Machine-readable JSON output |

Queries LCSC/DigiKey APIs for each component, downloads datasheets to `datasheets/`, and extracts parametric specs to `specs/` (ic_thermal.json, passives.json, si_params.json).

---

## extract-specs

Parse downloaded PDF datasheets and extract structured metadata to JSON.

```bash
circuit-weaver extract-specs <datasheets_dir> [--output <dir>] [--json]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Output directory for spec JSON files (default: specs) |
| `--json` | Machine-readable JSON output |

Requires `pypdf` (`pip install pypdf`). Extracts thermal characteristics (θJA, Pdiss, Tj_max), electrical specs (Vin, Vout, Iq, Fsw), and passive specs using regex patterns.

---

## fetch-spice

Download SPICE models and S-parameter files for analog/RF components.

```bash
circuit-weaver fetch-spice <spec.yaml> [--output <dir>] [--with-s-params] [--delay 0.5] [--json]
```

| Flag | Description |
|-|-|
| `--output`, `-o` | Project output directory (default: current dir) |
| `--with-s-params` | Also attempt to fetch S-parameter files |
| `--delay` | Seconds between download attempts (default: 0.5) |
| `--json` | Machine-readable JSON output |

Tries known manufacturer URL patterns (TI, ADI, Microchip, ON Semi) for SPICE models. Graceful degradation when models aren't available.

---

## log-status

Show workflow log summary for a project directory.

```bash
circuit-weaver log-status <project_dir>
```

Displays high-level status: current step, entry count, generated files, validation status, errors and warnings.

See docs/DESIGN_LOGGING.md for details.

---

## log-view

View recent design log entries in human-readable format.

```bash
circuit-weaver log-view <project_dir> [--lines N] [--type TYPE]
```

| Flag | Description |
|-|-|
| `--lines`, `-n` | Number of recent entries to show (default: 10) |
| `--type` | Filter by type: `all`, `wizard_step`, `cli_call`, `validation`, `research` (default: all) |

**Examples:**
```bash
# View last 10 entries (default)
circuit-weaver log-view my_project/

# View last 20 entries
circuit-weaver log-view my_project/ --lines 20

# Show only validation results
circuit-weaver log-view my_project/ --type validation

# Show only failed CLI calls
circuit-weaver log-view my_project/ --type cli_call
```

Helpful for troubleshooting tool failures, checking wizard inputs, and understanding validation issues.

See docs/DESIGN_LOGGING.md for details.

## simulate

Run SPICE simulations on a design. Auto-detects power regulators, filters, and
op-amps and generates appropriate analysis (transient, AC, DC, operating point).

```bash
circuit-weaver simulate <spec.yaml> [-o <output_dir>] [--type <scope>] [--model-dir <dir>] [--json]
```

| Flag | Description |
|-|-|
| `-o`, `--output` | Simulation output directory (default: `./sims`) |
| `--type` | Scope: `all`, `power`, `signal`, `thermal` (default: `all`) |
| `--model-dir` | Directory with SPICE models (default: auto-detect `spice_models/`) |
| `--json` | Output results as JSON |

**Exit codes:** 0 = success

Requires ngspice to be installed. If ngspice is not available, simulations are
reported as "skipped" with a recommendation to install it.

## confidence

Generate a unified design confidence report. Aggregates validation, simulation,
thermal, DFM, ERC, and cross-reference checks into a single 0-100 score.

```bash
circuit-weaver confidence <spec.yaml> [-o <report.html>] [--run-sims] [--pcb <file>] [--json]
```

| Flag | Description |
|-|-|
| `-o`, `--output` | Write HTML dashboard to file |
| `--run-sims` | Run SPICE simulations as part of the confidence check |
| `--pcb` | Path to `.kicad_pcb` file for DFM analysis |
| `--json` | Output results as JSON |

**Readiness classifications:**
- `ready_for_fab`: score >= 80 with no blockers
- `needs_review`: score >= 60 or has non-blocking warnings
- `not_ready`: score < 60 or has blockers

## discover

Auto-detect circuit projects in the current directory or specified root.

```bash
circuit-weaver discover [--root <dir>] [--depth <n>] [--json]
```

| Flag | Description |
|-|-|
| `--root` | Root directory to search (default: current directory) |
| `--depth` | Maximum search depth (default: 2) |
| `--json` | Output results as JSON array |

Detects projects by presence of `design.yaml`, `.kicad_pro`, or `.kicad_sch` files.

## log-event

Log a structured event to a project's `design.log` file. Designed for use by
skills and automation scripts.

```bash
circuit-weaver log-event <project_dir> --type <event_type> --message <msg> [--data <json>]
```

| Flag | Description |
|-|-|
| `--type` | Event type: `wizard_step`, `cli_call`, `validation`, `research`, `part_lookup`, `symbol_resolution`, `simulation`, `thermal`, `erc_drc`, `scoring`, `sourcing`, `generation`, `error` |
| `--message` | Event description |
| `--data` | JSON string with additional event data |
