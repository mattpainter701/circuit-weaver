<p align="center">
  <img src="assets/circuit-weaver-banner.svg" alt="Circuit Weaver" width="100%">
</p>

<p align="center">
  <a href="https://github.com/mattpainter701/kicad_automations/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/mattpainter701/kicad_automations/ci.yml?branch=main&label=CI&style=flat-square" alt="CI">
  </a>
  <a href="https://pypi.org/project/circuit-weaver/">
    <img src="https://img.shields.io/pypi/v/circuit-weaver?style=flat-square&label=PyPI&color=0ea5e9" alt="PyPI">
  </a>
  <img src="https://img.shields.io/badge/python-3.10%2B-0b1320?logo=python&logoColor=ffd43b&style=flat-square" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/KiCad-8%2B-0ea5e9?style=flat-square" alt="KiCad 8+">
  <img src="https://img.shields.io/badge/license-MIT-1f2937?style=flat-square" alt="MIT">
</p>

<p align="center">
  <strong>Programmatic KiCad schematic generation, strict validation, and manufacturing export<br>
  designed for AI-assisted hardware workflows — Claude Code, Codex, and offline CLI.</strong>
</p>

---

## How It Works

<p align="center">
  <img src="assets/workflow.svg" alt="Circuit Weaver full pipeline — from install to KiCad export" width="100%">
</p>

Circuit Weaver supports **describe the design → validate → generate KiCad artifacts**, with additional native placement, routing and review workflows. Start from an agent skill or the CLI and inspect the resulting evidence and blockers before proceeding.

### Validated scope

Circuit Weaver is an **alpha tool for engineering prototypes**. The 0.37.0 workflow corpus covers all 13 shipped samples and three connected mixed-signal boards with 149, 155 and 145 physical parts. Each passes 16 stages, including native routing, DRC, assembled model previews and a real MCP session. These are finite regression fixtures; passing them does not establish arbitrary-circuit electrical correctness or fabrication readiness.

Claude Code, Codex and OpenCode can use the packaged agent skills and CLI. The optional MCP server exposes five focused tools rather than the entire CLI lifecycle. Installer and MCP tests do not constitute a fresh end-to-end test inside every agent client; see [agent platform support](docs/agent-platforms.md).

RF research and circuit-generation assistance are available, but controlled-impedance routing, matching networks, antenna performance and RF stability require separate engineering verification. Native DRC and a rendered board are not evidence of RF performance. Review the [automation validation guide](docs/automation-validation.md) for prerequisites, evidence and prototype delivery gates.

---

<!-- capability-registry:start -->
## Capability Contract

This table is generated from the checked-in capability registry. Regenerate it with `python scripts/gen_capability_docs.py`; do not edit it by hand.

Verification is conservative: a capability only claims the evidence level its public path actually verifies. **not applicable** means an operational capability makes no design-artifact verification claim; it is not a lower verification level.

