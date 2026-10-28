# Roadmap follow-up: current budgets and native library memory

This continues the same eight-phase task and PR39 from `826dee21`. Earlier
evidence remains immutable in `four-layer-acceptance-20261024`; new attempts
and proofs are in `automation_validation_results/roadmap-followup-20261024`.

## Phase 3 and 7: explicit current operating cases

The new [Python checker](operating-cases.md) handles a common series feed with
explicit simultaneous loads and evidence-bound source, connector and
distribution current limits. Missing current, concurrency or ratings remain
unverified. Unsupported load provenance cannot establish either a pass or a
failure. Independently expected synthetic nominal, overload, missing-rating,
unknown-concurrency and changed-value controls exercise these distinctions.

The corpus audit still leaves all ten actual operating cases unverified. Its
relay all-active figures of 0.64, 3.84 and 23.04 A are conditional arithmetic
from authored channel counts and 80 mA coil assumptions, not verified operating
cases. No source or connector rating has been fabricated to make a case pass.
This is a separate Python/audit API; CLI/MCP/shared workflow integration and
device-specific electrical, timing and thermal qualification remain open.

Existing canonical power domains also reject nonfinite values, booleans,
nonnumeric envelopes, invalid sequencing and a peak current below the stated
steady load before those values become PCB constraints. Signed voltage rails,
zero loads and unknown limits remain supported.

## Phase 4: stock footprint context in isolated native DRC

The previous 25-part board had zero blockers and 25 missing-configured-library
warnings. An exact-board native comparison with the selected installation's
private stock context removed all 25 findings without changing PCB or project
settings. The first full-table approach peaked at 1,526,079,488 bytes of job
memory; a fresh 96-part build then stopped at the 2 GiB host RAM reserve with a
1,651,384,320-byte peak. Both owned process trees were reaped. This exposed a
catalog-loading memory cost rather than a circuit rejection.

The follow-up stages only the referenced verified stock footprints into a
private lookup root and generates the corresponding private table. Project
tables retain precedence. Original template, referenced source assets, staged
assets and generated table identities are retained and checked around native
execution. Unknown references remain unverified and their native findings are
not suppressed. User-global configuration is not modified or borrowed.

The narrower-context repeat on the same saved IoT board passed with zero DRC
findings and unchanged board/settings. Its peak committed job memory fell to
173,731,840 bytes, an observed 88.6% reduction from the full-table trial under
the same supervisor limits. This is a measured improvement for this case,
not a memory guarantee for every board. The fresh 96-part continuation then
published its native build; final placement/routing/rendering gates remain
separate until their workflow evidence passes. That continuation stopped during
placement after 42.083 seconds at the host RAM reserve, with 278,405,120 bytes of
peak job memory. The subsequent five-minute headroom window expired without
launching another job. All owned children and the queue are terminal; the
larger circuit remains incomplete rather than rejected.

The final focused source validation passed 411 tests in two disjoint serial
batches, with one optional real-native test deselected. Ruff passes. Separate
native proofs establish the exact-board library and memory comparison above;
unit-test counts do not establish larger-board completion.

## Remaining gates

The 25-part IoT board is a completed native CAD case with an assembled preview;
that prior completion is separate from this exact-board DRC repeat. The current
wave has not yet accepted a larger native board. The 96-part ladder comes
first, followed by the 466/482-part cases and the remaining family controls.
Native jobs retain the 2 GiB free-RAM reserve and 2.5 GiB hard cap; changing a
lookup implementation does not justify lowering those limits.

Four native copper layers and filled polygons do not establish dielectric
geometry, impedance, return paths, RF behavior or manufacturing readiness.
Required final-head CI remains a merge/release gate, with the previously
reported Actions budget condition unchanged until new checks provide evidence.
