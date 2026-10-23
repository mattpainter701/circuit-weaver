---
name: circuit-weaver
description: >
  Create, import, analyze, resume, or revise an end-to-end Circuit Weaver electronic design from
  requirements through validated functional KiCad schematics, placement review, and a truthful
  manufacturing handoff. Use for new designs and for existing KiCad/PCB/Gerber projects that need
  durable state. Use kicad for one-off file inspection, bom for standalone sourcing, and
  design-wizard only when the user explicitly requests the slower questionnaire.
---

# Circuit Weaver — Main Entry Skill

The master entry point for circuit design workflows. This skill orchestrates:
1. New design creation (wizard + requirements + research + IC selection + BOM + passive generation + schematic)
2. Existing design loading and review

This skill is **platform-aware**:
- **Claude Code**: Uses native interactive buttons (AskUserQuestion tool) for all choices
- **Codex/OpenCode**: Uses conversational prompting with numbered options
- **CLI**: Uses `py -m circuit_weaver design-wizard` with `input()` for terminal mode

All platforms follow the same design flow — just different UI for user input.

## Domain Routing

## Start, Open, and Resume Projects (First Route)

For every request to start, open, continue, or resume a project, first identify the
runtime that will perform the work. Use the installed `circuit-weaver` command
when available; otherwise use the explicitly selected Python interpreter with
`-m circuit_weaver`. Run `runtime-info --json` (or `python -m circuit_weaver
runtime-info --json`) and report its installed version, declared capabilities,
and any discovered native-tool paths. Treat KiCad, Java/Freerouting, and
optional-library paths as candidates only: this probe does not run tool-version
checks or verify KiCad Python `pcbnew`. Do not claim those executables, APIs, or
libraries work until the relevant operation checks them successfully.

Discover the current working directory with the platform's shell and run
`circuit-weaver discover --root <cwd> --json` (or the matching explicit Python
command). Offer the discovered projects plus a choice to browse to another
directory, create a new project, or start from a detected sample. Never assume
the agent process is already in the user's project directory. Once a project is
selected, use the same project path for every operation below and for the
interactive CLI (`circuit-weaver` with no subcommand), Python CLI, or MCP.

Use the shared project lifecycle commands:

```text
circuit-weaver start <absolute-project-dir> [--spec <yaml>] [--name <name>] --json
circuit-weaver open <absolute-project-or-source-path> --json
circuit-weaver workflow plan <absolute-project-dir> [--stages validate generate build place route preview] --json
circuit-weaver workflow run <absolute-project-dir> [--stages ...] --json
circuit-weaver workflow status <absolute-project-dir> --json
circuit-weaver workflow resume <absolute-project-dir> --json
circuit-weaver workflow cancel <absolute-project-dir> --json
circuit-weaver resume <absolute-project-dir> --json
circuit-weaver resume <absolute-project-dir> --execute --json
```

Use the explicitly selected Python executable in place of `circuit-weaver` when
the installed command is missing or could resolve to a different environment.
The equivalent module form is `& $python -m circuit_weaver ...` in PowerShell
or `"$python" -m circuit_weaver ...` in POSIX shells. `workflow resume` and
the Python/MCP `resume_workflow` execute the verified saved workflow from its
first incomplete stage. The legacy top-level `resume` command prints a plan by
default; add `--execute` only when the user asked to continue execution.
Reconcile status and file hashes before retrying work.
Stages are checkpointed and resumable. Cancellation takes effect at a stage
boundary. A successful stage means only what its evidence records: planning,
preview, or placement does not claim a completed native KiCad build or route.
Never imply manufacture approval from a workflow status.

For MCP, call `runtime_info` first and use the same explicit interpreter to
launch `python -m circuit_weaver.mcp_server`. Invoke `start_project`,
`open_project`, `plan_workflow`, `run_workflow`, `workflow_status`,
`resume_workflow`, and `cancel_workflow` with explicit absolute project paths.
`resume_workflow` executes the verified saved plan; call `plan_workflow` for a
non-executing plan. Pass specs and options as JSON objects and stage selections
as JSON string arrays. MCP registration is a separate client configuration step; installing
Circuit Weaver or its agent skill does not register the server. See
[`docs/project-workflows.md`](../../docs/project-workflows.md) for signatures,
examples, checkpoint behavior, and the installation/registration distinction.

Use `circuit-weaver library-access [query] --project-root <absolute-dir> --json`
or MCP `library_access` to inspect local KiCad symbols, footprints, and 3D
models separately from curated functional/topology support and project SPICE
bindings. The report is read-only and labels library-root provenance. An exact
symbol may resolve to a generic package footprint and model even when those
filenames do not contain the MPN. Treat that as asset identity only: it does
not validate pinout, package fit, electrical behavior, or RF performance. A
missing functional builder or SPICE contract remains a separate engineering
gap; library availability alone does not qualify RF, radar, SDR, or transmitter
circuits.

For `simulate`, report the returned status and evidence rather than treating a
successful ngspice process as acceptance. `verified` requires fresh parsed
output and explicit supported project targets that pass; `failed` identifies a
failed analysis or target; `unverified` covers incomplete evidence, absent
models, or no explicit targets. CLI exit codes are 0 verified, 1 failed, and 2
unverified. IC models need readable files plus explicit `subckt` and
`pin_order` metadata matching connected pin numbers. Do not infer RF
qualification from generic SPICE models, KiCad assets, or a simulation pass.
For passive or custom analyses, use `spec.simulation.analyses` to state the
analysis kind, validated `analysis_params`, structured V/I `sources`, and
explicit `target_metrics`; AC `input_trace` / `output_trace` must identify
connected nodes. No stimulus or acceptance target is invented. See
`docs/project-workflows.md#explicit-spice-testbenches` for a complete RC example
and the evidence limits of a verified result.

