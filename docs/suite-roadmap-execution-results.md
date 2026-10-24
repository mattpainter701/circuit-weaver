# Next-wave roadmap execution results, October 23, 2026

This page records the next execution wave for the [suite enhancement roadmap](suite-enhancement-roadmap.md). Evidence is retained in the local `automation_validation_results/roadmap-execution-20261023` directory; links below are relative to this documentation folder. This wave completes several bounded work packages, but it does not complete the eight-phase roadmap or establish product, RF, manufacturing, or hardware qualification.

## Completed work packages

| Work package | Result and scope |
| --- | --- |
| Placement optimization | On the same 236-part SDR board and 750-iteration budget, the empty-rectangle proxy fell from 28.34% to 7.69%; the HPWL proxy fell from 13,077.85 mm to 11,638.43 mm. These are placement diagnostics, not routed-copper measurements. A deterministic sparse cycle for non-rail fanout above 32 keeps all owners connected with linear edge count and retains aggregate attraction. Its 1,200-owner regression passed; ordinary fanout is unchanged. The SDR's non-rail nets have at most five component references, so this follow-up does not alter that fixture. [Comparison evidence](../../automation_validation_results/roadmap-execution-20261023/placement-comparison03.json) |
| Native placement preview | Before/after native CAD previews resolve all 236 component models. The after image is an actual placed assembly preview and remains explicitly **unrouted**. [Preview proof](../../automation_validation_results/roadmap-execution-20261023/placement-preview01/proof.json) |
| Transient electrical suite | Five authored generic passive-circuit cases met their expected nominal or fault outcomes, including IoT voltage RC, OT pulsed-current burden R\|\|C, and TX loaded bias-divider transients. The wrong-value control failed both its physical oracle and target as expected; the wrong-target control passed its physical oracle and failed its target. Raw-vector provenance includes SHA-256 hashes. These results validate the suite and these simple circuits only. [Transient report](../../automation_validation_results/roadmap-execution-20261023/transient-suite01/transient-suite-report.json) |
| Client surfaces and workflows | 20 source-client operations passed across CLI, Python, real stdio MCP and private OpenCode skill installation/runtime checks; MCP registered 18 tools. The native availability probe now closes inherited protocol input. This wave did not run a new OpenCode model conversation. [Surface proof](../../automation_validation_results/roadmap-execution-20261023/source-surfaces02/proof.json) |
| Native library load and replay | The native library trial passed KiCad CAD loading and exact replay checks. A later native regression reproduced one library warning after staging the same PCB without context, then removed it with explicit project context; DRC had zero findings and native inspection passed. The combined placement fixture was correctly rejected for having no named nets; its named-net retry was stopped by the RAM reserve. This establishes CAD lookup and DRC context, not complete custom-device functionality. [Native library evidence](../../automation_validation_results/roadmap-execution-20261023/library-native02/native-proof.json), [baseline](../../automation_validation_results/roadmap-execution-20261023/native-context-baseline01/proof.json), [fixed DRC](../../automation_validation_results/roadmap-execution-20261023/native-context-fixed01/context-drc.json) |
| Route diagnostic baseline | The retained SDR route review measured 24 centerlines and evaluated 12 illustrative differential pairs against an authored 1 mm diagnostic skew limit: 4 passed and 8 failed. This result exposes follow-up work; the limit is not a device timing or RF specification. [Route review](../../automation_validation_results/roadmap-execution-20261023/sdr-route-review01/result.json) |
| New routed SDR | Retry `after04` completed both routing candidates, selected a zero-open/zero-clearance-violation session, passed native DRC with zero blocking findings, verified all 24 authored endpoint paths before publication, and rendered all 236 models. Native DRC retained 199 footprint-library configuration warnings in the isolated configuration; this is not warning-free or fabrication approval. [Workflow evidence](../../automation_validation_results/roadmap-execution-20261023/sdr-routing-after04/result.json), [actual routed assemblies](../../automation_validation_results/roadmap-execution-20261023/sdr-routed-review02/comparison.html) |
| Routed tradeoffs | Against the previous routed board, vias fell 231→199 and worst illustrative clock skew fell 19.94→4.94 mm; failing pairs fell 8→5. Summed session wire centerlines increased 13.653→14.436 m (5.74%). The new route used a larger two-candidate/600-second budget; these are observed results of the combined rerun, not a controlled claim that placement alone caused each change. Impedance, return paths and actual timing requirements remain unqualified. [New diagnostic review](../../automation_validation_results/roadmap-execution-20261023/sdr-routed-review02/review.json) |
| Focused regressions | 650 distinct tests passed, with 3 skips and 8 additional subtests. The disjoint inventory covers 129 optimizer/placement quality, 101 electrical/SPICE, 33 route/review/library/MCP, 67 placement regressions, 98 workflow/state/runtime, 38 context/DRC/bridge, 93 schematic/build/handoff and 91 cleanup tests. Single-threaded Ruff and generated capability documentation checks passed. This is not full cross-platform CI. [Inventory proof](../../automation_validation_results/roadmap-execution-20261023/test-inventory01/proof.json) |

