# Schematic Layout Concept: Readable by Construction

Status: proposal. Owner: core. Scope: the `.kicad_sch` presentation layer only. Netlists, validation and the PCB flow are unchanged.

## Problem

Generated schematics are electrically correct but hard to read. Long wires cross the sheet, labels sit on symbols, support parts are scattered, and many sheets hold only one or two ICs. An engineer reviewing the output has to reconstruct the circuit in their head. A schematic is the review artifact, so if it can't be read at a glance it can't be trusted, however good the netlist is.

The goal is output that looks like a datasheet's "typical application" figure, one functional block at a time, and that a reviewer can check in minutes.

## Why today's output is messy

The current pipeline (`allocator.allocate_sheets` → `placer.layout_sheet` → `generator._render_sheet`) has five structural causes. File references are to the 0.37.0 code.

1. **Placement ignores connectivity.** ICs are packed in rows by category bucket (connectors, regulators, other ICs by pin count) and gaps are stretched up to 3× (`placer.py` `_layout_components_rowwise`, `_density_scaled_gaps`). Nothing places a part near the parts it talks to, so connections become sheet-spanning global labels or long wires.
2. **Placement and rendering don't share one geometric model.** Space is reserved for a grid of passives, then motifs move those passives up to about 47 mm along the pin axis. Label and stub geometry is only known at render time. Collisions get patched afterwards by `_detour_wires_around_bodies` and `_resolve_label_collisions` instead of being prevented.
3. **The local motifs have geometry bugs.** Decoupling-bank pitch is 7.62 mm but each passive is 10.16 mm pin to pin, so a three-capacitor bank renders end to end and reads as capacitors in series. Ground symbols rotate to follow the pin instead of pointing down. R, C and L all render as the same box.
4. **Routing is greedy.** Each connection takes the first candidate that fits, falls back through a chain of relaxations to a BFS route, and finally to a straight line. Nothing minimizes crossings, bends or length. The nearest-anchor lookup isn't scoped to the owning IC, so a capacitor's ground can wire to another IC's ground anchor.
5. **The quality gate can't see most problems.** `layout_quality` checks body overlaps and wire-through-body only. It can't see labels, text, power symbols or pins, it reads only the first sheet, and it covers three samples. Crossings, label overlaps, long wires and bends aren't measured anywhere.

Small fixes inside this structure have already been tried (density scaling, label shifting, detour passes, a crossing penalty). They help locally, but they can't make the result readable, because readability isn't something the current model represents. This proposal makes it a first-class goal.

## Principles

These are the conventions experienced engineers follow. Each one becomes an invariant the engine enforces and the lint measures.

1. **Signal flows left to right.** Inputs and connectors start on the left, processing sits in the middle, outputs and loads go on the right.
2. **Power flows top to bottom.** Higher voltages sit higher on the page. Supply rails enter from the top and ground leaves from the bottom.
3. **A cell is an IC plus everything that serves it.** Decoupling, pull-ups, feedback dividers, crystals and bootstrap capacitors sit in fixed slots beside the pins they serve, as in the datasheet figure.
4. **Wires are short. Long distances use names.** A wire stays inside its cell. Connections between cells use a net label, connections between sheets use a hierarchical label, and rails use power symbols. No wire crosses the sheet.
5. **Every capacitor gets its own ground symbol.** No ground bus is drawn.
6. **Power symbols are upright.** Rails point up and ground points down, whatever the pin's orientation.
7. **Nothing overlaps.** Text, labels, symbols and wires keep clearance, measured from real text extents.
8. **Everything is on a 1.27 mm (50 mil) grid, and every T-junction gets a junction dot.**
9. **Each sheet tells one story.** A sheet is a functional block sized to its content, plus a top-level sheet showing the power tree and block interfaces. One-IC sheets are merged and overfull sheets are split.
10. **Regeneration is stable.** Running again after a small spec change moves only what changed, and parts a human has placed stay where they are.

## Architecture

The layout engine becomes a pipeline of four data-driven stages with one shared geometry model. Every stage reads normalized metadata (pin `side`, electrical type, pin roles, block ownership, interface direction, power domains). None of them reads part numbers, as `CLAUDE.md` requires.