Only after the project route is selected should a new-design request enter the
interactive wizard below. Existing projects should resume through the shared
lifecycle interface before invoking focused commands.

Do **not** treat the absence of built-in topology coverage as evidence that a design
domain is unsupported.

Use these routing rules:

- **Standard covered designs**: follow the normal wizard flow.
- **Specialized domains** such as RF/microwave, phased arrays, Ku-band radar,
  mmWave, precision analog instrumentation, unusual isolation, or mixed-signal
  boards with custom front ends: still proceed with Circuit Weaver, but switch
  to a **research-first custom architecture path**.

In the research-first custom architecture path:

- Use `ee` for first-principles analysis and domain-specific calculations.
- Use `python -m circuit_weaver simulate` for supported power, signal, and thermal
  analysis after the spec exists; document external RF/S-parameter analysis separately.
- Use `kicad` for review and downstream schematic/PCB analysis.
- Capture the system as **custom blocks + interfaces + explicit constraints**
  even when no turnkey builder/topology coverage exists.
- Be precise about what is automated versus what remains manual
  (for example: transmission-line synthesis, antenna tuning, EM/cavity effects,
  array calibration, and final RF layout closure).

Do **not** respond with blanket language like "Circuit Weaver is only for
standard embedded electronics" when the real limitation is narrower: some
domains have less automation and require more manual engineering review.

---

## Long-Running Operations & Timeout Awareness

Do not let Circuit Weaver work run silently for long periods.

When a step is expected to take noticeable time — for example `confidence --run-sims`,
`simulate`, `generate` on a large design, `optimize-placement`, `autoroute`, large
research passes, or repeated validation/generation retries — follow these rules:

1. **Before starting any command likely to exceed ~2 minutes**, tell the user:
   - what command or phase is starting,
   - why it may take a while,
   - what success artifact or status you expect.
2. **At ~2 minutes without completion**, send a follow-up and check progress using the
   best available source:
   - `python -m circuit_weaver log-status <project_dir>`
   - `python -m circuit_weaver log-view <project_dir>`
   - recent entries in `design.log`
   - recent entries in `circuit-weaver.log`
   - output/artifact timestamps or files created so far
3. **At ~5 minutes**, do not just keep waiting. Inspect for actionable issues:
   - unresolved validation blockers,
   - missing dependencies/tools,
   - stalled artifact generation,
   - repeated warnings/errors in logs,
   - no file or status movement since the last check.
   Then tell the user whether the work is actively progressing, blocked, or likely stuck.
4. **At ~15 minutes**, either:
   - explain concretely why the wait is still expected and what milestone remains, or
   - stop/pivot to a smaller bounded step and surface the blocker.
5. **Never allow a run to sit silent for ~30 minutes.** Long-running work must include
   periodic follow-up plus an explicit progress/issue check.

When reopening an interrupted or stale project, reconcile durable state before
re-running expensive commands. Minimum restart triage:

```bash
python -m circuit_weaver status <project_dir>
python -m circuit_weaver resume <project_dir>
```

`resume` prints a plan; it does not execute it. If state reports a prior failure
or modified/missing files, inspect `log-status`, `log-view`, `design.log`, and
`circuit-weaver.log` before deciding whether to retry, validate, or edit.

---

## Workflow: New Design

### Step -2 — Installed Version Banner (ALWAYS RUN FIRST)

Before presenting any choices, detect the **installed Circuit Weaver CLI version**
from the command on `PATH` and paste it back to the user.

Preferred command:

```bash
circuit-weaver --version
```

Fallback if the `circuit-weaver` entrypoint is missing:

```bash
python -m circuit_weaver --version
```

Rules:

- Prefer the `circuit-weaver` command on `PATH` when available. That is the installed CLI.
- Do **not** infer the version by reading `src/circuit_weaver/__init__.py` or other repo files.
- If forced to use `python -m circuit_weaver --version` from a checkout, clearly label it as
  the local/imported version rather than the installed CLI version.

Paste back a short banner before the menu, for example:

```text
Circuit Weaver installed: vX.Y.Z
```

If only the fallback worked:

```text
Circuit Weaver local/imported version: vX.Y.Z
```

### Step -1 — Auto-Detection

Before presenting any choices, **automatically scan for existing projects and available samples**:

```bash
python -m circuit_weaver discover --json
```

Then detect the sample sources that actually exist in the current installation.
Source checkouts may provide the full `samples/` gallery:

```bash
ls -d samples/*/  # POSIX
# or
dir /b /ad samples\  # Windows
```

Do not assume that directory exists in a PyPI installation. The wheel carries a
single portable starter spec instead; verify it with:

```bash
python -c "from importlib.resources import files; p=files('circuit_weaver').joinpath('examples','iot_sensor.yaml'); print(p if p.is_file() else '')"
```

**Separate user projects from available examples:**

- **User projects**: from the `discover --json` output, **exclude** any project whose path contains `/samples/` or `\samples\`. These are your actual user projects. Only show these in the "Open existing" option.
- **Source-checkout samples**: when a real repo-local `samples/` directory exists,
  its subdirectories are reference designs, not user projects. Show only the
  directories actually found under option [3].
- **Packaged example**: PyPI installs expose `iot_sensor.yaml` through
  `importlib.resources`. Show it as `iot_sensor` under option [3]. Do not claim
  that the full source-checkout gallery is bundled in the wheel.

If user projects are found, present them:

```
Found 2 existing circuit project(s):

  #  Project                  Type            Status       Files
  -  -------                  ----            ------       -----
  1  WiFi_Sensor_v1           circuit_weaver  validated    yaml, sch, pcb, log
  2  Motor_Controller         kicad_native    generated    sch, pcb, pro
