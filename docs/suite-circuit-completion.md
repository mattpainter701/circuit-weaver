# Circuit completion — October 27, 2026

This continues draft PR #39 with existing circuits. No benchmark cases were
added. Local evidence is retained in
`automation_validation_results/trim-circuits-20261027/`.

## Reviewable fixtures with unchanged coverage

Commit `6f45b7d` removes 15 expanded diverse-system YAML/JSON files, replacing
them with a frozen inventory and regeneration into each run's evidence.
The inventory was captured from candidate `9f76e3b`, before deletion. All 15
regenerated specifications, architecture records and connectivity contracts
match their original canonical parsed SHA-256 values. All five cases remain,
including all 768 relay paths and 74 SDR paths. The suite index previously
said 61 SDR paths; the actual retained contract already contained 74.

The existing general-scale CLI suite argument materializes these inputs
automatically. Generation refuses nonempty output, and verification rejects
missing, extra or changed coverage. The focused source batch passed 37 tests;
Ruff passed. Original expanded files remain in Git history and the retained
pre-trim archive. This reduces review bulk; it is not a measured CI-minute
saving or a new circuit acceptance result.

Commit `06dbd53` also removes the two expanded unseen-scale specifications and
the repeated inline route-contract bulk. A second frozen inventory preserves
the full pre-trim suite and all three inputs. The handcrafted gateway source
remains tracked. The three cases still expand to 187, 268 and 438 parts; the DAC
retains all 110 paths, 144 filtered output legs and 72 GND return assertions.
The diverse comparison cases use the same retained builders and still match
their earlier hashes. No circuit or benchmark case was added.

The resulting PR has about 40,900 additions, down from 106,010. The focused
fixture/runner/connectivity batch passes 55 tests, including a deleted return
assertion and an escaping input path. Commit `fc80597` makes verified generated
inputs usable in evidence directories outside the checkout while retaining
input-directory containment. Both suites also regenerate and load using the
isolated installed runtime. Its 229 package files match the committed archive;
see `installed-fixtures10/proof.json`. Ruff and whitespace checks pass.

## Diagnose the 96-part relay placement

The original design and native prototype remain frozen at 76.8 × 75 mm.
The installed runtime now retains full rejected placement results. The first
candidate passed physical placement DRC, excluding the expected unrouted-net
blockers, but native reference acceptance failed. The 1,500-iteration
continuation reported 14 unresolved references and 109 conflicts at 0.5 mm
component spacing; the next candidate at 1.0 mm reported 23 and 131.
At 1.5 mm the optimizer instead reported ten component-overlap pairs and one
out-of-bounds connector. These are rejected candidates, not accepted boards.

Commit `711af1f` fixes feedback that jumped from a feasible fallback spacing
directly to the much larger preferred reference spacing. Retries now visit
intermediate gaps. The focused placement/label/route-gate regression passed
65 tests, and Ruff passed. This fix alone has not completed the relay circuit.
An isolated 0.8 mm font diagnostic still left five unresolved references;
the authored 1 mm font and native publication gates remain unchanged.

An isolated **90 × 90 mm revision** now meets the native CAD gates. The user
approved modest outline growth on October 27 after reviewing the scope of this
generated fixture. The original 76.8 × 75 mm attempt remains failed and its
source files remain unchanged. Do not describe the original outline as solved.

The revision places all 96 parts at the preferred spacing with zero unresolved
references or conflicts. It keeps the authored 1 mm fonts and text visibility.
Routing required retained router copper and six explicit same-net connections,
each accepted only after fresh native DRC decreased the open count without
introducing another blocker. This controlled completion is not a demonstration
that the default one-shot autorouter completes the circuit unattended.

The final full SES imports through the public native route gate, preserves the
accepted footprint poses and every pad/net, and refills the authored internal
GND plane. Independent native reload verifies 96 footprints, 427 pads, four
copper layers, all 48 output paths, zero DRC blockers and zero label conflicts.
The adopted completed board then runs the normal preview stage, accounting for
96/96 represented parts and resolved models. It does not rewrite the earlier
failed workflow checkpoint as a pass.

Final project: `relay-90mm-revision01/completed-project/` in the evidence root.
Board SHA-256: `d44c95c0702850432254616df85177253a6c31e578dc0a09e283e340460dd123`.
Session SHA-256: `fdebd989901ab44ec21ed9f3133ec2e3ce0cb0eb00f1101321f19a6ccc06ac2e`.
Proof: `relay-90mm-revision01/final-validation02/proof.json`, including independent
pad/pose, path, plane, native-reference and preview checks. Electrical load,
thermal and hardware qualification remain open.