```
DesignIR + resolved components
        │
        ▼
1. Presentation graph     cells, rails, interfaces, signal-flow edges
        │
        ▼
2. Sheet planning         functional sheets sized to content, plus a root power-tree sheet
        │
        ▼
3. Macro placement        cells ranked by signal flow (columns) and power level (rows)
        │
        ▼
4. Cell layout + routing  motifs place support parts in pin-side slots;
                          a cost-based orthogonal router wires inside each cell
        │
        ▼
Shared geometry model  →  KiCad emitter (existing sexpr primitives)
        │
        ▼
Schematic lint (gating)   crossings, overlaps, long wires, bends, orientation, fill
```

### 1. Presentation graph

Built once from the IR, before any coordinates exist:

- **Cell:** a primary component (IC, module or connector) plus the support parts it owns. Ownership already exists as `block_id`, the passives' `parent_ref`/`owner_pin`, and `role`.
- **Rail:** a power net with its source (regulator output, connector) and loads, taken from `power_domains` and the power-tree inference in `report.py`.
- **Signal edge:** from a driving cell to a receiving cell, oriented by `DesignInterface.direction` and pin electrical types (output → input). Bidirectional buses go from the controller (MCU) to the peripheral.
- **Group:** `functional_section` / `presentation_group`, used to plan sheets.

### 2. Sheet planning

- Start from functional sections, as today.
- Merge a section into its most-connected neighbour when it would hold fewer than about three cells. Split a section that exceeds a cell or area budget along its weakest connections.
- Always emit a root sheet with the power tree (sources → regulators → rails, with voltage and current on each edge) and the block interfaces as sheet pins. This is the page a reviewer reads first.

### 3. Macro placement

Use layered graph drawing (Sugiyama style), which handles left-to-right flow well:

1. **Columns:** rank cells along signal edges. Input connectors and power inputs take the leftmost column, and loads or output connectors the rightmost.
2. **Rows:** order cells within a column by power level (rail voltage, from highest to lowest), then reduce edge crossings with the barycenter heuristic.
3. **Spacing:** derive it from each cell's real bounding box, including its slots and labels, never from a fixed gap. Keep a uniform routing channel between cells.
4. **Pinning:** connectors with a declared board edge keep that side. Human-locked cells keep their position.

### 4. Cell layout: motifs as data

A motif describes how support parts sit around a pin, by **role and topology**, never by part number. Motifs live in a data file (for example `presentation/motifs.yaml`), so a new topology needs data, not Python branches:

```yaml
decoupling:
  applies_to: {role: decoupling}
  anchor: owner_pin          # the supply pin this capacitor serves
  slot: beside_pin_outward   # placed off the pin's side, away from the body
  orientation: vertical      # rail at top, ground at bottom
  stack: horizontal          # several caps side by side, 2.54 mm clear between bodies
  ground: local_symbol       # own GND symbol, pointing down
feedback_divider:
  applies_to: {role: [feedback_top, feedback_bottom]}
  anchor: {pin_role: fb}
  slot: beside_pin_outward
  orientation: vertical      # top resistor to VOUT, bottom to a local GND
pullup:
  applies_to: {role: pullup}
  anchor: owner_pin
  slot: above_net_label      # resistor rises from the signal toward a rail symbol
crystal_pi:
  applies_to: {role: [crystal, crystal_cap]}
  anchor: {pin_role: [xin, xout]}
  slot: beside_pin_pair      # crystal across XIN/XOUT, one cap down to GND from each side
```

The slot geometry comes from the pin's `side` (L/R/T/B) and the symbol's real pitch. That fixes today's bank-pitch bug structurally, because a slot's size is the part's real size.

A rendered LDO cell then reads like the datasheet. Rails enter from the top, every capacitor has its own ground, EN is tied to IN locally with a junction dot, and no wire leaves the cell:

```
           VBUS_5V                              VDD_3P3
              ▲                                    ▲
              │       ┌───────────────┐            │
              ●───────┤ 1 IN    OUT 5 ├────────────●
              │   ┌───┤ 3 EN          │            │
              ●───┘   │     GND 2     │            │
              │       └──────┬────────┘            │
            ─────            │                   ─────
            ─────  C1 1µF    │                   ─────  C2 1µF
              │              │                     │
              ▼              ▼                     ▼
             GND            GND                   GND
```

### Symbol shaping

When part data lacks a good `side` assignment, derive one: power inputs on top, ground at the bottom, inputs on the left, outputs on the right, bidirectional pins by interface direction. Keep a bus's pins adjacent and in order (SDA next to SCL, D0…D7). Fix `generator._get_pin_side`, which treats a 90° pin as "top" while the placer treats it as "bottom".