| CLI capability | Maturity | Public surfaces | Verification contract | Evidence | Since |
|---|---|---|---|---|---|
| `validate` | beta | CLI: `validate`; Python: `circuit_weaver.dispatcher:validate_design`; HTTP: `POST /validate; POST /mvp/validate`; MCP: `validate_design`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `apply-patch` | beta | CLI: `apply-patch`; Python: `circuit_weaver.dispatcher:apply_design_patch`; HTTP: `POST /mvp/apply-patch` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `generate` | experimental | CLI: `generate`; Python: `circuit_weaver.dispatcher:generate_artifacts`; HTTP: `POST /generate; POST /mvp/generate`; MCP: `generate_artifacts`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse`, `kicad-load`, `erc` | 0.32.1 |
| `review-report` | experimental | CLI: `review-report`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `diff` | beta | CLI: `diff`; Python: `circuit_weaver.dispatcher:diff_designs`; HTTP: `POST /mvp/diff` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `ingest-pcb-feedback` | beta | CLI: `ingest-pcb-feedback`; Python: `circuit_weaver.dispatcher:ingest_pcb_feedback`; HTTP: `POST /mvp/pcb-feedback` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `import-placement` | review_only | CLI: `import-placement`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `list-templates` | beta | CLI: `list-templates`; HTTP: `GET /templates` | not applicable (operational) | `command-contract` | 0.32.1 |
| `scaffold` | beta | CLI: `scaffold`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `register-ic` | experimental | CLI: `register-ic` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `resolve-symbol` | experimental | CLI: `resolve-symbol` | not applicable (operational) | `command-contract` | 0.33.0 |
| `export-jlcpcb` | review_only | CLI: `export-jlcpcb`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `export-gerbers` | review_only | CLI: `export-gerbers`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `cost-bom` | experimental | CLI: `cost-bom`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `import-design` | experimental | CLI: `import-design`; Python: `circuit_weaver.design_import:import_design`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `analyze-design` | experimental | CLI: `analyze-design`; Python: `circuit_weaver.design_import:analyze_design`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `status` | beta | CLI: `status`; Python: `circuit_weaver.project_state:get_project_state_summary`; Skill: `circuit-weaver` | not applicable (operational) | `command-contract` | 0.32.1 |
| `resume` | experimental | CLI: `resume`; Python: `circuit_weaver.project_state:resume_project`; Skill: `circuit-weaver` | not applicable (operational) | `command-contract` | 0.32.1 |
| `design-wizard` | experimental | CLI: `design-wizard`; Skill: `design-wizard` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `build-pcb` | experimental | CLI: `build-pcb`; Python: `circuit_weaver.schematic_pcb:schematic_to_pcb`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `static-parse`, `kicad-load`, `external-tool` | 0.35.0 |
| `place-pcb` | experimental | CLI: `place-pcb`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `external-tool`, `user-supplied` | 0.32.1 |
| `plane-pcb` | review_only | CLI: `plane-pcb`; Python: `circuit_weaver.pcb_ground_plane:add_ground_plane`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `kicad-load`, `external-tool`, `user-supplied` | 0.35.0 |
| `plane-cleanup-pcb` | review_only | CLI: `plane-cleanup-pcb`; Python: `circuit_weaver.pcb_plane_cleanup:cleanup_plane_traces`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `kicad-load`, `external-tool`, `user-supplied` | 0.35.0 |
| `cleanup-pcb` | review_only | CLI: `cleanup-pcb`; Python: `circuit_weaver.pcb_copper_cleanup:cleanup_copper`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `kicad-load`, `external-tool`, `user-supplied` | 0.35.0 |
| `refine-pcb` | review_only | CLI: `refine-pcb`; Python: `circuit_weaver.pcb_return_paths:refine_return_paths`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `kicad-load`, `external-tool`, `user-supplied` | 0.35.0 |
| `preview-pcb` | review_only | CLI: `preview-pcb`; Python: `circuit_weaver.pcb_preview:render_board_preview`; Skill: `circuit-weaver` | `static-parse` → `kicad-load` | `command-contract`, `static-parse`, `kicad-load`, `external-tool` | 0.35.0 |
| `log-status` | beta | CLI: `log-status` | not applicable (operational) | `command-contract` | 0.32.1 |
| `log-view` | beta | CLI: `log-view` | not applicable (operational) | `command-contract` | 0.32.1 |
| `autoroute` | review_only | CLI: `autoroute`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `external-tool`, `user-supplied` | 0.32.1 |
| `install-skills` | experimental | CLI: `install-skills` | not applicable (operational) | `command-contract` | 0.32.1 |
| `schema` | beta | CLI: `schema` | not applicable (operational) | `command-contract` | 0.32.1 |
| `harvest-specs` | experimental | CLI: `harvest-specs` | `static-parse` → `static-parse` | `command-contract`, `external-tool` | 0.32.1 |
| `extract-specs` | experimental | CLI: `extract-specs` | `static-parse` → `static-parse` | `command-contract`, `user-supplied` | 0.32.1 |
| `fetch-spice` | experimental | CLI: `fetch-spice` | `static-parse` → `static-parse` | `command-contract`, `external-tool` | 0.32.1 |
| `cache` | beta | CLI: `cache` | not applicable (operational) | `command-contract` | 0.32.1 |
| `cache stats` | beta | CLI: `cache stats` | not applicable (operational) | `command-contract` | 0.32.1 |
| `cache clear` | review_only | CLI: `cache clear` | not applicable (operational) | `command-contract` | 0.32.1 |
| `optimize-placement` | review_only | CLI: `optimize-placement`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `placement-viewer` | review_only | CLI: `placement-viewer`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `si-constraints` | experimental | CLI: `si-constraints` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `thermal-analysis` | experimental | CLI: `thermal-analysis` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `export-dual-cpl` | review_only | CLI: `export-dual-cpl` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `panelize` | experimental | CLI: `panelize` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `design-enclosure` | experimental | CLI: `design-enclosure` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `check-dfm` | review_only | CLI: `check-dfm` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `generate-docs` | experimental | CLI: `generate-docs` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `erc` | review_only | CLI: `erc` | `kicad-load` → `erc` | `command-contract`, `kicad-load`, `erc` | 0.32.1 |
| `doctor` | beta | CLI: `doctor` | not applicable (operational) | `command-contract` | 0.32.1 |
| `repair` | experimental | CLI: `repair`; Python: `circuit_weaver.repair_service:apply_no_connect` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.35.0 |
| `confidence` | review_only | CLI: `confidence`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `manufacturing-readiness` | beta | CLI: `manufacturing-readiness`; Python: `circuit_weaver.manufacturing_readiness:read_manufacturing_readiness`; HTTP: `POST /manufacturing-readiness`; MCP: `manufacturing_readiness`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `fabrication-ready` | 0.34.0 |
| `simulate` | experimental | CLI: `simulate`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `discover` | beta | CLI: `discover`; MCP: `discover_projects`; Skill: `circuit-weaver` | not applicable (operational) | `command-contract` | 0.32.1 |
| `save-research` | experimental | CLI: `save-research`; Skill: `circuit-weaver` | `static-parse` → `static-parse` | `command-contract`, `static-parse` | 0.32.1 |
| `log-event` | beta | CLI: `log-event` | not applicable (operational) | `command-contract` | 0.32.1 |
<!-- capability-registry:end -->

## Quick Start

**Step 1 — Install**

```bash
pip install circuit-weaver
```

**Step 2 — Register the skill** *(one-time, for Claude Code / Codex / OpenCode)*

```bash
circuit-weaver install-skills            # install for all supported agent platforms
circuit-weaver install-skills --dry-run  # preview without touching disk
circuit-weaver install-skills --force --backup   # resolve conflicts, back up every replaced file
```

> **Upgrade safety:** each installed skill has a provenance manifest containing hashes for every
> managed file. A pristine older release upgrades automatically. A file changed or deleted by the
> user is preserved and reported as a conflict, and the command exits non-zero. Pass `--force`
> only after reviewing conflicts; add `--backup` to preserve every replaced file.

**Step 3 — Launch**

```text
# Claude Code:
/circuit-weaver

# Codex:
$circuit-weaver

# OpenCode: ask it to use circuit-weaver (loaded through its skill tool).

# Or run offline without any agent:
circuit-weaver design-wizard
```

The skill walks through IC selection, spec validation, schematic generation, and the electrical-PCB handoff.

For a repeatable real-tool demonstration, run `python scripts/validate_app_workflow.py --output ./workflow-evidence`.
The [automation validation guide](docs/automation-validation.md) describes native ERC/DRC evidence, optional MCP checks,
and the prototype/manufacturing boundary.

---

## Research Traceability

Agentic research runs are expected to persist their output into `output/research/`.
That directory is the source of truth for why an IC was chosen, which citations
backed it, and which backend produced the result.

For Codex / Claude / OpenCode workflows, Step 2 IC research should stay in the
current agent session. If a premium-research command would delegate to a
subagent or fails with a model/tool conflict, use the platform's native web
tools instead and persist the backend that actually ran.

```bash
# Check which backend and depth are active before research starts
circuit-weaver doctor

# Optional: force backend/depth selection for agent workflows
export CIRCUIT_WEAVER_RESEARCH_BACKEND=standard   # or sonar-pro / auto
export CIRCUIT_WEAVER_RESEARCH_DEPTH=fast         # or normal
# PowerShell: $env:CIRCUIT_WEAVER_RESEARCH_BACKEND="standard"
# PowerShell: $env:CIRCUIT_WEAVER_RESEARCH_DEPTH="fast"