A separate compact floorplan trial on the original outline also remains
rejected: three physical blockers, 52 unresolved references and 136 conflicts.
Its fonts and original source were preserved. This is evidence about that
specific trial, not proof that every possible layout at that size is impossible.
See `relay-compact12/proof.json`. The accepted 90 mm revision remains separate.
The later scope approval is recorded in
`relay-90mm-revision01/final-validation02/scope-approval.json`, bound to the
accepted board hash. Historical trial records retain their status at execution.

## Continue the 438-part DAC

The first attempt used committed source `711af1f`. Subsequent controlled runs
use the installed candidate built from `1b74580`; all 229 package files match
its archive, wheel, sdist and isolated install. They remain identical at
`fc80597`. Wheel SHA-256:
`e179ac9127491d1ebd1852716ec8c63eaa038bf99dee5046e9b494a1438183e5`.
The saved 438-part / 1,220-pad placement is frozen, including its 300 × 220 mm
outline, pad-net identities and two internal GND planes. A stock Freerouting
2.5 run used one router CPU, a 384 MiB Java heap, the 2 GiB host reserve and
2.5 GiB owned-job cap. Native work remains serial.

The workflow parent previously shared the router's 1,800-second maximum,
leaving no postprocessing time for a full-budget route. The follow-up source
change reserves a bounded 600 seconds for native import/refill/DRC, for a
2,400-second parent maximum. The DAC attempt used the frozen
`711af1f` runtime. The fix is committed as `89a6a0f`; 67 focused workflow,
route-gate, generated-report and plane tests pass, including the maximum-router
deadline regression. Ruff passes. The pure test batch used a 125 MiB cap and
peaked at 60,448,768 bytes while the sole native routing job continued.

That attempt reached the old 1,800-second parent timeout with no final SES.
The frozen inputs were preserved. Peak owned memory was 611,397,632 bytes;
this was a stage timeout, not a resource-floor stop. The virtualenv launcher's
termination left its Java descendant alive. Its exact PID, creation time and
attempt path were verified before stopping it; the supervisor then confirmed
zero remaining owned processes. No routed board or assembly was accepted.

The follow-up now owns each CLI process tree, assigning Windows children while
suspended and terminating their job on timeout or failure. Real-process tests
cover a launcher/grandchild timeout, an unrelated live control, closed stdin,
launch failure and failed assignment before execution. This addresses the
observed resource leak without weakening circuit acceptance. The expanded
workflow/process regression passes 91 tests; peak owned memory was 94,769,152
bytes and no owned children remained after the batch.

The frozen DAC now meets those native CAD acceptance gates. Controlled routing
without prefilled planes retained partial copper instead of discarding each
incomplete attempt. Native checks accepted progress from 224 to 105 to 94 opens,
with no other DRC blockers. A private bounded geometry helper completed the
remaining 94 explicit same-net links, accepting each only after fresh native
DRC strictly reduced the open count without another blocker. It preserved all
footprint poses, pad geometry and reference fields. This helper and the private
checkpoint adapter are retained diagnostics, not a promoted default router.

The fully connected no-plane board exported a complete SES using stock
Freerouting with routing, fanout and optimization disabled. The public route
import gate then imported that full session onto the frozen placement with
both authored GND-plane definitions, refilled them and passed fresh DRC.
The final board was explicitly adopted and completed the normal preview stage;
the historical failed workflow remains failed. This controlled CAD completion
does not establish unattended completion of the default one-shot workflow.

Read-only independent acceptance passes on the exact saved board:

- 438 footprints, 1,220 pads, unchanged native placement and pad-net geometry;
- all 110 authored paths, 144 filtered signal legs and 72 GND return assertions;
- F.Cu / In1.Cu / In2.Cu / B.Cu, with both preserved GND planes filled, connected
  and free of reported islands or pending GND pads;
- zero fresh native DRC blockers;
- actual assembly SVG bound to that board, with 438/438 represented components,
  438/438 resolved models and zero missing entries.

Final project: `dac-completed-project10/` in the evidence root.
Board SHA-256: `1e2c1feebaf58ba644530f202a04150458b08ff0000e6f17b574d71050537a8e`.
Session SHA-256: `b6cb63ca5908e42417366810b1a6aea13329d5c24271061aca1fc6c91afea7b7`.
Independent proof: `dac-final-validation13/proof.json`. The validator compares
exact archived runtime bytes, retaining Windows archive line endings, and
pins its source, native files and original contract snapshot before execution.
Its native geometry, contract, plane, DRC and preview gates are unchanged.
Source inputs remain unchanged. The repair, final import/preview and independent
validation jobs peaked at 285,409,280 / 606,437,376 / 305,614,848 bytes respectively;
all terminated with zero remaining owned processes.

Electrical, timing, thermal, impedance and hardware qualification remain open.

## Fresh public workflow

