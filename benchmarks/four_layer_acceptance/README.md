# Four-layer acceptance corpus

`corpus.py` defines deterministic, connected design-spec variants for acceptance of four-layer PCB preparation and routing workflows below 500 physical assembly items. `build_cases()` returns data only: it does not compile designs, call KiCad, invoke native layout, or write files. The harness is responsible for compiling each spec and measuring the exhaustive assembly manifest, including generated capacitors, straps, and other support parts. The `component_count_target` values are planning estimates, not assertions of measured counts; the harness must publish its actual count and reject any positive case at 500 or more.

The corpus has nine connected acceptance designs plus the retained SDR clock-carrier comparison control:

| Family | Variants | Purpose |
| --- | --- | --- |
| Connected IoT controller | USB-powered environment node; ground-anchorless variant | Fast connected baseline and inner-plane bootstrap case |
| Serialized relay output | 8, 48, and 288 channels | Small-to-large digital/control ladder |
| Analog setpoint output | 24 and 80 channels | DAC/filter/channel connectivity and support-component scale |
| Fieldbus gateway | Existing connected gateway design | RS-485, CAN, SPI, and sensor-acquisition control |
| Precision acquisition | Existing bridge/load DAQ design | Differential analog inputs and SPI acquisition control |
| Clock distribution | Existing SDR clock-carrier design | RF/IF-support and differential-clock-density comparison |

Variants derived from proven builders or established designs are marked `comparison_fixture_reuse: true`; their reused topology is not represented as a new architecture. Specs declare four copper layers. Most use a GND zone on `In1.Cu`, leaving `In2.Cu` available for signals. One relay case declares the dual-inner-GND comparison (`In1.Cu` and `In2.Cu`) so the harness can expose the routing tradeoff.

Each case records a four-layer copper stack, named nets, explicit endpoint-to-net expectations, required components and library parts, board dimensions, and declared operating assumptions. Native review uses the explicit prototype-profile assumptions `clearance_mm: 0.127` and `minimum_drill_mm: 0.2`, so the USB-C connector's 0.15 mm pad gap can be checked under a declared rule. This fixture profile is not a fabrication approval and does not override authored project rules. Generated input/output filters are selected by manifest ownership, role, type, and exact terminal nets instead of guessed reference numbers. The electrical limits are unverified fixture assumptions. They do not establish electrical safety, power integrity, thermal performance, timing, signal integrity, radio range, antenna matching, EMC compliance, or manufacturing readiness. In particular, the SDR carrier is RF/IF-support evidence only; it is not a qualified RF design.

The serialized relay fixture builder applies the relay-driver template's single-used-channel net convention to partially populated final drivers. Regression coverage exercises full, one-channel, and six-channel tails, including the 8- and 288-channel acceptance cases; it validates every register/driver/field-connector endpoint against compiled pad nets.

Structured controls include an endpoint net swap, a missing-library entry, a two-layer stackup against a four-layer request, and removal of key electrical limits. The weak-limits control is reported as unverified metadata; the corpus does not claim the harness has an oracle that detects every inadequate limit. The ground-anchorless variant keeps the USB-C connector's plated pads on `POWER_RETURN` and joins that return to GND through an SMD zero-ohm link, so its contract requires zero plated GND anchors without claiming the connector itself is all-SMD.

Run `tests/test_four_layer_acceptance_corpus.py` and `tests/test_unseen_scale_cases.py` in the repository's guarded serial test workflow. The former checks corpus determinism, schema completeness, family diversity, size bounds, four-layer plane declarations, endpoint contracts, and the negative-control set. The latter adds compiled endpoint regression for partial relay drivers. Neither test invokes KiCad or routes boards.

`operating_case_audit.py --output <new-report.json>` runs the separate authored
current-budget checker against missing corpus operating cases and five synthetic
controls. Missing concurrency, feed ratings and provenance stay unverified;
all-active relay arithmetic is explicitly conditional. It does not claim device
function or thermal qualification. See [the Python API](../../docs/operating-cases.md).