# Persist a completed research result
circuit-weaver save-research --project-dir ./output --file research.json
```

Each saved run produces `{slug}.json`, `{slug}.md`, and `summary.md`, and the
matching `design.log` entry records the canonical JSON path for later audit.

---

## Branch Maintenance

Use `scripts/cleanup_branches.py` to audit stale local branches before pruning them.
The command is dry-run by default and only targets merged, unprotected branches,
so dated work can be reviewed without risking useful code. When run with
`--delete`, deleted branch tips are archived under `refs/archive/branches/<name>`
unless `--no-archive` is explicitly supplied.

```bash
python scripts/cleanup_branches.py                 # audit only
python scripts/cleanup_branches.py --delete        # prune merged stale branches and keep archives
python scripts/cleanup_branches.py --force-unmerged # include unmerged branches only when intentionally forced
```

---

## What You Get

A successful default `generate` run produces a complete artifact bundle
(`--no-placement-review` deliberately omits the four placement-review files):

| Artifact | File | Description |
|-|-|-|
| KiCad schematic hierarchy | `{project}.kicad_sch` plus functional sub-sheets | Real, loadable hierarchy; explicit block `section` values remain separate professional sheets and support passives follow their owner |
| Markdown design report | `{project}_report.md` | Validation results, component summary, and design notes |
| Placement review | `placement_result.json`, `placement_review_context.json`, `placement.svg`, `placement_editor.html` | Exhaustive, reference-reconciled heuristic proposal with review blockers and official-reference context; never fabrication data |
| Assembly inventory | `assembly_manifest.json` | Stable physical-part references, including generated support passives |
| Evidence ledger | `evidence_manifest.json` | Stable component, pinout, footprint, parameter, finding, tool, and verification provenance IDs |
| Canonical spec and IR | `canonical_spec.yaml`, `design_ir.json` | Normalized source of truth and generated design IR |
| Validation evidence | `validation_report.json`, `placement_readiness.json` | Machine-readable release/readiness checks |
| Artifact inventory | `artifact_manifest.json` | Portable relative paths, verification state, kinds, and sizes for every generated artifact |
| Test point map | `{project}_test_points.csv` | Auto-detected power, clock, bus, and differential-pair nets |
| Firmware stubs (when applicable) | `{project}_pinout.csv`, `.ioc`, `sdkconfig.defaults` | MCU pin map and STM32/ESP32 co-design files |

---

## Resume or Analyze an Existing Design

Circuit Weaver keeps restartable state in `<project>/.circuit-weaver/project.json`.
Import is non-destructive: source KiCad, PCB, Gerber, drill, and ZIP files are
inventoried and hashed rather than regenerated.

```bash
# Import a KiCad project, a single KiCad file, a Gerber directory, or a ZIP
circuit-weaver import-design ./existing_board --analyze

# Inspect durable state or get a deterministic restart plan
circuit-weaver status ./existing_board
circuit-weaver resume ./existing_board

# Re-run every applicable bundled analyzer; unchanged results are cached
circuit-weaver analyze-design ./existing_board
```

Use `--project-dir <dir>` when state should live somewhere other than the
source directory. Use `--force` only to intentionally replace a changed import
source set, ZIP staging tree, or cached analysis. Netlist-only imports are
inventoried, but analysis is reported as unsupported because a netlist alone
does not contain schematic or physical-layout evidence.

### Bounded no-connect repair

The first experimental imported-design repair is deliberately narrow: it can
add one explicit KiCad no-connect marker only when a
`circuit-weaver-repair-metadata/v1` envelope binds the exact source hash,
analyzer component/pin identity, a v2 finding, and its validated evidence
manifest. Build that envelope with
`circuit_weaver.repair_service.build_no_connect_metadata`; the matching trusted
finding producer is `no_connect_finding_from_intent`. Both require current,
verified user evidence whose exact `pin:REF.NUMBER` claim says the pin is
intentionally unused. Raw analyzer JSON or an unbound boolean is rejected.

The service then independently proves the component UUID/library ID, pin
coordinate, and absence of a net, wire, label, junction, bus entry,
hierarchical sheet pin, power symbol, or coincident component pin. The plan is
reviewable JSON; the apply step will not infer approval from that file.

```bash
circuit-weaver repair suggest board.kicad_sch repair-metadata.json \
  --ref U1 --pin 4 \
  --finding-id FND-0123456789ab \
  --evidence-id EV-VALIDATION-0123456789ab \
  --output no-connect-plan.json

# Review the plan and copy its plan_sha256 through the approval channel.
circuit-weaver repair apply no-connect-plan.json repair-metadata.json \
  --approved-plan-hash <approved-plan-sha256> \
  --reviewer <reviewer> \
  --finding-id FND-0123456789ab \
  --evidence-id EV-VALIDATION-0123456789ab

circuit-weaver repair verify board.kicad_sch no-connect-plan.json
```

The public capability guarantee is static parsing. Run KiCad ERC after review;
unsupported topology, geometry, part replacement, and pin-remapping repairs
fail closed instead of being improvised. Apply holds cross-process source and
audit locks, keeps an exact hard-linked preimage through publication, and uses
prepared/committed append-only audit events so an interrupted successful
publication can be recovered on retry. Lock hardlinks and reparse aliases are
rejected before mutation; the default audit sidecar cannot escape through a
directory junction. On Windows, atomic replacement preserves and verifies the
target owner, group, and DACL access semantics. Applications that write
schematics without honoring Circuit Weaver's lock remain outside the
cooperative locking contract; snapshot, identity, and post-image checks still
detect ordinary write races and preserve observed user bytes rather than
rolling over them.

---

## PCB Placement Workflow

Circuit Weaver generates a review-only placement proposal, then KiCad creates
the electrical PCB. The SVG/HTML/JSON proposal is never a PCB, CPL, or other
fabrication artifact.

### 1 — Generate initial placement

`generate` returns the path to `artifact_manifest.json`. Resolve that manifest's portable `root_schematic` and artifact paths relative to the manifest directory instead of guessing filenames. Check `verification_status` before claiming KiCad verification. Placement review is enabled by default:

```bash
circuit-weaver generate design.yaml -o ./output
# → output/{project}.kicad_sch
# → output/placement_result.json
# → output/placement_review_context.json
# → output/placement.svg
# → output/placement_editor.html
# → output/{project}_report.md
```

`placement_result.json` must contain the exact `assembly_manifest.json`
reference inventory. It remains `fabrication_ready: false` by design. Review
its overlap, boundary, support-part, geometry, sourcing, and constraint
blockers. `placement_review_context.json` records constraints, targeted
research prompts, and official component/reference-layout links available to
the agent; manufacturer documentation remains authoritative.

### 2 — Optimize with simulated annealing

```bash
# Basic optimization (5 000 iterations, reproducible with seed)
circuit-weaver optimize-placement design.yaml --iterations 5000 --seed 42 \
    -o output/placement.json