```

If no user projects are found (only examples or nothing), skip the project list.
If neither a repo-local sample nor the packaged example is available, omit
option [3] instead of presenting a broken route.

**Log:** `python -m circuit_weaver log-event <project_dir> --type wizard_step --message "Auto-detection: N projects found, M samples available"`

### Step 0 — Welcome & Route

Present a choice (platform-adapted):

**Claude Code / Codex / OpenCode:**
```
Welcome to Circuit Weaver

What would you like to do?
  [1] Design a new circuit
  [2] Open an existing design
  [3] Start from a sample design
```

For Claude Code: Use AskUserQuestion with options `["Design a new circuit", "Open an existing design", "Start from a sample design"]`
For Codex/OpenCode: Present as numbered list, ask user to type [1], [2], or [3]
For CLI: User already running `design-wizard`, skip this step

Based on choice:
- **[1] New design** → Proceed to Step 1
- **[2] Existing design** → Jump to "Workflow: Existing Design" section
- **[3] Sample design** → Proceed to Step 0.3 (below)

### Step 0.3 — Start from a Sample Design

List only sample designs detected in Step -1. In a source checkout, enumerate
the real subdirectories under `samples/`. In a normal PyPI install, present the
packaged starter:

```
Available sample designs:

  #  Sample                   Description
  -  ------                   -----------
  1  iot_sensor               ESP32-based IoT environmental sensor

  [0] Back to main menu
```

When the repo gallery exists, replace that one-item example with the actual
directory names and descriptions found there. Ask for a number in the displayed
range; never hard-code a 13-item range when those files are absent.

On selection:
1. Ask for the destination before copying.
2. For a repo-local sample, use the platform's filesystem tools to copy the
   complete sample directory to the chosen destination. If only Python is
   available, use `shutil.copytree`; do not assume a POSIX shell command.
3. For the packaged `iot_sensor` example, create the destination and copy the
   resource bytes from
   `importlib.resources.files("circuit_weaver").joinpath("examples", "iot_sensor.yaml")`
   into `<destination>/design.yaml`. Use Python's `Path.mkdir()` and
   `Path.write_bytes()` so this works on Windows, macOS, and Linux.
4. Ask the user for a project name (default: same as sample).
5. Rename the YAML if desired and set the project name in the spec.
6. Print: `✓ Sample copied to <project_name>/`.
7. Log: `[Step 0.3] Started from sample: <sample_name>`.
8. Jump to "Workflow: Existing Design" section (treat the copied sample as an existing design to review/modify).

### Step 1 — Project Setup & Folder Creation

**This step must happen FIRST, before any other questions.**

#### 1a. Project Name (REQUIRED FIRST)

Question: "What's the name of your project?"

Examples:
- "WiFi_Sensor_v1"
- "Motor_Controller_2024"
- "USB_Audio_Interface"

**All platforms:** Ask as open text input.

**Action:**
1. Take the project name
2. Create folder: `${PROJECT_NAME}/`
3. Create logfile: `${PROJECT_NAME}/design.log`
4. Log: `[Step 1] Project created: {project_name}`
5. Print to user: `✓ Project folder and logfile created`

Continue immediately to Step 1b once folder + log are created.

#### 1b. Experience Level

Question: "What's your EE experience level?"

**Claude Code:** Use AskUserQuestion with options:
- Beginner (I'm new to circuit design)
- Intermediate (I've designed 1-2 circuits)
- Advanced (I've designed 5+ circuits)
- Professional (I design circuits for a living)

**Codex/OpenCode:** Present as numbered list, ask user to select [1-4]

**Reasoning:** Calibrate explanation depth and component complexity throughout the wizard.

**Log:** `[Step 1b] Experience level: {selected_level}`

Immediately branch the intake style by level. Do **not** use the same first
requirements question for every tier:

- **Beginner**: Start with a plain-language application question, then ask form factor, power, and interfaces separately.
- **Intermediate**: Keep guided prompts with examples and defaults.
- **Advanced**: Start with one compact design brief covering purpose, power, interfaces, and constraints, then ask only the missing fields.
- **Professional**: Do **not** immediately ask "What does this circuit do?" as a standalone question. Ask for a compact design brief or spec fragment instead.

Useful professional format:
`purpose; input power; rails/current; interfaces; mechanical constraints; preferred ICs`

#### 1c. Requirements Intake

For **Beginner / Intermediate**, ask:

Question: "What does this circuit do? (describe the end application)"

Examples:
- "WiFi environmental sensor, battery-powered, 50x30mm enclosure"
- "Motor controller for robot arm, wall-powered"
- "USB audio interface, desktop device"

For **Advanced**, ask:

Question: "Give me a compact design brief covering purpose, power, interfaces, and constraints."

For **Professional**, ask:

Question: "Paste a compact design brief or spec fragment. Useful format: purpose; input power; rails/current; interfaces; constraints."

**Log:** `[Step 1c] Purpose: {user_input}`

If the brief indicates specialized RF/microwave work (radar, phased array,
Ku-band, mmWave, custom RF front-end), immediately switch from generic
embedded intake to a specialized architecture intake. Ask for:

- frequency band and bandwidth
- system architecture (FMCW, direct conversion, superhet, IF chain, etc.)
- channel/array count
- LO/reference clock plan
- gain / NF / power / dynamic-range targets
- controlled-impedance and shielding constraints

Do **not** refuse the design space. Frame it as:
"supported through a custom engineering workflow with manual RF closure."

#### 1d. Form Factor & Mechanical

Only ask this as a separate follow-up if it was not already captured clearly in Step 1c.

Question: "What are the size and component height constraints?"

Examples:
- "50×30mm enclosure, max component height 12mm"
- "Credit-card sized (85×54mm), compact"
- "No size constraint, but want to fit in existing housing"

**All platforms:** Ask as open text input.

**Log:** `[Step 1d] Form factor: {user_input}`

#### 1e. Power Source & Rails

Only ask this as a separate follow-up if Step 1c did not already capture input power and rail/current needs.

Question: "What power source will you use, and what voltage rails do you need?"

Examples:
- "3.7V LiPo battery, needs 3.3V@500mA for MCU and 5V@100mA for USB"
- "5V USB, only needs 3.3V rail"
- "12V wall supply, needs 5V and 3.3V"

**All platforms:** Ask as open text input.

**Log:** `[Step 1e] Power rails: {user_input}`

#### 1f. Interfaces & Sensors

Only ask this as a separate follow-up if Step 1c did not already capture the key interfaces, buses, or sensors.

Question: "What interfaces and sensors does your circuit need?"

Examples:
- "USB charging, I2C for BME280 sensor, WiFi via ESP32"
- "SPI for SD card, UART for debug, GPIO for button/LED"
- "CAN bus, no sensors"

**All platforms:** Ask as open text input.

**Log:** `[Step 1f] Interfaces: {user_input}`

#### 1g. Confirm & Summarize

Compile the answers and present a summary:

```
=== Requirements Summary ===

