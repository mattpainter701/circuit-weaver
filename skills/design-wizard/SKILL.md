---
name: design-wizard
description: >
  Run the explicit, human-in-the-loop Circuit Weaver questionnaire from requirements
  through a validated design and KiCad handoff. Use only when the user asks for the
  design-wizard skill, the offline interactive wizard, or a slow step-by-step guided
  questionnaire. Do not trigger for ordinary new-design requests; use circuit-weaver
  for automatic end-to-end orchestration.
---

# Circuit Design Wizard

An interactive, console-level question-and-answer workflow that hand-holds
users through the full Circuit Weaver pipeline — from vague idea to
quote-ready KiCad outputs.

> **Disambiguation:** This is the **step-by-step guided** wizard with human-in-the-loop control at every step. For **automatic same-agent** design orchestration, use `$circuit-weaver`. For **analyzing existing KiCad files**, use `$kicad`.

**About this skill:** This is the **manual, step-by-step guide** with human-in-the-loop control. Compare with:
- **`$circuit-weaver` skill** — Automatic, same-agent orchestration for IC research and design assembly; supports `fast` vs `normal` research depth, and uses `sonar-pro` when configured and compatible, otherwise native web tooling
- **`circuit-weaver design-wizard` CLI** — Offline interactive wizard (no agents, works standalone, good for learning)

**How this skill works:** You (the AI agent) drive a multi-step conversation.
At each step you ask the user targeted questions, summarize what you learned,
and then proceed. Never skip a step without telling the user why. Use the
`AskUserQuestion`-style conversational pattern throughout — present numbered
options, suggest defaults, and let the user confirm or override.

---

## Output Formatting Rules

When presenting CLI command output to the user, follow these rules for clarity:

| Command | Success Output | Error Output |
|---------|---|---|
| `validate` | `"Validation passed: X errors, Y warnings"` or full categories if issues exist | List each error with code + message |
| `apply-patch` | `"Added [ref] ([topology]) to design.yaml"` | List errors from patch validation |
| `scaffold` | Print the generated YAML snippet | Report error with details |
| `generate` | JSON containing `artifact_manifest`; the manifest contains `root_schematic` and `artifacts` | Report generation failure reason |
| `export-jlcpcb` | `"JLCPCB export: [bom_rows] rows, [missing_lcsc] need manual lookup"` | Report export error |
| `cost-bom` | Print formatted BOM table with totals | List pricing lookup failures |

Never output raw JSON unless the user explicitly requests `--json` flag. Present human-readable tables and summaries instead.

For `validate` specifically:

- The current Circuit Weaver CLI emits structured JSON to **stdout** by default.
- Diagnostics and environment warnings may still appear on **stderr**.
- Do **not** add `--json` to `validate` unless the CLI explicitly supports it.
- Do **not** parse JSON from a stream where stderr has been merged with stdout via `2>&1`.
- Read `valid`, `summary`, `categories`, and `metadata` from the JSON object.

---

## Long-Running Operations & Timeout Awareness

Do not leave the user without status during long wizard-driven work.

For any step likely to exceed ~2 minutes — research, repeated validation,
generation, simulations, confidence runs, placement, exports, or large log/design
triage — the wizard must:

1. Announce the long-running step before starting it.
2. Follow up at ~2 minutes with a progress check.
3. At ~5 minutes, inspect for issues rather than just waiting:
   - `python -m circuit_weaver log-status <project_dir>`
   - `python -m circuit_weaver log-view <project_dir>`
   - `design.log`
   - `circuit-weaver.log`
   - recently created or updated artifacts
4. Tell the user whether the work is progressing, blocked, or likely stuck.
5. Never stay silent for ~30 minutes; keep sending bounded follow-ups and surface blockers.

If resuming after an interrupted run, check status/logs first before retrying the
same expensive command sequence.

---

## Step -1 — Auto-Detection (ALWAYS RUN FIRST)

Before starting, scan the current directory for existing projects:

```bash
python -m circuit_weaver discover --json
```

**Exclude bundled samples**: Filter out any project whose path contains `samples/` or `samples\`. The `samples/` directory contains reference designs shipped with Circuit Weaver — they are NOT user projects. Only show user-created projects.

If user projects are found, present them and ask whether the user wants to:
1. Continue/modify an existing project (load its design.yaml and skip to the relevant step)
2. Start a fresh design (proceed to Step 0)

If no user projects are found, proceed directly to Step 0.

---

## Step 0 — Welcome & Orientation

Greet the user and explain what the wizard will cover:

```
Welcome to the Circuit Weaver Design Wizard.

I'll walk you through building a new circuit design from scratch:

  Step 1  Requirements & goals
  Step 2  IC selection & research
  Step 3  BOM assembly & sourcing preferences
  Step 4  Schematic generation
  Step 5  Design review checkpoint
  Step 6  Confidence & simulation check
  Step 7  PCB layout guidance & next steps

