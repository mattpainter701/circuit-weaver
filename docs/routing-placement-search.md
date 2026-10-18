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
owner's shared non-ground pads, with the configured body clearance. Dedicated
signals take priority over recognized shared supply rails in both seeding and
parent-affinity measurements. A reset pull-up follows its reset pad even when
its other terminal shares the MCU supply. Equivalent decouplers start beside
successive real shared-rail pad endpoints, spreading them around an IC's supply
pins. Missing pad evidence retains the deterministic support-ring fallback.
Assembly support records retain their authored owner pin. Native placement
uses its actual numbered pad when its net matches, so capacitors on a shared
rail can remain associated with different digital and analog supply pins.
Legacy records, symbolic pin labels and ambiguous duplicate physical pads
retain the net-based fallback. Local supply/ground endpoint pairs receive a
placement preference without adding a global attraction between ground nets.
These two-leg straight-line measurements are reported separately from routed
copper length.
For supports sharing several dedicated nets, affinity uses the worst of each
net's nearest endpoint distances, so one short connection cannot hide a long one.
Related support parts receive bounded cohesion and row/column alignment
preferences using their owner and dedicated signal-net family. A reset pull-up
and filter capacitor can form one local cell despite their different roles;
shared ground and supply rails alone do not establish a group.
Compactness measures the occupied envelope and centering using rotated movable
courtyards, including support parts. Primary owners retain the radial cohesion
term to balance the influence of large blocks and their passive banks.
For multiple movable owner/support families, refinement tries bounded row or
column alignment candidates with a channel derived from the configured body
clearance and component gap. It can recenter the complete free envelope in the
same candidate. Fixed or edge-constrained parts preserve their positions, and
every candidate uses the existing hard-constraint rank.

`--tidy-references` searches nearby horizontal native reference positions and
places the most constrained fields first. Locked fields reserve their positions;
obsolete positions of movable fields do not block cleanup. A label must remain
associated with its own component rather than a nearer component with the same
designator prefix. An IC label can sit beside its support capacitors, while two
neighboring capacitor or resistor labels remain distinguishable. Native checks
also cover courtyard, pad/mask, silkscreen, reference and board-edge collisions.
After finding a legal label slot, cleanup considers nearby alternatives with a
clearer distance advantage over same-prefix neighbors. Both the soft penalty
and additional courtyard distance are limited to 0.75 times the native label
height. The first legal slot remains the fallback; font size, locked fields and
all physical/association checks are preserved.

If that placement leaves unresolved labels, the native placement command tries
up to three private candidates using the requested seed, seed + 1 and seed + 2.
Each candidate receives the requested iteration budget and must pass physical
DRC and native reference checks. Only the first passing candidate is published;
exhaustion preserves any existing destination. Results record attempted seeds,
label counts and the selected seed. Without `--tidy-references`, placement keeps
its single-attempt behavior.

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
by incomplete connections, whether clearance was reported clear, measured copper
centerline length, via count, and trace segment count. A shorter valid route may
use more vias; fewer vias break equal-length ties. Length is summed
from every validated SES wire polyline using the session's own physical
resolution; per-net lengths are retained in the routing statistics. Via barrels
and copper inside pads are excluded. Unsupported geometry or units report an
unavailable measurement instead of a partial or zero length. Unknown metrics
rank after known metrics. Ties retain the
earlier candidate. A session with unreported clearance still requires review;
partial results retain their partial status.

Candidates stay in an isolated temporary directory until selection. The winner
is published atomically, losing sessions are removed, and all-attempt failure
preserves any existing output. A changing source DSN aborts publication. The
returned `search` object explains the selection and failed attempts. The final
artifact is a Specctra session by default. `--routed-board` performs the import
and native DRC automatically when the input is a real PCB. No fabrication
readiness or controlled-impedance guarantee is inferred.