## Eight-phase roadmap status

The results above are completed work packages, not a claim that their corresponding roadmap phases are fully closed. Placement and accepted SDR routing advance phase 1; phase 3 has a generic transient acceptance suite; phase 4 has a native library load/replay trial; and phase 5 has broader client-surface coverage. Phase 2 retains the first-wave zero-crossing evidence; its generator did not change here. Phase 6 still needs accepted 438/1,202-part routing, and phase 7 remains limited to generic authored fixtures rather than qualified product-family behavior. Phase 8 still needs the pending fresh-wheel validation and external final-head CI. No RF or device-specific electrical claims are made.

## Pending validation

| Item | Status |
| --- | --- |
| SDR native route retry `after04` | **COMPLETED.** Endpoint-gated routing and native assembly passed. The diagnostic review still requires attention for five clock pairs. |
| DAC438 native route | **INCOMPLETE.** New placement passed. The 300-second router 2.4.1 trial reached 155 opens after its last completed pass; the 600-second retry produced no acceptable session. An isolated official 2.5.0 experiment used the same placement/budget through the immutable previous installed wheel; its last completed pass had 142 opens before the 2 GiB RAM reserve stopped and reaped it. No routed DAC board was accepted. [600-second result](../../automation_validation_results/roadmap-execution-20261023/dac438-routing02/result.json), [2.5 supervisor result](../../automation_validation_results/roadmap-execution-20261023/dac438-routing03-job/d8.json) |
| Fresh installed wheel | **PASSED for packaging and pure client operations.** An exact committed-source wheel/sdist and new venv verified all 225 package files against source/wheel/install. The fresh interpreter passed 17 CLI/Python/real stdio MCP lifecycle/local-bundle/private-skill operations with 18 registered tools. This deliberately used validation-only continuation; fresh native generation/review remains held by the 2 GiB RAM reserve. [Package proof](../../automation_validation_results/roadmap-execution-20261023/candidate01/proof.json), [fresh client proof](../../automation_validation_results/roadmap-execution-20261023/fresh-surfaces-pure01/proof.json) |
| Large-fanout placement regression | **PASSED.** Included in the 129-test placement batch and 650 total. |
| Full eight-phase roadmap and release gates | **INCOMPLETE.** Fresh final-head CI, required native routing/board checks, and any remaining roadmap phase evidence must be recorded before claiming completion. |

Native route completion alone would not establish impedance, return-path integrity, differential timing, RF performance, EMC, or manufacturing readiness. Those require their own specified checks and evidence.

The library follow-up fixed dropped footprint context in native worker,
placement/import, schematic build and DRC staging. A shared copier bounds files,
bytes and traversal; rejects links/reparse points and escaping/ambiguous paths;
and checks table/content/membership drift. The workflow passes its explicit
circuit root for generated schematics under `output/`. Placement/routing
checkpoints watch absent tables and declared library trees. DRC verifies exact
isolated board bytes and authored settings before recording acceptance.
Symbol tables and authored schematic settings still use the schematic source
directory; preview-specific staging was not extended. The 199 SDR warnings also
reflect the intentionally isolated global configuration and are not all caused
by the fixed project-local issue.

RAM later fell below 1 GiB. Pure Python tests continued with a 128 MiB cap and
512 MiB reserve; single-threaded lint avoided its default thread-pool allocation.
Native jobs retained the 2 GiB reserve. Packaging and real stdio client checks
used a 384 MiB cap with the 512 MiB reserve. The 2.5 routing experiment and
named-net native fixture retry remain incomplete rather than being relabeled
as passing.
