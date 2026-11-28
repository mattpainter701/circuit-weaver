# Heddle silicon tile

The tile targets a Tiny Tapeout IHP SG13G2 slot at the shuttle's 25 MHz clock.
It contains standard cells only. The shuttle supplies pads, clock, reset,
power and package. Pin functions are in [info.yaml](info.yaml) and the host
protocol is in the [design README](../README.md).

## Status

**Layout is not complete.** Placement and global routing succeed on the 4x2
and 6x2 slots. Detailed routing, extracted timing, DRC and LVS had not
finished when this revision was written, so there is no signed-off layout, no
timing result and no submission bundle yet. `info.yaml` names 4x2 as the
candidate slot.

## What the layout attempts showed

All runs used the pinned LibreLane 3.0.14 image and IHP PDK revision named in
`harden.sh`, with a 60% placement density target.

| Weight logic | Slot | Cells after timing repair | Placement congestion estimate | Global routing |
|---|---|---:|---:|---|
| Shared across columns | 3x2 | 11,523 cells, 140,328 µm² | 1.39 | Overflow remained after 50 repair iterations |
| Shared across columns | 4x2 | 12,161 cells, 146,876 µm² | 1.26 | Overflow remained after 50 repair iterations |
| Shared across columns | 6x2 | 13,247 cells, 157,764 µm² | 1.16 | Overflow remained after 50 repair iterations |
| One cone per column | 4x2 | 16,949 cells, 171,835 µm² | 0.73 | Passed |
| One cone per column | 6x2 | 16,939 cells, 171,702 µm² | 0.72 | Passed |

A congestion estimate above 1 means the placer expects more wiring demand than
the metal stack offers.

The first structure let synthesis share row-decoding terms between all 128
weight columns. It was the smallest in cells, 100,621 µm² before timing
repair, but each shared term became a net that spans the datapath. Tripling the
slot area did not help, because the wiring demand comes from the netlist's
shape and not from its density.

The second structure gives each column its own weight logic. It is 37% larger
before timing repair, 137,633 µm², and it passes global routing. The generator keeps each
column in its own module and the flow maps the modules separately
(`SYNTH_HIERARCHY_MODE` set to `deferred_flatten`).

Timing repair then adds cell area on top: 39 to 57% for the shared structure
and 25% for the per-column one.

## Reproduce

Run from the repository root on Linux with Docker. Choose a new build
directory for each run.

```sh
work=$(mktemp -d)
git clone -q https://github.com/TinyTapeout/tt-support-tools.git "$work/tt"
git -C "$work/tt" checkout -q 7d2d2ef5261eb1df16d0bc342834d48a8a276881
docker run --rm -v "$PWD:/repo:ro" -v "$work:/work" --entrypoint sh \
  ghcr.io/librelane/librelane@sha256:f91b21d75f79871f9ccf37451020b5d2f7b3236881a997ff709c561e8280a30f \
  /repo/asic/heddle/silicon/harden.sh /work/build /work/tt
```

The script stages the generated logic against the pinned slot template,
replays the host scenario on the RTL, runs the flow, replays the same scenario
on the routed gate netlist with the PDK cell models, and writes
`tt_submission`. Set `TILES=6x2` in the container environment to try another
slot. A run takes well over an hour on four cores, most of it in detailed
routing.

## Still to do before a submission

- Finish a run and record setup and hold slack at all corners, DRC, LVS and
  antenna results here.
- Run the shuttle's precheck on the exported layout.
- Review the routed netlist replay result and the power report.
- Decide between 4x2 and 6x2 from the finished results.
