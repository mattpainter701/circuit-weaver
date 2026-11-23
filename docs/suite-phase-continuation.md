# Phase continuation — October 25, 2026

This continues draft PR #39 after the [pre-merge review](suite-premerge-review.md).
The next native target is the retained 96-part relay workflow. Source work
advances the independent phase 3/5 integration item while preserving native
memory limits and earlier failed attempts.

## Authored current checks in the normal workflow

The optional `operating_cases` spec document is now evaluated by
`validate_design`, CLI validation, MCP validation and shared workflow validation.
It resolves each ID from a validated evidence ledger, matches the case and
load/boundary subject plus exact numeric value and mA unit, and preserves
confidence, freshness, conflicts and supersession checks. A source content
hash is required; this remains provenance supplied by the author, not source
authentication or a physical component/rail mapping.

A supported overload is an electrical error before artifact generation.
Unknown ratings remain warnings and are rejected by strict validation.
Successful workflow checkpoints retain the structured current result.
Input/result hashes and ledger links survive CLI and MCP reports. No new MCP
tool or client registration is required. See [the schema and limits](operating-cases.md).

The integration began at `07c6a1fe3ab5c4e62dfd52682a9c0eed249834f6`;
the final implementation, including canonical preservation and native failure
retention, is `f4b08d90ba9ff17107afd3400ceb38186232c3e7`. A serial focused regression passed
166 tests across current cases, evidence policy/calculations, MCP pagination,
workflow state/generated reports, canonical conversions and execution policy.
A disjoint repair regression passed 65 tests. Ruff passes. Canonical/engine
round trips and automatic repair retain the current document and ledger;
semantic diffs expose changes instead of silently dropping this intent.

A fresh archive, wheel, sdist and isolated install match all 228 package files.
The wheel SHA-256 is
`b6ff5e64a73e6216e20f8f0597e0a46e40804036d755e6efa4e712267b5485cf`.
Ten installed Python/CLI/real stdio MCP operations passed, with 18 declared
tools: synthetic nominal/overload/unknown controls, matching CLI/MCP overload
reports, canonical round-trip validation, a nested-engine overload, project
creation, blocked workflow validation and persisted failed status.
These synthetic controls do not establish ratings for the actual
relay circuit. Actual corpus current envelopes, device qualification and the
full phase 3/7 engineering goals remain open.

Evidence is retained under
`automation_validation_results/phase-continuation-20261025/`: `candidate02/proof.json`,
`canonical-regression03`, `repair-regression01`, `installed-operating02/proof.json`
and bounded job records.
The initial frozen-message mutation test failure was corrected before the
passing batch; the failed attempt remains retained.

The continuation also retains a rejected native subprocess's full structured
result in its failed workflow checkpoint, with a concise public error. The
source regression covers this recovery behavior; the original failure below
predates that fix.

## Native continuation

The native project is the retained mutable
`roadmap-followup-20261024/native-relay01/relay_output_48/project`.
Its initial status was interrupted placement after completed validation,
generation and native build. The new installed package resumes that saved
workflow with frozen spec/prototype hashes and the existing 2 GiB host reserve,
2.5 GiB owned-job cap, one router CPU and serial native execution.

Native acceptance still requires complete placement/routing, the saved
four-layer stack, authored route paths, DRC, plane connectivity and actual model
coverage in the assembled preview. Initial headroom observations and every
launched attempt remain separate evidence. No electrical or manufacturing
qualification follows merely from native workflow completion.

One native resume actually ran with the first installed candidate. It completed
with an error at placement: no candidate passed the physical DRC/reference-label
acceptance gate. It did not hit the resource floor; peak owned job memory was
277,721,088 bytes and all owned processes were reaped. The frozen design and
prototype hashes were preserved. No placed/routed board or new assembled preview
was accepted. The old runner discarded the detailed child result, which motivated
the checkpoint diagnostic-retention fix above.

Two subsequent bounded diagnostic headroom windows expired without launching
another native job as host memory fell below the reserve. That leaves the exact
placement conflict unclassified; this is not proof of a specific clearance or
label defect, nor a routing failure. The next native attempt should use the
final installed candidate and retain its full rejection payload, resolve the
actual placement issue, then continue through routing and all final CAD gates.

The [October 27 circuit continuation](suite-circuit-completion.md) supersedes
that unclassified diagnosis with retained native label conflicts and optimizer
geometry failures, trims generated fixture copies without dropping coverage,
and continues the existing DAC route.
