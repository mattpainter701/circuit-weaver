# General-scale workflow benchmark

This suite exercises three connected prototype compositions that use different
signal and interface patterns: a multirail sensor/control hub with I²C and SPI,
an industrial field I/O gateway with several RS-485 channels and CAN,
and a precision analog acquisition/control datapath. Each fixture is specified
as real connected blocks; repetition represents separate sensor or bus
channels rather than disconnected count padding.

Guarded native generation and `build-pcb` preflight currently produce:

| Case | Physical components | Pads | Nets |
| --- | ---: | ---: | ---: |
| `sensor_control_hub` | 149 | 480 | 85 |
| `field_io_gateway` | 155 | 483 | 101 |
| `precision_control_daq` | 145 | 457 | 81 |

All three produced native prototype boards with exact pin-number matching,
KiCad PCB round-trip validation, and zero non-connect blockers. Each declares
eight distinct differential input channels. The runner checks each channel
against native PCB pad nets from the external connector through the INA128,
its 3.0V reference, any intermediate follower, the ADC input filter, and the
selected MCP3208 input pad. These are prototype workflow results, not
fabrication or electrical certification.

The analog operating envelope is explicit in each fixture: the INA128 is
powered from 5V (above its curated 4.5V minimum), each external differential
source is assumed to present 2.5V common mode and at most ±50mV differential,
and the local 3.0V reference biases the INA128 REF pin. At gain 5 this gives a
nominal 2.75–3.25V output range for the 3.3V MCP3208 domain. The hub and DAQ
connectors expose 5V for bridge excitation; the gateway channels are external
differential inputs and expose 3.3V auxiliary power. These bounded prototype
assumptions are workflow inputs, not validated sensor specifications.

Run from the checkout with the repository source package:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python benchmarks/general_scale/run_general_scale.py `
  --output <new-evidence-directory> `
  --workers 1 --require-clean
```

Use `--installed --expected-wheel <wheel.whl>` to test an installed wheel. The
harness verifies the Python import location before running and then checks the
validator's wheel-payload evidence. It snapshots the suite and all input YAML
files, records each full command and subprocess log, and preserves per-case
failures in a new output directory. It never overwrites an existing run.
The selected Python path retains its virtual environment even when its
executable is a symlink. Import preflight checks the real MCP client in that
same interpreter before any case starts.

Run every compact reference design through the same 16-stage workflow with
`--suite samples`. This integration suite has its own one-component minimum;
it does not lower the 100-part floor for the general suite. The two ESP32
sample boards explicitly request a 0.2 mm minimum drill for their real thermal
plated holes. Use `--case <id>` (repeatable) for a focused smoke run before a
full suite.

Every case delegates to `scripts/validate_app_workflow.py` and requires all 16
workflow stages to pass, zero DRC blockers, matching routed-board hashes, no
silkscreen conflicts, and native assembly preview coverage for at least 100
actual populated components with resolved visible 3D models. `--workers` is
limited to 1 or 2. `--route-timeout` is forwarded to the workflow validator;
that validator budgets it across routing attempts. `--timeout` bounds the
entire case. Results are validation evidence for prototype workflow behavior,
not a fabrication-readiness or electrical-certification claim.