Native placement also reserves nearby reference-label space with a bounded
soft preference. It uses the actual horizontal text bounds and courtyard
geometry, including the existing silkscreen margins and same-prefix label
association rule. This estimate ignores other movable labels; native reference
cleanup and physical DRC remain the publication gates. Hidden and locked
reference fields do not receive this preference, and text size is preserved.

The AP62300 default enable policy follows its documented floating-EN automatic
startup option. This avoids connecting the 8 V maximum EN input directly to
higher input rails. An explicit enable-net choice retains precedence; other
regulators keep their existing default. See the manufacturer's
[AP62300 datasheet](https://www.diodes.com/datasheet/download/AP62300.pdf),
TSOT26 pin descriptions and absolute maximum ratings (pages 3–4).

## Local return connections

For a routed board with a known local-return detour, `refine-pcb` accepts an
explicit JSON connection plan. It adds same-net copper without moving components,
text or existing tracks. A per-connection `max_length_mm` can bound the new
planar path. The bounded search may add front-side tracks or a back-side link
through vias. Native copper collision checks screen candidates, and full KiCad
DRC plus native label checks gate publication. Unsupported geometry or no legal
local path fails while retaining the input and any previous output.
The current search requires two copper layers, no copper zones, front-accessible
endpoint pads and supported native copper shapes; same-net through vias can be
reused without adding another hole.

The automated workflow accepts `--return-paths plan.json` to run this optional
phase after routing and before delivery gates and previews. It retains
`routed-base.kicad_pcb`, renders the refined `routed.kicad_pcb`, checks the plan's
source/copy hashes, and records refinement evidence. This adds one stage to the
usual workflow. Existing routes are retained so adding a legal same-net link
cannot remove an already available copper path; independent path extraction is
still needed to assess the requested return improvement.

## Protected copper cleanup

`cleanup-pcb` accepts explicit net names and protected pad pairs. It measures
their native copper paths before changing anything, removes redundant unlocked
segments and transitions, and can replace several local vias with a shared pair.
Every trial preserves selected-net pad connectivity and the original protected
path ceilings. Unselected nets, locked copper, component poses and labels are
retained. The bounded proposal search uses native copper/mask geometry; full
KiCad DRC and native reference checks remain final publication gates.

The geometry graph includes track intersections, actual copper-shape contacts,
interior pad/track contacts and via layer transitions. Heap ties use a stable
ordering so mixed pad and coordinate nodes cannot interrupt shortest-path checks.
The path lengths are geometric proxies, not loop inductance or current-capacity
analysis. Current cleanup supports two-layer boards without zones or copper arcs.

The workflow's optional `--cleanup-plan cleanup.json` runs after routing and any
return refinement. It retains both intermediates when both phases are enabled;
all delivery checks and previews use the final cleaned board. The combined mode
adds two stages to the usual workflow. Input plan and board/project/rule hashes
are verified throughout, including native save/reload checks.

## Filled ground planes after routing

Use `plane-pcb` with an explicit ground-plane plan to add filled GND pours after
return refinement and copper cleanup. The native operation retains all original
tracks and vias and checks protected paths using their original trace geometry.
KiCad fills both outer-layer zones with thermal pad connections and removes
unconnected islands. Saved filled polygons, connectivity, original placement and
labels, full native DRC and source/settings hashes gate publication. The current
bounded operation accepts two-layer rectangular boards without existing zones.
The automated workflow's `--ground-plane` option retains its intermediate boards
and renders the final filled board; see [workflow validation](automation-validation.md).

After filling, `plane-cleanup-pcb` can remove unlocked straight traces already
covered by native same-net plane copper. Full copper-shape containment is checked
before deletion and again after refill and save/reload. Protected route geometry,
all vias, locked copper, other nets, plane settings, placement and labels are
preserved. Full DRC and native connectivity gate publication. The workflow's
`--plane-cleanup` option retains the filled intermediate board and renders the
final cleaned board; measured protected paths remain trace-only.

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