You can pause, go back, or skip any step. Let's start.
```

Ask: **Do you have an existing YAML spec or are we starting fresh?**

- If they have a spec, load it and skip to the step that makes sense.
- If starting fresh, proceed to the experience check below.

### 0a. Experience Level Calibration

Before diving in, gauge the user's background so you can adjust depth and
pacing throughout the wizard. Ask:

- **How would you describe your electronics experience?**
  1. **Beginner** — I've used dev boards (Arduino, Raspberry Pi) but never
     designed a custom PCB
  2. **Intermediate** — I've designed 1-3 boards, comfortable reading
     schematics, done basic soldering
  3. **Advanced** — I regularly design boards, familiar with KiCad, understand
     signal integrity and DFM
  4. **Professional EE** — Just get me to a YAML spec fast, I know what I'm doing

Adapt behavior based on their answer:

| Level | Wizard behavior |
|-------|----------------|
| Beginner | Explain every concept briefly as it comes up. Suggest safe defaults aggressively. Flag anything that needs manual EE judgment. Link to the `ee` skill reference for learning. |
| Intermediate | Explain non-obvious trade-offs. Suggest defaults but invite overrides. Skip basic definitions. |
| Advanced | Lead with options, not explanations. Compact summaries. Start with a compact design brief that covers purpose, power, interfaces, and constraints, then only fill gaps. |
| Professional | Minimize chatter. Do not open with a standalone "What does the circuit do?" prompt. Start with a structured design brief or pasted spec fragment, then ask only missing or high-risk questions. |

Store the experience level and reference it throughout — don't re-ask.

### 0b. Intake Mode By Experience

Use a different first prompt after calibration:

- **Beginner**: Start with a plain-language purpose question, then walk through mechanics, interfaces, and power one topic at a time.
- **Intermediate**: Group 2-3 related requirement questions per turn, with examples and defaults.
- **Advanced**: Start with one compact design brief covering purpose, input power, rails/current, key interfaces, and mechanical constraints. Follow up only on missing fields.
- **Professional**: Start with one batch intake prompt or a pasted YAML/spec fragment. Useful format: `purpose; input power; rails/current; interfaces; mechanical constraints; preferred ICs/vendors`. Do **not** immediately ask a generic standalone "what does it do?" question.

---

## Step 1 — Requirements Definition

**Goal:** Capture enough context to make informed IC and topology decisions.

Use the intake mode that matches the stored experience level:

- **Beginner / Intermediate**: Ask these questions one group at a time. After each answer, acknowledge and summarize before moving on.
- **Advanced / Professional**: Treat sections 1a-1d as a completeness checklist, not a fixed question order. Start with a compact brief, then ask follow-ups only for missing, ambiguous, or risky details.

### 1a. Purpose & Application

For **Beginner / Intermediate**, ask this directly as the opening requirements question.
For **Advanced / Professional**, this should usually come from the compact design brief; only re-ask if the brief is vague or incomplete.

Ask:
- What does this circuit/board need to do? (e.g., "battery-powered IoT sensor",
  "USB-powered motor controller", "audio amplifier with Bluetooth")
- What is the end-use environment? (consumer, industrial, automotive, hobby)
- Any size or form-factor constraints? (e.g., "fits in a 50x30mm enclosure")

### 1a-mech. Mechanical & Enclosure Integration

If the user mentioned an enclosure or physical constraints, dig deeper:
- Is there an existing enclosure this board must fit inside? (dimensions, mounting pattern)
- Any fixed connector positions? (e.g., "USB port must be on the left edge")
- Height restrictions? (component height limit from enclosure lid)
- Mounting method? (screw holes, snap-fit tabs, standoffs, adhesive)
- Board-to-board stacking? (if part of a multi-board system)
- Cable routing constraints? (which edges have cable egress)

For beginners: explain that connector placement and mounting holes are best
decided now — moving them after PCB layout is expensive rework.

Record all mechanical constraints — they feed directly into Step 7 (PCB layout)
for board outline and keep-out zone generation.

### 1a-rf. Specialized RF / Microwave Intake

If the design brief mentions RF/microwave work beyond ordinary module-level
wireless integration (for example: radar, phased array, beamforming, Ku-band,
mmWave, SDR front-end, custom LNA/mixer/filter chain), switch to this
specialized intake instead of treating it like a generic embedded board.

Ask:
- What frequency band and bandwidth are you targeting?
- What is the system architecture? (direct conversion, superhet, FMCW radar, phased array, IF sampling, etc.)
- How many channels or array elements are involved?
- What are the transmit / receive power, gain, NF, dynamic range, and phase-coherence requirements?
- What reference clock / LO architecture is required?
- What transmission-line / controlled-impedance constraints already exist?
- What parts of the chain are fixed versus still open for selection?

Then state clearly:
- Circuit Weaver can still help with architecture capture, block partitioning,
  BOM research, custom block scaffolding, review, and simulation planning.
- Final microwave layout closure, EM behavior, antenna performance, shielding,
  and calibration remain manual engineering tasks.

### 1b. Features & Interfaces

Ask:
- What external interfaces are needed? (USB, SPI, I2C, UART, Ethernet, WiFi,
  BLE, CAN, analog I/O, GPIO, display, buttons, LEDs, etc.)
- What sensors or actuators? (temperature, pressure, IMU, motor, relay, etc.)
- How many of each? Any specific models or preferences?

### 1c. Power & Electrical Requirements

Ask:
- What is the primary power source? (USB 5V, battery type/voltage, wall adapter,
  PoE, solar, etc.)
- What voltage rails do you need? (3.3V, 1.8V, 5V, 12V, etc.)
- Estimated current budget per rail? (rough is fine — "under 500mA" works)
- Any battery charging requirements?

### 1c-validate. Power Budget Sanity Check

**Do this immediately after collecting power requirements.** Don't wait until
IC selection — catch impossible power trees now.

Using the `ee` skill reference formulas, run a quick power budget:

```
=== Power Budget Estimate ===

Source: USB 5V (500mA USB 2.0 or 3A USB-C PD)

Rail         Voltage   Est. Current   Power
---------------------------------------------
VDD_3P3      3.3V      350 mA         1.16 W
VDD_1P8      1.8V       50 mA         0.09 W
VMOT        12.0V      800 mA         9.60 W
---------------------------------------------
Total load                            10.85 W
Source capacity (USB 5V @ 3A)         15.00 W
Headroom                               4.15 W (27%)  OK

