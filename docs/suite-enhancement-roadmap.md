# Circuit design suite enhancement programme

Started October 23, 2026 from `295e321`, continuing the existing PR #39 task. Each milestone requires executable evidence. A partial or blocked stage remains visible; a preview does not establish electrical or manufacturing approval.

| Phase | Enhancement and workflow | Acceptance criteria | Execution status |
| --- | --- | --- | --- |
| 1 | Complete representative native boards, beginning with the 236-part SDR clock carrier and 150-part BLE sensor node | Preserve declared connectivity through placement/routing; native DRC and reference labels checked; assembled preview from actual native board; explicit runtime and memory limits | SDR placement/routing/DRC/contracts/assembled preview passed; BLE placement passed, routing stopped by RAM reserve at 3 opens |
| 2 | Improve dense schematic presentation | Remove reproduced body crossings with safe routing/labels; preserve native pad-net assignments; no weakened foreign-net or page-bound checks | SDR final sheets: 24 crossings to 0, zero overlaps; native regeneration/build passed |
| 3 | Validate active electrical behavior and load/supply variation | Explicit source/model contracts, exact unit-bearing limits, independent analytic expectations and deliberate bad-model/pin/limit controls; document generic model boundaries | Five generic active cases passed; ten passive regression cases passed; device-specific model qualification remains open |
| 4 | Add controlled local library onboarding | Dry-run plan, SHA/provenance, contained project assets, preserved unrelated tables, stale/conflict rejection and transactional rollback; CLI/Python/MCP access | Implemented; unit and real CLI/Python/MCP source trials passed; exact-wheel trial pending |
| 5 | Make acceptance and continuation reproducible | Accurate stage evidence, failed/unverified results, interruption/resume and fresh-install client checks | Fixed generated-output self-dependencies and unrelated ancestor selection; source lifecycle passed; final fresh-install validation pending |
| 6 | Extend the routing scale ladder | Repeat successful full workflows at 155/236, 438 and 1,202 parts, recording unrouted connections, DRC, vias, path constraints, runtime and RAM | 438-part placement passed; routing did not complete within 300 seconds. 1,202-part routing remains planned. Constraint-aware placement/routing takes priority over count alone |
| 7 | Expand practical circuit families | IoT battery/load transients; OT input/output protection and fault tests; SDR clock/ADC carriers; radar external-IF acquisition; transmitter control/filter subsystems | Planned; add only independently checked functionality |
| 8 | Close release validation | Focused tests and local native evidence, exact committed wheel, fresh CLI/Python/MCP checks, required CI on final SHA and release documentation | CI budget remains an external release gate |

Validation jobs run serially under the pinned owned-process supervisor. Native jobs retain a 2 GiB free-RAM floor and 2.5 GiB hard cap; small Python batches use a 1 GiB reserve and 384 MiB hard cap when needed. Source and review work can proceed in parallel. Original executed inputs and failed runs remain unchanged; retries use new evidence directories.

The previous [diverse trials](diverse-system-stress.md) establish native import and connectivity at 1,202 parts and passive SPICE oracles at 1,280 parts. The new work must extend that evidence into routed boards, active behavior, reliable part onboarding and usable client workflows. Full RF device models, electromagnetic analysis, hardware measurements and device-specific timing/thermal requirements remain separate engineering gates.

Current execution details and retained failures are in [the enhancement results](suite-enhancement-results.md). The next placement work must address functional zones, empty-space distribution, power/return loop geometry, clock-pair requirements and measured route lengths; a zero-open DRC result alone cannot meet those criteria.

The next placement review will measure contiguous unused interior areas and
flag any exceeding 20% of usable area for review unless an authored keepout or
functional reserve explains it. This is a proposed review threshold to test
across the corpus, not an electrical acceptance rule or a demand for uniform
density. Functional grouping and authored mechanical constraints take priority.
