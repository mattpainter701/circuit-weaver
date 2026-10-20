# Changelog

## [Unreleased]

- Assign interchangeable native connector slots using their private-net support-family pad endpoints, preserving references, pad nets, occupied geometry and authored mechanical constraints. Accept only an improved placement rank and retain native label and DRC gates.
- Explicitly disable the optimizer for zero-thread native routing on Freerouting 2.4+, honor modern optimizer thread caps, and map or reject changed optimizer modes on Freerouting 2.5+.
- Bound deterministic placement refinement per restart and report its evaluation budgets. Try a topology-derived starting layout only when it improves the existing constraint rank, preserving authored mechanical constraints and support-family geometry.
- Start reference-aware native placement with spacing derived from actual text dimensions, retaining larger authored gaps and subsequent native label and DRC checks.
- Shorten generation staging and rollback paths for deeply nested Windows projects.
- Shorten schematic-to-board staging paths for deeply nested Windows projects, retaining complete copied hierarchies and native build verification.
- Derive scaled gateway ADC filter contracts from their owning channels and verify generated filter pad nets before native workflow runs.
- Retain router timeout output and log portfolio candidate progress while continuing to discard partial or unverified sessions.
- Scale the bounded native placement-worker timeout with footprint count so larger boards can complete the existing DRC and reference-label checks.
- Split port-heavy schematic hierarchies before their top-level symbols exceed standard pages. Partition connector annotations without duplicating or dropping notes, and fail closed when a hierarchy cannot fit.
- Page large MCP validation reports without losing findings, audit fields, or provenance IDs. Bind pages to a report digest, verify complete collection counts and hashes, and reject oversized individual records explicitly.
- Add native signal-path contracts and higher-count fieldbus, relay-output, and analog-setpoint workflow fixtures, with individual physical-part floors and retained routing, model-coverage, label, and delivery gates.
- Document fresh OpenCode environment binding so shell commands and stdio MCP use the same installed package, and preserve explicit prototype drill assumptions.

## [0.37.0] - 2026-10-20

### Physical sample inventory and workflow scale

- Give logical I²C pull-up containers an explicit nonphysical identity while retaining their two real resistors in schematics, assembly inventories, and native boards. Keep missing physical footprints as errors, and preserve distinct references for multi-component templates.
- Correct MAX17048 TDFN and nRF52840 aQFN footprint bindings and physical pin maps, include the Nordic crystal/DEC support, and bind the OLED sample to a real Adafruit module and model. Verify all sample physical inventories with native KiCad.
- Retain foreign-net protection on dense schematic support routes and use visible net labels when no wire corridor exists. Fit support parts beside large module courtyards without expanding the outline, and permit an explicit new-project minimum drill assumption while preserving existing project rules and full DRC.
- Correct DAC8552 package pins, passive header electrical types and reference-voltage operating limits; keep moved schematic labels clear of unrelated wires and recognize real series-filter interfaces.
- Retain raw router findings and compare exact pre-existing same-pad thermal-land overlaps against native identities, followed by full imported-board DRC. Supply the missing dimensioned TDFN STEP with pinned KiCad generator provenance and license.
- Reduce repeated placement and label searches with conservative spatial candidates followed by the existing exact geometry checks. Add reproducible full-workflow corpus and diverse 100-part benchmark evidence, including package identity, native routing and assembled model coverage.
- Use native label failures to increase placement spacing from real reference dimensions on later attempts. Retain authored minimum gaps, font sizes, board constraints and the three-attempt limit; incomplete native reports fail closed.
- Preserve the selected virtual-environment interpreter for workflow and MCP subprocesses. Validate all 13 shipped samples and three connected 145–155-part benchmark boards through 16 prototype stages, including native routing, DRC, assembled previews and real MCP sessions; these finite fixtures do not establish arbitrary-circuit, RF or fabrication correctness.
- Clarify the supported agent entry points and prototype validation scope, including the need for separate controlled-impedance and RF engineering verification.
- Keep MCP validation responses compact by default without dropping findings or evidence IDs. Offer explicit full-detail retrieval for the embedded evidence ledger, after a real Claude Code check reproduced a tool-output overflow on the IoT example.

## [0.36.0] - 2026-10-19

### Plane-backed ground trace cleanup

- Remove redundant unlocked traces whose complete copper shape is covered by a same-net filled plane, while retaining every via, protected return and decoupling route, other-net copper, placement and labels. Native refill, saved coverage/connectivity verification and full DRC gate the result.
- Add an optional workflow stage after plane creation, retain the grounded intermediate board and render the final cleaned assembled preview.

### Filled ground planes with preserved routing

- Add an explicit native ground-plane command with thermal pad connections, island removal and saved fill/connectivity verification. Retain existing tracks, vias, placement and labels, and keep protected trace-only return and decoupling paths unchanged.
- Allow automated workflows to apply a ground-plane plan after routing cleanup, retain every intermediate board and render the final assembled board from the verified filled PCB.

### Protected copper cleanup

- Add explicit selected-net copper cleanup that removes redundant segments and searches for shared via pairs while keeping protected pad-to-pad paths within their original lengths. Retain component placement, labels, locked copper and unselected nets; native geometry, connectivity and full DRC gate publication.
- Allow automated workflows to retain the routed and refined intermediate boards, apply an optional cleanup plan, and render the final cleaned assembled board with bound input and output hashes.

### Local return-path refinement

- Add an explicit `refine-pcb` connection-plan workflow that creates short same-net copper links while retaining existing tracks, component positions and labels. Bounded local candidates must pass native geometry, full DRC and reference checks before atomic publication.
- Allow the automated workflow to apply an optional return-path plan after routing, retain its base board and input hashes, and render the final refined assembled board.

### Power and decoupling routing

- Retain support capacitors' intended owner pins through assembly and native placement, and consider local supply and return legs together when arranging power support parts.
- Prefer placements with nearby readable reference-label space, using native text dimensions without shrinking labels or weakening final clearance checks.
- Rank complete, reported-clear routed candidates by measured copper length, then via and segment counts, so a long detour cannot win solely by avoiding vias. Report per-net wire lengths in millimeters, with unsupported geometry clearly marked unavailable.
- Use the AP62300's documented floating-enable startup by default instead of tying its 8 V maximum enable input to a higher input rail. Explicit enable-net choices remain supported.

### Coherent support placement on larger boards

- Anchor support parts to their dedicated signal connections before shared supply rails, keeping reset pull-ups on the reset-pin side of an MCU. Spread equivalent decouplers among actual shared-rail pad endpoints with deterministic seeds and retained body clearance.
- Center and compact complete movable courtyard envelopes, including owner/support families. Try bounded family row/column alignment with a clear channel between groups, and group mixed-role supports that share a dedicated signal, such as a reset pull-up and filter capacitor.
- Give nearby native reference labels a bounded preference for clearer association with their own component. Retain font size, locked fields, strict physical checks and the first legal slot as a fallback.

### Assembled board SVG previews

- Add `preview-pcb --appearance assembly` to embed KiCad's actual component-model render in a self-contained SVG. Retain native model orientation, dimensions and source/model hashes; missing or hidden-only populated-component models and unavailable rendering fail clearly without replacing an existing preview.
- Make the automated workflow's `--render-3d` mode show the assembled board in `board-preview.svg` and keep native copper, mask and silkscreen geometry in a separate `board-layers.svg` view. Validate the embedded image against the routed board and model evidence.

### Related component placement and readable references

- Start support parts beside their owner's actual shared signal pads, retaining body clearances and a deterministic fallback when pad geometry is unavailable. Refine related signal support parts onto common rows or columns, and include support parts in compactness scoring for small designs while preserving placement constraints.
- Give native reference labels a consistent horizontal position close to their own component, with collision-checked alternatives and stable results on repeated cleanup. Preserve locked labels and report any unresolved conflicts.
- When reference cleanup has no readable slots, try up to three distinct native placement seeds privately. Publish only a candidate that passes physical DRC and reference checks, retain attempted/selected seeds, preserve source settings, and leave any existing output intact if no candidate passes.

### Repeatable local workflow validation

- Keep explicit demo board-size overrides consistent across the copied design, native board construction, and placement; preserve the source spec and retain original/effective dimensions in the evidence.
- Add an expected-wheel payload check before installed workflow commands, so an older package with the same version cannot silently count as validation of a newly built artifact. Compare packaged resources as well as code and retain artifact hashes.
- Give native routing attempts distinct placement declaration orders in isolated DSN copies, resolving a reproduced KiCad 9 export that repeatedly left one supply connection unfinished with optimization disabled. Preserve all geometry, nets, rules, original exports and caller-provided DSNs, and retain strict final-board DRC.
- Replace the STM32F103C8T6's incomplete and incorrect pin map with its full manufacturer-documented LQFP-48 definition. Correct power, boot, reset, and debug pin numbering, supply support, and dependent sample declarations while retaining strict native pin/pad reconciliation and manufacturing gates.
- Give the motor-controller demo a validated 45 × 35 mm review outline, replacing the sparse 80 × 60 mm fallback while retaining readable reference labels and clear functional groups.

### Placement intent and readable board layouts

- Preserve functional groups and support-part ownership in native `place-pcb` with a matching `--design`. Bind intent to actual footprint/net inventory, enforce explicit connector edge and rotation, and report missing access intent and excessive board whitespace.
- Optimize real courtyard centers for off-center footprints and measure support affinity from connected non-ground pads. Preserve fixed origins, and align new movable origins with the native DSN coordinate resolution so routing retains exact positions.
- Add opt-in native reference-field cleanup with retained collision diagnostics. The automated demo uses an explicit compact board outline, carries its design constraints through placement, and retains layout and silkscreen evidence alongside routed-board DRC.
- Keep component body geometry in SVG previews while omitting duplicate fabrication text overlays; native silkscreen reference labels remain visible.

### Native board and component previews

- Add `preview-pcb` front/back SVG views using actual KiCad copper, mask, silkscreen, footprint body geometry, and board outlines/cutouts, with source/output hashes and a mirrored physical underside view.
- Add optional native assembly-model PNG rendering on KiCad 9+, resolving the board's declared model files in an isolated copy and recording their coverage and hashes. Missing models fail clearly.
- Put named board-view controls and full-size previews into the automated workflow report; validate geometry-rich front/back fixtures on KiCad 8, 9, and 10. Rendering preserves source files and does not grant manufacturing approval.

### Generated schematic to routed prototype automation

- Add `build-pcb` to forward-annotate a KiCad schematic into a separate prototype board using actual library footprints and exact pin/pad reconciliation. Missing footprints or incompatible pad maps block publication; prototype generation does not grant manufacturing approval.
- Add a repeatable CLI and optional MCP workflow demo with retained command logs, native ERC, placement, Freerouting, final-board SHA-256-bound DRC, visual previews, and checked assembly/manufacturing-export gates. Run the generated-design workflow in the KiCad 8/9/10 CI matrix.
- Default native-board routing to skip post-route optimization unless explicitly requested; Freerouting 2.4.1's optimizer can narrow fine-pitch traces below imported width rules even with neckdown disabled. Native DRC continues to block invalid results.
- Add a 0.01 mm routing-only margin to generic DSN clearance rules for native-board autorouting, after reproducing a router-clean result that missed KiCad's required clearance by 0.0007 mm. Preserve typed exceptions, native board/project rules, and direct DSN inputs; final native DRC remains required.
- Keep inline MCP generation state inside its explicit output directory, so nearby projects cannot make a new generation ambiguous or acquire its artifacts. Isolate router subprocess working directories to prevent source-tree logging side effects.
- Pin official KiCad container digests for native CI and release validation, verify matching CLI/Python versions and libraries, and retain subprocess failures for diagnosis. Cancel superseded branch CI runs.
- Correct hierarchical instance paths and power/flag annotation for KiCad netlist export. Represent every USB-C power connector pad, including duplicated supply contacts and explicitly unused contacts, and keep CC resistor label wires outside the connector body. Bind the builtin USB4085's unused shield to the installed library's exact S1/SH pad spelling without renumbering connected pins. Resolve CH340G and AMS1117 to qualified standard footprints, and correct agent instructions for Windows sample copying, MCP installation, and manufacturing-readiness interpretation.

### Real-board placement and routing completion

- Add `place-pcb` for unrouted rectangular KiCad boards. Use actual courtyard envelopes and rotated pad endpoints; preserve locked parts, board sides, pad/net identity, source files, and project rules. Publish placements only after physical DRC.
- Recognize KiCad 10 named pad nets and use KiCad's native Python API when the CLI cannot export Specctra. Add `autoroute --routed-board` to import the selected session into a separate PCB and require final KiCad DRC before publishing it.
- Support current Freerouting final-score logs and optimizer strategies when seeds are unavailable. Measure missing via/trace counts from the validated session, isolate router settings, disable analytics, and prevent default fine-pitch fanout/necking from violating imported widths.
- Add real LED, divider, RC, back-side, and SOIC placement-to-routing regression circuits, including locked connectors, offset outlines, and custom project rules. Run them with pinned Freerouting in KiCad 8/9/10 CI and release validation.

### PCB placement and routing search

- Compare multiple seeded placement candidates after overlap repair and bounded fine refinement. Retain a legal baseline, prioritize constraint satisfaction, normalize shared-bus attraction, and report candidate scores and before/after component-center routing estimates.
- Preserve precise fixed coordinates and reserve fixed support parts before movable owners. Move support parts with displaced owners, honor edge constraints during legalization, apply the requested component gap once, and bound arbitrary footprint rotations conservatively.
- Add `autoroute --attempts` with seeds or optimizer strategies, a shared routing timeout, per-attempt diagnostics, best-session selection, input-change detection, and atomic publication. Failed or inferior attempts cannot replace a better session; preview rejection and the KiCad DRC requirement remain enforced.
- Add a reproducible three-design placement benchmark and document search controls, measurement limits, and runtime tradeoffs.

## [0.35.0] - 2026-08-26

### Imported-design finding trust

- Version the normalized finding interchange shape as `circuit-weaver-findings/v2`, separating stable root-cause identity from a full-content SHA-256. Deserialization now rejects field coercion and content drift, re-derives severity/confidence from observations, and never carries a serialized `verified` state into a fresh run.
- Retain every suppression/override ID during deduplication, including mixed actionable/suppressed observations and existing short authoritative IDs such as `OVR-1`, and declare the SARIF `%SRCROOT%` base used by physical locations.

### Electrical accuracy benchmark

- Replace the duplicate benchmark rule bridge with the validator-owned contract plus the legacy `power-current-budget` producer alias. Promote executable adverse/control pairs for I2C pull-ups, SPI chip select, UART pairing, and ERC single-pin nets.
- Expand the corpus to 42 fixtures and the scored inventory from 14 to 18 of 33 registered rules. The committed scorecard records 22 true positives, zero false positives, zero false negatives, and 1.000 supported precision/recall without reclassifying unsupported diagnostics as expected findings.

### Transactional imported-design repair

- Add an experimental explicit-no-connect repair service and `repair suggest|preview|apply|verify` CLI. A content-addressed `circuit-weaver-repair-metadata/v1` producer binds exact source/analyzer bytes to a validated v2 finding and deterministic evidence manifest; signed plans then bind source/object/post-image hashes, prerequisites, semantic invariants, rollback data, and the low-risk operation.
- Require an out-of-band approved plan hash and reviewer before mutation; reject path aliases, ambiguous identity, implicit pin/power/junction connectivity, stale plans, malformed evidence, and concurrent source changes. Apply uses source/audit locks, a hard-linked exact preimage, metadata-preserving same-directory publication, post-audit verification, and prepared/committed append-only audit events with interrupted-commit recovery.

### Verification status

- Local Windows/Python 3.13 source and isolated exact-wheel suites: **1892 passed, 18 classified skips** each. The one warning is the existing Starlette/httpx deprecation. CI-scoped Ruff, diff, generated capability docs, benchmark baseline, `twine check`, and wheel-payload checks pass.
- KiCad 10.0.4 loads and exports the exact-wheel transactionally repaired no-connect fixture. ERC moves from 28 to 27 findings by clearing only the intended `U1.4` pin-not-connected error; the remaining semantic finding multiset is unchanged. Existing local KiCad 10 two-layer and four-layer load/DRC goldens also pass.

## [0.34.0] - 2026-07-29

### Authoritative PCB handoff

- Freeze the placement-preview and authoritative-board contracts in code: previews remain padless and carry the review-only banner, while a separate fresh-emission path produces real library footprints, pads, authoritative nets, stable UUIDs, semantic change manifests, and `tool:pcb_handoff` provenance.
- Reuse the T247 identity handoff at the first pad-emitting boundary. Unresolved footprints, pin/pad mismatches, stale placement approvals, missing constraints, fewer than two independent identity sources, and self-reported reconciliation states fail before pad rendering or board publication.
- Exercise two-layer and four-layer authoritative boards through the KiCad load/save/DRC golden contract. Local verification proves KiCad 10, and hosted CI proves KiCad 8, 9, and 10.

### PCB constraints and transactional DRC

- Add deterministic `PCBC-<CLASS>-<12hex>` constraints compiled from normalized interfaces, power domains, fabrication profiles, manufacturer data, and user declarations. Conflicts are detected before board mutation, and emitted KiCad rules are hash-linked to the board manifest.
- Run KiCad DRC against the exact staged board bytes and reuse the shared T248 `ValidationIssue` model with stable `CW-DRC-<NNN>` IDs. Missing tools, parser drift, stale hashes, blocker findings, and interrupted publication preserve the complete last-known-good board and reports.

### Manufacturing readiness and export safety

- Replace scattered readiness booleans with one evidence-linked state machine covering identity, placement, routing, ERC, DRC, BOM/CPL reconciliation, fabrication artifacts, terminal blockers, and explicit expiring overrides. CLI, API, MCP, HTML, board manifests, and delivery manifests read the same canonical payload.
- Add `manufacturing-readiness --json` and make Gerber/drill export fail closed before output creation unless the canonical state is fabrication-ready or a complete, unexpired override is supplied.
- Revalidate read-back fabrication claims against a schema-validated evidence manifest and rerun the T244.4 gate; forged or stale readiness JSON cannot authorize export merely by naming `fabrication_ready`.
- Recompute identity reconciliation from its source assertions at handoff, preventing a canonically hashed but self-reported single-source agreement from reaching copper.
- Key footprint-geometry caching by resolution state, resolved path, and content hash, and independently reject heuristic geometry at authoritative preflight.

### Verification status

- Local Windows/Python 3.13 source suite: **1819 passed, 18 classified skips**. Ruff and diff checks pass; local KiCad 10 two-layer and four-layer load/DRC goldens pass.
- Hosted CI proves the two-layer and four-layer authoritative round trip on **KiCad 8, KiCad 9, and KiCad 10**.

## [0.33.1] - 2026-07-28

### Security

- Close evidence-safety redaction bypasses where a machine-local POSIX path is glued to a preceding dot or hyphen, or follows a bracketed remote URL. Remote HTTP(S) URLs and ordinary slash notation remain accepted.