Conversion losses (~85% buck efficiency):
  Total input draw ≈ 12.76 W → 2.55 A @ 5V   OK (under 3A)
```

**Flag problems early:**
- Total load exceeds source capacity → stop and renegotiate requirements
- Headroom < 15% → warn about thermal margins
- Battery runtime math: capacity_mAh / avg_draw_mA = runtime_hours
- If battery-powered, estimate runtime and ask if it's acceptable

For beginners: explain why we check this now ("if the power math doesn't add
up, no amount of clever IC selection will fix it").

### 1d. Goals & Constraints

Ask:
- Target unit cost at volume? (or "doesn't matter, it's a prototype")
- Target production volume? (1-off prototype, 10 units, 1000+, etc.)
- Timeline or deadline?
- Regulatory or certification needs? (FCC, CE, UL, automotive, medical, etc.)
- Any must-use or must-avoid components/vendors?

### 1e. Comparable Products or Reference Designs

Ask:
- Are there existing products, dev boards, or reference designs that do
  something similar? (e.g., "like an Adafruit Feather but with CAN bus")
- Any open-source hardware projects to draw from?
- Datasheets or app notes you've already found?

### Summary Gate

After collecting all answers, present a structured **Requirements Summary**:

```
=== Requirements Summary ===

Project:       [name]
Application:   [description]
Environment:   [consumer/industrial/etc.]
Form factor:   [constraints]

Interfaces:    [list]
Sensors:       [list]
Actuators:     [list]

Power source:  [description]
Voltage rails: [list with estimated currents]
Battery:       [charging? type?]

Volume:        [quantity]
Budget:        [target cost or "prototype"]
Certifications: [list or "none"]

Comparables:   [list]
```

Ask: **Does this look right? Anything to add or change?**

Iterate until the user confirms.

### Summary-risk. Complexity & Risk Flags

After the user confirms the requirements summary, automatically scan for
design areas that are harder than average. Present any flags found:

```
=== Complexity Flags ===

  ⚠ HIGH-SPEED DIFFERENTIAL: USB 3.x or Ethernet requires controlled-
    impedance routing and a 4-layer stackup. This adds PCB cost and
    requires careful layout — it can't be fully autorouted.

  ⚠ RF / ANTENNA: WiFi/BLE with a PCB antenna or external antenna
    needs RF matching and ground plane management. Expect manual
    tuning and possibly a VNA for validation.

  ⚠ MICROWAVE / PHASED ARRAY / RADAR: Ku-band and other microwave
    systems are still in scope, but they are not turnkey-generated
    designs. Expect a research-first workflow, explicit RF chain
    blocks, transmission-line constraints, custom simulation work,
    and manual layout/validation closure.

  ⚠ MIXED-SIGNAL: Combining analog sensors with digital/switching
    circuits requires careful ground partitioning and power filtering.
    Layout matters more than schematic here.

  ⚠ HIGH CURRENT (>2A per rail): Power traces need width calculations,
    thermal relief, and possibly copper pours. May need thermal vias
    under regulator pads.

  ⚠ BATTERY + CHARGING: Adds a charging IC, protection circuit, and
    fuel gauge considerations. Safety implications for Li-ion/LiPo.

  ✓ No certification flags for hobby/prototype use.
```

Only show flags that apply. For each flag:
- Explain what it means in plain language (especially for beginners)
- State whether Circuit Weaver can handle it automatically or if it needs
  manual EE work
- Ask if the user wants to proceed, simplify, or get more details

For **Professional** users in specialized domains, default to "proceed with
custom architecture mode" instead of framing the domain as unsupported.

If no flags apply, say so: "This design is straightforward — no special
complexity concerns."

### Summary-test. Test & Debug Strategy

Before moving to IC selection, ask about testability. Many users forget this
and regret it when the board arrives and they can't debug it.

Ask:
- **Debug interface:** Do you need SWD/JTAG header for MCU debugging?
  (Suggest: yes, even for prototypes — it's a $0.20 header that saves hours)
- **Serial console:** UART header for debug logging?
  (Suggest: yes, expose TX/RX/GND on a 3-pin header)
- **Power LEDs:** LED on each voltage rail to confirm power is up?
  (Suggest: yes for prototypes, DNP for production)
- **Test points:** Exposed pads on critical nets for oscilloscope probing?
  (Suggest: at minimum on power rails and clock signals)
- **Spare GPIO:** Break out 2-4 unused MCU pins to a header for rev2 features?
  (Suggest: yes — costs nothing and enables future flexibility)

For beginners: explain why each matters. "An LED on your 3.3V rail costs
$0.02 and immediately tells you if power is working when you first plug in."

For professionals: present as a compact checklist, defaults all yes, let them
trim.

Add confirmed debug/test features to the requirements summary.

---

## Step 2 — IC Selection & Research

**Goal:** Use AI reasoning to identify the primary ICs, then kick off
research to validate and enrich selections before BOM construction.

### 2a. AI-Driven IC Reasoning

Based on the requirements summary, reason through the design and propose
primary ICs for each functional block:

- **MCU / processor** — Match peripherals, memory, package, ecosystem
- **Power management** — Buck, boost, LDO, charger ICs based on rails and source
- **Communication ICs** — WiFi/BLE modules, Ethernet PHY, CAN transceivers, etc.
- **Sensor ICs** — Match specifications to requirements
- **Driver ICs** — Motor drivers, LED drivers, display drivers
- **Protection** — ESD, overcurrent, reverse polarity

For MCUs specifically, also consider the **firmware & software ecosystem**:
- **Toolchain:** Arduino, ESP-IDF, STM32CubeIDE, Zephyr RTOS, PlatformIO, etc.
- **SDK maturity:** Is the SDK well-documented? Active community? Stable releases?
- **Debugger support:** SWD/JTAG support, compatible probes (J-Link, ST-Link, etc.)
- **RTOS compatibility:** FreeRTOS, Zephyr, NuttX — if real-time is needed
- **Language support:** C/C++, MicroPython, Rust, CircuitPython
- **OTA update capability:** If the device needs field updates

Ask the user: **What's your firmware development preference?** (Arduino IDE,
PlatformIO, vendor SDK, or "whatever is easiest"). Factor this into MCU choice —
a technically superior MCU with a bad SDK is worse than a modest one with
great tooling.

For each proposed IC, explain **why** it was chosen:

```
=== Proposed IC Selection ===