Project:        {project_name}
Application:    WiFi Environmental Sensor
Experience:     Intermediate
Form Factor:    50×30mm enclosure, SMD only, <12mm component height
Power Source:   3.7V LiPo battery (500mAh nominal)
Output Rails:   3.3V @ 500mA (MCU), 5V @ 100mA (USB charging circuit)
Interfaces:     USB for charging, I2C for sensor (BME280)
```

Question: "Does this look right? Any changes?"

**Claude Code / Codex / OpenCode:** Use yes/no question
**All platforms:** Accept "yes", "no", or redirection to specific field

If user wants to change something, loop back to the relevant question.

**Log:** `[Step 1g] Requirements confirmed. Ready for IC research.`

### Step 2 — IC Research & Selection

Before the first query, resolve the effective research settings:

Backend:

- Respect `metadata.research_backend` from a scaffolded spec when present.
- Otherwise respect `CIRCUIT_WEAVER_RESEARCH_BACKEND={auto,sonar-pro,standard}`.
- `auto` means: use `sonar-pro` when `PERPLEXITY_API_KEY` is configured, otherwise `standard`.

Depth:

- Respect `metadata.research_depth` from a scaffolded spec when present.
- Otherwise respect `CIRCUIT_WEAVER_RESEARCH_DEPTH={fast,normal}`.
- `fast` means: latency-first pass. Run one project-context query plus at most 2 targeted
  block queries. Skip deep alternates, detailed pricing, and connector research unless the
  user explicitly asks or the block is critical.
- `normal` means: fuller pass. Run one project-context query plus the standard 3-5 targeted
  block queries, including alternatives and rough pricing context where useful.
- `circuit-weaver doctor` is the source of truth for the effective backend, depth, and
  credential status.

Use the backend consistently for the whole session:

- Keep IC research in the current agent/session. Do **not** spawn a separate
  research subagent or worker for Step 2.
- `sonar-pro` → prefer the platform's high-quality research mode **only if it
  runs in-thread in the current agent**.
- `standard` → use the platform's native web search / web fetch tools in the
  current agent.
- If the premium path would delegate to a subagent, or if it throws a model /
  tool conflict, skip it and continue with native web tooling in the current
  agent. Persist the backend that actually ran.

Persist every completed research run with `circuit-weaver save-research`. The saved
`{project_dir}/research/*.json` files are the source of truth; `design.log` should
point back to those JSON artifacts for reproducibility.

#### Phase 2a — Project Context

Single broad query to understand the design space in the current agent session:

```
Design a [application description].
  Constraints: [form factor], [power source], [interfaces].
  Find 1-2 existing reference designs, key IC families (MCU, power conversion, sensors),
  typical topologies, and estimated BOM size."
```

If `sonar-pro` is available without delegation, use that path for the query.
Otherwise, run the same query with the platform's built-in web tools and record
`standard` as the backend you actually used.

After the result is consolidated, persist it:

```bash
circuit-weaver save-research --project-dir ./output --backend <sonar-pro|standard> --topic "project-context" --file research.json
```

This grounds subsequent searches in reality.

**Log:** `[Step 2a] Started IC research for {application} | Query logged`

#### Phase 2b — Targeted Function Queries

For each major functional block, run targeted research in parallel:

If research depth is `fast`, run at most 2 of these and prioritize the highest-risk
blocks first:

- MCU / main SoC
- Primary power conversion path
- Primary sensor or interface only if it is novel, safety-critical, or likely to drive the package choice

In `fast` mode, return 1-2 options per block and skip deep alternates, detailed cost tables,
and non-critical connector lookups unless the user explicitly asks.

If research depth is `normal`, run 3-5 of these (adapt to your design):

```
MCU for [interfaces: WiFi, BLE, Ethernet, etc.],
  [power constraint: battery, low-power, high-performance].
  Return: Top 3 options with MPN, LCSC cost, key specs (flash, RAM, peripherals).

Power conversion: [input voltage] to [output voltage, current].
  Application: [battery/USB/wall-powered, form factor constraints].
  Return: Top 3 IC options (topology, MPN, LCSC cost, efficiency), required passives.

[Sensor type: environmental, motion, audio, etc.] for [application].
  Interface: [I2C/SPI/analog], power constraint: [mA budget].
  Return: Top 3 sensors with MPN, LCSC cost, typical application circuit.

Connector/interface: [USB/Barrel Jack/JST-PH/etc.] for [application].
  Return: Recommended part with MPN, LCSC cost, pin assignment, typical footprint.
```

Run these in parallel where the platform supports it, but keep them in the
current agent/session. Do not offload Step 2 to a research subagent. Use the
selected backend's same-agent tooling when available; otherwise use native web
tooling. Persist the backend and depth that actually ran with
`circuit-weaver save-research --project-dir ./output ...`.

**Log:** `[Step 2b] Targeted research queries:` [list each query]

#### Phase 2c — Present & Confirm

Consolidate findings into a table:

```
=== IC Selection Results ===

MCU (WiFi, 4MB flash, 500mA):
  [1] ESP32-S3-WROOM-1 (most common, $5.80)
  [2] ESP32-C3 (smaller, $3.50)

Power Conversion (3.7V → 3.3V @ 500mA):
  [1] TPS62300 (buck, 95% eff, $1.20)
  [2] LDO (simpler, lower noise, $0.50)

Sensor (I2C, temp+humidity+pressure):
  [1] BME280 (standard, $2.15)
  [2] BME680 (with gas, $3.50)

Charging Circuit (LiPo, USB 5V input):
  [1] TP5000 (simple, $1.80)
  [2] BQ24075 (feature-rich, $3.50)
```

**Claude Code / Codex / OpenCode:**
"Do these IC selections look good? Want to swap any?"

**Log:** `[Step 2c] IC selections confirmed: {selected_ics_list}`

### Step 3 — Generate Design Spec

Choose the closest supported topology from `list-templates`, scaffold one valid
starting block, and then edit the YAML to incorporate the confirmed architecture,
ICs, interfaces, and constraints. `scaffold` accepts a template and reference; it
does not accept project-name or MPN flags.

```bash
python -m circuit_weaver scaffold \
  --template "${SELECTED_TEMPLATE}" \
  --ref "U1" \
  --output "${PROJECT_NAME}/design.yaml"
```

Example:
```bash
python -m circuit_weaver scaffold \
  --template "buck" \
  --ref "U1" \
  --output "WiFi_Sensor_v1/design.yaml"
```

Set the top-level `project` value to `${PROJECT_NAME}`. Replace scaffold
placeholders and add the selected blocks before validation. For a custom
architecture with no matching template, author `design.yaml` directly from a
validated sample/schema rather than inventing unsupported scaffold flags.

Give every architectural block a stable unique `id` and an explicit functional
`section`, for example `power_input`, `core_processing`, `sensors`,
`communications`, `user_interface`, or `debug`. Generation preserves those
sections as separate schematic sheets even for a small design; support
passives stay with their owning block. Do not collapse unrelated functions into
one section merely to reduce file count. The allocator sanitizes and
disambiguates filenames, but unique, descriptive section names make the
hierarchy legible to humans.

**Output:** a complete `${PROJECT_NAME}/design.yaml` ready for validation.

**Log:** `[Step 3] Design spec generated: design.yaml`

### Step 4 — Validate Design

Run validation to catch errors before generation:

```bash
python -m circuit_weaver validate "${PROJECT_NAME}/design.yaml"
```

If validation passes:
```
[PASS] Design validated successfully.
  - Electrical checks: OK
  - Power domain consistency: OK
  - Decoupling coverage: OK
```

If validation fails, display errors and ask user to refine the spec.

**Log:** `[Step 4] Validation: {PASS|FAIL}. Errors: {error_list if any}`

### Step 5 — Generate Artifacts

Generate the schematic and placement files:

```bash
python -m circuit_weaver generate "${PROJECT_NAME}/design.yaml" \
  --output "${PROJECT_NAME}/output"
```

Before any release-quality, handoff, or fabrication-readiness claim, regenerate
with the real KiCad gate:

```bash
python -m circuit_weaver generate "${PROJECT_NAME}/design.yaml" \
  --output "${PROJECT_NAME}/output" --require-kicad
```

Internal validation is not a substitute for this final KiCad load/ERC check.

Read the JSON result from stdout, open the path in its `artifact_manifest`
field, and use `artifact_manifest.json` as the source of truth. Read
`root_schematic` and select paths from its `artifacts` array. Manifest paths
are portable and relative to the directory containing the manifest, so resolve
them against `artifact_manifest.json`'s parent directory. Use the result's
`files` only as a compatibility fallback.

Inspect `valid`, `kicad_verified`, `verification_status`, and `erc` in the
manifest before reporting success. `verification_status: unverified` means
KiCad was unavailable or skipped; say so explicitly and never describe that
artifact as KiCad-verified. The generated bundle includes:

- the manifest-selected root schematic plus one sub-sheet per functional section;
- `assembly_manifest.json`, the exhaustive physical-part/reference inventory;
- `placement_result.json`, `placement_review_context.json`, `placement.svg`, and
  `placement_editor.html`, the review-only placement bundle; and
- `${PROJECT_NAME}_report.md`, the design analysis and power budget.

Never guess `main.kicad_sch` or any section filename. Resolve every artifact
through `artifact_manifest.json`. Inspect `placement_result.json` before showing
the visual artifacts: it must reconcile exactly to `assembly_manifest.json` and
must always say `fabrication_ready: false`. Surface every placement blocker.
Treat manufacturer datasheets and reference layouts linked from
`placement_review_context.json` as authoritative over the heuristic proposal.

**Log:** `[Step 5] Artifacts generated in output/`

### Step 6 — Confidence & Simulation Check

**This step runs automatically after artifact generation. Do not skip it.**

Run the confidence dashboard to get a unified design readiness score:

```bash
python -m circuit_weaver confidence "${PROJECT_NAME}/design.yaml" \
  --run-sims \
  -o "${PROJECT_NAME}/output/confidence_report.html"
```

This runs **all available checks** in one pass:
- Electrical validation (14 checks: decoupling, enable pins, power budget, thermal, SI, etc.)
- Circuit simulation via ngspice (power supply transient/AC, filter response)
- Thermal analysis (junction temperature vs Tj_max)
- Cross-reference audit (spec vs schematic, BOM completeness, duplicate refs)
- ERC (if KiCad CLI available)

Present the result to the user:

```
=== Design Confidence Report ===

Score:      82/100 (B)
Readiness:  NEEDS REVIEW

Sections:
  [OK] Electrical Validation     90/100 (A)
  [OK] Simulation                75/100 (C)
  [OK] Thermal Analysis         100/100 (A)
  [--] Signal Integrity            N/A (skipped)
  [--] Manufacturing (DFM)         N/A (skipped)
  [OK] Cross-Reference Audit     85/100 (B)
  [--] ERC/DRC                     N/A (skipped)

Action Items (2):
  - [high] U1: Ripple 62 mV exceeds target 50 mV
  - [medium] Install ngspice to enable full simulation

HTML report: output/confidence_report.html
```

**Interpretation guidance for user:**
The confidence score and grade summarize the checks in this report. They do not
determine manufacturing readiness. Read the canonical state and report its
blockers and next actions:

```bash
python -m circuit_weaver manufacturing-readiness "${PROJECT_NAME}"
```

Use the `state` in `manufacturing_readiness.json` as the canonical manufacturing readiness authority. Do not infer
`fabrication_ready` from a confidence score or describe a design as ready to fabricate from the score alone. Review the
confidence report's action items with the user and address the relevant ones.

**Log:** `[Step 6] Confidence: {score}/100 ({grade}), readiness: {readiness}`

### Step 7 — PCB Layout Preparation

**Goal:** Review the exhaustive heuristic proposal, then apply approved
coordinates to a separate, real electrical PCB created in KiCad. The generated
placement JSON/SVG/HTML files are not a PCB, CPL, or fabrication evidence.

#### 7a. Placement Optimization

Start with the placement bundle produced by `generate`. Open
`placement_result.json` and `placement_review_context.json`; do not hide
overlap, boundary, missing-geometry, missing-sourcing, support-part, or
constraint blockers. Use the official component/reference-layout URLs in the
context to review power loops, decoupling, RF/antenna keepouts, crystals,
connectors, and high-speed interfaces.

Optionally rerun the standalone optimizer with a deliberate configuration:

```bash
python -m circuit_weaver optimize-placement "${PROJECT_NAME}/design.yaml" \
  -o "${PROJECT_NAME}/output/placement.json"
```

This optimizes component positions for:
- Thermal spacing (power ICs separated, copper pour areas)
- Signal integrity (high-speed pairs close, short traces)
- Manufacturing (courtyard clearances, assembly accessibility)

#### 7b. Interactive Placement Viewer

Open `${PROJECT_NAME}/output/placement_editor.html` to drag the exact assembly
inventory and export an edited `placement.svg`. The standalone viewer remains
available for alternate optimizer settings:

```bash
python -m circuit_weaver placement-viewer "${PROJECT_NAME}/design.yaml" \
  -o "${PROJECT_NAME}/output/placement.html"
```

Present both the visual proposal and its blockers/context. Never equate an
attractive diagram with electrical or manufacturing readiness.

#### 7c. Create the Electrical PCB

For automated prototype work, take the exact `root_schematic` returned by
generation and run `build-pcb` into a separate output, followed by `place-pcb`:

```bash
python -m circuit_weaver build-pcb "${ROOT_SCHEMATIC}" -o "${ELECTRICAL_PCB}"
python -m circuit_weaver place-pcb "${ELECTRICAL_PCB}" -o "${PLACED_PCB}" --design "${SPEC_FILE}" --tidy-references
```

This loads real library footprints and requires exact pin/pad numbering. Missing
footprints, incomplete symbol pad coverage, annotation errors, or physical DRC
block publication. Review any explicit prototype clearance assumption before
passing `--clearance`; it cannot weaken existing source board rules. The board
remains a prototype without identity evidence or manufacturing approval.
Declare board dimensions, connector mating edge and rotation in `pcb_constraints`
before placement. Pass the matching design with `--design` to retain generated
support ownership and functional grouping. Review `layout_review`, native
silkscreen unresolved counts, and the actual rendered board alongside DRC.
Do not infer a connector's mating direction from its reference prefix, or shrink
an enclosure-constrained board just to improve its appearance.

Alternatively, open the generated schematic in KiCad, assign and verify
footprints, run **Update PCB from Schematic**, and save the electrical board.

Dry-run the edited SVG against that board. Exact SVG↔PCB reference matching is
the default; use `--allow-partial` only when the user explicitly intends a
subset update. Unknown SVG refs always fail.

```bash
python -m circuit_weaver import-placement \
  "${PROJECT_NAME}/output/placement.svg" "${ELECTRICAL_PCB}" \
  -o "${PLACED_PCB}" --dry-run
python -m circuit_weaver import-placement \
  "${PROJECT_NAME}/output/placement.svg" "${ELECTRICAL_PCB}" \
  -o "${PLACED_PCB}"
```

#### 7d. Autorouting an Electrical PCB (Optional)

Offer autorouting only for a real pad-bearing, named-net board or a user-exported
Specctra `.dsn`. For a real PCB, request a separately published routed board:

```bash
python -m circuit_weaver autoroute "${PLACED_PCB}" --routed-board "${ROUTED_PCB}" --attempts 3
```

KiCad's native Python can export DSN and import SES automatically when the CLI
cannot. The app publishes the routed board only after native DRC passes;
verify its exact board hash and surface warnings. Without `--routed-board`,
output remains a validated `.ses` requiring import and DRC. Freerouting must be
installed separately. Treat exit code 2 / `status: partial` as incomplete.
Review power, switching loops, differential pairs, clocks, crystals, RF, and
other critical nets against engineering constraints. Native DRC does not grant
manufacturing readiness.

**Claude Code / Codex / OpenCode:** Present choices:
- **[1] Optimize placement** (recommended)
- **[2] View placement** (generate interactive HTML)
- **[3] Open the schematic and create/update the electrical PCB in KiCad**
- **[4] Autoroute a user-supplied electrical PCB** (requires Freerouting)
- **[5] Skip to review**

Run whichever the user selects. After each action, offer to run another or proceed.

**Log:** `[Step 7] PCB layout: {actions_taken}`

#### Optional Repository Templates

A source checkout may contain deeper templates under `project-skills/` for
placement, autorouting, pin maps, hierarchy, and custom generation. They are not
included by PyPI `install-skills`. Read one only after verifying its `SKILL.md`
exists in the current repository; never invoke a missing template by skill name.
When absent, continue with the built-in CLI commands and KiCad workflow above.

### Step 8 — Design Review & Next Steps

Display:

```
=== Design Complete ===

Project:      ${PROJECT_NAME}
Logfile:      ${PROJECT_NAME}/design.log
Manifest:     ${ARTIFACT_MANIFEST_FROM_GENERATE_JSON}
Schematic:    ${ROOT_SCHEMATIC_FROM_ARTIFACT_MANIFEST}
Placement:    ${PROJECT_NAME}/output/placement_result.json (review status)
Context:      ${PROJECT_NAME}/output/placement_review_context.json
Editor:       ${PROJECT_NAME}/output/placement_editor.html
Report:       ${DESIGN_REPORT_FROM_ARTIFACT_MANIFEST}
Confidence:   ${PROJECT_NAME}/output/confidence_report.html

Next steps:
  1. Open the root schematic reported by generate and review it in KiCad
  2. Assign footprints and update an electrical PCB from the schematic
  3. Place and route the electrical PCB (power and critical nets first)
  4. Run KiCad ERC/DRC, then Circuit Weaver DFM on the electrical PCB
  5. Export Gerbers from the electrical PCB and order after review
```

Question: "What would you like to do next?"

**Claude Code / Codex / OpenCode:** Present choices:
- Export BOM & CPL for assembly
- Re-run confidence report (after making changes)
- Re-optimize placement
- Run DFM check on PCB (`python -m circuit_weaver check-dfm <pcb>`)
- Make changes to the design (return to Step 2)
- Done (exit)

**Log:** `[Step 8] Design review complete. User choice: {export|confidence|placement|dfm|edit|done}`

---

## Workflow: Existing Design

### Route to Existing Design

If Step -1 already discovered user projects (excluding samples), present the list with numbered choices and let the user select one. Also offer a "Browse for path" option.

If no user projects were found, or the user chooses to browse:
- Ask for a design path.
- Accept `design.yaml`, a KiCad project/file, a PCB, a Gerber/drill directory,
  or a ZIP archive.

**Claude Code / Codex / OpenCode:** Ask for a path as free text.

If the selected project already has `.circuit-weaver/project.json`, reconcile it:

```bash
python -m circuit_weaver status "${DESIGN_PATH}"
python -m circuit_weaver resume "${DESIGN_PATH}"
```

If it has no durable manifest, import it non-destructively. `--analyze` runs all
applicable bundled schematic/PCB/Gerber analyzers and caches their evidence:

```bash
python -m circuit_weaver import-design "${SOURCE_PATH}" --analyze
```

Read `project_root`, `manifest`, `analysis_status`, and `next_actions` from the
JSON. Do not assume an imported native design has `design.yaml`. A netlist-only
source is inventoried but its analysis is truthfully unsupported. Use `--force`
only when the user intentionally replaces a changed import source set/staging
tree or asks to invalidate the analysis cache.

### Display Current State

```bash
python -m circuit_weaver status "${PROJECT_ROOT}"
python -m circuit_weaver resume "${PROJECT_ROOT}"
```

These commands reconcile the manifest with current files and show:
```
=== Design Status ===

Project:      WiFi_Sensor_v1
Type:         circuit_weaver or kicad_native
Status:       generated, imported, analyzed, failed, or modified
Current phase: placement_review
Next actions: deterministic commands/inspections for this state
```

`resume` reports a restart plan and never executes it. Use `log-status` and
`log-view` only as supporting diagnostics after durable state is reconciled.

### Offer Actions

Build the menu from the reconciled inventory. If no `design.yaml` exists, omit
validate, regenerate, optimize, confidence, simulation, and spec-based BOM
actions. For a native import, offer source analysis, state/reconciliation,
review of `.circuit-weaver/analysis/index.json`, and normal KiCad editing. Do
not fabricate a spec merely to make a menu command runnable.

**Claude Code / Codex / OpenCode:** Use AskUserQuestion / numbered options:

```
What would you like to do?

  --- Verify ---
  [1] Analyze/reanalyze imported KiCad, PCB, and Gerber sources
  [2] Validate design spec (when design.yaml exists)
  [3] Run confidence/simulation checks (when design.yaml exists)

  --- Generate & Layout ---
  [4] Regenerate schematic (after making edits)
  [5] Optimize PCB placement (simulated annealing)
  [6] Review placement result, context, SVG, and interactive editor
  [7] Autoroute an electrical PCB (requires Freerouting)

  --- Export ---
  [8] Export BOM & CPL for ordering
  [9] Export Gerbers for fabrication
  [10] Run DFM check on PCB

  --- Other ---
  [11] View design report
  [12] Make changes to the design
  [13] Exit
```

Route based on selection:
- **[1] Analyze** → `python -m circuit_weaver analyze-design "${PROJECT_ROOT}"`; add `--force` only for an intentional rerun
- **[2] Validate** → If a design spec exists, `python -m circuit_weaver validate ${DESIGN_PATH}/design.yaml --enhanced --verbose`
- **[3] Confidence/simulate** → If a design spec exists, run `confidence --run-sims`; never invent a spec for a native import
- **[4] Regenerate** → `python -m circuit_weaver generate ${DESIGN_PATH}/design.yaml -o ${DESIGN_PATH}/output`
- **[5] Optimize placement** → `python -m circuit_weaver optimize-placement ${DESIGN_PATH}/design.yaml -o ${DESIGN_PATH}/output/placement.json`
- **[6] Placement review** → Read `placement_result.json` and `placement_review_context.json`, then open `placement_editor.html`; do not skip blockers
- **[7] Autoroute** → Ask for a real PCB or DSN, then run `python -m circuit_weaver autoroute "${ROUTING_SOURCE}" -o ${DESIGN_PATH}/output/routed.ses`; import the SES in KiCad and run DRC
- **[8] Export BOM & CPL** → Require both a design spec and `${ELECTRICAL_PCB}`, then run `python -m circuit_weaver export-jlcpcb ${DESIGN_PATH}/design.yaml --pcb "${ELECTRICAL_PCB}" -o ${DESIGN_PATH}/export`; without `--pcb`, label the result BOM-only
- **[9] Export Gerbers** → Ask for `${ELECTRICAL_PCB}`, then run `python -m circuit_weaver export-gerbers "${ELECTRICAL_PCB}" -o ${DESIGN_PATH}/gerbers`
- **[10] DFM check** → Ask for `${ELECTRICAL_PCB}`, then run `python -m circuit_weaver check-dfm "${ELECTRICAL_PCB}"`
- **[11] Report** → Read `artifact_manifest.json` and show its `*_report.md` artifact
- **[12] Changes** → Return to Step 1 (requirements capture for edits)
- **[13] Exit** → End the skill

---

## Implementation Notes

### Project Logging (ALL PLATFORMS)

**Critical:** Project folder + design.log must be created **immediately** after user enters project name (Step 1a), BEFORE any other questions.

Use the platform's filesystem tools to create `${PROJECT_NAME}/` and an empty
`${PROJECT_NAME}/design.log`. Do not emit POSIX-only `touch` or `mkdir -p`
commands on Windows. Log: `[Step 1a] Project created: ${PROJECT_NAME}`.

Subsequent steps must write logs like:
```
[Step 1b] Experience level: Intermediate
[Step 1c] Purpose: WiFi environmental sensor
[Step 2a] Started IC research for WiFi environmental sensor
[Step 2c] IC selections confirmed: [ESP32-S3, TPS62300, BME280]
[Step 3] Design spec generated: design.yaml
[Step 4] Validation: PASS
[Step 5] Artifacts generated in output/
[Step 6] Confidence: 82/100 (B), readiness: needs_review
[Step 7] PCB layout: optimize-placement, placement-viewer
[Step 8] Design review complete. User choice: export
```

### For Claude Code

The skill emits AskUserQuestion tool calls. Claude Code's TUI renders buttons/checkboxes and responses return as tool results. Claude creates the project folder and log with its filesystem tools in Step 1a; terminal users can instead run the built-in `design-wizard` command.

### For Codex/OpenCode

Use conversational prompting with numbered options. The AI model handles the input, user types their selection. Same logging behavior as Claude Code.

### For CLI Users

They run:
```bash
python -m circuit_weaver design-wizard
```

This directly invokes `_run_design_wizard()` in Python with `input()` prompts. **The Python function handles folder + log creation immediately after getting project name.**

### Python Subcommands

All Python operations accept **command-line arguments only**, no interactive prompts:
- `scaffold --template buck --ref U1 --output design.yaml`
- `validate design.yaml [--enhanced] [--verbose] [--detailed-score]`
- `generate design.yaml --output ./out`
- `import-design <source> [--project-dir <dir>] [--analyze] [--force]`
- `analyze-design <project> [--force]`
- `status <project> [--json]`
- `resume <project> [--json]`
- `export-jlcpcb design.yaml --output ./export [--pcb board.kicad_pcb]`
- `export-dual-cpl design.yaml --pcb board.kicad_pcb --output ./export`
- `import-placement placement.svg board.kicad_pcb [-o placed.kicad_pcb] [--allow-partial]`
- `autoroute board.kicad_pcb -o routed.ses`
- `preview-pcb routed.kicad_pcb -o board-preview.svg [--side front|back] [--render-3d]`
- `confidence design.yaml [--run-sims] [--pcb file.kicad_pcb] [-o report.html] [--json]`
- `simulate design.yaml [-o ./sims] [--type power|signal|thermal|all] [--json]`
- `discover [--root .] [--depth 2] [--json]`
- `log-event project_dir --type <type> --message <msg> [--data <json>]`
- `log-status project_dir`
- `log-view project_dir` (show recent log entries)

This ensures the skill can call them without dealing with subprocess stdin/stdout complexity.

`preview-pcb` draws actual board layers and footprint fabrication geometry; its
back view is physically mirrored. Optional `--render-3d` uses the board's declared
model files with KiCad 9+, and fails clearly if models cannot be resolved. A
render is an engineering view and does not grant manufacturing approval.

### Validation Output Handling

Treat `validate` output carefully.

- In the current Circuit Weaver CLI, `validate` already emits JSON to **stdout** by default.
- Human diagnostics and environment warnings may still appear on **stderr**.
- Do **not** add `--json` to `validate` unless the CLI explicitly grows that flag in a later version.
- Do **not** merge stderr into stdout with `2>&1` before parsing JSON.
- A non-zero exit code from `validate` usually means the design is invalid, not that the command wrapper failed.

Expected top-level JSON keys:

```text
valid
summary
categories
metadata
```

Do **not** assume top-level `error_count`, `warning_count`, or `errors` fields.
Use `summary` and `categories`.

Safe pattern:

```bash
circuit-weaver validate design.yaml > val.json 2> val.err
python -c "import json; data=json.load(open('val.json', encoding='utf-8')); print('Valid:', data['valid']); print('Summary:', data['summary'])"
```

If you need to summarize failures, read from `categories` rather than inventing a
wrapper that reparses mixed stdout/stderr streams.

---

## Related Skills

- **design-wizard** — Explicit interactive/offline wizard variant
- **ee** — Electrical engineering formulas and analysis
- **bom** — BOM management and sourcing
- **kicad** — Schematic and PCB analysis
- **jlcpcb** — Manufacturing and ordering