### Validation

- Windows/Python 3.13 source suite: **1771 passed, 16 classified skips**. The isolated exact-wheel evidence/release suite passes **51 tests**; Ruff, `twine check`, and diff checks pass. One pre-existing Starlette deprecation warning remains.

## [0.33.0] - 2026-07-27

### Planning

- Define Sprints 55-60 around release-truth recovery, measurable electrical-accuracy benchmarks, evidence-backed component decisions, authoritative KiCad PCB handoff, transactional imported-design repair, sourcing resilience, and calibrated simulation/confidence outputs.
- Record the current Windows/Python 3.13 source-tree baseline (`1431 passed, 16 skipped, 7 failed, 6 errors`) as Sprint 55's first release-recovery gate instead of treating the v0.32 feature surface as implicitly complete.

### Sprint 55 execution — T241 release-gate recovery

- Keep long centered root-sheet title banners inside the page by reserving half their estimated rendered width at the left edge. This clears the `svg-left-overflow` errors in `oled_display_module`, `led_power_indicator`, and `usb_regulated_supply` and restores their dependent layout-quality fixtures.
- Isolate the generate-call-count regression from presentation validation while preserving the production fail-closed gate: a successful generation performs one validation smoke generation plus one real generation, not three.
- Make plain `python -m pytest tests -q` select and verify the checkout's `src/circuit_weaver`, propagate that identity to subprocess tests, and add an explicit exact-wheel mode that refuses checkout imports.
- Require every skip to declare `platform`, `optional-tool`, `network`, or `defect`; missing checked-in fixtures fail, and unclassified runtime or setup skips fail the session.
- Local Windows/Python 3.13 source command `python -m pytest tests -q`: **1448 passed, 16 classified skips**. Exact-wheel command `$env:CIRCUIT_WEAVER_TEST_PACKAGE='wheel'; <venv-python> -m pytest tests -q`: **1448 passed, 16 classified skips**. `python -m ruff check src tests` also passes.
- CI now builds the distribution once, uploads that exact artifact, and runs the complete wheel suite across Linux Python 3.10–3.14 and Windows Python 3.12/3.13, matching the source matrix. The tag workflow selects exact-wheel mode explicitly as well.

### Sprint 55 execution — T242 capability truth contract

- Add a single checked-in registry for all 44 top-level CLI commands and the two `cache` child operations, with fixed maturity, surface, evidence, and verification vocabularies. Python entrypoints are listed only where a public function actually exists.
- Keep the ordered design-verification ladder separate from the paired `not_applicable` state used by operational commands such as `doctor`, cache, logs, and skill installation. Default guarantees are conservative; stronger runtime claims require matching returned evidence.
- Expose copy-safe registry JSON through the Python API, `doctor --json`, and HTTP `GET /capabilities`; terminal doctor output summarizes maturity counts.
- Generate the README capability table deterministically from the registry and fail CI when the generated section drifts. Contract tests cover every dispatcher command, registry shape/vocabularies, and runtime verification overclaims.

### Sprint 55 execution — T243 electrical-accuracy benchmark

- Add a versioned 18-fixture corpus with positive and negative cases for power, clock, USB, I2C, SPI, UART, analog, protection, and manufacturing domains. Every oracle carries a stable `CW-<DOMAIN>-<NNN>` rule ID, rationale, and explicit provenance; generator-authored controls remain separate from independently authored adverse references.
- Add a real-validation benchmark runner that reports per-rule and per-domain precision, recall, false positives, false negatives, unsupported scope, and runtime. Unmapped validator findings and not-yet-implemented review cases remain visible as unsupported instead of receiving false pass credit.
- Check in a precision/recall-only baseline and CI gate. Supported power rules use the canonical `CW-PWR-*` namespace at 1.0 precision and 1.0 recall with zero regression budget; eight other domain rules are honestly recorded as unsupported for later accuracy sprints.

### Sprint 55 execution — T244 evidence manifest

- Add a versioned `evidence_manifest.json` with deterministic `EV-*` IDs for component identity, pinout and footprint provenance, critical parameters, validator findings, tool versions, and ERC verification. Retrieval timestamps do not destabilize IDs, and absent source facts remain absent rather than being inferred.
- Propagate resolvable evidence IDs through validation JSON, generated design reports, review and confidence reports, assembly and delivery manifests, HTTP responses/archives, MCP results, and the portable artifact inventory.
- Reject credentials, machine-local paths, unsafe relative references, malformed links, silent heuristic/stub upgrades, and unresolved conflicts. Fabrication-ready delivery claims now fail closed unless every represented critical subject has sufficiently trustworthy backing.
- Full Windows/Python 3.13 source suite after T244: **1525 passed, 16 classified skips**. The focused exact-wheel evidence/surface suite passes **133 tests**.

### Sprint 56 execution — T245 typed power domains

- Add omission-preserving typed power envelopes across `PowerDomain`, component `PowerReq`, and `PowerPin`: optional min/nominal/max voltage, source/load/bidirectional direction, steady/peak current, sequencing order/dependency, tolerance, provenance, and regulator dropout. Canonical and legacy ingest, component registries/caches, template parameters, DesignIR round trips, auto-repair, and resolver hydration preserve declared values without filling unknowns.
- Add conservative `CW-PWR-001` through `CW-PWR-007` checks for over-voltage, under-voltage, reverse flow, source contention, LDO/regulator dropout, steady/peak current budgets, and sequencing. A rule runs only when every required operand is present, and each finding exposes sparse unit-labelled inputs, observed/expected values, margin, remediation, and a first-class calculation evidence record.
- Materialize user-declared rail envelopes into the evidence ledger, link component and rail parameter evidence to calculations, and remove unresolved provenance tokens before serialization so every published evidence ID resolves. Power-tree Markdown, design-document CSV/Markdown, HTML review, and placement/PCB constraint context now expose the same typed envelope without rendering absent values as zero.
- Replace generic topology power-pin substring matching with prefix-exact matching plus a curated equivalence allowlist; signal names such as `MAINSVIN_SENSE` no longer bind accidentally to `VIN`.
- Expand the electrical benchmark from 18 to 27 fixtures with valid multi-rail/battery margins and independently authored adverse cases for every new power rule. `CW-PWR-001`–`CW-PWR-007` each measure **1.0 precision and 1.0 recall** with zero false positives and false negatives.
- Full Windows/Python 3.13 source suite after T245: **1551 passed, 16 classified skips**. CI-scoped Ruff and diff checks pass, the 27-fixture benchmark baseline passes, and the focused exact-wheel T245/evidence/report suite passes **72 tests**.

### Sprint 56 execution — T246.1 shared passive calculations

- Add a pure, immutable `CalculationRecord` substrate with explicit SI units, equation/version identity, deterministic `CALC-<FAMILY>-<12hex>` IDs, raw results, and frozen placeholders for later selection policy, margin, and evidence emission.
- Move feedback-divider, RC/LC-cutoff, inverse RC sizing, and crystal-load equations into `calc.py`; validator checks and the clock, ADC, DAC, sensor-front-end, audio-coupling, and legacy synthesis paths delegate through the shared module, with monkeypatch contracts proving there is no second implementation in those paths.
- Correct the pre-existing crystal synthesis drift from `2*CL - Cstray` to the validator/oracle-consistent `2*(CL - Cstray)`, accept zero idealized stray capacitance, and reject non-finite or invalid equation domains in the calculation core.
- Extend the evidence subject grammar compatibly so both existing `calc:CW-*@<REF>` validation acts and frozen `calc:<equation_id>@<REF>` calculation acts resolve.
- Full Windows/Python 3.13 source suite after T246.1: **1574 passed, 16 classified skips**. Ruff and diff checks pass; the focused exact-wheel calculation/evidence/delegation suite passes **36 tests**.

### Sprint 56 execution — T246.2 calculation evidence and T247.1 identity

- Emit pure calculation results through a separate deterministic ledger adapter as `kind=calculation` evidence, require cited inputs to resolve, preserve idempotence, and return a new `CalculationRecord` with the emitted evidence ID.
- Add the P0 identity substrate joining exact manufacturer/MPN/package, symbol pins, footprint pads, explicit one-to-one pin→pad mappings, distributor aliases, and provenance without heuristic filling.
- Reject ambiguous mappings and malformed identities while preserving real manufacturer, package, and distributor text exactly; integrated focused Wave 1 contracts pass **51 tests** with Ruff and diff checks clean.

### Sprint 56 execution — passive policy and P0 identity handoff

- Normalize passive recommendations with strict provenance and fail-closed datasheet → equation → bounded-fallback precedence; retire the validator's per-MPN feedback-Vref table in favor of sourced component metadata.
- Add deterministic E-series selection, joint ratio-preserving divider snapping, safe-direction capacitor selection, traceable datasheet/fallback factories, and self-describing `CW-PSV-*` withheld-value findings.
- Reconcile exact identity across independent sources with distinct agreement/conflict/missing/human-approved states; approvals target a specific identity and never rewrite the underlying source result.
- Block physical CPL generation unless every primary part has a validated identity bundle, complete pin→pad coverage, and an exact selected-footprint match. BOM-only output remains available when the P0 gate blocks manufacturing placement.
- Add nine executable identity adversarial/control fixtures; native reconciliation and handoff checks score `CW-ID-001` through `CW-ID-004` at 1.0 precision/recall with no unsupported identity scope.

### Sprint 56 execution — T246 producer closure

- Wire the shared calculation substrate through automatic bypass generation and representative switching/LDO, crystal, USB-C, CAN/RS-485, reset/display, and analog-filter producers. Emitted support parts retain policy, confidence, `CALC-*` identity, and resolvable calculation evidence; unprovenanced legacy parser hints remain compatibility data and are not promoted into typed recommendations.
- Select feedback dividers jointly to preserve ratio within a declared impedance window, round minimum capacitors upward, and keep conventional support values only as bounded heuristic fallbacks. Invalid or incompatible selections are withheld under `CW-PSV-001` through `CW-PSV-003` before a passive network is emitted.
- Verify ten independently reviewed passive cases through the calculation substrate and actual producers, including buck/boost/LDO, crystal, USB-C, CAN/RS-485, analog RC, and adverse fail-closed behavior. All three passive rules score 1.0 precision and recall.

### Sprint 56 execution — T248 validator calibration

- Split validator impact (`blocker|major|minor|info`) from detection confidence while retaining `level` as a derived compatibility property. Only verified/corroborated blockers render as hard defects; the verified stub-pinout safety gate remains blocking.
- Require every actionable validator finding to publish its stable rule ID, unit-labelled observation, expected constraint, resolvable evidence, and safest next action. Versioned suppressions require a narrow target, owner, reason, approval, and future expiry; they mark findings without removing them from benchmark denominators.
- Publish deterministic JSON and Markdown scorecards covering exactly 33 emitted/contract rule IDs: 14 scored and 19 explicitly unsupported. The 40-fixture supported aggregate is **1.000 precision / 1.000 recall**, above the **0.95 / 0.90** release floor, and the release workflow now runs the benchmark baseline gate.
- Full Windows/Python 3.13 source and exact installed-wheel suites after Sprint 56: **1766 passed, 16 classified skips** each. The `0.33.0` wheel and sdist pass `twine check`; wheel provenance resolves from the isolated environment. Ruff, benchmark baseline, and diff checks pass.

### T244 remediation — embedded-path safety and supersession

- Reject machine-local Windows drive, UNC, POSIX/home, and `file://` paths anywhere inside recursively inspected evidence strings, including the real `footprint:C:\Users\…\.kicad_mod` producer path and local paths hidden in HTTP query/fragment text. Direct record validation and ledger admission use the same detector; HTTP(S) userinfo credentials are also rejected.
- Restore the frozen nullable `supersedes` link across `EvidenceRecord`, builders, copies, manifests, and legacy-compatible rehydration without changing deterministic evidence IDs. Forward links load independently of manifest order; unresolved/self/cyclic links fail closed.
- Make sorted-manifest rehydration order-independent for corroborated records and prevent superseded evidence from remaining authoritative at the fabrication trust gate.
- Consolidate all emitted power-rule identities on `CW-PWR-*`; accept `CW-POWER-*` only as explicit legacy benchmark input aliases, canonicalize before scoring, and enforce the boundary with a repository namespace contract test.
- Full Windows/Python 3.13 source suite after T244.R1/R2: **1601 passed, 16 classified skips**. Ruff and diff checks pass; the focused exact-wheel evidence safety/schema suite passes **60 tests**. T244.R3 adds **48 focused passing tests** plus a green benchmark baseline gate.

### Fixed

- Restore automatic local KiCad symbol resolution: the resolver now passes its
  component category by keyword instead of treating it as a KiCad library name.
  This allows symbols such as `nRF52840` and `MAX-M10S` to resolve from the
  installed KiCad libraries.

## [0.32.0] - 2026-07-09

### Packaging, MCP, and release reliability

- Make PyYAML a bounded core dependency so a default PyPI install always parses nested design specs correctly; retain the `yaml` compatibility extra and add coherent `api`, `mcp`, `lookup`, `pdf`, `test`, `dev`, and `all` extras.
- Make `circuit_weaver.__version__` the single package version source and bump the release to 0.32.0. The tag workflow now rejects a mismatched version before publication.
- Make the advertised `circuit-weaver-mcp` command installable via the `mcp` extra, repair project discovery's `max_depth` call and dataclass serialization, isolate engine stdout from JSON-RPC stdio, preserve stable structured error details while setting MCP `isError`, validate port configuration, and exercise real success and failure calls in CI.
- Keep `circuit-weaver-mcp --help` usable without the optional MCP runtime, ship the packaged `examples/iot_sensor.yaml` starter in both wheel and sdist, and make the release gates reject missing examples or any bundled `__pycache__`, `.pyc`, or `.pyo` payload.
- Replace the non-blocking Windows smoke leg with full blocking tests on Windows Python 3.12/3.13, expand Linux coverage to Python 3.10-3.14, install complete test dependencies, and run the integration corpus instead of suppressing it through `CI=true`.
- Build release distributions exactly once, run `twine check`, inspect wheel metadata, install and test the exact wheel across the support matrix, and publish that same artifact only after all gates pass.
- Add blocking KiCad 8, KiCad 9, and KiCad 10 gates from KiCad's official stable Ubuntu PPAs that generate both an IoT design and a complex FPGA power carrier, then parse and run ERC on their root schematics with `kicad-cli`.
- Initialize each headless KiCad gate from KiCad's installed official `fp-lib-table`, so KiCad 10 resolves real standard footprints and the zero-violation gate is not distorted by an uninitialized user profile.
- Validate every provenance-manifest path lexically and by resolved containment before skill upgrades, so crafted traversal, Windows device/ADS names, or symlink escapes cannot read, overwrite, or delete files outside the installed skill.
- Reconcile the Codex, Claude, and OpenCode skills with the shipped CLI, use each platform's current skill location and naming contract, and remove provenance-owned legacy skill copies during upgrade so agents cannot discover a stale workflow beside the new CLI.
- Make installed sample guidance truthful: source checkouts enumerate only real `samples/` directories, while PyPI-installed skills discover and copy the packaged IoT example through `importlib.resources` instead of advertising an absent 13-design gallery.
- Require the same full suite from the source tree and a clean install of the built wheel; hosted Linux CI additionally exercises the symlink cases skipped on Windows.

### Resumable workflows, hierarchy, and imported design analysis

- Add durable, atomic project state with explicit `status`, reconciliation, and `resume` flows so Codex, Claude, OpenCode, MCP, CLI, and Python callers can recover the same design phase and artifact inventory after context loss or interruption.
- Add a shared import workflow for existing KiCad schematics and boards plus Gerber directories or ZIP archives. Imports are containment-checked, inventoried, cached by content identity, and analyzed recursively instead of treating only a root file as the design.
- Partition complex designs into functional subsystem sheets, keep hierarchical pins on exact sheet edges, and preserve stable cross-sheet assembly references so generated projects read like an intentional professional hierarchy rather than arbitrary overflow pages.
- Analyze Gerber alignment from authoritative Ucamco X2 `SameCoordinates` declarations, report drill alignment independently, and keep sparse copper-artwork bounds as a diagnostic instead of falsely declaring misalignment.

### Placement, manufacturing delivery, and routing truth

- Restore a reviewable placement workflow with exhaustive SVG/HTML component maps, real references and footprints, movable placement boxes, persisted review state, placement constraints/blockers, and exact SVG-to-PCB coordinate transfer. Placement previews remain review artifacts until applied and checked in KiCad.
- Generate manufacturing exports transactionally and fail closed: BOM/CPL rows reconcile exact references and footprints, missing PCB placement cannot be guessed, failed reruns cannot overwrite a previously valid delivery, and a staged JLCPCB delivery is published only after cross-artifact verification.
- Make Freerouting integration Specctra-only and transactional, with strict PCB/DSN preflight, bounded timeouts/pass presets, canonical DSN quote handling, correct one-pin-net semantics, semantic SES validation, and explicit incomplete-route reporting. A current Freerouting CLI result that omits final clearance statistics is marked `review_required` and requires KiCad DRC; it is never called fabrication-ready.
- Validate the supported autoroute invocation against the real Freerouting 2.2.4 engine and its official DSN corpus rather than relying only on mocked command output.

### Programmatic compatibility and artifact validity

- Restore compatibility import shims for the legacy `mvp` module and seven subcircuit-template modules removed after 0.30.3, keeping older programmatic consumers importable while routing them to the current implementation.
- Preserve the deprecated `mvp_strict` validation-profile alias and documented `add` / `remove` patch aliases with explicit warnings, while failing closed on unknown patch operations, mixed aliases, and unsupported profiles instead of silently accepting a no-op.
- Make generated results complete and machine-readable: `artifact_manifest.json` inventories every output with stable relative paths, sizes, and kinds; the returned `files` list is exhaustive; and placement SVGs render real compiled components and footprints.
- Keep schematic bytes immutable after generation, validate every final S-expression, and run ERC against those exact bytes. Programmatic callers can set `require_kicad=True`, and the CLI exposes `--require-kicad`, to fail when KiCad is absent, ERC fails, or ERC reports errors.
- Parse KiCad 8/9/10 ERC violations from their per-sheet JSON structure (while retaining legacy top-level compatibility), and make release gates request a non-zero KiCad CLI exit when violations exist.
- Keep reruns into an existing output directory isolated from stale artifacts, and carry portable artifact identity plus final validation/ERC state through the manifest, HTTP ZIP, and MCP response.
- Lease each output directory across processes and serialize in-process generation around the process-global logging bridge, preventing concurrent runs from invalidating manifests, mixing project logs, or leaking lock files into API/MCP/archive results.
- Reject non-portable or path-like project names before creating output, and containment-check every generator/dispatcher-managed target so pre-existing symlinks cannot redirect writes outside the requested output directory.
- Re-check every staged source and live publication destination immediately before backup or replacement, rejecting generated symlinks and nested live-parent escapes that are invisible while generation is isolated in its staging directory.
- Stage each rerun outside the published output and atomically replace the prior successful delivery only after final verification, so a failed or partial rerun cannot destroy or mislabel the last known-good result.
- Require full PyYAML parsing throughout file-based and programmatic workflows, removing the lossy fallback parser from release-critical paths.
- Treat PCB placement previews, padless boards, and zero-copper boards as review-only or incomplete; static analysis is explicitly `unverified` and can never report vacuous routing success.
- Emit deterministic project-local `CircuitWeaver.kicad_sym`, `sym-lib-table`, and matching `.kicad_pro` artifacts; namespace same-named symbols by sheet, plan one PWR_FLAG only for genuinely undriven rails, avoid foreign-net topology anchors, reserve local wire paths by net, and reject connected named-net components that accidentally mix labels. Fresh IoT and FPGA outputs both pass official KiCad 9 ERC with zero violations.

