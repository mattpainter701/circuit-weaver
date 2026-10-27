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

The next floorplan revision depends on whether the generated outline is a
mechanical requirement. A larger outline must be an explicit circuit revision,
with the original attempt retained. Completion still requires all 96 parts,
all 48 functional output paths, physical/reference acceptance, complete routing,
four copper layers, saved GND fill, fresh DRC and full assembly coverage.

## Continue the 438-part DAC

The installed candidate was built from committed source `711af1f`; all 228
package files match the archive, wheel, sdist and isolated install. Wheel
SHA-256: `6522fb6604f0475d332a712e34db8f90651cad6a60cba6dd161382dd431e702c`.
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

Final acceptance requires the exact routed board, session and assembly preview;
unchanged placement/pads; all 110 authored paths, 144 filtered signal legs and
72 GND returns; both preserved and connected internal planes; fresh zero-blocker
DRC; and 438/438 represented components and resolved models. An active routing
process, generated DSN or incomplete candidate does not satisfy these gates.
Electrical, timing, thermal, impedance and hardware qualification remain open.
