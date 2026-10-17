# PCB placement and routing search

Use a real KiCad board with footprints, pads, nets, and a rectangular Edge.Cuts
outline. Lock parts whose physical position must stay fixed, such as connectors.

```sh
circuit-weaver place-pcb board.kicad_pcb -o placed.kicad_pcb --seed 7
circuit-weaver autoroute placed.kicad_pcb --attempts 3 --routed-board routed.kicad_pcb
```

The first command places real footprints and checks physical DRC, allowing only
the expected unrouted connections. The second exports DSN, routes, imports SES,
and publishes the finished PCB only after KiCad DRC passes. Original files remain
unchanged. Project settings and custom rules travel with the output, and changing
the source or settings during routing prevents board publication. Existing output
requires `--overwrite`.

## Tool setup

Install KiCad with its Python API and standard footprint libraries. Windows
KiCad 8/9/10 Python installations and Linux `/usr/bin/python3` are discovered
automatically; use `--kicad-python-path` or `CIRCUIT_WEAVER_KICAD_PYTHON` to select
another installation. The application Python does not need to import pcbnew.

Install [Freerouting](https://github.com/freerouting/freerouting/releases) and its
required Java runtime. Set `CIRCUIT_WEAVER_FREEROUTING` to the JAR or launcher and
`CIRCUIT_WEAVER_JAVA` to Java when it is not on PATH. A portable installation may
instead use `~/.freerouting/freerouting.jar` and `~/.freerouting/runtime/bin/java`
(`java.exe` on Windows). CI pins Freerouting 2.4.1 with Java 25.

## Supported scope

`place-pcb` currently accepts unrouted rectangular boards without copper zones.
It preserves locked footprints, board sides, and pad/net identity. Real courtyard
envelopes include off-center footprint origins. Its connectivity cost uses rotated
pad endpoints, so orientation affects the search. DRC checks actual copper and
project constraints after placement and again after routing. Passing these checks
does not replace review of switching loops, high-speed constraints, thermal design,
silkscreen warnings, or fabrication readiness. Padless generated placement previews
must first be forward-annotated into a real electrical PCB. `build-pcb` provides
a prototype path from the generated root schematic using local library
footprints and exact pin/pad checks; it grants no manufacturing approval. See
the [automated workflow demo](automation-validation.md) for a repeatable run.

## Placement

`PlacementConfig` adds `restarts=3` and `refinement_passes=2`. All restarts share
the existing `iterations` budget. Each restart has its own seed, cooling schedule,
and progressively smaller local moves. Final refinement tries small translations,
rotations, whole-block moves, and attractions toward electrically connected blocks.
Every accepted refinement improves the lexicographic pair `(hard defects, cost)`.
Candidate selection uses the same pair after legalization. A legalized initial
layout remains available even when every annealed candidate is worse.

Fixed components reserve space first, including fixed support parts. Movable
support parts follow owners displaced during legalization. The edge-to-edge gap
is split between courtyard envelopes, and non-orthogonal rotations use a
conservative enclosing rectangle. Fixed coordinates retain their supplied
precision; movable coordinates are evaluated at their published precision.
Unresolved or impossible constraints remain visible as review blockers.

When native pad locations are available, support parts start beside their
owner's shared non-ground pads, with the configured body clearance. Missing
pad evidence retains the deterministic support-ring fallback. Related support
parts, such as USB termination resistors, can be refined onto a common row or
column using their owner, role and signal net family. A shared ground or power
rail alone does not establish a group. Designs with fewer than two movable
primary ICs include their support parts in the compactness objective; larger
designs retain the objective based on their primary components.

`--tidy-references` places horizontal native reference fields above their own
courtyards when space allows, then tries nearby alternatives. Locked fields
reserve their positions. Each accepted field reserves space for later fields,
so obsolete positions of movable fields do not block cleanup. The native
conflict report still checks courtyard, pad/mask, silkscreen, reference and
board-edge collisions; an unresolved field remains visible for review.

The existing objective still balances thermal separation, connectivity, parent
affinity, edge access, compactness, and zoning. Shared-net attractions are divided
by fanout minus one to prevent a large bus overwhelming point-to-point nets.
Spec-based placement uses library courtyard geometry when available and explicit
estimates otherwise. Real-board placement uses geometry and pad locations from
the board. Neither search is a signal-integrity solver.

```sh
circuit-weaver optimize-placement design.yaml --iterations 6000 --restarts 4 --seed 7 --json
circuit-weaver placement-viewer design.yaml -o placement.html --restarts 4 --seed 7
python benchmarks/placement_search.py --output .test-tmp/placement-search.json
```

## Historical search comparison

The earlier search-only comparison at `bff0f74` uses full generated physical assembly inventories from
three checked-in examples, seed 7, 3,000 total annealing iterations, and the same
declared constraints. The baseline is commit `2d036c3`. HPWL is the sum of
component-center net bounding-box width plus height, excluding ground nets.

| Example | Parts | Original HPWL (mm) | Upgraded HPWL (mm) | Reduction |
|---|---:|---:|---:|---:|
| USB regulated supply | 17 | 99.11 | 92.77 | 6.4% |
| IoT sensor node | 27 | 239.24 | 228.17 | 4.6% |
| Motor controller | 10 | 55.03 | 50.25 | 8.7% |

Both versions have zero overlaps and zero declared-constraint violations in
these runs. This small corpus demonstrates these particular seeded outcomes;
it does not establish improvements for every board or predict routed length.
Refinement performs additional evaluations beyond the annealing budget and
increases runtime. Use fewer restarts/refinement passes when latency matters.

## Routing

```sh
circuit-weaver autoroute board.dsn --attempts 3 --timeout 600 -o selected.ses
```

The default remains one attempt. For native KiCad boards, the first attempt uses
stable footprint-reference order in an isolated DSN copy; later attempts also try
descending reference order and the original export order. Shared-image placement
records follow the same order. Only declaration order changes: pad geometry,
coordinates, net assignments, outlines, and routing rules remain intact. Search
evidence records the actual order used. The original exported DSN is preserved.

Builds advertising `-random_seed` use consecutive seeds (zero-based if unspecified).
Other builds cycle through greedy, global, and hybrid optimizer strategies unless
a strategy was explicitly selected. Those strategies affect enabled optimization;
they provide no primary-routing variation when optimizer threads are zero.
Native declaration-order variants provide that variation without enabling the
optimizer. Direct caller-provided DSNs are routed unchanged. An explicit seed
requires advertised seed support. `--routed-board` defaults to disabled optimization;
other multiple-attempt runs default to one optimizer thread. The timeout is a shared
budget for routing attempts; capability probes and optional DSN export have
their existing separate limits. Each attempt receives the remaining time divided
by the remaining attempts, so time saved by early completion can be reused.

Each candidate must pass existing SES syntax, semantic, source correlation,
connection-statistics, and reported-clearance checks. Candidates with clearance
violations or unknown completeness are rejected. Remaining candidates are ranked
by incomplete connections, whether clearance was reported clear, via count, and
trace segment count. Unknown metrics rank after known metrics. Ties retain the
earlier candidate. A session with unreported clearance still requires review;
partial results retain their partial status.

Candidates stay in an isolated temporary directory until selection. The winner
is published atomically, losing sessions are removed, and all-attempt failure
preserves any existing output. A changing source DSN aborts publication. The
returned `search` object explains the selection and failed attempts. The final
artifact is a Specctra session by default. `--routed-board` performs the import
and native DRC automatically when the input is a real PCB. No fabrication
readiness or controlled-impedance guarantee is inferred.

## Real-board regression corpus

`tests/test_native_pcb_workflow.py` executes the public CLI against real KiCad and
Freerouting for five circuits: LED, resistor divider, RC filter with custom width
rules, back-side RC components, and a SOIC fanout network. The corpus starts with
overlapping movable parts, an offset outline, and locked through-hole connectors.
It requires legal placement, complete routing, zero final DRC errors, preserved
pad/net identity and board sides, unchanged locked placements, and unchanged
source files. CI and release jobs run it on KiCad 8, 9, and 10; missing tools fail
those jobs. Tool-independent tests cover timeout, malformed outputs, stale inputs,
and preservation of existing output on failure.

Current router final-stage summaries establish connection and clearance counts;
missing via/trace counts are measured from the validated SES. Each routing process
gets isolated settings and disabled analytics. On Freerouting 2.4+, automatic
necking and the default fine-pitch fanout pass are disabled so routing uses the
imported DSN widths/vias. Final KiCad DRC remains the acceptance gate.
Native-board autorouting adds a 0.01 mm clearance margin to generic DSN rules,
preserving typed exceptions, to accommodate observed export/router/import
geometry differences. The result reports the applied margin. This does not change native board/project rules or
caller-provided DSN files; the imported board must still pass native DRC.