### Release prep — professional templates and deliverables

- Give data-driven templates real documentation contracts: topologies served by `DataDrivenTemplate` (`buck`, `boost`, `buck_boost`, `ldo`, `can_transceiver`, `eeprom`, `protection`, `diode`, `led`, `component`, `logic`, `sensor`) now carry curated descriptions and parameter schemas (`TOPOLOGY_TEMPLATE_INFO` in `topology_builders.py`) instead of the placeholder "Data-driven X template" with no parameters. Required params (`vin`/`vout`/`iout`, `protect_net`) now fail validation with a clear message instead of a KeyError inside the builder.
- Add the missing parameter descriptions to the `gate_driver`, `level_shifter`, and `opamp` template schemas.
- Fix `scripts/gen_template_docs.py` example snippets: reference designators now use each template's own prefix (`J1` for connectors, `D1` for protection, `Y1` for crystals, `Q1` for MOSFETs, `RP1` for pull-up packs) instead of a hardcoded `U1`; the crystal `cl_spec` example is `12` (pF, matching the parameter's unit) instead of `9e-12`; boost/buck-boost examples use physically sensible voltages (3.7V in → 5V out for a boost). Regenerated `docs/templates.md`, which was stale at 37 templates (now 42, including the comparator opamp config).
- Make the generated design report's Power Tree truthful and readable: regulator output rails are credited to the regulator that produces them (a buck reached through its external inductor previously showed as `external ->`), per-instance internal nodes (`SW_x`, `BST_x`) and support-passive plumbing no longer appear as rails, consumers are deduplicated per component (no more `U1:auto` / `U1:CBST` noise), a rail's source is not repeated in its consumer list, and unconsumed rails read `(no consumers)`.
- Polish the report deliverable: the spec's `description` renders under the title, the BOM summary labels are accurate ("Placed components" / "Support passives" instead of "Total ICs" for a count that includes connectors and passives), and the report ends with a generator version footer.
- Refresh the checked-in `*_report.md` for all nine bundled samples with the corrected report output.

## [0.31.0] - 2026-07-04

### Sprint 54 — layout quality zero-crossing gate, density strategy, sheet splitting, autorouter hardening

- (T236) Drive the remaining wire-through-body crossings on the quality-gated samples to **zero** (13 -> 0):
  - Make net-marker stub emission body-aware: label and power-symbol stubs now pick a length whose endpoint *and* marker glyph clear every neighboring symbol body (`_clear_stub_length`), instead of only avoiding the owning symbol's own geometry. This was the dominant crossing class — power symbols and labels parked inside adjacent capacitor bodies.
  - Teach the final wire-hygiene pass to handle T-joint-carrying segments: tapped rails are split at each tap point (taps become piece endpoints, preserving every junction) and each piece is detoured around bodies independently, instead of skipping the segment wholesale.
  - Stop grid-snapping collision keep-out boxes: `component_body_bounds` and the sheet-wide body-box set no longer round corners to the 1.27mm wire grid, which could shrink a box past a wire running just inside its true edge and hide the collision from every detour pass.
  - Lower the `tests/test_layout_quality.py` ceilings to zero for all three gated samples.
- (T237 / audit F13) Replace the connector-heavy boolean cliff in `placer.py` with a continuous density score: `_connector_dominance` computes the fraction of estimated block area contributed by connectors, so a 7-connector board or an 8-connector board with one small regulator now packs width-first, while a few headers around one large MCU keep the normal layout.
- (T238 / audit F15) Stop emitting crammed A0 pages: a sheet that overflows every paper size up to A0 is now split into two area-balanced half-sheets (`split_sheet_allocation`) and re-laid-out; a sheet that cannot be split further raises a clear "Design too large" generation error instead of shipping an unreadable page.
- (T239) Promote the geometric layout-quality analysis from the test suite into `circuit_weaver.layout_quality`: the generator analyzes every emitted sheet, logs warnings for overlaps / wire-body crossings on real designs, and the design report gains a per-sheet "Layout Quality" section. `tests/test_layout_quality.py` now consumes the shared module.
- (T240) Harden the Freerouting autorouter integration:
  - Preflight every board before routing (`preflight_pcb`): the engine's own `*_placement.kicad_pcb` preview (zero pads by design), pad-less boards, and boards without named nets fail closed with a forward-annotation remediation message.
  - Use the supported Specctra pipeline when `kicad-cli` is available: DSN export -> Freerouting `-de/-do` -> `.ses` session with a KiCad import hint; fall back to direct `.kicad_pcb` invocation otherwise.
  - Add `--effort fast|medium|high` (Freerouting `-mp` pass budget) and `--timeout` CLI options, and surface incomplete-route counts in results.
- Full suite: 1107 passed, 1 skipped.

### Sprint 53 — schematic layout quality (congestion, overlaps, readability)

- Stop support passives from stacking at identical coordinates: the sidecar cluster pass now groups by resolved parent-pin location and walks collision-free slots against a sheet-wide occupancy list seeded with cluster poses and junction anchors. Symbol-body overlaps across the nine-sample corpus: 11 → 0.
- Fix passive face detection to trust the pin's angle (top-face pins near a body corner were classified "left", parking buck support parts on the wrong face), and make the buck cluster face-aware so CIN, the SW/FB junction anchors, COUT, and the feedback divider grow away from the pin's actual face instead of hanging below the IC and wiring straight through it.
- Route local passive connections around *all* nearby symbol bodies: `_route_local_connection` now takes sibling-passive/own-body/neighbor-IC keep-out boxes, generates detours around every blocker, and logs a warning when it must relax clearance instead of silently emitting a direct line through symbols (audit F6).
- Cap "local" wiring at 50.8mm of Manhattan run — anchors and owner pins farther away fall back to net labels, eliminating the 60–130mm cross-sheet wires that made sheets unreadable.
- Add a final wire-hygiene pass that rewrites any remaining emitted segment crossing a symbol body as an endpoint-preserving detour (T-joint-carrying segments are left alone). Wire-through-body segments across the corpus: 147 → 36; remaining cases are catalogued in TASKS.md as bank/ladder occupancy follow-ups.
- Add `tests/test_layout_quality.py` — geometric analysis of generated `.kicad_sch` output gating zero symbol overlaps and per-sample wire-crossing ceilings, plus unit regressions for the sidecar, face-detection, routing, budget, and hygiene fixes.
- Make decoupling-bank, strap-ladder, and LDO topology motifs occupancy-aware: each motif now walks a collision-free origin for its passive bodies and local anchors, then reserves those positions before later motifs or sidecars are placed. The shared occupancy pass now deduplicates reservations and immediately seeds anchors created by earlier topology parents before placing later motifs. This closes the first layout follow-up from the Sprint 53 audit and leaves only the post-occupancy endpoint/tapped-rail reductions open in `TASKS.md`.

### Sprint 52 completion — critical and high-priority generation fixes

- Fix the T229 `orphan-net` placement-readiness gate to recognize support-passive endpoints (straps, bypass/bootstrap caps, inductors, feedback dividers) as real second consumers. Regulator SW/FB/BST nodes, ESP32 EN/IO0 strap nets, charger PROG/CELL/QSTRT programming nets, and display IREF/charge-pump nets no longer raise false-positive errors — this restores the sample and corpus release gates from 24 failing tests to green.
- (T228) Complete the removal of `build_generic`'s per-instance signal-net synthesis: unresolved non-power signal pins are never given `{PIN}_{REF}` phantom nets. Pins route through normalized declared interfaces and shared-bus metadata, pin-name role inference fills undeclared roles (so imported parts with pins named `USB_DP` / `SDA` / `XTAL1` land on the shared buses), NC-named pins become explicit no-connects, metadata-declared optional/debug pins stay safely unrouted, and everything else fails generation closed with the pin name and component reference.
- (T228) Stop defaulting SPI chip-select to a per-instance `CS_{REF}` net in `build_generic` — the mapped default made the pin look handled and blocked the SPI chip-select repair pass. CS now maps only when the caller names the net.
- (T233) Add UART flow-control repair: metadata-declared RTS/CTS pins on an actively wired UART complete onto the existing sibling flow-control net when derivable, and otherwise become explicit no-connects instead of floating-input or unmapped-required generation failures. Component repairs now also clear the T228 fail-closed marker for pins they resolve.
- (T234) Propagate the normalized schema to datasheet-derived ingest: `parse_datasheet_text` extracts pin-function tables into normalized `pins`, `pin_roles`, `pin_vdd` / `pin_gnd`, `power_domains`, `explicit_no_connects`, and `debug_pins`; datasheet-recommended bypass values land in `recommended_bypass`; `extract_specs` propagates index-declared `vendor_aliases` as sourcing metadata.
- Add `MCP1700-1802E` to the linear-regulator catalog (1.8V sibling of the existing `MCP1700-3302E` entry) so `samples/oled_display_module` resolves without silent substitution.
- Add regression coverage: T228 synthesized-net and fail-closed paths, support-passive orphan-net exemptions, pin-name-inference-only SPI/UART repair, UART flow-control completion and NC fallback, imported-`pin_roles` crystal building, datasheet pin-table ingest flowing end-to-end through `build_generic`, and a catalog-wide sweep asserting every generic-dispatched entry emits no synthetic nets. Full suite: 1062 passed, 1 skipped.
- Fix the tag-triggered release workflow to install the full test extras without editable-mode dependency resolution drift, and queue the next sprint follow-up for Codex / Claude / OpenCode / Kilo compatibility config recovery.

## [0.30.52] - 2026-05-05

### Sprint 52 — Generation Pipeline Hardening

- Codify the part-neutral, vendor-agnostic, data/spec-driven direction in repo policy: add a root `CLAUDE.md` that tells future agents to prefer normalized schema + topology behavior over per-part engine branches.
- (T228) In progress — interface-heavy generic topologies now stop synthesizing NC phantom nets, default crystal nets to shared `XTAL_IN` / `XTAL_OUT` names, and hard-fail generation on unresolved declared-interface pins. The remaining work is the full removal of fallback per-instance signal nets in favor of normalized interface / pin-role routing across the rest of `build_generic`.
- (T229) Add a placement-readiness `orphan-net` gate for non-power nets consumed by only one block, with curated exemptions for test points, debug connectors, and connector-local USB-C control nets. Re-checking `I:/my_circuit/design.yaml` now keeps the placement-readiness total at `59` with `0` extra `orphan-net` false positives.
- (T230) Centralize generic IC decoupling policy under `auto_generate_bypass_caps`: `build_generic` no longer bakes in a blanket 100nF cap, the central pass now augments missing per-rail caps instead of skipping any component that already had one, and datasheet-driven `recommended_bypass` entries override the heuristic when present.
- (T231) Add weighted shared-net attraction to the PCB placement optimizer using a net-to-component map from `pcb_export`. `balanced` / `si` placement now penalizes long connected pairs instead of staying purely zone-based; on `samples/iot_sensor_node`, a high-weight connected pair (`RP1`↔`U4`) moved from `38.48 mm` apart in `simple` mode to `3.92 mm` in `balanced` mode.
- (T232) Lift placement-readiness gating into `generate_from_components`, add the single `readiness_gate` / `--no-readiness-gate` override path for debug emits, and turn `_classify_unhandled_pin(... level=\"error\")` into a hard generation failure instead of a logged no-connect. Direct runtime probes now confirm the gate blocks direct callers, the override reaches allocation, and floating power pins still hard-fail.
- (T233) Add normalized SPI/UART repair plumbing on top of the dedicated crystal-builder work: `generational_repair.py` now uses shared `pin_roles` metadata to complete floating SPI chip-select pins onto an existing unique CS net on the same bus and to complete missing UART TX/RX directions only when the peer net already exists. Optional flow-control / explicit-NC behavior is still open.
- (T234) Start turning catalog growth into a real schema pipeline: add normalized `pin_roles` to `ComponentDef`, cache payloads, `ic_data` conversion, EasyEDA imports, and generic-builder outputs so repairs/builders consume vendor-agnostic interface capabilities instead of exact MPN branches. The remaining work is broader datasheet/schema propagation plus acceptance-corpus coverage.
- Add a repo-local `tests/conftest.py` `tmp_path` override so focused pytest modules no longer depend on the broken Windows `tmp_path` / basetemp ACL path on this machine.

### Sprint 51 — Restart & Validation Hardening

- Add an installed-version banner to the `/circuit-weaver` startup flow and keep the repo-local and bundled `circuit-weaver` skills byte-identical. The skill now prefers `circuit-weaver --version` and falls back to `python -m circuit_weaver --version` only when needed.
- Add timeout-aware workflow guidance to `/circuit-weaver` and `/design_wizard`: long-running steps now require pre-announcement, follow-up checks around 2/5/15 minutes, and explicit log/status/issue inspection instead of letting work sit silent for ~30 minutes.
- Add canonical `validate` output-handling guidance to the workflow skills: the current CLI emits JSON to stdout by default, stderr must remain separate when parsing, `--json` should not be assumed, and the skills now include a safe capture/parsing recipe instead of brittle `2>&1 | json.load(...)` wrappers.
- Make restart state truthful for validate-only sessions: `log-status` no longer reports an empty workflow when a project only has validation activity, and persisted validation summaries now prefer the authoritative final report over the earlier raw-check pass.
- Harden Windows console validation output: text mode now falls back to ASCII `PASS` / `FAIL` when stdout cannot encode Unicode checkmark/cross glyphs, avoiding cp1252 `UnicodeEncodeError` crashes.
- Fail closed on bad component/template resolution: unknown data-driven `ic` selections now error instead of silently substituting the first database entry, `type: component` routes through the standalone resolver chain, handwritten templates again win over the generic JSON fallback when both exist, and `connector` now supports explicit `subtype: usb-a` resolution via `USB_A_4P`.
- Harden I2C auto-repair matching: synthetic pull-up repair now requires real SDA/SCL participant overlap, and hand-authored `i2c_bus` blocks suppress only the matching bus instead of disabling repair globally for every I2C bus in a design.
- Tighten placement-readiness interface semantics: passive support parts such as straps and bypass caps no longer count as true inter-block consumers, so orphan-interface checks stop being accidentally satisfied by support-only nets.
- Trim compile-path and warning debt: remove the dead duplicate shared-net synthesis helper from `dispatcher.py`, switch confidence-report timestamps to timezone-aware UTC, register the `network` pytest marker, and refresh stale import-site coverage around the renamed IR normalizer.
- Re-validate the live restart repro against the patched source: `I:/my_circuit/design.yaml` no longer produces the bogus barrel-jack `TIP_J2` / `RING_J2` connector artifacts or the repeated `USB_DP_*` substitution noise from unrelated components. The current remaining validation summary is `1 structural`, `59 electrical`, `9 implementation`, and `59 placement_readiness`; the remaining blockers are now exposed honestly instead of being masked by the old connector/template noise.

## [0.30.5] - 2026-05-04

### Sprint 50 — Generic Builder Parity Cleanup (T215–T218)

- Fix data-driven `sensor_frontend` dual-rail power handling: `build_generic()` now maps explicit `pin_vpos`/`pin_vneg` metadata to `vdd_net`/`gnd_net`, eliminating INA128PA floating power pins and reducing the full-suite failure count from 20 to 18.
- Preserve shared I2C/SPI net names in data-driven generation: add dedicated `i2c_bus` pull-up/level-shifter builders and map explicit SDA/SCL/SPI metadata to shared nets instead of per-instance `{PIN}_{REF}` names.
- Restore data-driven battery charger and fuel-gauge support networks: `battery_charger` and `battery_monitor` now emit PROG, STAT, CELL, QSTRT, I2C, bypass, and support-passive contracts without reintroducing legacy template classes.
- Restore data-driven display and passive diode contracts needed by the release gate: SSD1306-class display drivers now include reset, charge-pump, I2C/SPI, and support passives; diode/LED topologies now generate passive two-pin components without false power/GND requirements.
- Harden packaged/read-only workflows for the 0.30.5 release candidate: CLI commands now fall back to a writable log directory instead of crashing when the spec directory cannot host `circuit-weaver.log`, unreadable `~/.config/secrets.env` no longer aborts `doctor` or unknown-part fallback, and the bundled skill payload is resynced byte-identical with `skills/` for PyPI installs.
- Rework wizard intake by experience level: the offline `design-wizard` now captures experience before requirements, uses a compact design-brief path for advanced/professional users, and avoids the old one-size-fits-all opening questionnaire.
- Update Circuit Weaver workflow skills for the data-driven architecture: professional/specialized RF flows now route into research-first custom block definition instead of being framed as out-of-scope or template-gated, and the design-wizard/circuit-weaver docs now describe topology/block coverage rather than the old template-first mental model.
- Close the Sprint 50 release gate: full suite passes `1009 passed, 1 skipped, 6 warnings`; focused sample/presentation/generation gate passes `38 passed`; 9-archetype corpus hard-error gate passes `9 passed`. The external `I:/my_circuit` probe still validates with known placement-readiness errors and generation remains blocked by that design's pre-existing connectivity issues.

### Sprint 49 — Incremental Legacy Migration (T180–T182)

- Flip `SubcircuitRegistry.get()` to data-driven-first resolution order for all topologies; legacy templates serve as fallback when ic_data JSON has no entries.
- Remove `_DATA_DRIVEN_FIRST` class variable (dead code after flip).
- Add `test_registry_uses_data_driven_first` and `test_registry_legacy_fallback_when_no_ic_data`; replace old `test_registry_prefers_legacy_for_unported_topologies`.
- Catalog 26 test failures from boundary-port naming mismatches in verdict-B/C topologies (`build_generic` vs legacy). Buck/boost/buck_boost/ldo unaffected.
- Delete `subcircuits/buck.py`, `boost.py`, `buck_boost.py`, `ldo.py` — migrated to data-driven `DataDrivenTemplate` + topology builders with 34 parity tests (all pass). Remove registry import/register calls. Update test/script references to use registry-backed lookups via `get_default_registry().get()`.
- Delete remaining verdict-A legacy templates `can_transceiver.py`, `eeprom.py`, and `protection.py` after porting their contracts to data-driven builders. Add regression coverage for CAN net naming/options, EEPROM I2C/SPI strapping, and passive protection devices. Full-suite failures are reduced from 26 to 20; remaining failures are the pre-existing T180 boundary-port/generic-builder gaps.