MCU: ESP32-S3-WROOM-1 (N16R8)
  Why: WiFi + BLE built-in, sufficient GPIO for your sensor array,
       large community, good availability, JLCPCB basic part

Power: TPS563200 (5V→3.3V buck, 3A)
  Why: Wide input range covers USB 5V with margin, high efficiency
       at your current draw, SOT-23-6 keeps it small

Sensor: BME280
  Why: Temp + humidity + pressure in one package, I2C, matches your
       environmental monitoring requirement

[... etc for each block ...]
```

Ask: **Do these selections look reasonable? Want me to research alternatives
for any of them?**

### 2b. Parallel Research Jobs

For each confirmed IC, kick off research to gather:

1. **Datasheet review** — Verify pinout, electrical specs, recommended circuit
2. **Application circuit** — Extract reference design from datasheet
3. **Availability check** — Stock at DigiKey, Mouser, LCSC (use distributor skills)
4. **Alternative parts** — At least one pin-compatible or functional alternative
5. **Known issues** — Errata, common design pitfalls, community reports

Use the `digikey`, `mouser`, and `lcsc` skills to search for parts and check
stock. Use the `ee` skill for any calculations (power budgets, filter values,
thermal checks).

Present research results per IC:

```
=== Research: ESP32-S3-WROOM-1 ===

Sourcing:
  DigiKey:  In stock (2,400+ units), $3.15 @ qty 1
  Mouser:   In stock (800+ units), $3.22 @ qty 1
  LCSC:     In stock (JLCPCB basic), $2.85 @ qty 1

Reference circuit: See datasheet Section 4.2
  - Requires 3.3V rail, 500mA peak during TX
  - 10uF + 100nF decoupling on VDD
  - EN pin needs RC delay (10k + 1uF)
  - USB D+/D- need 22R series resistors

Alternatives:
  - ESP32-C3-MINI-1: Cheaper, single-core, fewer GPIO (if you can trim pins)
  - STM32WB55: ST ecosystem, better low-power, no WiFi (BLE only)

Known issues:
  - GPIO 12 must be LOW at boot (strapping pin)
  - ADC2 unavailable when WiFi active
```

### 2b-thermal. Thermal Checks on Power Components

For every power IC (buck, boost, LDO, charger, motor driver), run a thermal
sanity check using the `ee` skill formulas:

```
=== Thermal Check: TPS563200 (3.3V buck) ===

Input:  5V @ ~380mA (estimated input draw)
Output: 3.3V @ 350mA
Efficiency: ~88% (from datasheet at this load point)
Power dissipated: Pin - Pout = 1.90W - 1.16W = 0.74W

Package: SOT-23-6 (θJA ≈ 150°C/W without copper pour)
Junction temp: 25°C + 0.74W × 150°C/W = 136°C  ← OVER LIMIT (125°C max)

With recommended copper pour (θJA ≈ 55°C/W):
Junction temp: 25°C + 0.74W × 55°C/W = 65.7°C  ← OK (59°C margin)

→ Copper pour under this IC is MANDATORY. Flag for PCB layout.
```

**Flag thermal problems immediately:**
- Tj > 100°C → warn, suggest copper pour or heatsink requirement
- Tj > Tj_max → stop, suggest a different package or IC
- LDO with large Vin-Vout drop at high current → calculate Pdiss and flag
- Motor drivers → check Rds(on) × I² heat at operating current

For beginners: explain that "the chip has to get rid of the power it wastes as
heat, and small packages can only dump so much heat into the board."

### 2b-leadtime. Lead Time & Availability Reality Check

After gathering stock data for all ICs, present a consolidated availability
summary:

```
=== Supply Chain Summary ===

Part                    DigiKey    Mouser     LCSC       Lead Time
--------------------------------------------------------------------
ESP32-S3-WROOM-1        2,400+     800+      In stock    Immediate
TPS563200               5,200+     3,100+    In stock    Immediate
BME280                  150        0          In stock    ⚠ Low stock
DRV8833                 0          0          42 units    ⚠ 16 weeks

⚠ DRV8833: Out of stock at major distributors. Lead time 16+ weeks.
  Options:
  1. Use alternative: TB6612FNG (pin-compatible, in stock)
  2. Order from LCSC now (42 remaining)
  3. Wait for restock (check distributor ETAs)

⚠ BME280: Low stock at DigiKey (150 units). Order soon or identify
  alternative (BMP280 if humidity not needed, SHT40 for better accuracy).
