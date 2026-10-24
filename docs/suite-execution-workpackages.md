# Eight-phase execution work packages

This breaks the [suite roadmap](suite-enhancement-roadmap.md) into executable
work, continuing the same PR #39 task from `7a9c50c`. Earlier validated results
remain in [the first-wave report](suite-enhancement-results.md). Each package
must retain its inputs, failed attempts and measured acceptance evidence.
Measured outcomes and open gates are recorded in [the next-wave results](suite-roadmap-execution-results.md).

| Phase | Current work package | Dependency and acceptance | Owner lane |
| --- | --- | --- | --- |
| 1. Representative boards | Compare revised SDR placement using identical native input, seed and budget; then rerun routing/assembly; retry BLE when placement is improved | Native pad-net identity and physical gates preserved; compare actual geometry and routed constraints, not just a rendered image | Placement agent + root native queue |
| 2. Schematic quality | Retain zero-crossing SDR regression and extend coverage when each new family is introduced | Unchanged generator retains its 24→0 crossing proof; new circuit families need independent emitted-sheet checks | Root regression queue |
| 3. Active behavior | Add transient voltage/current windows and structured pulsed current sources | Full finite raw time coverage; time-weighted means; nominal, faulty component and faulty-limit controls; raw hashes | Electrical agent |
| 4. Library onboarding | Keep shared transactional imports, verify imported CAD access, preserve project-local footprint context in native worker/build/placement/import/DRC stages, and add vendor/model bundles only with explicit contracts | Bounded context copier and checkpoint invalidation implemented; installed native DRC/inspection/placement passed with unchanged sources. Symbol/model/device qualification remains separate | Root client/library queue |
| 5. Reproducible continuation | Expose read-only board review through CLI/Python/MCP and preserve failed/unverified distinctions | Same board hash and report semantics on every surface; read-only review creates no workflow checkpoint; fresh-installed client trial | Root integration |
| 6. Routing scale | Measure endpoint paths, via counts and clock-pair skew before attempting larger boards; rerun 438 after placement improves | Authored limits only; incomplete/unsupported evidence unverified; bounded graph and owned native runtime; 1,202-part routing is not implied by native import | Route-quality agent + root native queue |
| 7. Practical families | Execute IoT input RC, OT current-burden filter and transmitter loaded-bias transients | Distinct excitation/topology, independent analytic response and deliberate wrong-component control; no RF-core or device qualification claim | Electrical agent |
| 8. Release validation | Focused serial test batches, exact committed wheel, private skill/runtime and real stdio MCP, final-head CI | Final source/wheel/install hashes match; client checks pass; merge/release requires fresh required green CI | Root release queue |

Source work runs in parallel with separate file ownership. All pytest, native,
SPICE and packaging jobs run serially under the pinned owned-process supervisor.
Native work keeps the 2 GiB reserve and 2.5 GiB cap; small Python batches use a
1 GiB reserve and 384 MiB cap. A memory stop is retained as incomplete evidence.
When RAM later fell below 1 GiB, pure Python batches used a 512 MiB reserve
and 128 MiB cap; packaging may use a bounded 384 MiB cap with that same reserve.
Native jobs still require the 2 GiB reserve.

The native builder now supports explicit two- and four-copper-layer boards,
with input/checkpoint plumbing and native saved count/order verification.
Selected-layer GND planes and post-session saved refill checks are implemented;
a small native four-layer route passed. Both matched 438-part DAC routing arms
stopped at the RAM reserve, so that larger route remains unaccepted. The next
work package is the [under-500 four-layer acceptance campaign](four-layer-acceptance.md),
which broadens circuit families and records missing gates explicitly.

The board review measures conservative grid footprint occupancy and the largest
empty axis-aligned rectangle, with authored reservations recorded explicitly.
Its default 20% threshold is a review prompt, not a requirement to fill the
board uniformly. Route measurements are centerline distances and via counts;
they do not establish impedance, plane continuity, delay, jitter or RF behavior.