The October 27 continuation promotes bounded native recovery into the public
CLI/Python/shared workflow as an explicit option. It preserves source settings,
pad/footprint/text identity and existing copper, derives dimensions from authored
netclasses and board minimums, and accepts each proposed connection only after
fresh native DRC reduces opens with no other blocker. Partial boards remain
private evidence. Search uses bounded coarse/fine grids and sweeps fresh pairs;
timeouts retain terminal progress instead of leaving a running history.

The first fresh installed DAC run used one global pass. It reached 224 opens
and then 63 after bounded native recovery, but exhausted its time budget and
published no completed board. That failure remains retained. A second fresh
run uses the same zero-copper placement, four-layer stack, netclasses, labels,
local footprints and authored plane definitions. It allows eight global passes
before native recovery and prepares the two planes after routing.

Stock Freerouting 2.5.0 progresses through 224 / 83 / 24 / 1 / 1 / 1 / 1 / 0
opens with zero reported violations. **No native repair or private adapter is
used in this completed DAC run.** The installed public route/preview workflow
finishes in 279 seconds, imports the full SES, refills both internal GND planes
and generates the native assembled preview. The source placement remains
unchanged. Independent native reload verifies the same retained acceptance:
438 footprints, 1,220 pads, all 110 paths, 144 filtered legs, 72 GND returns,
both connected internal planes, zero native DRC blockers and 438/438 models.

Evidence root: `automation_validation_results/routing-automation-20261027/`.

- Workflow: `dac-fresh02/proof.json`; final project: `dac-fresh02/project/`.
- Independent proof: `final-validation03/proof.json`.
- Board SHA-256: `836d75d892d3b00ba2198928f839948fd23835114766c7b7b5560a5df282457e`.
- Assembly SVG SHA-256: `43f2c76e0ec515ef5aca8694573c27964b7a5051572dcfa987a056b028323ad6`.
- Isolated implementation: `1d01221d43b0e4e5752b2455f04f5e21c744858f`;
  all 232 package files match the committed archive, wheel, sdist and install.
  Wheel SHA-256: `2e3faa2f56c5636ce260a4df72abbeaf91df4b814906b3cbf9e05bd76c8e7ae4`.
- Focused regression: 223 passed; the final evidence-hash adjustment also passes
  the recovery-only batch. Ruff and whitespace checks pass.
- Workflow / independent-check peak owned memory: 639,225,856 / 292,323,328 bytes;
  both jobs finish with zero owned descendants under the unchanged reserve/cap.

`report.html` compares the actual previous controlled preview with this fresh
automated result. The first independent-check invocation rejected a mistyped
expected archive hash before native execution; its failed record is retained,
and a new helper pins the measured unchanged archive. Acceptance gates were not
relaxed. This establishes the explicit recipe on this frozen DAC, not universal
default-router completion or electrical/hardware qualification.

### Native recovery and session handoff

The 96-part relay exposes a separate transport failure. Eight stock-router
passes leave 12 opens, and automatic native recovery closes all 12 with clean
KiCad DRC. The earlier handoff then reroutes that completed board while trying
to serialize it, producing a different Freerouting connectivity/clearance result.
The rejected attempt remains under `relay-fresh02/`; no final board is published.

Implementation `4d6835c3a9d0e20121e49bfbbe6a780b9fb6d227` instead exports the
native DSN unchanged and uses stock Freerouting with autorouting, fanout and
optimization disabled. It requires a verified 2.5+ router, strict full-net and
design correlation, one shared deadline and publication without overwriting an
existing file. Export statistics remain explicitly unavailable. The original
board still receives the complete session through the same fresh native DRC gate.

The fresh installed relay workflow now completes in 86.662 seconds, including
all 12 automatic repairs, the original-board import, authored GND-plane refill
and assembled preview. Independent acceptance passes on 96 footprints, 427 pads,
all 48 output paths (96 signal segments), unchanged placement and labels, saved
connected In1.Cu GND fill, zero native DRC blockers and 96/96 models. The frozen
workflow plan contains two explicit route contracts; the separate frozen native
verifier checks the full 48-path circuit. Neither coverage set is substituted
for the other. The approved 90 × 90 mm scope remains unchanged.

- Workflow: `relay-fresh03/proof.json`; independent proof:
  `relay-final-validation05/proof.json` in the same routing-automation evidence root.
- Board SHA-256: `03b242017ae3ff27c05037da0ebbb6ba33292e30338abd703cea608f2fb703a0`.
- SVG SHA-256: `ea3373fd152438cadba57fd41b01becba89c97879bcad320565a5fe16612c43d`.
- Exact installed wheel: `4ccebcf5a09dee18abc72778276901059a757bd6d84f850e246e857a1e32f1ad`;
  all 232 package files verified against archive, wheel, sdist and install.