```

**Flag any part with:**
- Zero stock at all checked distributors → blocker, must find alternative
- Stock < 2× order quantity → warn about ordering soon
- Lead time > 4 weeks → warn, ask if timeline allows it
- NRND (Not Recommended for New Design) or EOL status → blocker

Ask after presenting all research: **Any ICs you want to swap or dig deeper on?**

### 2c. Generation Coverage Check

Check which of the confirmed blocks map cleanly onto existing Circuit Weaver
data-driven topology/builder coverage and which will need explicit custom block
definition.

Common well-covered areas include:

- power conversion and regulation
- USB / common digital interfaces
- common mixed-signal support circuits
- standard driver, protection, sensing, and conditioning blocks

Report which blocks have direct coverage and which will need custom definition:

```
Direct coverage:
  [x] Power: buck regulator path
  [x] Power: ldo regulator path
  [x] Sensor conditioning path
  [x] USB interface path

Custom blocks needed:
  [ ] MCU: ESP32-S3 — will use component_db + manual pin mapping
  [ ] Custom analog frontend — explicit custom block needed
```

Important: **"no direct coverage" does not mean "unsupported."** It means
the design should continue using explicit custom blocks, manual interface
definitions, and targeted EE/simulation review instead of turnkey generation.

For specialized RF/microwave designs, continue with:

- RF chain blocks captured explicitly (LNA, mixer, LO, PA, filter, antenna path)
- Interface/net constraints recorded explicitly (impedance, diff-pair, isolation, shielding)
- Custom sourcing/research for the active RF parts and passives
- Manual layout and validation expectations stated up front

For professional users, present this as a capability boundary:
"supported with custom engineering flow," not "outside scope."

---

## Step 3 — BOM Assembly & Sourcing Preferences

**Goal:** Build the BOM with user input on manufacturing, vendors, and budget.

### 3a. Manufacturing Intent

Ask:
- **Where do you plan to manufacture?**
  - Hand-solder prototype at home
  - JLCPCB (economic or standard assembly)
  - PCBWay
  - Local PCB house (specify)
  - Other (specify)

- **Assembly level?**
  - Bare PCB only (you solder everything)
  - Partial assembly (fab places SMD, you do through-hole)
  - Full turnkey assembly

- **PCB specs?**
  - Layer count preference? (2-layer is cheapest, 4-layer for complex designs)
  - Any controlled impedance needs? (USB, Ethernet, RF)
  - Board thickness? (standard 1.6mm unless specified)
  - Surface finish? (HASL is cheapest, ENIG for fine-pitch)

### 3b. Vendor & Sourcing Preferences

Ask:
- **Preferred distributors?** (or "whatever is cheapest")
  - DigiKey (fast US shipping, best for prototypes)
  - Mouser (similar to DigiKey, good international)
  - LCSC (cheapest, best for JLCPCB assembly)
  - No preference / mix-and-match

- **Budget constraints?**
  - Target BOM cost per board?
  - Maximum spend for this prototype run?
  - Number of boards for first run?

- **Part preferences?**
  - Prefer JLCPCB basic parts where possible? (cheaper assembly fee)
  - Prefer automotive-grade parts?
  - Any approved vendor list (AVL) to follow?
  - Preferred passive sizes? (0402 for density, 0603 for hand-solder, 0805 for easy rework)

### 3c. Real Spec Assembly (Scaffold + Apply-Patch Workflow)

Build the YAML spec incrementally using the CLI commands:

**Step 1: Scaffold the first block**

Identify the primary power source and scaffold the first IC.

Note: the current CLI still uses the legacy flag name `--template` for topology
selection. Treat that as compatibility syntax, not the workflow model.

```bash
circuit-weaver scaffold --template buck --ref U1 --output design.yaml
```

This creates a minimal spec with the first power block. Present the output to the user and confirm it looks reasonable before proceeding.

**Step 2: Add blocks iteratively with apply-patch**

For each additional IC, create a patch JSON file and apply it.

Give every architectural block a stable unique `id` and a descriptive
functional `section`. Use separate sections for each major power stage,
processing, sensing/analog, communications, external I/O, UI, and debug/test.
Generation preserves those explicit sections as professional sub-sheets even
for small designs and keeps support passives with their owning block.

Note: the patch schema still uses legacy internal field names like
`kind: "template"` and `template_type`. In workflow terms, think of these as
"generated block" and "topology name".

```bash
# patch_u2_ldo.json
{
  "upsert_blocks": [
    {
      "id": "U2_ldo",
      "section": "power",
      "kind": "template",
      "template_type": "ldo",
      "ref": "U2",
      "params": {
        "vin": 3.3,
        "vout": 1.8,
        "vin_net": "VDD_3P3",
        "rail_name": "VDD_1P8"
      }
    }
  ]
}

# Apply the patch
circuit-weaver apply-patch design.yaml patch_u2_ldo.json --output design.yaml
```

For each new block, present ONLY the newly-added components (from `diff.added_blocks`). Do NOT show the full updated spec — it's too verbose.

```
=== Block Added: U2 LDO ===

U2  TLV75533  3.3V → 1.8V LDO, 500mA
  Bypass caps: C2, C3 (100nF on input/output)
  Enable pulled to VDD_3P3

Ready for next block? (or 'done' to move to validation)
```

Repeat this for each IC until all blocks are added.

**Step 3: Cost Estimation (Optional)**

Once all blocks are added, generate an estimated BOM cost at target quantities:

```bash
circuit-weaver cost-bom design.yaml --qty 1,10,100
```

Present the pricing summary (prices by quantity break), noting any parts without LCSC codes that need manual sourcing.

---

## Step 4 — Schematic Generation

**Goal:** Generate the KiCad schematic and set expectations for what comes out.

### 4a. Pre-Generation Review

Before generating, confirm the YAML spec with the user:

```
I'm about to generate the KiCad schematic from this spec:

  Project:    [name]
  Sheets:     [estimated count] (1 top-level + N sub-sheets)
  Components: [count] active + [count] passive
  Power tree: [description]
  Buses:      [I2C, SPI, UART, etc.]

