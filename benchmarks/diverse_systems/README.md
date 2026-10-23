# Diverse systems benchmark corpus

This suite contains three new architecture cases and two explicitly reused comparison fixtures. Authored minimum/target counts are preflight criteria; measured outcomes belong to each run's evidence. The October 22 local trial completed validate/generate/native build and all declared native connectivity contracts for 150-, 155-, 236-, 438- and 1,202-part boards. See [the trial report and next circuit matrix](../../docs/diverse-system-stress.md) for coverage and remaining gates.

- `ble_sensor_8` derives the existing connected sensor hub with an nRF52840-QIAA, 32 MHz crystal, the existing eight-channel analog acquisition and SPI DAC paths, and a direct ANT test port to SMA. The pin map uses aQFN-73 pads AD8/AC9/AD10/AC11 for P0.13/P0.14/P0.15/P0.16, and H23 for ANT. The RF path is not an antenna match or RF performance claim.
- `sdr_clock_carrier` adds AD9528 clock synthesis, differential reference input, MCU SPI/reset control, and twelve differential outputs to module headers. The AD9528 output mapping through `pin_nets_extra` is deliberately represented in the native contract; native generation must confirm the current template supports it. This is a clock carrier prototype, not a complete SDR or qualified clock design.
- `relay_768` contains 768 channel-specific relay output paths through 96 cascaded 74HC595 devices and 110 ULN2003A arrays. SWD J9000 is distinct from output J162. Clock fanout, aggregate coil power, thermal behavior, field isolation, and safety are unqualified.
- `ot_mixed_io_comparison` and `dual_dac_72_comparison` reuse existing connected fixtures as comparisons; they are not new architectures.

Run `python benchmarks/diverse_systems/build_diverse_trials.py --repo-root . --output benchmarks/diverse_systems` to recreate the YAML and JSON artifacts. The generator imports the existing pure stress fixture builders only for relay fixture construction; it does not compile, build, invoke KiCad, place, route, render, or simulate designs.

## Portable SPICE regression suite

`spice_suite.py` defines passive networks independently from the architecture
fixtures and runs them through `circuit_weaver.simulation.run_design_simulations`
and the product exporter/parser. It covers an IoT RC low-pass, matched and
mismatched SDR IQ channels, a 50 Ω 100 MHz third-order Butterworth ladder, 32-,
256-, and 640-channel radar IF banks (up to 1,280 physical passives), and a
1 MΩ / 1 pF unit case. The suite runs the product pipeline once per fixture;
each fresh raw file includes all channel vectors, and every AC point/channel is
checked against an analytic complex transfer function and magnitude. The first
channel's bandwidth targets are checked by the product metric verifier;
secondary-channel windows are explicitly reported as analytic-oracle checks.
Intentional IQ mismatch is a negative control based on the measured complex
I/Q transfer delta and phase delta at the nearest parsed 100 kHz point, with a
matched-channel run as its control.

The OT cases export and run 20 mA current-source burden checks at 150 Ω and
250 Ω, with analytic 3 V and 5 V expectations. The 5 V case is detected as an
overrange against the suite's illustrative 3.3 V headroom; this comparator is
not an engineering acceptance limit, so the DC target state remains
unverified.

Run into a new or empty output directory:

```text
python benchmarks/diverse_systems/spice_suite.py --output ./spice-suite-results
```

Ngspice is discovered on `PATH`, or selected via
`CIRCUIT_WEAVER_NGSPICE` / `--ngspice <executable>`. The script writes
netlists, one fresh raw file and SHA-256 per AC fixture, and `suite-report.json`; it refuses to reuse a
non-empty output directory. Without ngspice it exits unverified rather than
claiming results. These passive fixtures test exporter/parser behavior and do
not model ICs, RF electromagnetic behavior, PCB layout, or manufacturing.
