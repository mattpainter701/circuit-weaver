# Suite pre-merge review — October 25, 2026

## Decision and scope

Keep [PR #39](https://github.com/mattpainter701/kicad_automations/pull/39) draft.
It is the only open PR in this repository. All returned earlier PRs are closed;
all except #4 are recorded as merged. There are no pending sibling PRs to consolidate.
The local worktrees for older releases and CI fixes are retained history, not
additional pending releases. Do not reopen or cherry-pick them merely because
their commits differ after squash merges.

The reviewed candidate was `3287cef3ee94d6d2f0bdb645157085bc9e05092a`, compared
with fetched `origin/main` at `fcd3797a5ecd32d7ec4b668ee2fef3ee4fa04952`.
It contains 32 commits and changes 206 files, including substantial generated
benchmark inputs and connectivity contracts. The review considered the PR
history and body, its complete changed-file inventory, roadmap/work packages,
acceptance aggregation, workflow integration, CI definitions and current check
diagnostics. It is not an exhaustive correctness proof for every changed line.

Continue refining this single candidate, with small internal commits and a
stable acceptance target. Splitting it into more release PRs would duplicate
integration work and CI without resolving the missing engineering evidence.
The existing [eight-phase roadmap](suite-enhancement-roadmap.md) remains the
source of the intended outcome; merged PR counts and unit-test counts do not
complete a phase.

## Phase closure ledger

| Phase | Retained evidence | Required before declaring the phase complete |
| --- | --- | --- |
| 1. Representative native boards | A 236-part SDR CAD route and assembled preview; BLE placement | Complete BLE routing beyond its retained three opens; check authored route constraints and native DRC on the exact saved boards. SDR clock-skew diagnostic gaps remain explicit. |
| 2. Schematic presentation | SDR crossings reduced from 24 to zero with regeneration/build | Extend independent emitted-sheet and pad-net checks across each accepted family. One clean schematic is not corpus coverage. |
| 3. Active electrical behavior | Generic active/passive/transient nominal and fault controls; authored-current Python checker | Integrate operating-case checks into shared validation/CLI/MCP; qualify device-specific models and actual load/source/connector/distribution envelopes. All ten actual corpus operating cases remain unverified. |
| 4. Library onboarding | Transactional hash-bound local imports and private footprint lookup; native exact-board library/DRC comparison | Demonstrate the promised symbol/model/device onboarding scope with negative controls and installed native workflows. Asset existence or declared provenance alone does not qualify a device. |
| 5. Reproducible continuation | Shared lifecycle, lock/liveness checks, hash-bound checkpoints and real stdio MCP lifecycle trials | Repeat installed native interruption/resume and complete workflows across the acceptance families. Lifecycle calls alone do not prove a model-driven full design conversation. |
| 6. Routing scale | 25-part four-layer CAD case; accepted earlier SDR; 438-part DAC placement | First finish the 96-part ladder, then 438/466/482-part native routes and the remaining family cases. The 438-part DAC has no accepted final SES/board/preview. Prove all 110 authored paths, 144 filtered output legs and 72 GND return assertions; verify final planes and DRC. The 1,202-part import does not establish routing. |
| 7. Practical families | Six-family preflight corpus and generic family transient examples | Complete native CAD cases and independently modeled faults/limits for the actual operating circuits. The relay's conditional 23.04 A arithmetic is not a verified operating case. RF, timing, thermal, impedance and return-path qualification remain separate open gates. |
| 8. Release validation | Earlier exact-wheel/install/client proofs and focused local tests | Build and verify the final committed candidate, repeat installed interfaces and required native acceptance, then obtain fresh required green CI for its exact SHA. No merge, version tag or publication while those gates remain open. |

The [follow-up evidence](suite-roadmap-followup.md) documents the library memory
improvement and the still-incomplete 96-part continuation. The later DAC handoff
is retained under `automation_validation_results/full-dac-routing-20261024/`:
the native checkpoint adapter compiled, but its geometry restore and final
native routing have not been verified. Do not promote it into production merely
because compilation succeeded.

## Findings corrected in this review

1. **Acceptance aggregation could conceal failures.** `_acceptance_status`
   checked phase failures but not failed qualification gates; all-passed partial
   phases and an empty gate dictionary could also satisfy its pass expression.
   CAD aggregation could pass from gate summaries without the complete named
   stage sequence. Reports now require all seven stages exactly once, all
   required gates, and the relevant negative controls. An explicit failure
   remains failed and makes the runner exit nonzero. Unknown evidence remains
   unverified. CAD completion remains distinct from product qualification.
2. **The new shared four-layer acceptance runner was absent from native CI.**
   KiCad 10 now runs the small `iot_environment_node` through installed
   validation, generation, build, placement, routing and preview, with
   `--require-cad`. A process exiting successfully with unverified CAD evidence
   no longer makes this acceptance step green. Evidence stays in the existing
   always-uploaded native artifact. This is a regression floor, not full
   corpus or larger-board acceptance.
3. **Design validation ran twice for a pushed open-PR branch.** Its push trigger
   now covers `main`; PR revisions retain the PR trigger. Concurrency cancels
   superseded revisions, permissions are read-only, and runtime is bounded.
4. **Expensive jobs could consume resources before prerequisite checks passed.**
   The ladder is now source/bundled skills → distribution → exact-wheel matrix
   → native KiCad matrix → unseen large-board matrix. Native matrix concurrency
   is one. Existing Python/OS/KiCad matrices and native assertions remain.

## CI resource decision

The inspected KiCad 10 check at the reviewed SHA explicitly reports:
“The job was not started because an Actions budget is preventing further use.”
The head has 14 failed check records and two skipped placeholders; these records
are not proof of 14 code-test failures. See the
[exact-head run](https://github.com/mattpainter701/kicad_automations/actions/runs/40141526254).
Branch-protection details were inaccessible to the connected integration, so
this review does not claim to enumerate the authoritative required-check list.

Consolidation is already achieved at the PR level. The workflow changes reduce
duplicate runs and avoid expensive downstream work when prerequisites fail.
Serial native execution reduces concurrent demand; it does not reduce all
successful-run minutes, and retaining both full source and wheel matrices still
has a substantial cost. No exact cost reduction is claimed without measured
successful runs. These changes do not restore an exhausted Actions budget.
Restored runner capacity/budget and fresh successful checks are still required.
Do not waive checks or substitute old/local results for final-head CI.

## Candidate freeze and merge checklist

- Keep one reviewed candidate and record each remaining result against an exact
  source SHA, package hash, input hash, native tool version and saved board hash.
- Keep failed, unverified and resource-stopped attempts visible. Never change
  the 2 GiB native host reserve or 2.5 GiB owned-job cap to manufacture a pass.
- Finish the phase ledger above or explicitly agree to a narrower release
  scope before changing the completion claim. Partial phase work must remain
  labeled partial in PR/release documentation.
- Run focused regressions, then package/install/client verification at the
  final commit. Require the new native CAD gate and the existing merge checks.
- Fetch current `origin/main`, resolve any conflicts semantically, inspect the
  resulting diff and revalidate the final head. Merge only after fresh required
  checks are green; verify the exact merge commit before retiring the worktree.

## Review validation

The final focused regression batch passed 90 tests across acceptance assets,
controls, corpus, runner aggregation/exit status, CI dependency/trigger policy,
release contracts, route publication and stackup/plane workflow gates. Ruff
passed for `src`, `tests` and the acceptance runner. A real ten-case preflight
passed with 30 detected CAD faults and ten intentionally unverified limit
controls; all ten full-acceptance results remained unverified. A real strict
preflight-only invocation exited 1 as required. These are local source
checks; the changed GitHub workflow and new native CI step have not executed
successfully under GitHub Actions yet. Evidence is retained separately under
`automation_validation_results/pr-review-20261025/`.