Output directory: [path]
```

Ask: **Ready to generate? Any last changes?**

### 4b. Generate & Validate

Run the generation pipeline:

```bash
# Generate artifacts
circuit-weaver generate "${SPEC_PATH}" --output "${OUTPUT_DIR}"

# Validate the generated output
circuit-weaver validate "${SPEC_PATH}"

# Before a release-quality/handoff claim, require real KiCad on the final hierarchy
circuit-weaver generate "${SPEC_PATH}" --output "${OUTPUT_DIR}" --require-kicad
```

Report results:

```
=== Generation Results ===

Files created:
  [x] project.kicad_sch          (top-level schematic)
  [x] power.kicad_sch            (power sheet)
  [x] digital.kicad_sch          (MCU + peripherals)
  [x] project_report.md          (design report)
  [x] assembly_manifest.json     (physical-part inventory)
  [x] placement_result.json      (review status and blockers)
  [x] placement_review_context.json (official/reference context)
  [x] placement.svg + placement_editor.html (review-only proposal)

Validation:
  Structural:      PASS (12/12 checks)
  Electrical:      PASS (8/8 checks)
  Implementation:  PASS (6/6 checks) [or WARN with details]
  Presentation:    PASS (4/4 checks)
```

Internal validation is not real KiCad verification. Read
`artifact_manifest.json` and claim release-quality generation only when it says
`kicad_verified: true` and `verification_status: verified`.

### 4c. What's Automated vs. Manual

**Be explicit about what the user still needs to do.** This is critical for
setting expectations:

```
=== What's Done vs. What You'll Finish ===

DONE (generated automatically):
  - Symbol placement and declared pin mappings for review
  - Power connections and decoupling capacitors
  - Net labels for all buses (I2C, SPI, UART, etc.)
  - Hierarchical sheet structure
  - Support passives (pull-ups, bypass caps, RC filters)

YOU'LL FINISH IN KICAD (~10-20% of the schematic work):
  - Review net label names for clarity
  - Adjust component positions for readability
  - Add any project-specific notes or annotations
  - Verify pin assignments match your physical layout intent
  - Run ERC and resolve any remaining warnings
  - Fine-tune sheet aesthetics (alignment, spacing)

Open the schematic:
  Open `artifact_manifest.json`, then open its `root_schematic` path in KiCad.
```

---

## Step 5 — Design Review Checkpoint

**Goal:** Catch expensive mistakes before committing to PCB layout. This is
the cheapest place to find problems — a schematic fix costs minutes, a PCB
respin costs weeks and dollars.

### 5a. Automated Review

Run the generated schematic through the validation tools:

```bash
# Full schematic validation
circuit-weaver validate "${SPEC_PATH}" --enhanced --verbose

# Full confidence report (validation + simulation + thermal + DFM + cross-reference)
circuit-weaver confidence "${SPEC_PATH}" --run-sims -o "${OUTPUT_DIR}/confidence_report.html"
```

**Confidence Report:** The `confidence` command aggregates all available checks
into a single 0-100 score with a readiness classification (ready_for_fab /
needs_review / not_ready). Present the terminal output to the user. If an HTML
output path was given, mention the HTML file for detailed review in a browser.

Parse the validation report (categories level=error or level=warning) and present a structured review summary:

```
=== Design Review ===

Power Tree:
  [x] All rails have valid sources
  [x] Decoupling caps on every IC power pin
  [x] Enable pins properly connected
  [ ] ⚠ No bulk capacitor on 3.3V rail — recommend 10-47µF near source

Connectivity:
  [x] All MCU peripheral pins connected to intended targets
  [x] I2C bus has pull-ups (4.7kΩ to 3.3V)
  [x] SPI bus CS lines properly assigned
  [ ] ⚠ UART TX/RX — verify TX→RX crossover (pin names can be ambiguous)

Protection:
  [x] USB VBUS has overcurrent protection
  [ ] ⚠ No ESD protection on USB data lines — recommend TVS diode
  [ ] ⚠ No reverse polarity protection on power input

Debug & Test:
  [x] SWD header connected
  [x] UART debug header connected
  [x] Power LEDs on all rails
```

### 5b. Human Review Guidance

Tell the user what to check manually in KiCad:

```
=== Manual Review Checklist ===

Open the schematic in KiCad and verify:

  [ ] Pin assignments match your physical layout intent
      (e.g., I2C on pins closest to the sensor)
  [ ] Net names are meaningful (not just NET_001)
  [ ] No unintended connections (zoom in on dense areas)
  [ ] All ICs have correct pin-1 orientation markers
  [ ] Power flags on all power nets (avoids ERC warnings)
  [ ] Footprint assignments are correct for your sourced parts
      (SOT-23-3 vs SOT-23-5 is a common mistake)

If this is for a real product, consider having another engineer
review the schematic before proceeding to layout.
```

### 5c. Review Gate

Ask: **Have you reviewed the schematic and are you satisfied with it?**

- If issues found → help fix them, re-generate if needed, re-validate
- If satisfied → proceed to confidence check (Step 6)
- If they want peer review → suggest they share the .kicad_sch and come back

Do not let beginners skip this step. For advanced users, a quick "looks good,
moving on" is fine.

---

## Step 6 — Confidence & Simulation Check

**Goal:** Run all available checks in one pass to produce a single readiness
score before the user invests time in PCB layout.

### 6a. Run Confidence Dashboard

**This step runs automatically after the review gate passes. Do not skip it.**

```bash
# Run confidence report with simulations
circuit-weaver confidence "${SPEC_PATH}" --run-sims -o "${OUTPUT_DIR}/confidence_report.html"