# Thermal-aware strategy (minimizes hotspot proximity)
circuit-weaver optimize-placement design.yaml --strategy thermal --iterations 10000 \
    -o output/placement.json

# Read component thermal/SI specs from YAML files
circuit-weaver optimize-placement design.yaml --specs-dir ./specs/
```

The optimizer minimizes: overlap, boundary violations, thermal clustering, and DFM zone penalties via simulated annealing. Returns a placement JSON with x/y/rotation/layer per component.

### 3 — Review in the interactive viewer

Open the generated `output/placement_editor.html`, or generate a standalone
viewer for a different optimizer configuration:

```bash
circuit-weaver placement-viewer design.yaml -o output/viewer.html
```

Opens a self-contained HTML file with:
- Click-to-highlight nets
- Component hover tooltips (value, footprint, MPN)
- Thermal heatmap overlay (blue → red gradient by power dissipation)
- CSV export button for placement data

### 4 — Create the electrical PCB in KiCad

Open `artifact_manifest.json`, then open its `root_schematic`, assign and verify footprints, and run **Update PCB from Schematic**. Save that electrically annotated board under a separate name. Only this board has the pads and nets required by routing, DFM, and Gerber export.

Apply reviewed SVG coordinates only to that real electrical PCB. The default
import is strict: every PCB reference must appear exactly once in the SVG, and
unexpected or missing references block the write.

```bash
circuit-weaver import-placement output/placement.svg board.kicad_pcb \
    -o board_placed.kicad_pcb --dry-run
circuit-weaver import-placement output/placement.svg board.kicad_pcb \
    -o board_placed.kicad_pcb

# Intentional subset update only; unknown SVG references still fail.
circuit-weaver import-placement subset.svg board.kicad_pcb \
    -o board_placed.kicad_pcb --allow-partial
```

### 5 — Place and route the real board

After forward annotation, add a rectangular Edge.Cuts outline and lock connectors
that must remain fixed. Place the actual library footprints, route, import the
tracks, and check the final PCB:

```bash
circuit-weaver place-pcb board.kicad_pcb -o board_placed.kicad_pcb --seed 7
circuit-weaver autoroute board_placed.kicad_pcb --attempts 3 \
    --routed-board board_routed.kicad_pcb
circuit-weaver preview-pcb board_routed.kicad_pcb -o board-preview.svg
```

The preview draws actual board layers and footprint body geometry. Use `--side back`
for a mirrored underside or `--render-3d` for a native assembly-model PNG on KiCad 9+
with the declared model files installed. The [workflow demo](docs/automation-validation.md)
includes named preview controls and full-size views.

KiCad's native Python is discovered separately from your application Python.
Freerouting and a compatible Java runtime are required for routing. The input
board must have real pads and nets; padless previews are rejected. Placement
currently requires an unrouted rectangular board without zones. It preserves
locked footprints and board sides, uses real courtyards and rotated pad locations,
and checks physical DRC. Routing publishes the final board only after KiCad DRC
passes, preserving project rules. See [setup and limits](docs/routing-placement-search.md).

For imported boards with a legacy `.pro` project, convert and verify the project
rules in KiCad before placement, routing, or copper edits. These operations
require a sibling `.kicad_pro` when `.pro` exists; upgrading the PCB format alone
does not migrate those rules. Read-only inspection and previews remain available.

Router stdout and stderr are retained under `.circuit-weaver-router-logs` beside
the requested session output, with per-attempt hashes and byte counts in the
result. Returned diagnostics and statistics parsing use bounded windows; missing
final statistics remain unverified. Full log files can grow for the duration of
a run and should be included when diagnosing a timeout.

PCB handoff manifests distinguish requested constraints from emitted KiCad rules.
DDR interfaces retain explicit unverified requirements for device timing,
topology, signal roles and stackup. Rule rendering and ordinary DRC do not
establish DDR timing closure or controlled impedance.

### 6 — DFM check and panelization

```bash
# Check static track/via dimensions; native KiCad DRC is also required
circuit-weaver check-dfm electrical_board.kicad_pcb --profile jlcpcb

# Get panel layout suggestions for small boards
circuit-weaver panelize --board-width 50 --board-height 40 --qty 100

# Export dual-sided CPL (top + bottom)
circuit-weaver export-dual-cpl design.yaml --pcb electrical_board.kicad_pcb -o ./jlcpcb

# Export a reconciled JLCPCB delivery and inspect its commit manifest
circuit-weaver export-jlcpcb design.yaml --pcb electrical_board.kicad_pcb -o ./jlcpcb
# → bom_jlcpcb.csv, cpl_jlcpcb.csv, README_jlcpcb.txt, delivery_manifest.json
```

Without `--pcb`, `export-jlcpcb` is intentionally BOM-only. A supplied board
must match every assembly reference and footprint before CPL publication.
Treat `delivery_manifest.json` as the delivery status/commit marker.

---

## CLI Reference

```bash
# ── Design lifecycle ──────────────────────────────────────────────────────────
circuit-weaver validate design.yaml               # Validate spec (no files written)
circuit-weaver generate design.yaml -o ./output  # Full artifact bundle
circuit-weaver erc output/{project}.kicad_sch      # Use root_schematic from artifact_manifest.json
circuit-weaver diff old.yaml new.yaml             # Semantic diff + optional SVG
circuit-weaver import-design ./legacy_board --analyze
circuit-weaver status ./legacy_board
circuit-weaver resume ./legacy_board
circuit-weaver analyze-design ./legacy_board

