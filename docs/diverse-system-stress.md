# Diverse circuit stress trials

The repeatable inputs live in `benchmarks/diverse_systems`. They test different failure modes; component count alone does not establish design capability.

| Trial | Size | Purpose and current evidence |
| --- | ---: | --- |
| Serialized relay controller | 1,202 physical parts, 768 outputs | Completed validate/generate/native build; all 768 signal pad-net contracts passed on the native PCB. Placement and copper routing at this size remain separate gates. |
| Radar external IF bank | 32, 256 and 640 channels; up to 1,280 passives | Real ngspice execution through the product, with every channel compared against an independent complex RC transfer. This models external IF filters. |
| SDR external IQ | Matched and deliberately mismatched channels | Measured complex I/Q mismatch is detected against analytic filters. Mixer, converter, LO, noise and RF models are absent. |
| Transmitter output filter | 50-ohm, third-order passive LC trial | Real AC sweep checked against an independent Butterworth transfer. Power-amplifier behavior, parasitics, heating and emissions are outside this trial. |
| BLE sensor node | 150 physical parts | New nRF52840 acquisition-board fixture; validate/generate/native build passed, with 8 analog channels and 8 signal paths checked against the native board. |
| SDR clock carrier | 236 physical parts | Corrected package binding, GPIO assignments and crowded markers. Validate/generate/native build passed, with 8 analog channels and 74 signal paths checked on the native board. Schematic body-crossing warnings remain. |
| OT input and comparison boards | 150/250-ohm burden tests; 155/438-part boards | DC results match Ohm's law, including deliberate ADC overrange. Both boards passed validate/generate/native build; the 155-part board passed 8 analog channels, and the 438-part board passed 110 signal paths and 72 return assertions. |

The October 22 local evidence is under `automation_validation_results/diverse-system-stress-20261022` in the workspace. The five boards contain 2,181 physical parts in total. All declared native checks passed: 960 signal paths, 24 analog channels and 72 return assertions. The initial relay import hit a fixed 90-second worker timeout. Its successful retry reused generation artifacts unchanged, completed in approximately 117 seconds, and retained the 768 native pad-net checks. Native pad-net contracts establish assignments, not routed copper or internal IC behavior.

The manifest-only preflight cannot substitute for native pad identities: generated support capacitor orientation differs from its logical net1/net2 description. An initial harness reported 72 false failures on that basis; the actual 438-part native board passed the unchanged 110-path/72-return contract. Original failures and retries are retained. The BLE filter contract was corrected from compiled support ownership after replacing its MCU changed automatic reference allocation. The native verifier now ignores unnumbered stencil apertures only when native layer evidence proves they carry no copper or net; unknown and electrical pads remain blocking.

A fresh generation using the hierarchy optimization produced 66 schematic sheets. Its native KiCad netlist contained the same 1,202 physical parts and all 5,304 connected pad-net assignments matched the previously built PCB. The first comparison harness used the wrong parser result shape; a separate verifier checked the retained native export without regenerating or modifying it.

The final focused regression batch passed 452 tests with one environment-dependent skip. Ruff, generated capability documentation and the existing electrical benchmark baseline passed. Jobs ran serially under the owned memory supervisor with a 2 GiB free-RAM floor; native jobs had a 2.5 GiB hard cap. These are targeted checks rather than a new full cross-platform suite.

## Next circuits to extend coverage

These are proposed trial budgets, not measured designs or supported-performance claims. Resolve exact part identifiers and package assets before expanding each one.

| Next trial | Proposed scale | Existing library starting points | Additional gates |
| --- | ---: | --- | --- |
| Radar IF acquisition backplane | 500–1,500 parts | AD8421BRZ, OPA2340, MCP3208-CI/SL, AD9528 | Channel isolation, aggregate sample timing, reference loading, analog returns; actual ADC/clock simulation models required. |
| SDR clock and converter carrier | 300–800 parts | AD9528 and supported ADC/DAC/control parts | Differential topology, clock loading, jitter and SI calculations; an RF transceiver core needs a separately vetted symbol, package, functional template and model. |
| Multichannel transmitter control | 300–700 parts | DAC8552IDGK, OPA2340, INA180A1 and passive filters | PA interface/loading, sequencing, current/thermal budget; PA stability, harmonics and RF layout need suitable nonlinear/EM models. |
| Battery IoT sensor mesh gateway | 150–400 parts | nRF52840-QIAA, MCP73831T-2ACI/OT, MAX17048G+T, supported sensor/ADC parts | Peak radio current, sleep-state leakage, charger interaction, RF matching, antenna and firmware/protocol checks. |
| Industrial mixed-I/O controller | 400–1,000 parts | MAX485ESA+, MCP2551-I/SN, ADS1115IDGSR, DAC8552IDGK | Protection, isolation, grounding, simultaneous load and fault cases; interface/protocol and safety qualification remain separate. |
| Output rack and distributed controller | 1,200–2,000 parts | 74HC595D, ULN2003A and supported control/power parts | Hierarchy/import runtime, fanout, routing completion, return paths, connector/current/thermal limits. The 768-output fixture is a stress input, not a qualified coil-power design. |

## Library access before committing to an architecture

Use `circuit-weaver library-access AD9528 --project-root . --json`, the Python `audit_library_access` API, or MCP `library_access`. The audit searches stock, configured user and contained project libraries and reports provenance, conflicts and bounded asset matches. MPN-prefix queries can suggest curated exact MPNs; exact functional support remains separate from prefix search results.

Check each layer: symbol and pin functions; exact footprint and exposed pads; package-model geometry; functional template and connected interfaces; explicit simulation subcircuit and pin-order contract. A downloaded model file alone is insufficient. AD9361/AD9363/LMS7002/ADAR1000 were absent from the stock/curated assets checked locally; ADF4351 had CAD assets but no curated functional or SPICE binding. Those findings describe this installation, not all possible external libraries.

The audit does not fetch vendor libraries, establish redistribution rights, or verify RF performance. A complete radar, SDR or transmitter requires those missing device models and engineering checks before automation can claim electrical closure.

For executable passive tests, see [the benchmark instructions](../benchmarks/diverse_systems/README.md). For structured sources, trace selection and explicit engineering limits, see [project workflows](project-workflows.md).