# For enhanced validation (includes cross-reference audit)
circuit-weaver validate "${SPEC_PATH}" --enhanced --verbose
```

The confidence dashboard aggregates:
1. **Electrical validation** — all 14 checks (decoupling, enable pins, power budget, thermal, SI)
2. **Circuit simulation** — power supply transient/AC analysis via ngspice
3. **Thermal analysis** — junction temperature vs Tj_max for all power ICs
4. **Cross-reference audit** — spec vs schematic, BOM completeness, duplicate refs
5. **ERC** — KiCad electrical rule check (if KiCad CLI installed)

### 6b. Present Results

Show the confidence score and readiness to the user:

```
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
```

**Interpretation guidance:**
- **ready_for_fab** (80+, no blockers): "Your design looks solid. Proceed to PCB layout."
- **needs_review** (60-80): "Design works but has issues worth addressing first."
- **not_ready** (<60 or blockers): "Fix the blockers before investing in PCB layout."

For beginners: explain what each section means and why the score matters.
"This score tells you how confident we are that the circuit will work correctly
when you build it. A higher score means fewer surprises when the board arrives."

### 6c. Address Action Items

If blockers or high-priority action items exist, walk through each one:

1. Present the action item with context
2. Offer to fix it (loop back to the relevant step: Step 2 for IC changes, Step 3 for passives)
3. Re-run confidence after fixes to verify improvement

Ask: **Want to address any of these items before moving to PCB layout?**

If the user wants to proceed despite issues, note the decision in the log.

**Log:** `[Step 6] Confidence: {score}/100 ({grade}), readiness: {readiness}`

---

## Step 7 — PCB Layout Guidance & Next Steps

**Goal:** Guide the user into the PCB phase with clear expectations about
what can be scripted vs. what requires manual KiCad work.

### 7a. PCB Kickoff — Automated Layout Assistance

`generate` emits an exhaustive review-only bundle:
`placement_result.json`, `placement_review_context.json`, `placement.svg`, and
`placement_editor.html`. It does not emit an electrical PCB. Read the result
and context first, surface every blocker, and use linked manufacturer
datasheets/reference layouts as the placement authority.

```bash
# 1. Optimize placement (simulated annealing for thermal/SI/DFM)
circuit-weaver optimize-placement "${SPEC_PATH}" -o "${OUTPUT_DIR}/placement.json"

# 2. Optionally generate an alternate standalone viewer
circuit-weaver placement-viewer "${SPEC_PATH}" -o "${OUTPUT_DIR}/placement.html"
```

Present results:

```
=== PCB Layout Preparation ===

Automated steps completed:
  [x] Placement optimized (thermal + SI + DFM scoring)
  [x] Review status: output/placement_result.json
  [x] Official/reference context: output/placement_review_context.json
  [x] Interactive editor: output/placement_editor.html
  [x] Editable vector: output/placement.svg

Open placement_editor.html to review or drag the exact assembly inventory.
The placement files always remain `fabrication_ready: false`.

WHAT YOU'LL DO IN KICAD:
  - Board outline and edge cuts
  - Fine-tune placement for routing convenience
  - Route traces (power first, then signals)
  - Ground/power planes and zone fills
  - Silkscreen labels and polarity markers

WHAT WE CAN HELP WITH:
  - Trace width suggestions (use `$ee`)
  - Layer stackup recommendation (2-layer vs 4-layer)
  - Autorouting of non-critical nets after an electrical PCB exists (see 7c)
```

If mechanical constraints were captured in Step 1a-mech, incorporate them now:
- Board outline matches enclosure dimensions
- Mounting holes at specified positions
- Connectors placed on specified edges
- Height-restricted zones marked as keep-outs

### 7b. Create the Electrical PCB

Open the manifest returned by `generate` and resolve its `root_schematic`.
For automated prototypes, run `build-pcb "${ROOT_SCHEMATIC}" -o "${ELECTRICAL_PCB}"`
then `place-pcb "${ELECTRICAL_PCB}" -o "${PLACED_PCB}"`. Exact pin/pad mapping,
real library footprints, and physical DRC are required. Review explicit prototype
clearance assumptions; this path does not grant identity or manufacturing approval.
Alternatively, assign footprints and run **Update PCB from Schematic** in KiCad.

Dry-run the edited SVG against the real board before applying it. Exact
SVG↔PCB reference matching is the default; use `--allow-partial` only for an
explicit subset update.

```bash
circuit-weaver import-placement "${OUTPUT_DIR}/placement.svg" \
  "${ELECTRICAL_PCB}" -o "${OUTPUT_DIR}/placed.kicad_pcb" --dry-run
circuit-weaver import-placement "${OUTPUT_DIR}/placement.svg" \
  "${ELECTRICAL_PCB}" -o "${OUTPUT_DIR}/placed.kicad_pcb"