## [0.30.3] - 2026-04-29

### Sprint 48 - Continued Release Validation

This patch collects follow-up fixes found while validating the v0.30.2 release
candidate on Windows, bundled sample designs, package builds, CLI validation,
confidence reports, and simulation command behavior.

### Fixed

- **Windows ERC CLI output:** `circuit-weaver erc` now prints ASCII `PASS` on
  success instead of a Unicode checkmark, avoiding cp1252 console crashes after
  successful ERC runs.
- **`register-ic` malformed custom data:** Single IC JSON objects that use
  `mpn`/`template_type` are now registered as one IC instead of being mistaken
  for a map of many ICs. Multi-IC maps reject scalar entries, and IC database
  accessors skip malformed custom entries instead of crashing validation.
- **Invalid `generate` CLI failures:** Expected hard-validation failures now
  return clean JSON error output with exit code 2 instead of surfacing an
  unhandled Python traceback.
- **Generic connector power nets:** `PIN_HEADER_2P`/`PIN_HEADER_4P` style
  generic connectors now honor explicit `positive_net` and `negative_net`
  fields before assigning `signal_nets`, so mixed power/signal headers do not
  create dangling `P*_REF` nets for their power pins.
- **Comparator template mode:** TLV3691-class comparator ICs now generate a
  threshold divider and output pull-up instead of op-amp feedback resistors
  when used as threshold detectors.
- **Battery-holder placeholder replacement:** Explicit 2xAA battery-input
  BARREL_JACK placeholders are upgraded to the standard KiCad
  `Battery:BatteryHolder_Keystone_2462_2xAA` footprint.
- **Footprint library readiness:** Generated footprint references are checked
  against local KiCad `.pretty` libraries, and missing custom/manufacturer
  footprints are emitted as implementation warnings with the searched install
  path and an official KiCad footprint-library browser URL.
- **Compact schematic paper size:** Connector-heavy compact boards now pack
  connector rails into two columns and avoid density spreading, keeping the
  `I:/my_circuit` Zigbee PIR/vibration schematic on A3 instead of A1.
- **Custom-footprint alternative policy:** Validation now distinguishes
  custom/missing footprint-library parts from standard KiCad-backed parts and
  can recommend curated footprint-backed alternatives when available.

### Validation

- Re-ran lint, tests, bundled sample validate/generate/ERC, confidence report,
  simulation command, doctor, and package build checks.
- Simulation command generates SPICE netlists but skips actual runs when
  optional `ngspice` is not installed, as expected.
- Removed stale bundled-skill `__pycache__` bytecode from package inputs after
  the first build attempted to include it in the wheel.
- Re-ran the real `I:/my_circuit/design.yaml` validation/generation/ERC probe
  from source into `I:/my_circuit/output_v0303_probe`; ERC reports 0 errors and
  0 warnings, and the schematic now uses A3. The probe still flags missing
  local footprint libraries for the E72 module and EKMB PIR sensor with KiCad
  library/manufacturer-import guidance.
- Rebuilt the source and wheel distributions after the final fixes; inspected
  the wheel and found 0 `__pycache__` or bytecode artifacts.

## [0.30.2] - 2026-04-28

### Sprint 45-47 - Output Bug Fixes, Coverage, and Safe Migration Planning

This patch release addresses the issues found in the `I:/my_circuit/output/`
review: over-promoted schematic paper size, warning records mislabeled as
errors in `design.log`, and redundant validation generation passes. It also
closes the remaining Sprint 42/T191 dedicated-test gaps for allocator and
placement-readiness logic, finishes Sprint 46 short-runner coverage, and
revalidates the Sprint 47 legacy-template migration boundary.

### Fixed

- **Schematic paper over-promotion:** `layout_sheet()` now starts from the
  allocator-selected paper size and only promotes if that size does not fit.
  This prevents small IoT-sensor-class designs from cascading to A2 because
  density-scaled gaps grow with paper area. The fit gate also uses a tighter
  clearance above KiCad's reserved title block, keeping `IoT_AQ_Sensor_v2` on
  A3 instead of over-promoting to A2.
- **Design log severity:** `DesignLogHandler` now maps Python `WARNING`
  records to `type: "warning"` entries instead of `type: "error"`. Error and
  critical records still use `type: "error"`.
- **Generate overhead:** `validate_design(check_determinism=False)` allows
  `generate_artifacts()` to skip the dual deterministic-generation pass. A
  `generate` run now performs one validation smoke generation plus the real
  output generation, not three full generations.

### Added

- **Placement-readiness tests:** New `tests/test_placement_readiness.py` covers
  promoted validator codes, suggestion fallback behavior, orphan-interface
  detection, power-net exclusions, self-only net handling, and report
  serialization.
- **Allocator tests:** New `tests/test_allocator.py` covers paper thresholds,
  category/ref/description classification, small-design single-sheet behavior,
  passive-sheet merging, review-sheet partitioning, sort order, paper
  recomputation after merge, and annotation de-duplication.
- **JLCPCB assembly variants:** `generate_assembly_variants()` supports simple
  `include_refs`, `exclude_refs`, and `dnp_refs` assembly subsets. Optional
  `export_jlcpcb(..., assembly_variants=[...])` writes per-variant BOM/CPL
  files alongside the default BOM/CPL.
- **Sprint 46 tests:** Added coverage for sourcing alternate suggestions,
  mocked PDF datasheet parsing/extraction, JLCPCB price-break detection, and
  assembly-variant export.
- **Sprint 47 audit:** Re-ran `scripts/audit_legacy_templates.py`. Result is
  unchanged: A=7, B=21, C=9. Full legacy deletion remains unsafe for v0.30.2;
  the registry stays data-driven-first only for verified topologies.
- **BME688 bundled IC data:** Added BME688 sensor pin/footprint metadata to the
  bundled IC database so `IoT_AQ_Sensor_v2` no longer depends on local
  `custom.json` registration state.
- **Local workflow ignores:** Stopped tracking unused local OpenCode/KiCad agent
  workflow files and added ignore rules for them.

### Tests

- Added `tests/test_allocator.py` (28 tests).
- Added `tests/test_placement_readiness.py` (24 tests).
- Added `tests/test_logging_bridge_levels.py` (6 tests).
- Added `tests/test_validate_determinism_flag.py` (4 tests).
- Added `tests/test_jlcpcb_export.py` (7 tests).
- Expanded `tests/test_placer.py`, `tests/test_sourcing_auditor.py`, and
  `tests/test_spec_harvester.py`.

## [0.30.1] - 2026-04-28

### Sprint 44.1 — Cache Fix + Test Coverage

OpenCode's non-deterministic sub-agent prompt construction breaks DeepSeek's
prefix-based prompt caching, causing high cache-miss costs. This release
disables the oh-my-openagent plugin (which injected per-turn mode tags and
todo-continuation prompts) and adds dedicated test coverage for four
previously untested modules.

#### Cache-Friendly Workflow

- **Plugin:** Removed `oh-my-openagent` from `opencode.json` plugin list
  to stop per-turn directive injection that changed the prompt prefix on
  every request.
- **Docs:** Added `docs/cache-friendly-agents.md` — codifies cache-hit-first
  operating principles: reuse `task_id` sessions, avoid sub-agents for
  trivial work, keep `AGENTS.md`/`rules/kicad.md` small and stable, audit
  plugins for per-turn injection.
- **AGENTS.md:** New rule referencing the cache doc before adding any
  plugin, sub-agent, or per-turn directive.

#### New Test Modules (46 tests total)

- **`tests/test_api.py`** (16 tests): Smoke tests for all 9 FastAPI endpoint
  groups — `/health`, `/templates`, `/validate`, `/generate`,
  `/generate/from-bom`, `/mvp/validate`, `/mvp/apply-patch`, `/mvp/diff`,
  `/mvp/pcb-feedback`, `/mvp/generate`.
- **`tests/test_placer.py`** (16 tests): Smoke tests for all 6 public
  placer functions — `layout_sheet`, `reset_ref_counters`,
  `component_block_size`, `component_body_size`, `component_body_bounds`,
  `component_annotation_start_y`.
- **`tests/test_thermal.py`** (6 tests): Smoke tests for
  `analyze_thermal` and `generate_heatmap_svg` — full IoT sensor spec,
  placements, custom ambient temp, empty components, SVG output.
- **`tests/test_si_constraints.py`** (8 tests): Smoke tests for
  `analyze_si_constraints` — USB bus detection from pins and descriptions,
  impedance constraints, differential pairs, routing rules.

#### Known Limitations

- OpenCode upstream issue: sub-agent prompt construction is non-deterministic
  (agent list not sorted). This must be fixed upstream in OpenCode for full
  cache-hit optimization. Tracked in the cache doc.

## [0.30.0] - 2026-04-27

### Sprint 44 — CI Gate Repair & Label Collision Prevention

Generated schematics previously could have overlapping labels on dense
sheets, the CI gate was blind to sample regressions (zigbee_humidistat
skipped), and several code quality items from the Sprint 42 backlog
remained unaddressed. This release closes all of those gaps.

### Added

- **Validate-all regression gate** (T186): `test_corpus_validate_no_hard_errors`
  parametrized test asserting zero hard errors across all 9 corpus samples.
  All 14+1 sample YAMLs now validate clean.
- **Label collision avoidance pass** (T196):
  `primitives._resolve_label_collisions()` detects overlapping label
  bounding boxes on the same orientation axis and shifts labels along
  their wire-stub direction by 2.54mm increments, extending the
  connecting wire. Same-name labels are skipped. Wired into
  `assemble_sheet()` before dedup.
- **VUSB power rail detection** (T186): Added `VUSB` to
  `POWER_NET_PREFIXES` in `subcircuits/base.py`.
- **Sourcing auditor alternate suggestion** (T189):
  `sourcing_auditor._suggest_alternates()` queries LCSC/DigiKey for
  functionally similar parts when a component has CRITICAL/WARNING
  risk level.
- **JLCPCB price-break detection** (T192): `_detect_price_breaks()`
  in `jlcpcb_export.py` queries LCSC pricing and flags components
  where ordering qty 100 saves ≥ 20% vs qty 1. Alerts appended to
  assembly README.
- **MCP server for AI agent tool access** (T194):
  `src/circuit_weaver/mcp_server.py` — FastMCP-based server exposing
  `validate_design`, `generate_artifacts`, `discover_projects`, and
  `research_component` tools. Entry point: `circuit-weaver-mcp`.
- **Wire-crossing minimization** (T193): `_count_wire_crossings()`
  in `placer.py` counts horizontal/vertical wire intersections and
  penalises crossing-dense placements via `_layout_score()`.
  `_bus_net_groups()` detects bus signal groups (4+ numbered nets
  with shared prefix) for future parallel routing.
- **18 new regression tests** for `datasheet_parser.py` covering
  thermal, voltage, and parametric regex extraction (T190).
- **12 new regression tests** for `generational_repair.py` covering
  I2C pull-up auto-repair pipeline (T191).

### Changed

- **`design_loader.py`** extracted from `dispatcher.py` (T187):
  `compile_design_ir()` and its 300-line support pipeline moved to
  a new module. Backward-compatible import re-exported from dispatcher.
- **`sexpr_builder.py`** extracted from `generator.py` (T188):
  symbol property normalizers and S-expression balance validator
  moved to a new module. Backward-compatible imports from generator.
- **`zigbee_humidistat.yaml`** (T186): Added `pin_nets_extra` for
  EN/IN pins, USB-C-PWR connector block to drive VUSB.
- **CI `validate-design.yml`** (T186): Removed zigbee_humidistat skip.
- **`_layout_score()`** in `placer.py` (T193): Now includes a
  wire-crossing penalty term in the placement score.

### Fixed

- **VUSB now recognized as a power rail** by `_is_power_net()`
  (T186). Previously caused `orphan-interface` and `undriven-net`
  errors on USB-powered designs without a modeled connector.

### Tests

- Added `test_corpus_validate_no_hard_errors` (9 parametrized cases).
- Added `tests/test_datasheet_parser.py` (18 tests).
- Added `tests/test_generational_repair.py` (12 tests).
- Full suite: **853 passed, 19 skipped, 0 failed**. Ruff clean.

### Files

`src/circuit_weaver/design_loader.py` (new),
`src/circuit_weaver/sexpr_builder.py` (new),
`src/circuit_weaver/mcp_server.py` (new),
`tests/test_datasheet_parser.py` (new),
`tests/test_generational_repair.py` (new),
`src/circuit_weaver/subcircuits/base.py`,
`src/circuit_weaver/dispatcher.py`,
`src/circuit_weaver/generator.py`,
`src/circuit_weaver/placer.py`,
`src/circuit_weaver/primitives.py`,
`src/circuit_weaver/jlcpcb_export.py`,
`src/circuit_weaver/sourcing_auditor.py`,
`samples/zigbee_humidistat/zigbee_humidistat.yaml`,
`.github/workflows/validate-design.yml`,
`pyproject.toml`, `tests/test_generator_guards.py`

---

## [0.29.1] - 2026-04-27

### Sprint 43 — Schematic Density & Readability

Generated schematics suffered from annotation overlap on dense sheets,
unbounded lane routing drift, and corner-clustering of components on
large paper sizes. The initial v0.29.0 shipped an auto-sheet-splitting
approach that was reverted in this release in favor of density-scaled
grid spacing — a simpler, safer fix that spreads components across the
available page area without introducing cross-sheet connectivity
complexity.

### Changed

- **Density-scaled grid spacing** (Task 201, replaces T195).
  `_density_scaled_gaps()` in `placer.py` scales inter-component
  column and row gaps so that components spread across the available
  page area instead of clustering in a corner. When a large sheet
  (A0/A1) has a modest number of ICs, the row/col gaps are
  multiplied up to 3.0x so content fills ~30-40% of the page. On
  already-dense sheets (fill > 35%), gaps stay at their base
  values. Wired into all three component groups in
  `_build_layout()`: connectors, regulators, and other ICs.
  Module-level: no new constants needed — the scaling is fully
  automatic based on component footprint area vs page area.

### Changed (from v0.29.0)

- **Auto sheet splitting reverted** (was T195 in v0.29.0).
  `allocator.py` restored to v0.28.3 baseline. Sheet splitting
  created cross-sheet readability issues (connected ICs spanning
  different sub-sheets) and root-sheet scaling concerns for no
  meaningful benefit over density-scaled spacing.
- **Annotation overlap prevention** (Task 197, kept from v0.29.0).
- **Lane routing counter recycling** (Task 198, kept from v0.29.0).

### Deferred

- **Label collision avoidance** (Task 196) deferred to Sprint 44.

### Tests

- Added `tests/test_density_scaling.py` — 5 tests covering no-scaling
  for small/dense sheets, scaling on large sparse sheets, < 3
  component skip, and grid-snap idempotency.
- Removed `tests/test_allocator.py` (reverted with T195).

### Files

`src/circuit_weaver/placer.py`, `src/circuit_weaver/generator.py`,
`tests/test_density_scaling.py`, `pyproject.toml`,
`src/circuit_weaver/__init__.py`, `tests/test_bootstrap.py`

Full suite: 832 passed, 1 skipped, 0 failed. Lint: clean.

## [0.29.0] - 2026-04-27

### Sprint 43 — Initial attempt (reverted in 0.29.1)

- Auto sheet splitting (T195) — reverted in 0.29.1 in favor of density-scaled spacing.
- Annotation overlap prevention (T197) — kept.
- Lane routing counter recycling (T198) — kept.
- Label collision avoidance (T196) — deferred.

## [0.28.0] - 2026-04-26

### Sprint 41 — Resolver + Template UX Follow-ups

Two related failure modes surfaced by a user running the design wizard
on a novel IC (RP2040-based toy phone):

### Fixed

- **`SymbolResolver` now caches negative resolutions for the life of the
  process** (Task 175). A design with N identical unresolvable parts
  triggers 1 tier-chain walk, not N. The toy_phone 12-button matrix
  (`TS-1187A-B-A-B`) went from 12 DigiKey round trips per validate to
  1. `SymbolResolver.clear_unresolved_cache()` exposed for
  long-running callers that want to retry after a transient flap.
- **Legacy topology templates now honor `register-ic` pin maps**
  (Task 176). `USBControllerTemplate`, `ConnectorTemplate`,
  `USBCConnectorTemplate`, and `EEPROMTemplate` previously only
  consulted their own hardcoded `*_IC_DATABASE`. Registering a novel
  MPN (e.g. RP2040 as a `usb_controller`) silently fell back to the
  template's default IC, so the net-connectivity validator flagged
  `USB_DP` dangling even though the user's registered pin map put
  pin 43 on USB_DP. Each template now merges `ic_data` entries
  matching its topology via `merge_into_legacy_db()` — same pattern
  `audio_amplifier` / `motor_driver` / `protection` use.
  `USBControllerTemplate.generate()` also honors explicit
  `pin_usb_dp` / `pin_usb_dm` number fields from the JSON
  registration, falling back to `D_P` / `D_N` / `USB_DP` / `USB_DM`
  pin-name matching. Remaining ~25 legacy templates with hardcoded
  DBs tracked as Sprint 42 cleanup.
- **Placement preview `.kicad_pcb` files now use KiCad's fixed 2-layer
  hash** (Task 177). `pcb_export.py` had been emitting a KiCad-5-era
  hardcoded layer table (`B.Cu=31`, `ECO1.User`, `ECO2.User`) that
  KiCad 10 rejects on load with `Layer ECO1.User ... is not fixed layer
  hash`. The placement board now uses KiCad's canonical layer ids and
  names (`B.Cu=2`, `Eco1.User`, `Eco2.User`, plus `User.1-User.4`),
  so `*_placement.kicad_pcb` opens again as the intended layout hint.

### Tests

- Added `test_unresolved_mpn_is_cached_within_process` and
  `test_unresolved_cache_does_not_shadow_successful_resolutions` in
  `tests/test_resolver_chain.py` (Task 175).
- Added `test_usb_controller_hotload_via_register_ic`,
  `test_usb_controller_generate_wires_registered_ic_usb_pins`,
  `test_connector_hotload_via_register_ic`,
  `test_usb_c_connector_hotload_via_register_ic`,
  `test_eeprom_hotload_via_register_ic` in
  `tests/test_legacy_template_hotload.py` (Task 176).
- Added `test_preview_pcb_uses_kicad_fixed_layer_ids` in
  `tests/test_pcb_preview_invariants.py` (Task 177).
- Added `tests/test_template_smoke.py` — 74 tests iterating every
  template in the default registry, asserting (a) merged IC DB is
  non-empty, (b) `generate()` returns at least one ComponentDef.
  Covers the 28+ templates the 9-archetype corpus doesn't exercise
  (Task 178).
- Full suite: **825 passed, 1 skipped, 0 failed**.

### Fixed (continued)