# ── Placement ─────────────────────────────────────────────────────────────────
circuit-weaver optimize-placement design.yaml --iterations 5000 --seed 42 -o output/placement.json
circuit-weaver placement-viewer design.yaml -o output/viewer.html
circuit-weaver import-placement placement.svg board.kicad_pcb -o out.kicad_pcb
circuit-weaver import-placement subset.svg board.kicad_pcb -o out.kicad_pcb --allow-partial
circuit-weaver check-dfm electrical_board.kicad_pcb --profile jlcpcb
circuit-weaver panelize --board-width 50 --board-height 40 --qty 100
circuit-weaver export-dual-cpl design.yaml --pcb electrical_board.kicad_pcb -o ./jlcpcb

# ── Simulation & confidence ──────────────────────────────────────────────────
circuit-weaver simulate design.yaml -o ./sims      # Run SPICE simulations (ngspice)
circuit-weaver confidence design.yaml --run-sims   # Full design readiness score (0-100)
circuit-weaver confidence design.yaml -o report.html --pcb board.kicad_pcb

# ── BOM and manufacturing ─────────────────────────────────────────────────────
circuit-weaver cost-bom design.yaml --qty 1,10,100
circuit-weaver export-jlcpcb design.yaml --pcb electrical_board.kicad_pcb -o ./jlcpcb
circuit-weaver export-gerbers electrical_board.kicad_pcb -o ./gerbers

# ── Project management ───────────────────────────────────────────────────────
circuit-weaver discover                             # Auto-detect projects in CWD
circuit-weaver discover --json --depth 2            # JSON output, search 2 levels deep
circuit-weaver save-research --project-dir ./output --file research.json
circuit-weaver log-event ./project --type scoring --message "Review done"
circuit-weaver doctor                               # Check credentials, backends, and tool availability

# ── Design tools ──────────────────────────────────────────────────────────────
circuit-weaver scaffold --template buck --ref U1   # New spec from template
circuit-weaver list-templates --verbose             # Browse 37 subcircuit templates
circuit-weaver design-enclosure --board-width 50 --board-height 40 -o enclosure.scad
circuit-weaver harvest-specs design.yaml           # Download datasheets + extract specs
circuit-weaver fetch-spice design.yaml             # Fetch SPICE models (TI, ADI, Microchip…)
```

Run `circuit-weaver --help` or `circuit-weaver <subcommand> --help` for full flag reference.

---

## Python API

```python
from circuit_weaver.dispatcher import (
    validate_design,
    apply_design_patch,
    generate_artifacts,
    diff_designs,
    ingest_pcb_feedback,
)
from circuit_weaver.design_import import analyze_design, import_design
from circuit_weaver.project_state import get_project_state_summary, resume_project

# Validate a canonical design spec
report = validate_design(spec)

# Apply a transactional patch and re-validate
result = apply_design_patch(spec, patch)

# Generate the full bundle; require real KiCad for a release-quality claim
bundle = generate_artifacts(spec, output_dir="out/design", require_kicad=True)

# Non-destructively open and analyze KiCad/PCB/Gerber/ZIP sources
opened = import_design("legacy-board.zip", project_dir="work/legacy", analyze=True)
state = get_project_state_summary(opened["project_root"])
restart_plan = resume_project(opened["project_root"])
analysis = analyze_design(opened["project_root"])

# Semantic diff between two specs
changes = diff_designs(old_spec, new_spec)