```

### 7c. Routing Guidance

Based on the design:

- Identify critical nets that need manual routing (power, USB, high-speed)
- Suggest trace widths for power vs signal nets (use `ee` skill calculations)
- Recommend layer stackup if 4-layer
- Identify candidates for autorouting (non-critical signal nets)

**Autorouting with Freerouting (Optional)**

If the user has Freerouting installed, offer to auto-route only a real
pad-bearing named-net PCB or a user-exported Specctra DSN. Output is a validated
Specctra session by default. For a real PCB, request automatic SES import and
native DRC before publishing a separate routed board:

```bash
circuit-weaver autoroute "${PLACED_PCB}" --routed-board "${ROUTED_PCB}" --attempts 3
```

The native KiCad Python bridge can export DSN when the CLI cannot. Without
`--routed-board`, import the SES and run DRC separately. Treat exit code 2 /
`status: partial` as incomplete. Verify final-board DRC evidence, and review
power, switching loops, differential/high-speed pairs, RF, clocks, and crystals
against engineering constraints. Routing success does not grant fabrication approval.

### 7d. DFM Check (Recommended Before Ordering)

Run DFM checks against the target manufacturer:

```bash
circuit-weaver check-dfm "${ELECTRICAL_PCB}"
```

If DFM issues are found, present them and offer to fix (adjust trace widths,
via sizes, clearances).

#### Optional Repository Templates

A source checkout can contain optional `project-skills/` templates for advanced
placement, autorouting, pin-map, hierarchy, and custom-generation work. These
templates are not installed from PyPI. Read a template only after verifying its
`SKILL.md` exists in the current repository; otherwise continue with the built-in
CLI and KiCad steps in this wizard.

### 7e. Manufacturing Checklist

Based on Step 3 manufacturer choice, present the final checklist:

```
=== Pre-Order Checklist ===

  [ ] Confidence score >= 80 (circuit-weaver confidence design.yaml --run-sims)
  [ ] DFM check clean (circuit-weaver check-dfm board.kicad_pcb)
  [ ] ERC clean in KiCad
  [ ] DRC clean in KiCad
  [ ] BOM exported (use /bom skill)
  [ ] Gerbers exported and visually inspected
  [ ] CPL (component placement list) generated for assembly
  [ ] Design rules match manufacturer minimums
  [ ] Silkscreen readable, polarity marked
  [ ] Test points accessible
  [ ] Board dimensions verified against enclosure
```

Reference the `jlcpcb` or `pcbway` skill for manufacturer-specific DFM rules.

### 7f. Revision Planning & Future-Proofing

Before the user commits to fabrication, help them think about the next revision.
This is especially valuable for beginners who don't realize rev1 is almost never
the final board.

```
=== Revision Planning ===

This is Rev 1 of your design. Here's how to set yourself up for Rev 2:

Version tracking:
  - Add "Rev 1.0" and the date to the silkscreen
  - Tag this point in git: git tag -a v1.0 -m "Rev 1.0 sent to fab"
  - Keep your YAML spec, generated files, and BOM CSV committed

Built-in flexibility (already in your design):
  [x] Spare GPIO broken out to header (Step 1 test strategy)
  [x] Debug UART accessible
  [x] SWD header for firmware development

For Rev 2, common changes to plan for:
  - Component swaps based on testing results
  - Layout tweaks for signal integrity or thermal issues
  - Additional features from your "nice to have" list
  - Cost optimization (swap extended JLCPCB parts for basic ones)

For future revisions, keep your YAML specs in git so you can diff changes.
```

Ask: **Ready to order, or do you want to add any future-proofing features
before we finalize?**

---

## Conversation Style Guide

Throughout the wizard, follow these principles:

1. **One topic at a time.** Don't ask 10 questions in one message. Group 2-3
   related questions, wait for answers, acknowledge, then move on.

2. **Suggest sensible defaults.** "For a prototype, I'd suggest 0603 passives
   and HASL finish — does that work?" is better than "what passive size and
   surface finish do you want?"

3. **Show your reasoning.** When recommending an IC or topology, briefly
   explain why. Users learn from this and catch bad assumptions early.

4. **Summarize before proceeding.** At each step boundary, show what you
   captured and ask for confirmation before moving to the next step.

5. **Save progress.** After Steps 1 and 2, offer to save a draft YAML spec
   so the user doesn't lose work if the session ends. After Step 3, the
   spec should be saved to disk.

6. **Be honest about limitations, but distinguish automation limits from scope.**
   If the engine lacks pre-modeled builder coverage for something unusual, do **not**
   reject the whole design space. Instead say what Circuit Weaver can still do
   (requirements capture, architecture, custom block scaffolding, sourcing,
   validation planning, simulation setup, review) and what still needs manual
   engineering closure.

---

## Related Skills

| Skill | Used in step | Purpose |
|-------|-------------|---------|
| `ee` | 1, 2, 7 | Calculations: power budget, filter values, thermal, trace widths |
| `digikey` | 2, 3 | Part search, stock check, datasheets |
| `mouser` | 2, 3 | Alternative sourcing |
| `lcsc` | 2, 3 | Production sourcing, JLCPCB parts |
| `bom` | 3 | BOM export and order file generation |
| `kicad` | 4, 5 | Schematic analysis, validation, and design review |
| `jlcpcb` | 3, 7 | DFM rules, assembly ordering |
| `pcbway` | 3, 7 | Alternative fab DFM rules |

---

## Resuming a Wizard Session

If the user returns and says "continue my design" or "where were we":

1. Reconcile durable state before inferring progress from filenames:

   ```bash
   circuit-weaver status "${PROJECT_DIR}"
   circuit-weaver resume "${PROJECT_DIR}"
   ```

2. Treat `resume` as a plan only; it never executes the next action.
3. Use its `current_phase`, reconciliation findings, and `next_actions`. If no
   manifest exists, fall back to saved YAML completeness:
   - Has requirements but no ICs → resume at Step 2
   - Has ICs but no sourcing data → resume at Step 3
   - Has full spec but no generated files → resume at Step 4
   - Has generated schematics but no review → resume at Step 5
   - Has reviewed schematics → resume at Step 6 (confidence check)
   - Has confidence report → resume at Step 7 (PCB layout)
4. Summarize the current state and confirm with the user before proceeding.