- **Every hardcoded `*_IC_DATABASE` dict drained into
  `ic_data/*.json`** (Task 178). All 84 IC entries across 37 legacy
  subcircuit-template databases now live in JSON, tagged with the
  correct topology so every template's merge view resolves them.
  New `subcircuits.base.LegacyDBProxy` provides a dict-like shim so
  existing template code reading `XYZ_IC_DATABASE[key]` / `.get()` /
  `in` / `.keys()` works unchanged against a live JSON view —
  `register_ic()` entries become visible on the next access, no
  process restart needed. Subtype info that used to ride on
  `topology` (`low_side`/`high_side`, `series`/`shunt`, `buck`/
  `linear_sink`) is preserved as `topology_subtype`; the 3 templates
  that dispatched on it (`mosfet_switch`, `voltage_reference`,
  `led_driver`) now read the new field. 8 duplicate MPNs living in
  two JSON files (including `AT25SF128A` as both `component` and
  `eeprom`) deduped — topology-specific entry wins, generic
  `component` copy removed.

### Sprint 41 — Circuit Validity Generational Requirements

Completes the generational-requirements story started in Sprint 40. A
user can now describe a "dynamic and vastly different" circuit in YAML
and get a guaranteed placement-ready `.kicad_sch` back — dangling
buses, missing I2C pull-ups, floating enables, orphan interfaces, and
unverified-stub ICs are either auto-repaired in place or raise a hard
generation error with a specific fix suggestion.

### Added

- **New `placement_readiness` validation category** (`placement_readiness.py`)
  that promotes every placement-blocking check — `single-pin-net`,
  `undriven-net`, `i2c-missing-pullup`, `spi-floating-cs`,
  `uart-unpaired`, `floating-enable`, `floating-power-pin`,
  `unverified-pinout`, plus a new `orphan-interface` detector — into a
  hard category that `generate_artifacts` always blocks on.
  `_HARD_ERROR_CATEGORIES` is now
  `{structural, implementation, placement_readiness}`; soft electrical
  warnings remain bypassable via `--no-require-valid`.
- **`generational_repair.py`** auto-repair pass. Runs inside
  `compile_design_ir` after primary resolution, synthesizes a
  PULLUPS_ONLY I2C bus block when a named I2C bus lacks pull-ups, and
  records each fix in `placement_readiness.json`. Users opt out via
  `auto_repair: false` at the top of the spec.
- **Surgical per-IC YAML overrides** (`project_spec._apply_partial_pin_overrides`):
  `pin_nets_extra`, `power_pins_extra`, and `no_connects` merge onto
  registry defaults so users can rewire an MCU's I2C pins or mark an
  unused UART NC without re-declaring every other pin via `pin_map`.
  Stale template boundary ports retire automatically when their net is
  replaced.
- **`_synthesize_shared_net_interfaces`**: every non-power signal that
  shares a net across two or more blocks now gets an auto-declared
  `DesignInterface`, satisfying the MVP's `undeclared-shared-net`
  gate without user boilerplate.
- **Four new corpus archetypes** (`samples/`) — `inverter_gate_driver`,
  `wearable_bms`, `rf_frontend`, `high_voltage_isolation` — closing
  out the Sprint 40 follow-up archetype list.
- **`placement_readiness.json`** artifact written alongside
  `validation_report.json` in every `generate` output. Shape:
  `{ready: bool, blocking: [...], auto_repaired: [...], summary: {...}}`.

### Changed

- **`_bus_pairs` relaxed** to trigger on any named I2C bus with at
  least one participant (previously required ≥ 2). A lone sensor on
  `I2C_SDA` still needs pull-ups; the new detector covers that case.
- **`_build_net_pin_map` in the validator** now models bypass caps and
  strap resistors as full 2-terminal elements — a pull-up net no
  longer reads as single-pin just because the only "real" IC pin on
  it is the one the strap attaches to.
- **`_POWER_NET_PREFIXES`** expanded with `VBAT`, `VSS`, `AGND`,
  `DGND`, `PGND` so battery-only rails aren't flagged as
  `undeclared-shared-net`.
- **`design_ir_to_engine_spec`** preserves `block.ic` on template
  blocks, fixing a pre-existing bug where a user-selected IC (e.g.
  `ic: CH340G` on a `usb_controller`) silently fell back to the
  template's first default (CYUSB3014).
- **Existing samples fixed to be placement-ready**: `iot_sensor_node`,
  `battery_iot_sensor`, `motor_controller`, `usb_uart_bridge`,
  `oled_display_module`, and the built-in `examples/iot_sensor.yaml`
  now use the new surgical overrides to wire their MCU I2C / SWD /
  UART pins cleanly.

### Tests

- **9-archetype corpus**: `tests/test_generation_corpus.py` now runs
  `generate_artifacts` over 9 samples and enforces a fourth invariant:
  `placement_readiness.json` reports `ready: true`. Breadth guard
  raised from ≥ 5 to ≥ 9.
- Added `test_auto_repair_inserts_i2c_pullups` and
  `test_auto_repair_disabled_via_spec_flag` covering Task B directly.
- Full suite at sprint close: **743 passed, 1 skipped, 0 failed**.

### Sprint 40 — Generation Quality Regression Repair

Repaired regressions introduced during the dynamic IC designer, placement
pipeline, and schematic density work. Every fix landed behind a
diverse-circuit regression corpus so follow-on generator work can't silently
break these subsystems again.

### Added

- **Placement preview clarity**: `placement.kicad_pcb` now self-identifies
  as `(generator "schematic_engine placement_preview")` and uses
  `Placement_Preview:Missing_<ref>` placeholders when a footprint binding
  is absent — no more silent SOIC-8 fallbacks that look fabricatable.
- **`report.verify_report_fidelity(report_text, components)`** —
  diagnostic that catches references to component refs, nets, or
  annotations that don't exist in the resolved design (Task 172).
- **`symbol_cache.component_def_to_cache_payload()`** helper — serializes a
  ComponentDef with full pin / power / bypass / strap topology so cache
  entries round-trip as trusted on the next session (Task 169).
- **Five-archetype generation corpus**
  (`tests/test_generation_corpus.py`): LED driver, IoT sensor, motor
  controller, USB bridge, FPGA power carrier. Each runs `generate` end-to-
  end and enforces schematic / PCB / report invariants (Task 174).

### Changed

- **Cache-rebuilt components without pin data are now flagged
  `pinout_source="stub"`**, so the existing `pinout-source` validator
  correctly rejects multi-pin ICs that would otherwise ship as silent
  2-pin passives. This is the class of bug the IoT AQ audit hit on
  BME688 / LED1 / SW1 — fixed at the resolver so every user's cached
  parts are covered, not just one design (Task 169).
- **`assemble_sheet` now dedupes structural duplicates before emission**:
  symbol instances by `(lib_id, ref, at, uuid)`, wires by sorted
  endpoints, labels by `(kind, text, at)`, no-connects / junctions by
  position. Catches any upstream double-emission in the placer /
  topology dispatchers (Task 170).
- **`generate_artifacts` enforcement is now deterministic across runs**:
  structural + implementation category errors always raise, regardless
  of `require_valid`. `--no-require-valid` now only bypasses soft
  electrical warnings, and that bypass is logged at WARNING level
  (Task 173).

### Fixed

- **PCB placement preview no longer fabricates wrong footprints or
  synthetic pads** — previously any non-recognized footprint fell back to
  SOIC-8, and every footprint got exactly two 1.27-pitch SMD pads
  synthesized regardless of real pad count. ESP32 modules shipped with 2
  pads, barrel jacks with 0. Now the preview emits reference locations
  only — KiCad's schematic→PCB forward annotation is the authoritative
  source of pads (Task 171).

### Tests

- Added `tests/test_schematic_invariants.py` — reusable
  `assert_schematic_invariants()` helper + reproducer test that would
  detect the IoT AQ regression (Task 170).
- Added `tests/test_pcb_preview_invariants.py` — 3 tests proving the
  preview PCB never fabricates footprints or synthetic pads (Task 171).
- Added `tests/test_report_fidelity.py` — 5 tests covering clean report,
  ghost refs, ghost nets, annotation ghost claims, reconstructed IoT AQ
  audit scenario (Task 172).
- Added `tests/test_generate_enforcement.py` — 4 tests proving hard
  validation errors can't be bypassed via `--no-require-valid`, soft
  warnings can, verdicts are deterministic across runs (Task 173).
- Added `tests/test_generation_corpus.py` — 6 tests (5 archetypes + 1
  breadth guard) running full `generate_artifacts` pipeline with
  invariants (Task 174).
- Added 3 new cases to `tests/test_resolver_chain.py` covering cache
  stub flagging, full-topology cache round-trip, and validator
  integration (Task 169).
- Full suite at sprint close: **737 passed, 1 skipped, 0 failed**.

### Sprint 39 — Research Workflow Compatibility

### Fixed

- **Step 2 IC research guidance now stays in the current agent session** for
  Codex / Claude / OpenCode workflows instead of steering `sonar-pro` users
  into delegated `/research` / `research-analyst` paths that can trigger model
  conflicts. The docs now tell agents to fall back to native web tooling in the
  same session and persist the backend that actually ran.

### Changed

- **Research backend help text and persistence docs now describe the generic
  research workflow**, not a specific subagent implementation. README, workflow
  docs, and skill prompts were synchronized to match the real behavior.
- **Research workflow now has a latency selector**. `design-wizard` can persist
  `research_depth={fast,normal}` into spec metadata, `doctor` reports the
  effective depth, and the Circuit Weaver skill uses a smaller query budget in
  `fast` mode.

## [0.26.1] - 2026-04-22

### Fixed

- **Resolver credential checks now honor the real credential sources** used by
  the loaders (`env` or `secrets.env`) instead of looking only at process
  environment variables. DigiKey now treats both `DIGIKEY_CLIENT_ID` and
  `DIGIKEY_CLIENT_SECRET` as required for the tier, and Mouser honors
  `MOUSER_SEARCH_API_KEY` from the shared credential loader as well.
- **`circuit-weaver doctor` now surfaces DigiKey and Mouser credential status**,
  so the Task 156 skip message ("Run `circuit-weaver doctor` to configure")
  now points users to actionable diagnostics.
- **Saved research artifacts are now traceable from `design.log`**. Research
  entries include the effective backend and the canonical `{project}/research/{slug}.json`
  path written by `save-research`.

### Changed

- **`design-wizard --research-backend ...` now persists the effective backend**
  into the scaffolded spec metadata and the initial `design.log` step, so the
  downstream Codex / Claude / OpenCode workflow can actually honor the selector.
- **`skills/circuit-weaver/SKILL.md` and `README.md` now describe the real
  research workflow**: resolve backend first, use native web tooling for
  `standard`, and persist every run through `circuit-weaver save-research`.

### Tests

- Added regression coverage for DigiKey secret handling, credential-loader based
  resolver fallback, doctor credential reporting, `design.log` research artifact
  paths, and `design-wizard` research-backend persistence.
- Full suite at release cut: **708 passed, 1 skipped, 0 failed**.

## [0.26.0] - 2026-04-22

### Sprint 37 — Observability, Research Pipeline & Resolver Polish

Three categories bundled into one release, all driven by user reports
against v0.25.x.

### Added — Observability (Task 159, user-reported)

- **`circuit-weaver.log` now covers every subcommand**, not just `generate`. The
  dispatcher calls `init_logging_for_cli(args.command, args)` before dispatching,
  so `validate`, `confidence`, `simulate`, `erc`, `cost-bom`, `export-*`, etc.
  all produce a log file in the project directory.
- **Log directory heuristics:** file-style `--output` (e.g. `report.html`) →
  log to its parent dir; directory-style → log to that dir; otherwise fall back
  to the spec file's parent (so `validate design.yaml` with no `--output`
  still writes next to the YAML). Administrative commands (`doctor`,
  `discover`, `list-templates`, `cache`, `install-skills`, ...) skip file
  logging entirely to avoid littering CWD.
- **`log_workflow_step(command, step, message, details)`** helper emits
  `[command:step]` markers to both `circuit-weaver.log` and `design.log`.
  Handlers for `validate`, `generate`, and `save-research` now mark
  their entry points.
- **`CIRCUIT_WEAVER_LOG_LEVEL`** env var controls root level (default
  `INFO`; set `DEBUG` for byte-level trace).
- **Resolver tier resolution is now INFO-level** (was DEBUG) so users
  see which MPN resolved via which tier without enabling debug mode.

### Added — Research pipeline (Tasks 160 & 161, user-reported)

- **`circuit-weaver save-research`** new CLI subcommand. Reads a JSON
  payload from stdin or `--file`, writes `{project_dir}/research/{slug}.json`,
  `{slug}.md`, and a rebuilt `summary.md` index. Structured record
  includes: topic, query, backend, summary, findings (mpn/cost/notes),
  citations (title/url/snippet), timestamp.
- **`src/circuit_weaver/research_store.py`** new module: atomic-write
  persistence (`tmp`→rename), markdown renderer, summary-index rebuild,
  `list_research_topics()` for downstream tooling.
- **`--research-backend {auto,sonar-pro,standard}` on `design-wizard`**,
  plus `CIRCUIT_WEAVER_RESEARCH_BACKEND` env var. `auto` (default) picks
  `sonar-pro` when `PERPLEXITY_API_KEY` is set, else `standard` (Claude
  native WebSearch). Requesting `sonar-pro` without a key degrades to
  `standard` with an INFO log rather than crashing.
- **`src/circuit_weaver/research.py`** new module: `resolve_backend()`
  + `backend_info()` for the selection logic.
- **`doctor` reports research backend** — effective backend, whether
  `PERPLEXITY_API_KEY` is set, env var state.

### Added — Resolver polish (Tasks 156, 157, 158)

- **Task 156 — credential skip visibility:** `SymbolResolver` emits one
  INFO log per session when DigiKey or Mouser tier is skipped because
  `DIGIKEY_CLIENT_ID` / `MOUSER_SEARCH_API_KEY` is unset. Dedupe cache
  prevents spamming the log on large designs.
- **Task 157 — hermetic end-to-end regression test:** `tests/test_resolver_e2e.py`
  runs the user-reported Zigbee air-sensor YAML (SHT41 / SGP40 / nRF52840)
  through the full chain with mocked `load_from_digikey`. Asserts no MPN
  falls to a stub when a tier should have caught it.
- **Task 158 — legacy templates honour `register_ic()`:** `audio_amplifier`,
  `motor_driver`, and `protection` now merge `ic_data` entries for their
  topology into their hardcoded `*_IC_DATABASE` dicts via `ic_data.merge_into_legacy_db`.
  Users can now `register_ic("TEST-NEW-TVS", {...})` and have the new IC
  resolve through the legacy `type: protection` template as well as
  `DataDrivenTemplate`.

### Fixed

- `ic_data.merge_into_legacy_db` converts dict-shaped pin entries to
  `PinDef` instances so legacy `generate()` paths (which iterate pins as
  dataclass objects) don't choke on user-registered JSON.

### Tests

- **`tests/test_logging_workflow.py`** — 10 tests covering log-dir
  resolution, CLI subcommand log file creation, workflow markers, and
  `CIRCUIT_WEAVER_LOG_LEVEL=DEBUG` propagation.
- **`tests/test_research_store.py`** — 16 tests covering slugify, JSON
  persistence, Markdown rendering, summary index, atomic write, and CLI.
- **`tests/test_research_backend.py`** — 12 tests covering backend
  resolution precedence, env-var handling, and doctor integration.
- **`tests/test_resolver_e2e.py`** — 2 end-to-end tests locking in the
  Zigbee air-sensor user scenario with mocked DigiKey.
- **`tests/test_legacy_template_hotload.py`** — 4 tests proving
  `register_ic()` entries flow into audio_amplifier / motor_driver /
  protection template classes.
- **Task 156 regression test** added to `tests/test_resolver_chain.py` —
  asserts exactly one "DigiKey tier skipped" INFO log per session.

**Total: 702 passed, 1 skipped, 0 failed. Ruff clean.**

---

## [0.25.0] - 2026-04-22

### Sprint 35 — Install-UX hardening & platform parity

Closes three P0 footguns identified in the v0.24.x review.

### Added
- **`circuit-weaver install-skills` collision protection** — an existing `SKILL.md` with content different from the bundled source is now left untouched by default. The command reports each skipped skill and exits with status `partial`. New flags:
  - `--force` — overwrite existing `SKILL.md` files that differ from source
  - `--backup` — when overwriting, preserve the prior `SKILL.md` as `SKILL.md.bak.YYYYMMDD_HHMMSS`
  - `--dry-run` — walk the install plan without touching the filesystem
  - Result dict now includes `skills_skipped` and `skills_unchanged`
- **`scripts/sync_bundled_skills.py`** — keeps `src/circuit_weaver/_bundled_skills/` in sync with `skills/`. Run with `--check` in CI / pre-commit to fail on drift.
- **`bundled-skills` CI job** in `.github/workflows/ci.yml` — verifies parity between `skills/` and `_bundled_skills/` on every push.
- **`sync-bundled-skills` pre-commit hook** — prevents divergence at author time.
- **Windows CI leg** (`windows-latest` / Python 3.12, currently non-blocking) — smoke-tests CLI commands, doctor, skill installer, and `python -m circuit_weaver --version`.

### Changed
- **Bundled skills now include all 11 workflow skills** (`bom`, `circuit-weaver`, `design_wizard`, `digikey`, `ee`, `jlcpcb`, `kicad`, `lcsc`, `mouser`, `pcbway`, `vivado`). Previously only the stale `circuit-weaver` skill (410 of 651 lines) shipped in the PyPI wheel.
- `README.md` and `docs/agent-platforms.md` now describe the collision policy and new flags.

### Tests
- `tests/test_skill_installer.py` — 12 new tests covering collision skip, force overwrite, backup, dry-run, unchanged no-op, and bundled-parity.

### Sprint 36 — Resolver Chain Fix

Fixes the reason v0.24.x stubbed common parts (SHT41-AD1B-R2, SGP40-D-R4, nRF52840) that were available on DigiKey, Mouser, and in `ic_data` JSON — the standalone-component resolver never looked there.

### Fixed
- **`project_spec._resolve_component`** now delegates to `SymbolResolver`. Previously it had an ad-hoc 3-tier chain (ComponentRegistry → KiCad lib → EasyEDA) that ignored the cache, DigiKey, Mouser, and the `ic_data` JSON store that shipped in v0.24.0. Any MPN not in one of those three tiers became a stub, even if DigiKey/Mouser carried it.
- **`ic_data.register_ic()` deadlock** — the function held `_db_lock` while calling `_get_db()`, which also tries to acquire the same non-reentrant lock. First call to `register_ic()` after `reload()` hung indefinitely. Fixed by resolving the db reference before acquiring the mutation lock.

