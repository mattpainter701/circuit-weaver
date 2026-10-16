---
name: autoroute
description: Route non-critical nets on a real KiCad PCB or Specctra DSN through Circuit Weaver's fail-closed Freerouting integration. Use after placement and net-class setup when the user requests autorouting, a validated SES session, routing diagnostics, or post-route cleanup. Keep critical power, RF, clock, crystal, switching, and differential nets manual.
---

# PCB Autorouting

Produce a validated Specctra `.ses` session, or use `--routed-board` with a real
PCB to import the session and publish a separate `.kicad_pcb` only after native
KiCad DRC passes. Verify final-board hashes and surface all warnings. Successful
autorouting does not grant fabrication approval.

## 1. Establish the routing source

Accept either:

- a real, pad-bearing `.kicad_pcb` updated from the schematic in KiCad; or
- a Specctra `.dsn` exported by the user from KiCad PCB Editor.

Reject placement review JSON/SVG/HTML and any padless preview/template. Before
routing, verify:

- footprints have unique references, physical pads, and named nets;
- the board outline and stackup are final enough for routing;
- footprint positions and orientations have been reviewed;
- net classes, clearances, track widths, and via rules match the selected fab;
- zones are unfilled and obsolete teardrops are removed; and
- critical nets listed below are already routed or deliberately excluded.

## 2. Run the supported CLI

Use a finite timeout and write an SES:

```bash
circuit-weaver autoroute board.kicad_pcb \
  --output board.ses \
  --effort medium \
  --timeout 300
```

To publish a real PCB after native DRC, add `--routed-board final.kicad_pcb`.
The native KiCad Python bridge exports DSN when the CLI cannot. If neither
native Python nor CLI export is available, export DSN in KiCad PCB Editor:

```bash
circuit-weaver autoroute board.dsn \
  --output board.ses \
  --effort high \
  --timeout 600
```

Circuit Weaver probes the installed router before using optional controls. Use
only flags advertised by `circuit-weaver autoroute --help`:

- `--effort fast|medium|high` selects 100/500/1000 passes.
- `--max-passes N` overrides the preset; 0 means unlimited.
- `--headless` is enabled by default; `--no-headless` opens the GUI.
- `--optimization-threads N` sets optimizer threads; 0 disables optimization.
- `--optimizer-strategy greedy|global|hybrid` chooses board-update behavior.
- `--optimizer-hybrid-ratio m:n` is required only with `hybrid`.
- `--optimizer-item-selection sequential|random|prioritized` chooses item order.
- `--optimizer-improvement-threshold PERCENT` controls optimizer stopping.
- `--seed N` is accepted only when the installed build advertises
  `-random_seed`.
- `--freerouting-path PATH` selects a launcher/JAR; the environment alternative
  is `CIRCUIT_WEAVER_FREEROUTING`.
- `--kicad-cli-path PATH` selects the capability-probed KiCad CLI.
- `--overwrite` atomically replaces existing DSN/SES output. Without it,
  existing output blocks the run.

Do not bypass the wrapper with guessed raw Java flags. Freerouting option names
and availability vary by build; the wrapper probes and validates them.

## 3. Interpret the result truthfully

Treat CLI status and exit code as authoritative:

| Status | Exit | Meaning |
|-|-:|-|
| `ok` | 0 | A structurally valid, source-correlated SES was published with zero reported incomplete connections and zero clearance violations. |
| `partial` | 2 | The SES was published, but the router reported incomplete connections. Manual completion is required. |
| `error` | 1 | Preflight, capability, timeout, router, statistics, clearance, DSN/SES validation, or publication failed. |

Treat `status: partial` as unfinished routing even though a diagnostic SES was
published; never promote it to success because the file exists.

The wrapper stages output, validates DSN and SES structure, rejects SES nets
absent from the source, rejects missing source nets, requires known completion
and clearance statistics, and preserves prior output on failure. Do not infer
success from a router process exit code or from the mere presence of a file.

## 4. Import and verify in KiCad

1. Open the same electrical PCB in KiCad PCB Editor.
2. Choose **File → Import → Specctra Session** and select the published `.ses`.
3. Confirm the reported net count and any incomplete connections.
4. Refill zones and re-add teardrops only after inspecting the imported routes.
5. Run full KiCad DRC and resolve every violation.
6. Inspect neck-downs, via transitions, acute angles, stubs, return paths,
   silkscreen, courtyard clearance, and copper-to-edge clearance.
7. Save the verified board as a `.kicad_pcb`; the SES itself is not the final
   manufacturing source.

## Nets to route manually

Route these before bulk autorouting and review them against the authoritative
datasheet/reference layout:

| Net class | Reason |
|-|-|
| Input/output power, planes, and high-current paths | Width, via count, current density, and return path are design constraints. |
| Buck/boost switch nodes and hot loops | Loop area and parasitics dominate EMI and stability. |
| Decoupling and bootstrap loops | The shortest physical current path matters more than generic completion. |
| Differential pairs | Impedance, spacing, skew, and reference-plane continuity require controlled routing. |
| RF/antenna/matching networks | Vendor geometry, 50-ohm structure, keepouts, and tuning are authoritative. |
| Crystals and oscillators | Symmetry, parasitics, isolation, and guard strategy are placement-specific. |
| Clocks, DDR, memory buses, and other timing-critical nets | Topology and length constraints exceed generic autorouting guarantees. |
| Sensitive analog and feedback nodes | Noise coupling, Kelvin connections, and grounding require engineering judgment. |

## Completion checklist

- [ ] Routing input is a real electrical PCB or valid user-exported DSN.
- [ ] Critical nets were routed manually or explicitly excluded.
- [ ] Circuit Weaver returned `ok`, or every `partial` connection was completed.
- [ ] The SES was imported into the matching board in KiCad.
- [ ] KiCad DRC is clean after zones are refilled.
- [ ] Trace widths, vias, return paths, diff-pair rules, and copper-to-edge rules
      match the fabrication stackup.
- [ ] Final manufacturing exports come from the reviewed `.kicad_pcb`, not the
      DSN, SES, or placement review artifacts.