# Feed PCB layout feedback back into the design spec
updated = ingest_pcb_feedback(spec, pcb_feedback)
```

### Validation groups

`validate_design` runs five grouped checks and returns a structured `ValidationReport`:

| Group | What it checks |
|-|-|
| `structural` | Topology, block connections, hierarchy integrity |
| `electrical` | Power rails, ground nets, net continuity |
| `implementation` | Part bindings, footprint assignments, pinout verification |
| `placement_readiness` | **Hard-gated** — dangling buses, missing I2C pull-ups, orphan interfaces, floating enables; never bypassable by `--no-require-valid` |
| `presentation` | Labels, pin numbers, sheet readability |

### HTTP API

```bash
uvicorn circuit_weaver.api:app --host 0.0.0.0 --port 5000
```

| Endpoint | Method | Description |
|-|-|-|
| `/health` | GET | Service health |
| `/capabilities` | GET | Machine-readable capability maturity and verification contract |
| `/templates` | GET | Available subcircuit templates |
| `/generate` | POST | YAML spec → ZIP of `.kicad_sch` + report |
| `/validate` | POST | YAML spec → validation JSON |
| `/mvp/apply-patch` | POST | Transactional patch + re-validation |
| `/mvp/diff` | POST | Semantic diff between two specs |
| `/mvp/pcb-feedback` | POST | Merge PCB layout feedback into spec |
| `/mvp/generate` | POST | JSON/YAML spec to a ZIP with a portable artifact manifest |

---

## What's New in v0.33.0

Circuit Weaver `v0.33.0` makes electrical recommendations measurable and traceable instead of relying on silent universal defaults.

| Area | Improvement |
|-|-|
| Evidence-backed passives | Shared versioned equations, explicit datasheet/equation/bounded-fallback precedence, declared E-series policy, and retained `CALC-*` evidence for critical support networks. |
| Fail-closed synthesis | Invalid or unsupported regulator, crystal, reset, USB-C, CAN/RS-485, and analog support values are withheld under `CW-PSV-*` findings before emission. |
| Typed power accuracy | Rail limits, direction, current envelopes, sequencing, tolerance, and provenance flow through ingest, validation, reports, placement context, and repair. |
| Identity safety | Exact MPN/package, symbol pin, footprint pad, and two-source reconciliation block physical CPL handoff when identity or pin-to-pad coverage is ambiguous. |
| Calibrated validation | Severity is separate from detection confidence; actionable findings name the constraint, observation, evidence, and safest next action. |
| Published scorecard | A 40-fixture benchmark covers 33 emitted/contract rule IDs—14 scored and 19 explicitly unsupported—with supported precision/recall at 1.000/1.000. |

<details><summary>v0.32.0</summary>

Circuit Weaver `v0.32.0` is an integrity and compatibility release for agent workflows and programmatic users.

| Area | Improvement |
|-|-|
| Schematic integrity | Deterministic sheet/reference allocation, collision-safe symbol naming, strict rendered pin maps, final S-expression checks, and safer label/wire movement for dense designs. |
| Truthful verification | ERC runs against the exact final schematic; results expose `kicad_verified` and `verification_status`, while `--require-kicad` fails closed when verification cannot run. |
| Durable workflows | Atomic status/resume state plus non-destructive KiCad, PCB, Gerber/drill, and ZIP import with fingerprinted cached analysis. |
| Professional hierarchy | Explicit block sections remain separate functional sheets, support parts retain ownership, and filenames are collision-safe. |
| Placement review | Exhaustive assembly reconciliation, blocker-aware JSON/context, official-reference links, and editable SVG/HTML; review artifacts are never fabrication data. |
| Manufacturing and routing | JLC/CPL export reconciles real PCB references and footprints; Freerouting publishes validated SES sessions and reports incomplete routing as partial. |
| Agent workflows | Portable kebab-case skills, narrower triggers, provenance-aware upgrades, and aligned Codex, Claude, and OpenCode installation paths. |
| Programmatic compatibility | Restored legacy imports and deprecated aliases, strict YAML parsing, exhaustive artifact inventory, and real placement SVG data. |
| Release reliability | Python 3.10-3.14, blocking Windows coverage, exact-wheel tests, live MCP tests, and KiCad 8/9/10 generation and ERC gates before PyPI publication. |

</details>

<details><summary>v0.31.0</summary>

Circuit Weaver `v0.31.0` collected the Sprint 53-54 schematic layout quality work and graduated the prior `0.30.x` sprint-release series into a minor release.

| Sprint | Feature |
|-|-|
| 54 | **Layout quality zero-crossing gate** — all quality-gated sample schematics now target zero wire-through-body crossings, with body-aware stubs, tapped-rail splitting, unrounded keep-outs, and generator-visible layout-quality warnings/reports. |
| 54 | **Density, sheet splitting, and autorouter hardening** — connector-heavy layouts now use a continuous density score, oversized sheets split instead of cramming onto A0, and Freerouting preflight/effort/timeout handling fail closed with clearer remediation. |
| 53 | **Schematic readability and passive placement** — sidecar passives, passive motifs, and local wiring now use occupancy-aware placement and body-aware routing to remove symbol overlaps and sharply reduce wire-body crossings. |
| 52 | **Part-neutral repair and schema plumbing** — normalized `pin_roles` now flow through `ComponentDef`, `ic_data`, EasyEDA imports, generic-builder outputs, and cache round-trips, so SPI/UART repair can operate from shared capabilities instead of exact MPN branches. |
| 52 | **Generation and placement hardening** — readiness gating now lives in `generate_from_components`, generic bypass policy is centralized, orphan non-power nets are hard-gated in placement-readiness, and PCB placement preview now penalizes long connected pairs instead of staying purely zone-based. |
| 51 | **Restart and validation flows are more truthful** — `log-status` works for validate-only sessions, persisted validation summaries now reflect the final verdict, Windows text-mode validation falls back to ASCII, and bad data-driven `ic` resolution now fails closed instead of silently substituting another part. |
| 51 | **Workflow skills are less brittle under long-running sessions** — `/circuit-weaver` now reports the installed CLI version first, long steps require timeout follow-up checks, and the validate-output recipe now preserves JSON on stdout instead of mixing it with stderr. |
| 50 | **Generic builder parity gate is closed** — the data-driven path now covers the remaining sensor-front-end, shared-bus, battery, display, and passive-diode contracts that were blocking the sample/corpus release gate; the full suite passes `1009 passed, 1 skipped`. |
| 50 | **CLI/package workflows are hardened for PyPI use** — read-only spec directories no longer crash file logging, unreadable `~/.config/secrets.env` no longer breaks `doctor` or unknown-part fallback, and the bundled skill payload is resynced byte-identical with `skills/`. |
| 49 | **Wizard intake now tiers by experience level** — the offline `design-wizard` asks for experience before requirements, uses compact design-brief intake for advanced/professional users, and no longer forces the same opening question flow on every user. |
| 49 | **Specialized RF workflows are now framed correctly** — Circuit Weaver skills now route radar / RF / microwave requests into a research-first custom engineering flow instead of treating the lack of turnkey coverage as an out-of-scope rejection. |
| 45-47 | **Schematic paper over-promotion fix** — `layout_sheet()` now starts from the allocator-selected paper size and only promotes if it doesn't fit. Small IoT-sensor-class designs no longer cascade to A2. Tighter title-block clearance keeps content on A3. |
| 45-47 | **Design log severity mapping** — `DesignLogHandler` maps Python `WARNING` records to `"warning"` type instead of `"error"` in `design.log`. |
| 45-47 | **BME688 bundled IC data** — sensor pin/footprint metadata shipped in `ic_data/misc.json`. `IoT_AQ_Sensor_v2` resolves without local `custom.json`. |
| 45-47 | **JLCPCB assembly variants** — `generate_assembly_variants()` supports `include_refs`, `exclude_refs`, `dnp_refs` subsets; per-variant BOM/CPL output. |
| 45-47 | **Allocator + placement-readiness tests** — 52 new dedicated tests closing Sprint 42 coverage gaps. |
| 44 | **Label collision avoidance** — overlapping labels on dense sheets are detected and shifted along wire-stub direction. Same-name labels skipped. |
| 44 | **Validate-all regression gate** — 14+1 sample YAMLs now validate clean; CI gate enforces zero hard errors. |
| 44 | **Sourcing auditor alternates** — queries LCSC/DigiKey for functionally similar parts when a component has CRITICAL/WARNING risk. |
| 44 | **MCP server** — `circuit-weaver-mcp` entry point for AI agent tool access via FastMCP. |
| 44 | **Wire-crossing minimization** — placer penalizes crossing-dense placements; bus signal groups detected for future parallel routing. |
| 43 | **Density-scaled grid spacing** — inter-component gaps scale to spread content across available page area, preventing corner-clustering on large sheets. |
| 43 | **Annotation overlap prevention** — per-IC annotations shift down when they would collide, with overflow line drops. |
| 43 | **Lane routing counter recycling** — capped at 6 with modulo wrap, preventing lanes from drifting 194mm from the IC. |

</details>

<details><summary>v0.27.0</summary>

| Sprint | Feature |
|-|-|
| 40 | **Cache-rebuilt components no longer ship as silent 2-pin stubs** — `SymbolResolver._rebuild_from_cache` now marks cache hits without real pin topology as `pinout_source="stub"`, so the existing `pinout-source` validator gate catches any multi-pin IC (sensor, MCU, H-bridge, gate driver, regulator) that would otherwise emit as a passive. The EasyEDA tier now persists full pin + power + bypass / strap topology via `symbol_cache.component_def_to_cache_payload()` so round-tripping a part through the cache preserves everything |
| 40 | **Schematic emission invariant** — `primitives.assemble_sheet` now dedupes structural duplicates before writing (symbol instances, wires, global / hierarchical labels, no-connects, junctions, UUIDs). Catches the placer / topology-dispatcher double-emission class of bug that was causing stacked passives, duplicate wires, and shared-UUID power symbols |
| 40 | **Placement PCB preview never fabricates footprints or synthetic pads** — the generator field now reads `placement_preview` so reviewers can tell the file apart from fab-ready output; missing footprint bindings emit `Placement_Preview:Missing_<ref>` placeholders instead of the old SOIC-8 fallback; and no synthetic pads are emitted for any component. KiCad's schematic → PCB forward annotation is the authoritative source of pads |
| 40 | **`generate` enforcement is now deterministic across runs** — structural + implementation category errors always raise, regardless of `require_valid`. `--no-require-valid` now only bypasses soft electrical warnings (dangling dev signals, crystal load-cap tolerance) and the bypass is logged at WARNING level. Two runs on the same spec + cache state always reach the same verdict |
| 40 | **Report-fidelity diagnostic** — `report.verify_report_fidelity(report_text, components)` flags references to component refs, net names, or annotations that don't exist in the resolved design. Catches the ghost-feature pattern where a report claimed "BME688 I2C + pull-ups" or "LED + current-limit R4" without any backing wires |
| 40 | **Five-archetype generation regression corpus** — `tests/test_generation_corpus.py` runs `generate_artifacts` end-to-end on LED driver, IoT sensor, motor controller, USB bridge, and FPGA power carrier samples, enforcing all three Sprint 40 invariants. Breadth-guard test keeps the corpus at ≥ 5 archetypes |
| 39 | **Step 2 IC research stays in the current agent session** across Codex / Claude / OpenCode instead of delegating to a spawned `/research` / `research-analyst` path that could trigger model conflicts; workflow docs and skill prompts now describe the real behavior |
| 39 | **Research-depth latency selector** — `design-wizard --research-depth {fast,normal}` persists into spec metadata; `doctor` reports the effective depth; Circuit Weaver skill uses a smaller query budget in `fast` mode |

</details>

<details><summary>v0.26.1</summary>

| Sprint | Feature |
|-|-|
| 38 | **Research workflow is now reproducible end-to-end** — `design-wizard --research-backend` persists the effective backend into spec metadata, `save-research` records the canonical JSON artifact path in `design.log`, and the workflow docs now tell agents to treat `output/research/` as the source of truth |
| 38 | **Resolver credential diagnostics now match real runtime behavior** — DigiKey checks both `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`, Mouser honors `MOUSER_SEARCH_API_KEY` through the shared credential loader, and `circuit-weaver doctor` now reports both credential states directly |
| 37 | **Research persistence + backend selection shipped in the CLI** — `circuit-weaver save-research`, `CIRCUIT_WEAVER_RESEARCH_BACKEND`, and `circuit-weaver doctor` backend reporting are now available in the published package |

</details>

<details><summary>v0.25.0</summary>

| Sprint | Feature |
|-|-|
| 35 | **`install-skills` collision protection** — stopped silently overwriting a curated `SKILL.md`; added `--force`, `--backup`, and `--dry-run` with collision reporting for review |
| 35 | **All 11 skills bundled in the wheel** — the full set, including `design-wizard`, is kept in sync by `scripts/sync_bundled_skills.py` with CI and pre-commit drift guards |
| 35 | **Windows CI leg** — `windows-latest` / Python 3.12 smoke run (non-blocking first sprint) covering CLI commands, doctor, skill installer, and `python -m circuit_weaver --version` |

</details>

<details><summary>v0.24.0</summary>

| Sprint | Feature |
|-|-|
| 34 | **Data-Driven Template Engine** — IC data now lives in JSON files under `src/circuit_weaver/ic_data/` (11 categories). New `topology_builders.py` produces schematic fragments dynamically, replacing hardcoded template classes |
| 34 | **7 New Subcircuit Templates** — RTC (DS3231, PCF8563), EEPROM (24LCxx, 25LCxx), wireless modules (nRF52, ESP32, LoRa), USB-C connectors, SPI bus conditioning, voltage references (REF5025, LM4040), generic connectors — total template count now 37 |
| 34 | **IC Registration CLI** — `scripts/extract_ic_data.py` harvests IC entries from datasheet research; `register_ic()` API persists to `custom.json` |
| 33 | **Platform Compatibility** — full Claude Code / Codex / OpenCode support across all skills, updated shim files, skill trigger disambiguation |
| 32 | **CLI Integration Tests** — 24 end-to-end CLI tests; informational output moved to stderr |
| 31 | **Bug Fixes & Hardening** — thread-safe logging bridge, correct zero-check scoring, connector MPN validation, SPICE value parser edge cases |

</details>

<details><summary>v0.22.0</summary>

| Sprint | Feature |
|-|-|
| 30 | **Confidence Dashboard** — `circuit-weaver confidence` aggregates 7 data sources into a 0-100 readiness score with HTML report; now a mandatory step in the wizard flow |
| 30 | **Workflow Overhaul** — wizard now 9 steps: added auto-detection, confidence check, and PCB layout preparation (placement optimizer, SVG editor, autoroute, DFM check) |
| 29 | **Enhanced Validations** — 3 new checks (power budget, thermal limits, signal integrity); cross-reference auditor validates spec-vs-schematic-vs-BOM consistency |
| 28 | **Circuit Simulation Engine** — SPICE netlist generation, ngspice runner with graceful degradation, simulation orchestrator with auto-planning and confidence scoring |
| 27 | **Project Discovery** — `circuit-weaver discover` auto-detects projects in CWD; all skills now pre-check before asking for paths |
| 26 | **Logging Overhaul** — 13 structured event types in `design.log`, Python logging bridge, `log-event` CLI for skill-callable logging, 7 modules instrumented |

</details>

<details><summary>v0.21.0</summary>

| Sprint | Feature |
|-|-|
| 25 | **Component Selection Rationale** — HTML review report now includes a per-IC table showing why each part was chosen, key electrical specs, and any reference design cited |
| 25 | **Auto Test Point Generation** — `generate_artifacts()` emits `{project}_test_points.csv` and test-point report data while leaving the validated schematic bytes unchanged (`annotated_schematic: false`); detects power, ground, clock, bus, and differential-pair nets |
| 24 | **Firmware Co-Design Export** — Auto-generates `{project}_pinout.csv`, STM32 `.ioc` skeleton, and ESP32 `sdkconfig.defaults` when MCU blocks are present |
| 23 | **KiCad CLI ERC Integration** — `circuit-weaver erc` runs KiCad's built-in ERC and surfaces results in the HTML report with a pass/fail badge |
| 22 | **Pinout Verification Gate** — ICs using unverified stub pinouts now fail validation before any schematic is emitted |

</details>

---

## Repo Layout

```
kicad_automations/
├── src/circuit_weaver/      # Core engine
│   ├── dispatcher.py           # Public API: validate, patch, generate, diff, pcb-feedback
│   ├── design_ir.py            # Canonical design intermediate representation (DesignIR)
│   ├── generator.py            # .kicad_sch file emitter
│   ├── validator.py            # 14-check validation pipeline (electrical + thermal + SI)
│   ├── cross_reference_validator.py  # Spec vs schematic vs BOM audit
│   ├── confidence_dashboard.py # Design readiness scoring (0–100) + HTML dashboard
│   ├── simulation.py           # Simulation orchestrator (plan → run → score)
│   ├── spice_netlist.py        # SPICE .cir netlist generator
│   ├── spice_runner.py         # ngspice subprocess runner with graceful degradation
│   ├── symbol_resolver.py      # IC symbol resolution chain (EasyEDA → cache → stub)
│   ├── symbol_cache.py         # Persistent symbol cache with full pin/power map schema
│   ├── research.py             # Research query orchestration (Perplexity / native web)
│   ├── research_store.py       # Research artifact persistence (JSON + summary.md)
│   ├── doctor.py               # Credential + backend diagnostics (circuit-weaver doctor)
│   ├── logging_bridge.py       # Unified logging: Python logging ↔ DesignLogger bridge
│   ├── design_logger.py        # Structured JSON Lines workflow logging (13 event types)
│   ├── project_discovery.py    # Auto-detect circuit projects in directories
│   ├── review_report.py        # HTML review report generator
│   ├── test_point_gen.py       # Auto test-point classification and CSV export
│   ├── firmware_export.py      # MCU co-design stubs (pinout CSV, .ioc, sdkconfig)
│   ├── placer.py               # Topology-aware schematic placement engine
│   ├── placement_optimizer.py  # Simulated annealing PCB placement optimizer
│   ├── placement_viewer.py     # Interactive HTML viewer (net highlight, thermal heatmap)
│   ├── svg_placement.py        # Bidirectional SVG placement editor (export + import)
│   ├── pcb_export.py           # Initial .kicad_pcb generation with zone-based layout
│   ├── design_scorer.py        # Electrical quality score (0–100, A–F)
│   ├── dfm_checker.py          # DFM rule checks
│   ├── thermal_analysis.py     # Thermal modeling and junction temp analysis
│   ├── si_constraints.py       # Signal integrity constraint solver
│   ├── api.py                  # FastAPI HTTP server
│   ├── helpers/placement.py    # KiCad API abstraction, footprint matching utilities
│   └── subcircuits/            # Reusable circuit template library (37 templates)
├── tests/                   # Regression test suite (1,200+ tests)
├── skills/                  # Global workflow skills: kicad, bom, digikey, lcsc, ee…
├── project-skills/          # Per-project templates: kicad-gen, autoroute, sim…
├── agents/                  # Hardware reviewer AI agent definitions
├── rules/                   # KiCad workflow policy files
├── samples/                 # Sample designs (iot_sensor_node, zigbee_humidistat…)
└── assets/                  # README visuals: banner, workflow diagram
```

---

## Agent Platform Support

| Platform | What's included |
|-|-|
| **Claude Code** | `/circuit-weaver` global skill via `install-skills` |
| **Codex** | `$circuit-weaver` from the current global `~/.agents/skills` location |
| **OpenCode** | Agent Skills from `~/.agents/skills`, or `$OPENCODE_CONFIG_DIR/skills` when configured |
| **Kilo** | Global skills under `$KILO_CONFIG_DIR/skills` or `~/.kilo/skills` |

Install for all platforms at once:

```bash
# Bash (Mac/Linux/WSL)
./install.sh --platform all