### Added
- **`SymbolResolver` Tier 2: ic_data JSON store** — resolver now consults `ic_data.get_ic_data()` between `ComponentRegistry` (Tier 1) and the KiCad library (Tier 3). The 7-tier chain is now: registry → ic_data → kicad_lib → cache → easyeda → digikey → mouser → unresolved.
- **`ic_data.ic_data_to_component_def(mpn, data)`** — converts a JSON IC entry into a `ComponentDef` with pins, footprint, power_pins auto-derived from power_in pin types, and topology-based category/ref_prefix mapping. Returns `None` for entries that lack a usable `pins` list.
- **Stub reason now enumerates all 7 tiers** — so the diagnostic output makes it obvious what was tried and what the user can register to fix it.

### Tests
- **`tests/test_resolver_chain.py`** — 6 regression tests: registry-wins-first, ic_data-resolves-template-part (DS3231), register_ic-hot-load-visible-immediately, DigiKey-rescues-unknown-MPN (mocked SHT41-AD1B-R2), project_spec-delegates-to-SymbolResolver, unresolved-falls-to-informative-stub.

---

## [0.24.1] - 2026-04-22

### Sprint 34 — Follow-up hardening

Post-release review follow-ups for the data-driven template engine. No
functional changes to generated schematics.

### Changed
- **`register_ic()`** now writes atomically via tmp-file rename, preventing a corrupted `custom.json` if the process is interrupted
- **`register_ic()`** falls back to the user data directory (`$XDG_DATA_HOME/circuit-weaver/custom.json` on POSIX, `%APPDATA%/circuit-weaver/custom.json` on Windows) when the installed package directory is read-only — previously raised `PermissionError` on system/container Python installs
- **IC data loader** now merges `custom.json` overlays from both the package and user data dirs
- **`_get_db()`** lazy initialization is now thread-safe via `threading.Lock` (double-checked locking)

### Added
- **USB-C `source_current` param** (`usb_c_connector.py`): when `role="source"`, selects Rp per USB-C Rev 2.1 Table 4-25 — `"default"` → 56k (USB 2.0 / 500 mA), `"1.5A"` → 22k, `"3A"` → 10k. Previous behavior (56k) remains the default.

### Fixed
- **CHANGELOG v0.24.0**: removed inaccurate claim that `audio_amplifier.py`, `motor_driver.py`, `protection.py` had been migrated to the data-driven builder path. The `DataDrivenTemplate` infrastructure is in place and serves as a registry fallback, but migration of those three legacy modules is deferred to a follow-up sprint.

---

## [0.24.0] - 2026-04-21

### Sprint 34 — Data-Driven Template Engine

This release replaces the hardcoded subcircuit template classes with a JSON-driven IC data system plus dynamic topology builders, and expands the template library from 30 to 37 entries.

### Added
- **`src/circuit_weaver/ic_data/`** (new package): 11 JSON files — `amplifier.json`, `bus_interface.json`, `connector.json`, `converter.json`, `custom.json`, `linear_regulator.json`, `memory.json`, `misc.json`, `oscillator.json`, `protection.json`, `switching_regulator.json` — storing IC pin maps, topology classifiers, and electrical parameters
- **`ic_data/__init__.py`**: `load_ic_data()`, `register_ic()`, and lookup helpers for the new data store
- **`subcircuits/topology_builders.py`** (new module): dynamic builders — `build_switching_regulator()`, `build_linear_regulator()`, `build_generic()` — that read JSON IC data and produce schematic fragments without hardcoded template classes
- **7 new subcircuit templates** (brings total to 37):
  - `rtc.py` — real-time clocks (DS3231, PCF8563, MCP79410)
  - `eeprom.py` — I2C/SPI EEPROMs (24LCxx, 25LCxx, M95xxx families)
  - `wireless_module.py` — BLE/WiFi/LoRa modules (nRF52-DK, ESP32, RYLR896)
  - `usb_c_connector.py` — USB-C receptacles with CC/SBU routing
  - `spi_bus.py` — SPI bus conditioning (pull-ups, chip-select matrix)
  - `voltage_reference.py` — precision voltage references (REF5025, LM4040, ADR4540)
  - `connector.py` — generic pin-header/through-hole connectors
- **`scripts/extract_ic_data.py`**: harvests IC data entries from datasheet research output
- **`docs/ic-data-system.md`**: reference documentation for the new IC data system

### Changed
- **`subcircuits/base.py`** (+90 lines): `DataDrivenTemplate` class and `SubcircuitRegistry._get_data_driven()` fallback — when the registry has no hardcoded template for a type, it constructs one from JSON IC data and routes through `topology_builders`
- **`list-templates` CLI**: shows data-driven entries alongside legacy templates
- **`scaffold` CLI**: supports data-driven template parameters
- **Minor refactors** to `audio_amplifier.py`, `motor_driver.py`, `protection.py` (misc. tidy-ups; these templates remain hardcoded for now — migration to the data-driven path is a follow-up sprint)
- **Docs refreshed**: `docs/templates.md`, `docs/cli-reference.md`, `scripts/gen_template_docs.py`

### Fixed
- **`.pre-commit-config.yaml`**: updated `circuit_weaver.mvp` references to `circuit_weaver` (post-dispatcher rename)
- **`tests/test_cli_commands.py`**: uses `python -m circuit_weaver` module form

### Tests
- **380 new lines** in `tests/test_template_structure.py`: structural tests for the 7 new templates + IC data store loading/lookup/registration

---

## [0.23.0] - 2026-04-10

### Sprint 31 — Bug Fixes & Error Handling Hardening

### Fixed
- **`_score_from_issues()`** in `confidence_dashboard.py`: returned 100.0 when `total_checks=0` even with errors > 0
- **Logging bridge thread safety**: added `threading.Lock` for `_current_logger` and `_file_handler`; `cleanup_logging()` now uses try/finally
- **Connector MPN validation**: connectors ("J" prefix) no longer exempt from MPN checks in cross-reference validator
- **SPICE value parser**: handles `100pH` (picohenry), values with spaces, standalone F/H units
- **CLI error handling**: confidence, simulate, discover, log-event handlers wrapped in try/except with user-friendly messages

### Sprint 32 — CLI Integration Tests & Output Standardization

### Added
- **24 CLI integration tests** in `tests/test_cli_new_commands.py`: end-to-end tests for discover, simulate, confidence, log-event, log-status, log-view
- Informational messages moved to stderr (HTML report path, simulation output path, log-event confirmation)

### Sprint 33 — Platform Compatibility & Skill UX

### Added
- **OpenCode/Kilo sim shim**: `.agents/skills/sim/SKILL.md` with trigger phrases and CLI commands
- **Updated circuit-weaver shim** with full CLI command reference (discover, validate --enhanced, simulate, confidence, log-event)
- **Platform Guidance** section added to all 9 remaining skills (bom, digikey, mouser, lcsc, jlcpcb, pcbway, ee, vivado, kicad)
- **Skill trigger disambiguation**: bom, design_wizard, kicad, sim skills now have explicit "use X instead for Y" guidance

## [0.22.0] - 2026-04-10

### Sprint 26 — Logging Overhaul

### Added
- **9 new DesignLogger event types** in `design_logger.py`: `part_lookup`, `symbol_resolution`, `simulation`, `thermal`, `erc_drc`, `scoring`, `sourcing`, `generation`, `error` — each with structured JSON fields
- **`logging_bridge.py`** (new module): `DesignLogHandler` bridges Python's `logging` module to `DesignLogger`; `init_logging()` creates both `design.log` (JSON Lines) and `circuit-weaver.log` (text) from workflow start
- **`log-event` CLI subcommand**: skills can call `circuit-weaver log-event <dir> --type <type> --message <msg>` for structured logging
- **Instrumented 7 modules**: `erc_runner`, `dfm_checker`, `validator`, `design_scorer`, `thermal_analysis`, `exporters`, `spice_fetcher` now log to `design.log` via bridge

### Tests
- `tests/test_design_logger_extended.py`: 33 tests — all new event types, bridge routing, singleton accessors, CLI subcommand, extended summary

### Sprint 27 — Project Discovery & Skill Auto-Detection

### Added
- **`project_discovery.py`** (new module): `discover_projects()` scans CWD for `design.yaml`, `.kicad_pro`, and `.kicad_sch` projects with status/type inference and depth control
- **`discover` CLI subcommand**: `circuit-weaver discover [--root .] [--depth 2] [--json]` for project auto-detection
- **Skill auto-detection**: `circuit-weaver`, `design_wizard`, `kicad_validate`, and `sim` skills now run `discover --json` before asking for paths

### Changed
- **`_find_existing_circuits()`** in `dispatcher.py` refactored to use `discover_projects()` from the new module

### Tests
- `tests/test_project_discovery.py`: 20 tests — discovery by type, depth limiting, status inference, CLI output

### Sprint 28 — Circuit Simulation Engine

### Added
- **`spice_netlist.py`** (new module): generates SPICE `.cir` netlists from `ComponentDef` lists with R/C/L primitives, subcircuit instances, and `.tran`/`.ac`/`.dc`/`.op` analysis cards
- **`spice_runner.py`** (new module): ngspice subprocess runner with graceful degradation (`status="skipped"` when not installed), `.raw` file parser, metric extraction (ripple, phase margin, operating points)
- **`simulation.py`** (new module): orchestrator with `plan_simulations()` (auto-detects power/signal/thermal targets), `run_design_simulations()`, and `score_simulation_confidence()` (0-100 scoring)
- **`resolve_spice_models()`** in `spice_fetcher.py`: reads manifest.json to link downloaded models for simulation
- **`simulate` CLI subcommand**: `circuit-weaver simulate design.yaml [-o ./sims] [--type power|signal|thermal|all]`

### Tests
- `tests/test_spice_netlist.py`: 15 tests — netlist generation, passive conversion, analysis cards
- `tests/test_spice_runner.py`: 10 tests — graceful degradation, raw parsing, metric extraction
- `tests/test_simulation.py`: 11 tests — plan detection, confidence scoring, orchestrator integration

### Sprint 29 — Enhanced Validations & Cross-Reference

### Added
- **3 new validation checks** in `validator.py`: `power-budget`, `thermal-limits`, `signal-integrity` (total now 14 checks)
- **`cross_reference_validator.py`** (new module): 3 audit passes — spec vs schematic, schematic vs BOM, component consistency (duplicate refs, floating power pins)
- **`--enhanced` flag** on `validate` CLI: runs cross-reference audit alongside standard validation
- Updated `kicad_validate` skill with new CLI commands

### Tests
- `tests/test_enhanced_validation.py`: 16 tests — all new checks and cross-reference passes

### Sprint 30 — Confidence Dashboard & Workflow Integration

### Added
- **`confidence_dashboard.py`** (new module): aggregates 7 data sources (electrical, simulation, thermal, SI, DFM, cross-reference, ERC/DRC) into weighted 0-100 confidence score with HTML/terminal/JSON output and readiness classification (`ready_for_fab`/`needs_review`/`not_ready`)
- **`confidence` CLI subcommand**: `circuit-weaver confidence design.yaml [--run-sims] [-o report.html] [--pcb file]`

### Changed
- **Wizard workflow restructured** (both `circuit-weaver` and `design_wizard` skills):
  - Step 6 is now **Confidence & Simulation Check** (runs automatically after generation)
  - Step 7 is now **PCB Layout Preparation** with placement optimizer, interactive viewer, SVG export/import, autorouting, and DFM check
  - Step 8 is now **Design Review** with 13-option action menu organized by category
  - All project-skills (`kicad_pcb_place`, `autoroute`, `kicad_gen`, `kicad_hierarchy`, `kicad_pinmap`) are now cross-referenced from main skills
- **Existing Design menu** expanded from 8 to 13 categorized options (Verify / Generate & Layout / Export / Other)

### Tests
- `tests/test_confidence_dashboard.py`: 23 tests — scoring, weight redistribution, readiness, HTML/terminal output, CLI

## [0.21.0] - 2026-04-08

### Sprint 25 — Explainability & Test Points

### Added
- **`_generate_rationale_section(design_ir, log_entries)`** in `review_report.py`: new "Component Selection Rationale" section in the HTML review report. Per-IC table shows why each component was selected (description, key specs from params, wizard/research log entries). Falls back to "Selected via component registry — verify against datasheet" when no rationale is recorded.
- **`_extract_component_rationale(block, log_entries)`** helper: extracts rationale from `DesignBlock.description`, `params` (vin/vout/current/frequency etc.), and optional `design.log` entries.
- **`generate_review_report_html(..., log_entries=...)`**: new optional `log_entries` param threads design.log data into the rationale section.
- **`src/circuit_weaver/test_point_gen.py`** (new module): automatic test point generation from DesignIR.
  - `generate_test_points(design_ir)` — classifies nets as `power_rail`, `ground`, `clock`, `data_bus`, or `differential`; names them TP1, TP2, …
  - `write_test_points_csv(test_points, path)` — emits `{project_name}_test_points.csv` with columns `TestPoint, Net, Type, Priority`
  - `annotate_schematic(content, test_points)` — inserts `(text …)` labels into `.kicad_sch` content
  - `generate_test_point_artifacts(design_ir, output_dir, project_name, schematic_path)` — orchestrates CSV + schematic annotation
- **`generate_artifacts()`** in `dispatcher.py` auto-calls `generate_test_point_artifacts` after schematic generation; result stored under `result["test_points"]`.

### Tests
- `tests/test_review_report.py`: 4 tests — rationale renders per IC, missing rationale shows fallback, HTML escaping correct, key specs from params appear.
- `tests/test_test_point_gen.py`: 6 tests — power rails detected, differential pairs from net names, differential pairs from pcb_constraints, CSV format correct, schematic annotation added, empty design handled.

## [0.18.0] - 2026-04-08

### Sprint 22 — Pinout Verification Gate

### Added
- **`pinout_source` field on `ComponentDef`** (`"explicit"` | `"kicad_library"` | `"stub"`): tracks provenance of every IC's pin assignment. Defaults to `"explicit"` for registry/library components.
- **`pinout_verified` flag on `ComponentDef`**: opt-in override (`pinout_verified: true` in YAML) for users who have manually confirmed a stub pinout against the datasheet.

### Changed
- `digikey_loader.py` and `mouser_loader.py`: replaced silent `"STUB: verify pinmap"` annotation strings with structured `pinout_source="stub"`. Stub ICs no longer silently pollute the annotations list.

### Fixed
- **Task 116 — Pinout Source Validation**: added `_validate_pinout_sources()` check to `validator.py`. Any IC with `pinout_source="stub"` and `pinout_verified=False` now emits `ValidationIssue(level="error", code="unverified-pinout")`, preventing malformed stub schematics from reaching users.
- **Task 117 — Remove STUB annotations**: eliminated all four STUB annotation strings from DigiKey/Mouser loaders; pin provenance is now a typed field rather than an opaque string.
- **Sprint 22 follow-up hardening**:
  - `pinout_verified: true` now flows through standalone YAML/project-spec resolution instead of only direct `ComponentDef` construction
  - explicit YAML `pin_map` now upgrades distributor stubs to `pinout_source="explicit"` and rebuilds concrete pins/nets for generation
  - pinout-source validation now skips only truly pinout-irrelevant passives; diode-style stubs no longer bypass the gate

### Tests
- Added `tests/test_validator.py` with 10 unit tests: stub IC fails, explicit IC passes, spec-level `pinout_verified` and `pin_map` overrides work, passive skipped, diode stub fails, mixed design, multiple stubs, check registration in `_VALIDATION_CHECKS`.

### Sprint 24 — Firmware Co-Design Export

### Added
- **`src/circuit_weaver/firmware_export.py`** (new): `export_pinout_csv()`, `export_stm32_ioc()`, `export_esp32_sdkconfig()`. MCU detection via MPN prefix (STM32*, ESP32*, RP2040, ATmega*, etc.). `infer_peripheral()` maps net names to peripheral types; `infer_direction()` maps PinDef electrical type to IN/OUT/IO/PWR.
- **`generate_artifacts()` auto-emits firmware stubs**: when MCU components are present, `{project}_pinout.csv`, `{project}.ioc` (STM32), and `sdkconfig.defaults` (ESP32) are written to the output directory. Result dict gains `"pinout_csv"`, `"stm32_ioc"`, `"esp32_sdkconfig"` keys.
- **`generate --pinout` flag**: forces pinout CSV emission for non-MCU designs.

### Tests
- Added `tests/test_firmware_export.py` with 22 unit tests covering Tasks 120–122.

### Sprint 23 — Post-Generation ERC

### Added
- **`src/circuit_weaver/erc_runner.py`** (new): `run_erc(schematic)` invokes `kicad-cli sch erc --format json`, parses violation array into `ErcResult` / `ErcViolation` dataclasses. `_classify_severity()` promotes 13 known error types regardless of kicad-cli's own severity field. Degrades gracefully: returns `status="skipped"` when KiCad CLI absent, `status="failed"` on timeout or parse error.
- **`erc` subcommand**: `circuit-weaver erc <schematic> [--json]` — runs ERC and prints human-readable or JSON output; exits 1 if any errors found.
- **`_generate_erc_section()`** in `review_report.py`: green badge when clean, red badge + violation table when errors, neutral notice when skipped or not run. Accepts both `ErcResult` objects and plain dicts.

### Changed
- `generate_artifacts()` now auto-runs ERC after schematic generation when the root schematic is available; adds `"erc"` key to the result dict.
- `generate_review_report_html()` gains optional `erc_result` parameter; ERC section rendered above DFM section.

### Tests
- Added `tests/test_erc_runner.py` with 12 unit tests: mock kicad-cli success, timeout, absent CLI, JSON parsing, severity classification/promotion, `to_dict` roundtrip, missing schematic.
- Added `tests/test_review_report_erc.py` with 6 unit tests: clean badge, error badge + table, skipped, failed, None input, dict input.

---

## [0.17.0] - 2026-04-07

### Sprint 20 — Design Review Completion & Sourcing Audit

### Added (Task 107 — Component Sourcing Risk Audit)
- **Component sourcing auditor** (`sourcing_auditor.py`):
  - `audit_bom()` — queries LCSC (stock/lead time) and DigiKey (lifecycle) for each component
  - Risk classification: CRITICAL (obsolete, out-of-stock, >16 week lead), WARNING (low stock <100, lead 8-16 weeks), OK
  - Identifies specific issues per component (out-of-stock, low stock, long lead time, obsolete, no distributor PN)
  - `AuditFinding` and `AuditReport` dataclasses for structured output
  - `audit_report_text()` for human-readable reports with CRITICAL/WARNING/OK sections and recommendations
  - Integrates with existing `PartsLookup` and `compile_design_ir()` for BOM analysis
  - 20 unit tests covering risk classification, issue identification, and report generation

### Changed (Task 109 — Dispatcher Refactor)
- **Renamed core module** `mvp.py` → `dispatcher.py`:
  - Reflects actual role: CLI subcommand dispatcher, not MVP
  - Updated all imports in source code (7 modules), tests (6 test suites), and documentation
  - Updated module docstring to clarify dispatcher + workflow engine role
  - Updated CONTRIBUTING.md, api-reference.md, architecture.md, cli-reference.md
  - All tests pass with new module name