- Final focused regression: 228 passed; Ruff and whitespace checks pass.
- Workflow / independent-check peak owned memory: 582,668,288 / 213,573,632 bytes;
  all owned processes are reaped at terminal state.

The earlier one-pass relay trial remains partial at three opens. Native repair
adds copper without ripping up congestion; the explicit global pass budget
therefore matters. This provides measured full-workflow evidence for the approved
relay revision, while the original outline and electrical/hardware qualification
remain open as recorded above.

### Repeated DAC run on the transport fix

The exact installed `4d6835c` implementation also repeats the DAC recipe from
the frozen zero-copper placement. It completes routing and preview in 287.015
seconds without native repairs, then independently passes the unchanged
438-footprint / 1,220-pad, 110-path / 144-leg / 72-return, two-plane, DRC and
assembly gates. Thus both global completion and automatic recovery/handoff
have real installed workflow evidence; neither result substitutes for the other.

The repeat is retained in `dac-fresh03/proof.json` and
`final-validation04/proof.json`, with the side-by-side preview at
`report-final.html`. Board SHA-256:
`fae11b54b47495648348d4a3262a2825d763e09f195e20c1986c61b04439669b`.
SVG SHA-256: `a1c9f186f488c201f1e34b4e0a221470caf292de6151c160e1823f51e3602d3f`.
Workflow / independent-check peaks are 632,590,336 / 293,748,736 bytes, with a
minimum observed host reserve of 3.87 GiB and zero remaining owned processes.
The first passing run and all rejected attempts remain separate evidence.

### Fresh Python 3.10 MCP repeat and current native bindings

Implementation `63c220df07af92c510acfb8d688cf983d6d1250d` adds supported
Python 3.10 MCP error-envelope compatibility and KiCad 8/9 plane-connectivity
binding support. A fresh Python 3.10.21 environment installs the committed wheel
with MCP 1.30.0; all 232 package files match the archive, wheel and sdist.
Through real stdio MCP from the circuit directory, it adopts and plans the
frozen zero-copper DAC, runs routing and assembly preview, then verifies status
and an unchanged completed checkpoint after resume. The complete supervised run
takes 277.089 seconds. Independent native reload again passes all 438 footprints,
1,220 pads, 110 paths, 144 filtered legs, 72 GND returns, both saved connected
inner GND planes, zero native DRC blockers and 438/438 models.

The same installed package repeats the approved 90 mm relay workflow in 80.661
seconds, including 12 accepted automatic native repairs. Independent acceptance
again passes 96 footprints, 427 pads, both authored route contracts, all 48
output paths / 96 segments, original placement and labels, the saved connected
In1.Cu GND plane, zero native DRC blockers and 96/96 models. The larger frozen
268-part relay benchmark remains a separate case and gate.

- DAC workflow: `dac-mcp03/proof.json`; independent proof:
  `final-validation07/proof.json` in the same evidence root.
- DAC board SHA-256: `b74d3c1e7248cb96fe50eb7149e489689d109b39be06a77e9f408160abf57ebf`.
- DAC SVG SHA-256: `2bd03928e499f76b7f3f249a96b30e87a0c3f77f0ab4fad45d2d4c93f9ab359e`.
- Relay workflow: `relay-fresh04/proof.json`; independent proof:
  `relay-final-validation07/proof.json`.
- Relay board SHA-256: `cb4145820b14541c9bcaa619ae073888053e1c055c01cbfee91b58c5b7188b86`.
- Relay SVG SHA-256: `49db3c824d1ff75d73b41a6d7e7d0df369086c64a2d5d4c4192975cca3ac7ef5`.
- Installed wheel SHA-256: `035c2cda20aafb230a54080a22cdfb8ecf38949444443c0d9dc8aefb862daa89`.
- Workflow / independent-check peaks are 725,798,912 / 294,207,488 bytes for the
  DAC and 581,079,040 / 214,454,272 bytes for the relay. Minimum observed free RAM
  during the workflows is 2.794 / 3.079 GiB; all jobs finish with zero owned
  descendants under the unchanged 2 GiB reserve and 2.5 GiB owned-job cap.

`report-current.html` compares the actual controlled DAC SVG with the fresh
MCP output and includes the relay SVG. `validated-routing-current-20261027.zip`
retains 110 hash-verified evidence files, including the exact package artifacts
(SHA-256 `6c09b61ae805657201a4219b7e34ff6e815572f312bb03b5932f5da890b2220c`).
These are local KiCad 10.0.4 CAD proofs; actual KiCad 8/9 compatibility and the
remaining benchmark cases still require their fresh CI gates. Electrical,
impedance, fabrication and hardware qualification remain unverified.
