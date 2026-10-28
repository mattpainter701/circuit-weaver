# Circuit design suite enhancement programme

Started October 23, 2026 from `295e321`, continuing the existing PR #39 task. Each milestone requires executable evidence. A partial or blocked stage remains visible; a preview does not establish electrical or manufacturing approval.

The [October 25 pre-merge review](suite-premerge-review.md) reconciles the PR
inventory with all eight phase goals, records the remaining closure evidence,
and explains the staged CI policy and strict native CAD acceptance gate.

The subsequent [October 25 phase continuation](suite-phase-continuation.md) integrates authored current
operating cases into shared validation, CLI/MCP and workflow checkpoints with
ledger-resolved inputs. It leaves actual corpus operating cases and device
qualification unverified. The 96-part native continuation remains subject to
the existing 2 GiB host reserve and 2.5 GiB owned-job cap.

The current execution wave is broken into [owned work packages](suite-execution-workpackages.md), with measured outcomes and remaining gates in [the next-wave results](suite-roadmap-execution-results.md).

The [October 24 follow-up](suite-roadmap-followup.md) advances explicit current
operating cases, stock-library context and the native four-layer scale ladder.
It records the new memory cost exposed by complete stock-catalog lookup and
keeps CAD completion separate from device qualification.

| Phase | Enhancement and workflow | Acceptance criteria | Execution status |
| --- | --- | --- | --- |
| 1 | Complete representative native boards, beginning with the 236-part SDR clock carrier and 150-part BLE sensor node | Preserve declared connectivity through placement/routing; native DRC and reference labels checked; assembled preview from actual native board; explicit runtime and memory limits | SDR placement/routing/DRC/contracts/assembled preview passed; BLE placement passed, routing stopped by RAM reserve at 3 opens |
| 2 | Improve dense schematic presentation | Remove reproduced body crossings with safe routing/labels; preserve native pad-net assignments; no weakened foreign-net or page-bound checks | SDR final sheets: 24 crossings to 0, zero overlaps; native regeneration/build passed |
| 3 | Validate active electrical behavior and load/supply variation | Explicit source/model contracts, exact unit-bearing limits, independent analytic expectations and deliberate bad-model/pin/limit controls; document generic model boundaries | Five generic active, ten passive and five transient nominal/fault cases passed their expected outcomes; device-specific model qualification remains open |
| 4 | Add controlled local library onboarding | Dry-run plan, SHA/provenance, contained project assets, preserved unrelated tables, stale/conflict rejection and transactional rollback; CLI/Python/MCP access | Imported CAD load/replay passed; bounded footprint context follows native staging and generated builds. Installed native DRC/inspection/placement passed with zero library warnings and unchanged sources; device qualification remains separate |
| 5 | Make acceptance and continuation reproducible | Accurate stage evidence, failed/unverified results, interruption/resume and fresh-install client checks | Shared board review and authored route gates added; 20 source operations passed. Fresh install passed 17 pure lifecycle/local-bundle/private-skill operations with 18 tools; fresh native repeats are held by RAM |
| 6 | Extend the routing scale ladder | Repeat successful full workflows at 155/236, 438 and 1,202 parts, recording unrouted connections, DRC, vias, path constraints, runtime and RAM | Revised SDR accepted; 438-part placement passed but 300/600-second 2.4.1 routing failed; isolated 2.5 trial stopped at the RAM reserve. 1,202-part routing and explicit generated stackups remain open |
| 7 | Expand practical circuit families | IoT battery/load transients; OT input/output protection and fault tests; SDR clock/ADC carriers; radar external-IF acquisition; transmitter control/filter subsystems | Generic IoT input RC, OT current-burden and transmitter loaded-divider transients plus two fault controls passed; full device/product behavior remains planned |
| 8 | Close release validation | Focused tests and local native evidence, exact committed wheel, fresh CLI/Python/MCP checks, required CI on final SHA and release documentation | 650 distinct tests, exact source/wheel/install alignment for 225 files and 17 fresh pure client checks passed. Native repeats are held by the RAM reserve; final-head CI remains an external release gate |

Validation jobs run serially under the pinned owned-process supervisor. Native jobs retain a 2 GiB free-RAM floor and 2.5 GiB hard cap; small Python batches use a 1 GiB reserve and 384 MiB hard cap when needed. Source and review work can proceed in parallel. Original executed inputs and failed runs remain unchanged; retries use new evidence directories.

The previous [diverse trials](diverse-system-stress.md) establish native import and connectivity at 1,202 parts and passive SPICE oracles at 1,280 parts. The new work must extend that evidence into routed boards, active behavior, reliable part onboarding and usable client workflows. Full RF device models, electromagnetic analysis, hardware measurements and device-specific timing/thermal requirements remain separate engineering gates.

The next routing milestone is the [four-layer, under-500 acceptance campaign](four-layer-acceptance.md):
ten connected variants and controls across six families, measured at 25–482
physical parts. Explicit four-layer generation and plane support are now
implemented. The new sweep separates pure compilation, local CAD access,
packing, native DRC/routing, actual model rendering and product qualification;
its measured results and missing gates replace component-count-only claims.

Current execution details and retained failures are in [the enhancement results](suite-enhancement-results.md). The next placement work must address functional zones, empty-space distribution, power/return loop geometry, clock-pair requirements and measured route lengths; a zero-open DRC result alone cannot meet those criteria.

The placement review now measures empty axis-aligned interior rectangles and
flag any exceeding 20% of usable area for review unless an authored keepout or
functional reserve explains it. This is a proposed review threshold to test
across the corpus, not an electrical acceptance rule or a demand for uniform
density. Functional grouping and authored mechanical constraints take priority.

## Planned phase-8 showcase handoff

After the relevant phase acceptance gates pass, publish a reproducible showcase
of the measured workflows:

- [ ] Update the README with circuit-directory start/open/resume examples and
  links to accepted sample workflows and their actual assembled-board previews.
- [ ] Update the editable workflow and architecture diagrams to show the shared
  skill, CLI, Python and MCP runner, checkpoints and native acceptance gates.
- [ ] Curate small, medium and larger sample circuits with checked-in source,
  library/model provenance, exact commands and evidence from a fresh full run.
  Publish the exact four-layer DAC fixture before presenting it as a runnable
  public sample.
- [ ] Verify documentation links and commands from a fresh installation, and
  distinguish CAD acceptance from electrical, RF, timing, thermal, manufacturing
  and hardware qualification. Keep incomplete phase results visible.

Draft artwork or local-only source bundles do not close this handoff. Final
examples must correspond to the published source and tested release revision.