### Routing and the connection strategy

One rule table decides how each connection is drawn:

| Connection | Drawn as |
|-|-|
| Support part to its owner pin, inside a cell | Wire |
| Between cells on one sheet, short, adds no crossing | Wire |
| Between cells on one sheet, otherwise | Net label at both ends |
| Between sheets | Hierarchical label and sheet pin |
| Supply rail | Power symbol, upright |
| Ground | GND symbol, pointing down, at each part |

Wires are routed by a deterministic A* search on the 1.27 mm grid. The cost includes length, bends, crossings and proximity to text. Hard obstacles are symbol bodies, pins of other nets, and label and text extents measured with the real font metrics. Every T-junction is emitted as a `junction` (`sexpr_junction` exists but is unused today).

### Shared geometry model

Every placed object (body, pin, wire, label, power symbol, field text) registers its exact box in one spatial index before the emitter runs. Placement, routing and the lint all query the same index. That replaces today's "place, render, then detour" loop with "only place what fits."

## Schematic lint: measure first, then gate

Readability becomes a measured, gated quantity, the same way the electrical benchmark gates precision and recall. These metrics run on **every sheet of every sample**:

| Metric | Target |
|-|-|
| Wire–wire crossings (not at a junction) | 0 inside a cell; at most 2 per sheet |
| Wire through a symbol body, label or text | 0 |
| Label, field or text overlap | 0 |
| Wires longer than 25.4 mm | 0 inside cells; reported elsewhere |
| Bends per wire | at most 2 |
| Power symbols not upright (GND not pointing down) | 0 |
| Off-grid pins, wires or labels | 0 |
| Missing junction dots at T-junctions | 0 |
| Cells per sheet | 3–12, except the root sheet |
| Page fill ratio | 25–70 % |

The checked-in scorecard, like `benchmarks/scorecard.md`, records each sample's metrics. CI fails if any metric regresses. A visual regression job renders each sheet to SVG with `kicad-cli` and compares it with a reference image, so a reviewer sees layout changes in the pull request.

## Rollout

Each milestone ships on its own and is measured by the lint from M0.

| Milestone | Change | Visible effect | Acceptance |
|-|-|-|-|
| **M0: Measure** | Extend `layout_quality` to every sheet and to labels, text, power symbols and pins. Add crossings, long wires, bends and orientation. Publish a baseline scorecard. | None. The numbers become visible. | Baseline for all 13 samples is checked in, and CI fails on regression. |
| **M1: Connection strategy** | Use the rule table above: a local GND symbol per capacitor, upright power symbols, local labels instead of sheet-spanning wires, and junction dots. | Largest single improvement; the long ground combs disappear. | Long wires and non-upright symbols reach 0. |
| **M2: Motifs as data** | Move the motif library to data. Size slots from real symbol geometry, and scope anchor lookup to the owning cell. | Support parts look like the datasheet figure. | Overlaps reach 0 and in-cell crossings reach 0. |
| **M3: Signal-flow placement** | Layered macro placement, sheet merging and splitting, and the root power-tree sheet. | Pages read left to right and top to bottom. | Cells per sheet and fill ratio are within target for every sample. |
| **M4: Cost-based router** | A* routing with crossing and bend costs in the shared geometry index. Retire the detour and label-shift passes. | Clean, consistent wiring. | All lint metrics are within target, and the post-hoc repair passes are deleted. |
| **M5: Stability** | Stable placement keys. Import human moves from an edited `.kicad_sch` and keep them on regeneration. Add the visual regression job. | Engineers can edit and regenerate without losing work. | Round-trip test: a moved part stays where it was after regeneration. |

M0 and M1 are small and deliver most of the visible improvement. M3 and M4 are the larger investments.

## Non-goals

- PCB placement and routing, which are covered by the existing native KiCad flow.
- Matching any one engineer's personal style. The target is the shared conventions above.
- Per-part layout exceptions. If a part needs special treatment, it needs a role or topology in the data.

## Open questions

1. Should the root sheet be generated even for one-sheet designs? (Proposed: yes, once a design has two or more rails.)
2. Where should human placement locks live: in the design spec, or read back from the `.kicad_sch`? (Proposed: both. The spec wins for intent, and the schematic wins for coordinates.)
3. Should labels use KiCad global labels or local labels within a sheet? (Proposed: local labels within a sheet and hierarchical labels between sheets. Global labels only for explicitly global nets.)