### Fixed
- **Task 112 — Single-sheet schematic naming**:
  - Fixed single-sheet generation to write `{project_name}.kicad_sch` instead of generic `main.kicad_sch`
  - Preserved existing multi-sheet root naming behavior
  - Added bootstrap and CLI regression coverage for example-project artifact names
- **Task 113 — No .py artifacts in output**:
  - Confirmed current code never writes `.py` files to output directory
  - `--output` is required; all writes target the specified directory only
- **Task 114 — Circuit-weaver log file**:
  - `generate_artifacts()` now writes `circuit-weaver.log` to the output directory
  - Captures: validation warnings, component count, sheet allocations, file paths written
  - Key `print()` calls in `generator.py` converted to `_logger.info()` for file capture
- **Task 115 — S-expression paren-balance guard**:
  - Added `_validate_sexpr_balance()` called before each `.kicad_sch` write
  - Emits `_logger.warning()` (captured in log file) if open/close parens don't balance
  - Prevents silent malformed schematic output from reaching users

### Tests
- All 60+ existing tests pass with dispatcher.py module name
- Added 20 new tests for sourcing auditor (test_sourcing_auditor.py)
- Added 3 new Sprint 21 regression tests: no-.py-artifacts, log-file, paren-balance

---

## [0.16.0] - 2026-04-07

### Sprint 19 — Design Review & Quality Assurance

### Added (Task 104 — Design DFM Checker)
- **DFM (Design for Manufacture) checker** (`dfm_checker.py`):
  - Validates PCB design against fab capabilities: trace width, spacing, via size, annular ring, edge clearance
  - Supports JLCPCB (2/4-layer) and PCBWay profiles with customizable DFM rules
  - Parses `.kicad_pcb` S-expression format to extract traces, vias, board dimensions
  - Returns structured `DFMViolation` objects with severity (critical/warning), actual vs minimum values, fix suggestions
  - `check_dfm()` function and `dfm_report()` for human-readable output
  - CLI command: `circuit-weaver check-dfm <design.kicad_pcb> [--profile jlcpcb|pcbway]`

### Added (Task 108 — Design Documentation Generator)
- **Design documentation generator** (`design_docs.py`):
  - `generate_assembly_guide_csv()` — exports BOM table: reference, value, footprint, MPN, manufacturer
  - `generate_power_budget_csv()` — estimates power per supply rail based on component categories
  - `generate_ordering_checklist()` — markdown checklist with per-distributor status
  - `generate_datasheet_index()` — markdown index of all downloaded datasheets
  - `generate_all_docs()` — orchestrator to generate all documentation types at once
  - CLI command: `circuit-weaver generate-docs <design.yaml> --output docs/ [--datasheets-dir X]`
  - Supports integration with BOM workflow: `cost-bom → generate-docs`

### Added (Task 106 — Interactive Design Review Report)
- **HTML design review report generator** (`review_report.py`):
  - `generate_review_report_html()` — generates comprehensive, self-contained HTML reports
  - **Report sections**:
    - Design summary card with project name, version, overall score/grade, creation timestamp
    - Pre-fabrication checklist (8 standard review items, user-checkable)
    - Detailed scoring breakdown with visual bar charts for all 5 quality dimensions
    - DFM violations table (from Task 104) with severity, location, actual/minimum values, suggestions
    - Component BOM table (reference, value, footprint, MPN, manufacturer, category, qty)
    - Power distribution tree (rails, voltages, current, power budget)
    - Actionable recommendations based on gap analysis (sections < 75 score)
  - **HTML features**:
    - Embedded CSS with responsive grid layout (no external dependencies)
    - Professional styling with color-coded score badges
    - Print-optimized layout with proper page breaks
    - Fully self-contained (all data + styles in single HTML file)
  - CLI command: `circuit-weaver review-report <design.yaml> [--kicad-pcb board.kicad_pcb] --output report.html`
  - Optional DFM integration: analyzes violations from .kicad_pcb file if provided

### Added (Task 105 — Enhanced Design Scoring)
- **Comprehensive design quality scoring** (`design_scorer.py`):
  - `DetailedElectricalQualityScore` dataclass with per-section breakdown
  - **5 quality dimensions** (each scored 0-100, A-F grading):
    - **Power Integrity:** bulk capacitor presence, decoupling coverage, voltage regulator identification
    - **Signal Integrity:** pull-up/pull-down resistor detection, differential pair indicators
    - **Placement Quality:** component reference designation coverage, thermal constraints
    - **Thermal:** power component identification, operating temperature range specification
    - **Manufacturing:** MPN coverage, part sourcing bindings, assembly complexity (package variety)
  - Weighted composite score (20% each dimension) with letter grades A-F
  - `score_design_comprehensive()` analyzes `DesignIR` and returns detailed breakdown
  - `summary_with_gaps()` method flags sections < 75 score with actionable recommendations
  - CLI integration: `circuit-weaver validate --detailed-score` adds scoring to validation output
  - Fully autonomous scoring without PCB data (works on schematic/DesignIR only)

### Tests
- 50 new tests total:
  - 14 DFM checker tests: profiles, PCB parsing, violation detection, report generation
  - 17 design documentation tests: BOM table extraction, power budget calculation, CSV/markdown exports
  - 19 design scoring tests: score creation, all 5 dimensions, weighted composites, gap detection, grade mapping
  - All tests passing in ~0.25s total

### Changed
- `mvp.py`: Added three new CLI subcommands: `check-dfm`, `generate-docs`, `review-report` with full argument parsing
- `__init__.py`: Exported scoring, design_docs, DFM, and review_report functions in public API
- `validate` command: Added `--detailed-score` flag to include 5-dimension quality analysis

---

---

## [0.15.2] - 2026-04-07 (Hotfix)

### Fixed
- **Design wizard architecture**: Restructured to use SKILL.md instructions instead of broken Python-level platform detection
  - Python code cannot invoke Claude Code tools (AskUserQuestion) — only Claude instructions can
  - Removed non-functional `interactive_prompts.py` module (dead code)
  - Refactored `_run_design_wizard()` to use plain `input()` for CLI compatibility
  - Restored cross-platform support: Claude Code (buttons), Codex/OpenCode (conversational), CLI (terminal input)
- **Dependencies**: Removed unused `questionary` from optional dependencies

### Architecture
- Interactive prompts now handled by SKILL.md instructions (Claude invokes AskUserQuestion)
- Python `mvp.py` contains only CLI implementation (non-interactive input())
- CLI mode: `python -m circuit_weaver design-wizard` with plain input() prompts
- Skill mode: Invoked via `/circuit-weaver` in Claude Code for native interactive UI

---

## [0.15.1] - 2026-04-07

### Fixed
- Add missing import for interactive prompts module in mvp.py

---

## [0.15.0] - 2026-04-07

### Platform-Aware Interactive Prompts

### Added
- **Interactive prompts system** (`interactive_prompts.py`):
  - Auto-detects execution platform (Claude Code, Codex, OpenCode, CLI)
  - Uses native UI for each platform:
    - **Claude Code**: AskUserQuestion tool (interactive buttons/checkboxes, scrollable)
    - **Codex/OpenCode**: Conversational prompting (natural text responses)
    - **CLI**: Terminal UI with questionary (arrow keys, spacebar, Enter)
  - Graceful fallback across platforms

- **Refactored design-wizard**:
  - Uses `ask_form_section()` for grouped form sections
  - Uses `ask_multiple_choice()` for option selection
  - Uses `ask_text()` for free-form input
  - Better UX with section grouping (BASIC INFO, POWER SUPPLY, COMPONENTS & INTERFACES)

### Dependencies
- Added `questionary>=1.10.0` to optional `[ui]` dependencies

### Documentation
- Updated `/circuit-weaver` skill docs with platform support notes
- Documented fallback behavior and feature parity across platforms

### Tests
- All 42 CLI tests passing
- Full test suite: 299+ tests passing

---

## [0.14.2] - 2026-04-07 (Hotfix)

### Design Wizard & Logging Enhancements

### Added
- **Design Wizard UX Redesign**:
  - Form-like interface with grouped sections (Basic Info, Power Supply, Components & Interfaces)
  - Section headers and clear visual hierarchy
  - Indented input prompts for better readability
  - Structured summary display with labels
  - Improved next-steps guidance

- **Design Log Viewer** (`log-view` command):
  - New `circuit-weaver log-view <project_dir>` command for viewing recent log entries
  - Filter by entry type: `wizard_step`, `cli_call`, `validation`, `research`
  - `--lines N` flag to show last N entries (default: 10)
  - Human-readable timestamp and entry type formatting
  - Helps troubleshoot issues and understand project history

### Improved
- **Logging Visibility**:
  - Wizard now prominently displays log file location after project creation
  - Added `log-view` and `log-status` commands to next-steps output
  - Better integration between wizard and logging workflow

### Fixed
- Removed emoji characters for Windows terminal compatibility (cp1252 encoding)
- All terminal output now uses ASCII-safe characters

### Documentation
- Updated `docs/DESIGN_LOGGING.md` with `log-view` command documentation
- Updated `docs/cli-reference.md` with improved design-wizard and logging command details

### Tests
- All 37 CLI tests passing
- Full test suite: 299 passed, 1 skipped

---

## [0.14.1] - 2026-04-07

### Sprint 17 — Housekeeping & Test Coverage + Mechanical & API Enhancements

### Fixed
- Version mismatch: synced pyproject.toml, __init__.py, and test assertions to 0.14.0 (Task 97)
- .gitignore: added datasheets/, specs/, spice_models/, bom/orders/, *.bak (Task 98)
- CONTRIBUTING.md: updated release example from v0.11.0 to v0.14.0 (Task 101)
- architecture.md: updated roadmap — all sprints through 16 marked stable (Task 101)
- mvp.py: replaced SVG placement TODO with actual placement optimizer call (Task 101)

### Added (Sprint 17 — Housekeeping)
- `test_cli_commands.py` — 37 end-to-end CLI tests covering all 20 subcommands (Task 99)
- `test_enclosure_designer.py` — 7 tests for enclosure generation and package exports (Task 100)
- Parameterized --help test validates every subcommand accepts help flag
- JSON extraction helper for CLI output with prefix lines

### Added (Mechanical & API Enhancements — Post-Sprint)
- **OpenSCAD Enclosure Designer** (`enclosure_designer.py`):
  - `design-enclosure` CLI subcommand for parametric 3D-printable enclosure generation
  - `generate_enclosure_scad()` supports customizable wall thickness, clearance, component height, ports, mounting holes, and vents
  - Port types: USB-C, Micro-USB, barrel jack, round, rectangular
  - M3 mounting holes with countersink for PCB standoffs
  - `render_enclosure_stl()` calls OpenSCAD CLI to generate STL files (optional)
  - Full parametric OpenSCAD code generation — users can tweak dimensions and re-render in seconds

- **KiCad Python API Integration** (`kicad_placement_api.py`):
  - `check_kicad_available(min_version=6)` validates KiCad 6+ installed with platform-specific guidance
  - `detect_kicad_version()` CLI-based detection via `kicad --version` + pcbnew module import
  - `update_board_placements()` uses official KiCad pcbnew API for placement updates (robust, future-proof)
  - Automatic fallback to regex-based updates when API unavailable (offline, legacy versions)
  - Consistent result structure across both API and fallback paths
  - `import-placement` command now reports KiCad API status and uses official API when available

### Tests
- 44 new tests (37 CLI + 7 enclosure), all passing in 12.6s
- 255 total tests pass (full suite: 11:25 runtime)

### Documentation
- `docs/user_workflow.md` — Added 'Prototype Enclosure Design' section with examples
- Added openscad skill to Related Skills table
- `svg_placement.py` — Updated docstring with KiCad API integration notes

## [0.14.0] - 2026-04-07

### Sprint 16 — Advanced PCB Placement & Dual-Sided Assembly (complete)

### Added
- `placement_optimizer.py` — simulated annealing PCB placement optimizer with multi-objective cost functions (overlap, boundary, thermal proximity, zone affinity) (Task 87)
- `optimize-placement` CLI subcommand — `circuit-weaver optimize-placement <spec> --strategy balanced --board-width 100 --board-height 80`
- 5 placement strategies: `simple` (zone heuristic), `thermal`, `si`, `cost`, `balanced` (all combined)
- Reads Sprint 15 `specs/ic_thermal.json` and `specs/si_params.json` for thermal/SI-aware placement
- Deterministic placement with `--seed` flag for reproducible results
- `placement_viewer.py` — interactive HTML/SVG PCB placement viewer with dark theme (Task 90)
- `placement-viewer` CLI subcommand — runs optimizer then generates interactive HTML
- Viewer features: click-to-highlight nets, hover tooltips (MPN, value, position, power), thermal heatmap overlay toggle, CSV export button, category color-coding
- 16 tests covering optimizer (empty, single, multi-component, strategies, thermal warnings, determinism, specs loading) and viewer (HTML generation, file output, thermal overlay, CSV export, empty input)

### Added (P1/P2 completion)
- `si_constraints.py` — signal integrity constraint solver detecting USB/DDR/LVDS/PCIe/MIPI/Ethernet/CAN/RS-485 buses from net names and descriptions (Task 88)
- `si-constraints` CLI subcommand — impedance targets, differential pair detection, length-matching groups, routing rules
- `thermal_analysis.py` — junction temperature calculator with hotspot detection, proximity analysis, and thermal heatmap SVG generation (Task 89)
- `thermal-analysis` CLI subcommand with `--heatmap`, `--ambient`, `--specs-dir`
- `write_dual_sided_cpl()` in jlcpcb_export.py — splits placements into top/bottom CPL files with assembly mode warnings (Task 91)
- `export-dual-cpl` CLI subcommand with `--assembly-mode` flag
- `panelizer.py` — panel layout optimizer with breakaway positions, cost estimates, design rules for V-cut and mouse-bite (Task 92)
- `panelize` CLI subcommand with `--board-width`, `--board-height`, `--qty`, `--breakaway`

### Tests
- 16 tests in `test_placement_optimizer.py` (P0 tasks)
- 30 tests in `test_sprint16_remaining.py` (P1/P2 tasks) — all passing

## [0.12.0] - 2026-04-06

### Sprint 14 — Auto-Discovery + Visual Placement Editing

### Added
- **Task 85:** `symbol_cache.py` — 30-day TTL persistent cache for symbol resolution at `~/.cache/circuit-weaver/symbols/`
  - `SymbolCache.get()` / `.put()` / `.stats()` / `.clear()` interface with atomic index.json manifest
  - `cache stats` subcommand shows cache hit rate and size
  - `cache clear [--stale-only]` removes old/unused entries
- **Task 83:** `digikey_loader.py` — DigiKey API symbol autoloader with package-to-footprint mapping
  - `load_from_digikey(mpn)` queries DigiKey API, extracts package metadata, maps to KiCad footprints
  - Reuses `_search_digikey()` and `_get_credential()` from `parts_lookup.py` (no code duplication)
  - Creates minimal ComponentDef stubs when full symbol data unavailable
  - Graceful fallback when DIGIKEY_CLIENT_ID/SECRET missing
- **Task 84:** `mouser_loader.py` — Mouser Search API v1 symbol autoloader
  - `load_from_mouser(mpn)` queries Mouser, extracts package attributes, maps to KiCad footprints
  - Reuses `map_digikey_package_to_kicad()` for consistent package mapping across both APIs
  - Integrated as Tier 6 in symbol resolution chain (fallback after DigiKey)
  - MOUSER_SEARCH_API_KEY credential support
- **Task 83/84:** `symbol_resolver.py` — 6-tier unified symbol resolution chain
  - Tiers: registry → kicad_lib → cache → easyeda → digikey → mouser → unresolved
  - Lazy imports for DigiKey/Mouser loaders prevent startup failures when credentials absent
  - `resolve(mpn)` returns `(ComponentDef | None, source_str)` indicating which tier succeeded
  - `resolve_batch(items)` for bulk component resolution
- **Task 86:** Auto-MPN discovery during `generate` command
  - `--auto-source` flag: auto-discover and cache MPNs for unresolved components
  - `--update-spec` flag: write discovered MPNs/LCSC back to original YAML spec file
  - `_auto_source_report()` helper queries PartsLookup, DigiKey, and Mouser; returns summary stats
  - `update_spec_with_sourced_data()` in `project_spec.py` safely updates YAML specs (only fills blank fields)
  - Stderr output shows resolved component counts by distributor (DigiKey: N, Mouser: N, LCSC: N)
  - Enrich-parts mode enabled automatically when `--auto-source` is set
- **Task 93:** `svg_placement.py` — SVG-based bidirectional placement editor
  - `export_placement_svg(components, placements, board_w, board_h)` generates editable SVG with colored component rectangles
  - `import_placement_from_svg(svg_path)` parses user-edited SVG back to placement dict via regex transform parsing
  - `update_kicad_pcb_placements(kicad_pcb, placements)` updates .kicad_pcb footprint `(at ...)` clauses via regex
  - `update_cpl_placements(cpl_path, placements)` updates CPL CSV with new X/Y/Rotation values
  - `import-placement` subcommand: `circuit-weaver import-placement placement.svg design.kicad_pcb [--output-pcb FILE] [--dry-run]`
  - Auto-detects and updates `*_cpl.csv` siblings in same directory
  - Color-coded by component category (power=red, digital=blue, connector=green, passive=yellow)
  - Back-layer components drawn with 0.5 opacity and dashed borders
  - Component size heuristics for 20+ common packages (0402, SOT-23, SOIC-8, QFN, BGA, etc.)
  - `--svg-placement` flag in `generate` command exports placement.svg to output directory
  - SVG is text/XML and git-friendly for design review

### Changed
- `generate_artifacts()` signature: added `auto_source`, `update_spec`, `spec_path`, `svg_placement` parameters
- `generate` dispatch: passes auto-source flags from CLI to `generate_artifacts()`

### Tests
- All new modules compile and import successfully
- Symbol resolver 6-tier fallback chain verified
- (Full test suite: running)

## [0.10.2] - 2026-04-06

### Sprint 11 — Team Adoption & Collaboration (completion)

### Added
- `.github/workflows/validate-design.yml` — CI workflow that runs `circuit-weaver validate --strict` on all sample and example specs when design files change (Task 59)
- `.pre-commit-config.yaml` — ruff lint+format, YAML syntax validation, and design validation hooks (Task 62)
- `pre-commit>=3.7` added to `[dev]` optional dependencies
- `docs/api-reference.md` — public Python API with signatures, parameters, return types, and usage examples (Task 63)
- `docs/cli-reference.md` — all 13 CLI subcommands with flags, examples, and exit codes (Task 63)
- `docs/validation-codes.md` — all 10 validation check categories with severity, sub-codes, and fix guidance (Task 63)
- `docs/design-ir-schema.md` — annotated YAML schema for the canonical design IR (Task 63)
- "Reference Documentation" table in README linking all 5 reference docs
- "CI/CD Integration" section in README documenting both CI workflows
- "Contributing" section in README with pre-commit setup instructions
- Design validation CI badge in README header

