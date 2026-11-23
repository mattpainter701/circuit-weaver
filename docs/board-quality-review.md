# Native geometry and transient acceptance

Run `circuit-weaver review-pcb board.kicad_pcb --plan review.json --json`.
Python uses `circuit_weaver.board_review.review_pcb(board_path, plan,
kicad_python_path=...)`; MCP uses `review_board(board_path, plan_json)` with an
absolute board path. One native inspection supplies the review. It creates no
project checkpoint and checks board/settings bytes and file existence.

```json
{"placement":{"cell_size_mm":5,"max_empty_fraction":0.2,"reserved_regions":[]},
 "routing":{"schema_version":1,"routes":{
   "clock_p":{"source":"AUTHORED","net":"CLK_P","from":{"ref":"U1","pad":"1"},"to":{"ref":"J1","pad":"1"}},
   "clock_n":{"source":"AUTHORED","net":"CLK_N","from":{"ref":"U1","pad":"2"},"to":{"ref":"J1","pad":"2"}}
 },"pairs":[{"id":"clock","source":"AUTHORED","positive_route":"clock_p","negative_route":"clock_n","max_skew_mm":1.0}]}}
```

The endpoints and 1 mm limit illustrate the schema, not device requirements.
Routes can also set `max_length_mm` and `max_vias`. Measurements use the shortest
explicit centerline path and proven via transitions, omitting via barrel length
and copper inside pads. Missing pad contact, ambiguous pads, incomplete
geometry, arcs, same-net copper zones, incomplete zone inventories and oversized
graphs remain unverified. Completely inventoried zones on unrelated nets do not
block explicit trace measurements. They do
not establish impedance, propagation delay or RF performance.

Placement reports rotated native envelopes and the largest empty axis-aligned
rectangle on a bounded conservative grid. Overhanging envelopes require
review. Authored reservations require `name`, absolute native `x_mm`/`y_mm`,
`width_mm` and `height_mm`. The default 20% threshold is a review prompt,
not a requirement to fill the board uniformly. A verified diagnostic does not
establish electrical/manufacturing approval. CLI exit codes are 0 verified,
3 review_required, 2 unverified and 1 invalid/error. MCP callers must inspect
returned statuses even when tool invocation succeeds.

Set shared workflow `options.route_contracts` to the same nonempty `routing`
object to enforce it automatically. After native routing/DRC succeeds, the
runner checks the staged candidate before publication. Failed/unverified
checks retain candidate and `route-quality.json` in the attempt directory and
leave the stage failed. Verified checks publish board, session and hash-bound
report together. Contracts participate in saved plan hashes.

## Transient acceptance windows

Transient voltage/current targets support `statistic: min`, `max` or `mean`
and `window: {start_s, stop_s}`:

```json
{"metric":"voltage","trace":"v(OUT)","unit":"V","statistic":"mean",
 "window":{"start_s":0.004,"stop_s":0.008},"comparator":"between","min":3.2,"max":3.31}
```

The finite, strictly increasing parsed time axis must cover the entire window
and align with the real-valued trace. Mean is the time average of piecewise-
linear samples, integrated trapezoidally with interpolated window endpoints.
Min/max check only in-window samples; they do not prove continuous peaks and
remain unverified without samples. Structured PULSE sources support both V
and I sources, with numeric levels in volts or amps respectively. Boolean and
nonfinite values cannot establish verified limits.

Run `python -m benchmarks.diverse_systems.transient_suite --output <new-dir>`
for generic IoT input, OT current-burden and transmitter loaded-divider
transients, analytic responses, raw hashes and wrong-value/wrong-target
controls. These do not qualify complete products or RF cores.