# PowerShell (Windows)
./install.ps1 -Platform all
```

The wrappers install the checkout and delegate skill copying, upgrades, dry runs, and conflict handling to the same Python installer. They honor `CLAUDE_CONFIG_DIR`, `OPENCODE_CONFIG_DIR`, and `KILO_CONFIG_DIR`; Codex uses `~/.agents/skills`. Use `--skills-only` / `-SkillsOnly` to avoid reinstalling the package.

---

## Sample Design

PyPI installs include a portable IoT starter. Copy it into a writable project
directory, then run the full workflow:

```bash
python -c "from importlib.resources import files; from pathlib import Path; d=Path('iot_sensor_example'); d.mkdir(exist_ok=True); d.joinpath('design.yaml').write_bytes(files('circuit_weaver').joinpath('examples','iot_sensor.yaml').read_bytes())"

# Validate
circuit-weaver validate iot_sensor_example/design.yaml

# Generate schematic + report + test points + firmware stubs
circuit-weaver generate iot_sensor_example/design.yaml -o ./output

# Export a BOM-only delivery (no heuristic CPL is invented)
circuit-weaver export-jlcpcb iot_sensor_example/design.yaml -o ./output/jlcpcb

# After creating a real electrical PCB, export a reconciled BOM + CPL
circuit-weaver export-jlcpcb iot_sensor_example/design.yaml \
    --pcb ./output/iot_sensor.kicad_pcb -o ./output/jlcpcb
```

A source checkout also includes the larger reference gallery under `samples/`.

---

## Contributing

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Lint
python -m ruff check src tests

# Test
python -m pytest tests -q
```

Pre-commit hooks run ruff, YAML validation, and `circuit-weaver validate --strict` on changed design specs automatically.

---

## License

MIT. See [LICENSE](LICENSE).