### Changed
- Task 68 (`presentation_wiring_policy` removal) closed as won't-fix — field is actively used in placer.py, generator.py, allocator.py, and mvp.py for support-passive rendering
- Task 61 (costed BOM) confirmed already complete from Sprint 12 — checkboxes updated

### Tests
- Design validation CI validates all `samples/` and `examples/` specs on every push

## [0.10.1] - 2026-04-05

### Sprint 12 — Platform Integrity: Guided CLI Workflow

### Added
- `/circuit-weaver` master orchestrator skill — LLM-first skill that routes new vs existing designs, orchestrates research-analyst agent (Perplexity IC research), CLI subcommands, and generates quote-ready outputs
- Skill implementation: `skills/circuit-weaver/SKILL.md` with Steps 0-7 (new design: welcome → requirements → research → BOM → schematic → review → export; existing design: load → validate/regenerate/export/modify)
- `.agents/skills/circuit-weaver/SKILL.md` compatibility stub for Claude Code discovery
- `cost-bom` CLI subcommand — generates costed BOM with LCSC pricing at multiple volume breaks (1, 10, 100, 1000 qty)
- `cost_bom.py` module: queries LCSC, extracts price tiers, calculates per-board and total costs
- Extended `parts_lookup.py` with `get_unit_price()` helper to select correct price tier for given quantity
- Price tier parsing in `_search_lcsc()` — converts API `extra.prices` array to `[{min_qty, max_qty, unit_price}]` dicts
- Rewrote `design_wizard/SKILL.md` Step 3c to reflect real CLI workflow: `scaffold` → `apply-patch` → `cost-bom`
- All wizard steps now reference working CLI commands (removed non-existent analyze_schematic.py, kicad_gen, kicad_pcb_place, kicad_validate)
- Automated installation scripts: `install.ps1` (Windows) and `install.sh` (Mac/Linux) — handles Python package setup, PATH configuration, and Claude Code skill registration in one command

### Changed
- Design wizard Step 3c: shifted from abstract BOM description to concrete `scaffold`+`apply-patch`+`cost-bom` commands
- Design wizard Step 5a: replaced non-existent script call with `circuit-weaver validate` direct call
- Design wizard Step 6c: Freerouting made optional (separate installation required, graceful fallback)
- Installation process: automated via `install.ps1` (Windows) and `install.sh` (Mac/Linux) — one-command setup eliminates manual pip/PATH/skill registration steps

### Fixed
- Removed non-existent `comp.dnp` attribute check in `cost_bom.py` (ComponentDef has no DNP field)

### Tests
- `cost-bom samples/iot_sensor_node/iot_sensor_node.yaml --qty 1,10` verified working
- All wizard steps now use real, tested CLI commands
- Version bump: 0.9.0 → 0.10.1

## [0.9.0] - 2026-04-05

### Sprint 11 — Visual Design Diff (partial)

### Added
- `diff` CLI subcommand enhanced with `--svg` and `--output` flags for visual HTML diff reports
- `diff_renderer.py` module: structural block diff (added/removed/changed), metadata comparison, SVG side-by-side via KiCad CLI
- HTML report with summary cards, color-coded block table, inline SVG panels
- 5 diff tests covering add/remove/change/metadata/HTML output

### Sprint 10 — Close the Fab Gap

### Added
- `export-jlcpcb` CLI subcommand — exports BOM and CPL CSV files for JLCPCB assembly ordering
- `export-gerbers` CLI subcommand — wraps KiCad CLI to export Gerber and drill files with ZIP output
- Fabrication notes section in design reports (`_fab_notes_section()`) — auto-detects package types and recommends PCB specs (layer count, surface finish, assembly requirements)
- 5 new realistic sample designs: battery-powered IoT sensor, motor controller, OLED display module, USB-UART bridge, FPGA power carrier
- CPL placement data extraction from `generate_pcb_placement()` — returns both PCB file path and placement coordinates dict
- `jlcpcb_export.py` module with BOM grouping by (value, footprint, lcsc_pn) and CSV export
- BOM and CPL tests in `test_bootstrap.py`

### Changed
- `generate_pcb_placement()` signature: now returns `tuple[str, dict[str, tuple[float, float, float, str]]]` instead of just `str`
- Sample validation expanded: 8 total samples (3 original + 5 new), 159 tests passing (was 133)

### Fixed
- Circular import in `jlcpcb_export.py` resolved by deferring `compile_design_ir` import inside function

### Tests
- 159 total tests passing (8 samples × 4 validation tests each + other test suites)
- All 5 new samples pass validation with expected warnings (missing LCSC codes, floating pins on MCU)
- Version bump: 0.8.0 → 0.9.0

## [0.8.0] - 2026-04-05

### Sprint 9 — Unblock Day-1 Onboarding

### Added
- `list-templates` CLI subcommand with `--json` and `--verbose` flags
- `scaffold` CLI subcommand — emits valid YAML spec stubs from param_schema
- Auto-generated `docs/templates.md` reference (30 templates) via `scripts/gen_template_docs.py`
- `python-multipart` dependency for API extras
- Design wizard skill (`skills/design_wizard/SKILL.md`) — interactive 6-step circuit design workflow
- User workflow guide (`docs/user_workflow.md`)

### Changed
- Example `iot_sensor.yaml` cleaned up to pass validation
- Net connectivity check counts bypass_caps and straps as connections (FB/BST nets no longer flagged)
- MCU floating GPIO warnings summarized (28 warnings → 1 summary)
- Bootstrap tests tolerate KiCad CLI unavailability

### Fixed
- Net connectivity validator treated passive components (feedback dividers, pull-ups, bootstrap caps) as undriven — now recognizes passive pin type as valid driver
- 4 test_presentation.py failures caused by false-positive undriven-net warnings on internal subcircuit nets

### Tests
- 133 total tests passing (was 113 + 4 failures)
- Version bump: 0.7.0 → 0.8.0

## [0.7.0] - 2026-04-05

### Sprint 6–8 — Circuit Quality Overhaul

Comprehensive rework of circuit generation quality. The engine now catches floating pins,
validates power domains, checks bus completeness, and scores electrical quality — reducing
the ~50% error rate in generated circuits to near zero for template-based designs.

### Added
- **Pin-type-aware no-connect classification** (`generator.py`): replaces blanket NC with per-pin-type logic — errors on floating power_in, warns on floating inputs, silently NC's outputs and NC-named pins
- **`explicit_no_connects` field** on `ComponentDef`: templates declare intentionally unconnected pins (7 templates migrated: USB, clock, ethernet, opamp, relay_driver, sensor_frontend, charge_pump)
- **`_validate_pin_coverage()`** gate in MVP pipeline: catches floating pins at design-validation time
- **Power domain cross-check** (`_validate_power_domain_consistency`): conflicting voltage requirements, rail name vs voltage mismatch, missing bypass cap declarations
- **PWR_FLAG auto-generation**: one `PWR_FLAG` per unique power net per sheet for KiCad ERC compliance (`sexpr_pwr_flag_lib_entry`, `sexpr_pwr_flag_instance`)
- **Net connectivity graph**: single-pin-net (dangling) and undriven-net (input-only, no driver) detection
- **Enable pin validation**: detects floating EN/SHDN/CE pins on regulators that would prevent startup
- **Bus completeness checks**: I2C missing pull-ups, SPI floating CS, UART unpaired TX/RX
- **Intent annotations**: generator annotates schematics with "Unused: PIN(num): NC" summaries
- **Auto schema-driven `validate_params()`** on `SubcircuitTemplate` base class: type checking, required params, options validation, range bounds
- **Component contract validation** (`SubcircuitResult.validate_contract()`): components present, ICs have power pins, boundary ports declared
- **Inductor selection validator**: flags inductors < 0.1µH or > 100µH on switching converters
- **Capacitor voltage rating validator**: warns when rail voltage exceeds 80% of cap rating
- **In-process ERC** (`_validate_pin_type_conflicts`): output-to-output on same net = bus contention
- **Electrical quality scorer** (`score_electrical_quality`): weighted composite of pin coverage, decoupling, power pin coverage, validation pass rate — 0–100 score with A–F grade
- **`--strict` mode**: `validate_design(strict=True)` treats warnings as errors; CLI flag on validate subcommand
- **Design checklist generator** (`generate_design_checklist`): Markdown with checkbox items for pre-fab review
- **Fix suggestions** on `ValidationIssue`: `suggestion` field with actionable remediation text
- **EasyEDA pin type enrichment**: infers input/output/bidirectional from name patterns when EasyEDA type is "unspecified"
- **Pin count sanity check**: ICs with < 3 pins flagged as likely incomplete symbol
- **Footprint-to-pin consistency**: extracts pad count from footprint name (QFN-48, SOIC-8, BGA-121) and compares to symbol pin count

### Changed
- **Validation now has 10 checks** (was 4): feedback-divider, rc-lc-filter, crystal-load, decoupling, inductor-selection, cap-voltage, net-connectivity, enable-pins, bus-completeness, pin-type-conflicts
- **`_FEEDBACK_VREF`** expanded from 1 IC (AP62300TWU) to 7 (+ TPS62088, TPS61230A, MT3608, TPS63020, TPS63000)
- **`valid` computation** in `validate_design`: only errors cause failure (warnings are informational unless `--strict`)
- Power pin inference in `kicad_lib.py` expanded from 8 name patterns to 20+ (AVDD, DVDD, EPAD, EP, VBAT, VSYS, etc.)
- Power pin inference in `easyeda_parser.py` expanded with VDDCORE, VDD_*, VCC_*, 1P2, 2P5 variants

### Fixed
- **opamp.py**: replaced hardcoded pin numbers ("1","2","3") with database lookups (`pin_out_a`, `pin_inm_a`, `pin_inp_a`)
- **power_mux.py**: replaced hardcoded LTC4357 VIN pins 6/7/8 with `pin_vin_extra` database field
- **can_transceiver.py**: RS pin moved from `power_pins` to `pin_nets` (input type, not power); fixed ordering bug
- **usb.py**: CYUSB3014 wires all data bus pins (SSRX, GPIF, SPI, RESET, XTALIN); USB2514B unused pins → explicit NC
- **ethernet.py**: KSZ9031 RGMII data bus + PHY pair pins wired to named nets
- **clock.py**: AD9528 unused optional inputs → explicit_no_connects
- **relay_driver.py**: ULN2003A unused channel inputs → explicit_no_connects
- **sensor_frontend.py**: INA128PA RG gain pins at unity → explicit_no_connects

### Tests
- 123 total tests passing (was 106), 26 in test_template_structure.py
- 0 regressions across all 4 commits
- Version bump: 0.6.0 → 0.7.0

## [0.6.0] - 2026-04-03

### Sprint 5 — EasyEDA/LCSC Symbol Import Pipeline

### Added
- EasyEDA API client (`easyeda_api.py`): fetch symbol/footprint data from JLCPCB/LCSC by LCSC part number — zero auth, 300K+ parts, 7-day disk cache
- EasyEDA symbol parser (`easyeda_parser.py`): parse tilde-delimited shapes into PinDefs with power/signal classification, footprint inference from 25+ package patterns, category inference from description keywords
- 4th-tier component resolution fallback: built-in → KiCad official lib → JSON DB → **EasyEDA/LCSC**
- YAML `lcsc:` key support — specify `lcsc: C14663` on any component to fetch from EasyEDA
- MPN-to-LCSC auto-discovery: when `parts_lookup.py` finds an LCSC code for an MPN, the EasyEDA tier fires automatically
- `lcsc_pn` and `digikey_pn` first-class fields on `ComponentDef` (replaces features-list workaround)
- `search_easyeda()` function for keyword search across JLCPCB component library
- EasyEDA regression coverage in `test_easyeda_import.py` for pin parsing, symbol assembly, ComponentDef conversion, footprint/category inference, and mocked resolution paths

### Changed
- `enrich_component()` now populates `lcsc_pn` and `digikey_pn` fields directly (backward-compatible: still writes features list too)
- Resolution chain stub message updated to mention EasyEDA as a checked source
- Version bump: 0.5.0 → 0.6.0

### Fixed
- EasyEDA imports now fail closed when any UUID payload is missing, instead of silently returning a truncated symbol
- Explicit YAML `lcsc:` keys now prefer EasyEDA import even when earlier registry/KiCad tiers would otherwise resolve the part by name
- `easyeda_to_component_def()` now carries `lcsc_pn` directly from EasyEDA metadata
- Restored `LTC4357CMS8` support in the `power_mux` template after the sprint 5 regression review

### Tests
- Added regression coverage for incomplete EasyEDA UUID fetches, explicit `lcsc:` override precedence, and restored `LTC4357CMS8` generation

## [0.5.0] - 2026-04-03

### Sprint 4 — Template Expansion: Analog, Sensing & Control

### Added
- Crystal oscillator template (`crystal_oscillator`): HC-49S, ABM8G — auto-calc load caps, feedback R
- I2C bus conditioning template (`i2c_bus`): pull-ups or PCA9306 — auto-calc pull-up R from speed/capacitance
- Current sense amplifier template (`current_sense`): INA219, INA180A1 — Rsense from Imax, footprint by power
- MOSFET switch template (`mosfet_switch`): BSS138, AO3400A, AO3401A — gate R, default-off pull, snubber RC
- DAC output template (`dac`): MCP4725, DAC8552 — output RC filter from update rate
- Sensor front-end template (`sensor_frontend`): INA128, AD8421 — gain R (G=1+50k/Rg)
- Relay driver template (`relay_driver`): ULN2003A, discrete NPN — base R with overdrive
- Audio amplifier template (`audio_amplifier`): PAM8302A, MAX98357A — input coupling cap
- Power mux template (`power_mux`): TPS2113, LTC4357 — ILIM resistor auto-calc
- Charge pump template (`charge_pump`): LM2776, ICL7660 — flying/output cap from Iout/fsw/ripple
- `boost_inductor()` and `buck_boost_inductor()` helpers in base.py
- `qualify_footprint()` auto-resolves bare footprint names to Library:Name
- Template count: 20 → 30

### Tests
- 68 tests passing

## [0.4.0] - 2026-04-03

### Sprint 3 — Placement, Routing & Presentation Readiness

### Added
- Schematic aesthetics scorer (`scorer.py`): 6-metric rule-based quality gate (spacing uniformity, whitespace ratio, label overlap, wire crossings, aspect ratio, density)
- `score_layout()` returns 0-100 score with A-F grade; `score_project()` aggregates across sheets
- `--score` CLI flag on `generate` subcommand; piped through `generate_artifacts()` API
- LDO cluster motif renderer (`_apply_topology_ldo_cluster`): CIN + COUT as compact unit below IC
- USB-C CC network motif renderer (`_apply_topology_cc_network`): CC1/CC2 pull-downs as tight pair beside connector
- Single-passive inline placement in sidecar cluster: 8.89mm offset for solo passives (vs 12.70mm grid)
- Dispatch chain: buck -> LDO cluster -> CC network -> decoupling bank -> strap ladder -> sidecar
- 10 new tests in `test_sprint3.py`

### Changed
- Sidecar cluster distinguishes single-passive (inline) from multi-passive (grid) placement

### Tests
- 68 total tests passing

## [0.3.0] - 2026-04-02

### Sprint 2 — Template Expansion & DFM Quality

### Added
- Op-amp template (`opamp`): non-inverting, inverting, follower, differential configs with auto-computed E24 feedback network
- Protection template (`protection`): TVS diodes (SMBJ5.0A/12A, SMBJ5.0CA, PESD5V0S1BA) for power/signal lines
- Gate driver template (`gate_driver`): IR2110 (half-bridge with bootstrap cap), UCC27524 (dual low-side)
- Level shifter template (`level_shifter`): TXB0108 (8ch), TXS0102 (2ch) with OE pull-up and dual-rail decoupling
- Template count: 6 → 10
- Library-name-based category classification (`_LIB_CATEGORY_MAP`): 30+ KiCad library prefixes mapped to categories
- Footprint heuristic inference (`_infer_footprint`): SOT-23, SOIC-8, SOD-123, 0402 from pin count + name
- External component database: `ComponentRegistry.load_json()` and `.load_json_dir()` for project-local JSON parts
- YAML `components_db` key for auto-loading project-local component databases
- KiCad `Description` property used for better category/description inference

### Changed
- `symbol_to_component_def()` now accepts `lib_name` parameter for library-aware classification
- `get_component()` passes resolved library name through search cache
- Category inference priority: library name > ref prefix > description keywords
- `project_spec.py` skips metadata keys (`spec_version`, `presentation_profile`, `components_db`) in section loop

### Tests
- 58 total tests passing (no regressions)

## [0.2.0] - 2026-04-02

### Sprint 1 — Robust KiCad Import Pipeline

### Added
- Stub ComponentDefs for unresolved components — never silently drop parts
- `_make_stub_component()` creates visible "UNRESOLVED" placeholders in schematics
- `_apply_power_map()` remaps KiCad power pin names to project rail names (VCC→VDD_3P3, etc.)
- `_apply_net_prefix()` prevents net collisions by prefixing generic signal names with ref (Q1_G, Q1_S)
- Optional `power_map` dict in YAML spec for explicit power pin remapping
- "Miscellaneous / Discrete" sheet category for unknown/protection/discrete/analog components
- Passive inference from bare YAML entries: `{value: "10k", ref: "R1"}` resolves without `ic:` field
- Expanded section category map: analog, discrete, motor, protection, audio, display, misc
- 26 new tests in `test_import_pipeline.py`

### Changed
- `auto_generate_bypass_caps()` now bypasses 6-pin minimum for power ICs and U-prefixed parts
- Default allocator fallback changed from "mcu" to "misc" — unclassified parts get their own sheet
- `_validate_component_resolution()` detects stub components and emits structural errors
- `_SECTION_CATEGORY_MAP` expanded with common analog/discrete/motor/protection sections

### Fixed
- Voltage regulators (3 pins) now get auto-decoupling caps (was blocked by pin count threshold)
- Two BSS138 FETs in the same design no longer short their gates via shared "G" net
- Components not in registry or KiCad library now produce visible stubs instead of vanishing

### Tests
- 58 total tests (32 presentation + 26 import pipeline), all passing

## [0.1.0] - 2026-04-02

### Initial Release

### Added
- Circuit Weaver schematic generation engine
- 6 subcircuit templates: buck, ldo, clock, ethernet, usb_controller, usb_hub
- 22-component built-in registry with full pin/bypass/strap metadata
- KiCad 8/9/10 symbol library import (10,000+ parts via local install or GitLab)
- Multi-sheet hierarchical schematic generation with boundary net promotion
- Auto-placement engine with topology-aware motif renderers
- Transactional MVP workflow: validate, generate, apply-patch, diff, ingest-pcb-feedback
- PCB placement export and constraint feedback loop
- 3 sample circuits: usb_regulated_supply, led_power_indicator, iot_sensor_node
- Presentation parity: topology-local rendering for all support passives
- Review presentation profile (--presentation-profile review)
- Decoupling bank and strap ladder motif renderers
- 32 tests (bootstrap, helpers, presentation regression)
