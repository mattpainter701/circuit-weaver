# Enhancement execution, October 23, 2026

This is the first executed wave of the [suite roadmap](suite-enhancement-roadmap.md), continuing PR #39. Local evidence lives in `automation_validation_results/suite-enhancements-20261023`; original failures and executed inputs are retained unchanged. It does not establish arbitrary circuit, RF, hardware or manufacturing qualification.

| Workflow | Measured result |
| --- | --- |
| 236-part SDR carrier | The prior 260 × 190 mm native prototype passed placement and routing with zero incomplete connections and zero clearance violations; native KiCad 10.0.4 DRC passed with zero blockers. The final routed board retained all 8 analog-channel and 74 signal-path contracts. |
| SDR schematic presentation | Independent analysis of final emitted sheets found 24 body crossings before the marker fix and zero afterward, with zero symbol overlaps. A separate fresh validate/generate/native build at 180 × 140 mm passed in approximately 59 seconds; this is not the routed preview snapshot. |
| SDR assembled preview | All 236 component models resolve and the native assembled SVG renders. One missing stock LFCSP model uses a documented generic dimensioned family envelope generated from pinned KiCad sources; it is not exact AD9528 manufacturer CAD. |
| 150-part BLE node | Native placement passed. Routing reached three open connections before the supervisor stopped the owned job at its 2 GiB free-RAM reserve. It is incomplete; no routed-board or assembled acceptance is claimed. |
| 438-part DAC carrier | Native placement passed after rejecting the first candidate and accepting the second with no remaining conflicts. Routing reached 269 open items after its first pass, then produced no verified usable session within the 300-second routing limit. The workflow remained failed and published no routed board. Total owned-job runtime was approximately 443 seconds, with a 1.06 GB peak job memory counter. |
| Active electrical suite | Five ngspice 47 cases passed: two nominal supply/load combinations, an under-supply control, a diode operating point and a same-topology swapped-pin control. Generic authored models are explicitly distinguished from manufacturer-qualified behavior. |
| Passive electrical regression | Ten cases passed, including all channel vectors of the 640-channel/1,280-passive IF network, analytic transfer functions and intentional mismatch controls. |
| Directory lifecycle and library imports | Both source and fresh-installed CLI/Python/real stdio MCP trials passed 16 operations with 17 registered MCP tools: start/open/run/resume, source-preserving adoption, planned local imports, apply and exact replay. The generated IoT schematic reported zero ERC errors and 26 warnings; this is not warning-free schematic approval. |
| Committed package and fresh runtime | A wheel and sdist built from code commit `fd7c878` installed in a new venv with MCP 1.30.0. All 221 package files matched committed source, wheel and installation. The installed package also passed all five generic active SPICE cases and rendered the routed SDR assembly with all 236 models resolved. This is an unreleased local candidate, not a new PyPI version. |

The final focused local batches passed 355 tests: 130 project/lifecycle checks, 51 schematic/layout checks and 174 electrical/library/MCP/model checks. Ruff, generated capability documentation and the electrical benchmark baseline passed. The freshly regenerated SDR board also retained all 8 analog-channel and 74 signal-path contracts. These are focused local checks, not a new full cross-platform CI run.

The native SDR router session records 994 traces, 231 vias and approximately 13.65 m of summed wire centerlines. This omits via barrels and copper inside pads. Differential clock matching, impedance, return geometry, power-loop quality and jitter are not qualified. The assembled view also shows substantial empty space and uneven placement density; visual and electrical placement quality remain explicit next gates.

## Failures used to improve the product

- Safe local-wire routing alone did not remove the 24 crossings. The actual cause was power-marker stubs from rotated support capacitors crossing adjacent bodies; both wire and marker geometry now participate in detour selection.
- The first active op-amp model used a nonportable clamp expression and produced an impossible output. An independent oracle rejected it. The corrected clamp and suite reporting distinguish expected physical saturation from failed nominal acceptance limits.
- Generation initially included its own new CAD outputs in its input snapshot, making immediate resume stale. Generated output and internal attempts are now excluded from generation dependencies; external project CAD changes still invalidate them.
- Real directory trials found that unrelated ancestor manifests and project markers could absorb a selected circuit. Recorded ownership and explicit local boundaries now control selection, while canonical generated output folders retain their parent workflow.
- A standalone preview diagnostic recorded a failed checkpoint in an unrelated ancestor manifest. Its full pre-repair state is retained; only that task-owned checkpoint was removed, with other project fields verified unchanged. Separate output roots now receive their own checkpoints.
- Local library tests exposed multi-entry table serialization, replay and rollback gaps. Imports now preserve tables, retain receipts, reject altered source/installed bytes and stale plans, and roll back a failure after the first table replacement.
- Final review also rejected source paths through symlinked ancestors, prevented a matching descendant state from overriding a directly selected circuit directory, and rejected boolean values in legacy scalar simulation targets.

Native jobs run serially with the existing 2 GiB reserve and 2.5 GiB cap. Small Python batches use a 1 GiB reserve and 384 MiB cap when needed. Supervisor records prove owned descendants were reaped. GitHub Actions budget and fresh green final-head CI remain external merge/release gates.

The retained fresh-install evidence is `candidate01/proof.json`,
`fresh-surfaces01/proof.json`, `fresh-active01/active-suite-report.json` and
`fresh-preview01-job/stdout.log`. The preview itself is
`fresh-assembled01/board-preview.svg`. These paths are relative to the local
evidence root above; native source boards were unchanged.
