# Boot Weftline 16 on a desktop

Weftline 16 runs two Slot16 firmware images. The virtual board connects the verified instruction interpreter to a pair of word-stream inputs and outputs. One tick advances one core clock edge. It runs on Python 3.10 or newer using the standard library.

## Boot the encoder demo

From the repository root, run the following command.

```sh
python asic/slot16/weftline.py
```

The board boots the encoder in lane 0 and its diagnostics worker in lane 1. The default batch run queues nine readings, runs 512 clocks, and prints every accepted output with its zero-based clock edge.

| Input A:B | 00 | 01 | 11 | 10 | 00 | 00 | 10 | 01 | 11 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Position on output 0 | 0 | 1 | 2 | 3 | 4 | 4 | 3 | 3 | 4 |
| Invalid transitions on output 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 |

The **encoder firmware** reads a 16-bit word and masks it to the two encoder bits. The first reading establishes the origin. Each adjacent Gray-code transition advances or reverses the position. A repeated state holds position. A simultaneous two-bit change holds position and resynchronizes to the new state. The firmware emits position and sends a one-word validity flag through the hardware mailbox. The **diagnostics firmware** consumes that flag and publishes the cumulative invalid-transition count. Both results wrap at 16 bits. For example, one reverse step from zero yields `0xffff`.

This demo models a sampled encoder. An actual board must synchronize the external inputs and sample fast enough to capture transitions. Diagnostic output backpressure can eventually fill the mailbox and stall the next SEND. The monitor makes that dependency observable. [Encoder firmware](firmware/encoder.s16) · [Diagnostics firmware](firmware/encoder_diagnostics.s16)

## Step the board

```sh
python asic/slot16/weftline.py --monitor
```

Enter the following commands. The first run holds the diagnostic sink busy. The second releases it and drains the pending messages.

```text
ready 1 0
input 0 0 1 3 2 0
step 256
ready 1 1
step 256
status
quit
```

`step` advances one clock by default. `status` shows the next lane, PC, registers, inbox, pending input depth, sink readiness and wait reason. The host queues keep each input word valid until the owning lane accepts it. Each output is recorded only on an accepted transfer. `reset` pulses core reset, clears host queues, restores sink readiness, and restarts the displayed clock at zero. Scratchpad contents survive a warm reset. The virtual board initializes cold scratchpad storage to zero; portable firmware must initialize any scratchpad word it reads.

## Boot other firmware

To exercise both scratch banks from an arbitrary reproducible cold state, boot
the same two memory-test images as the silicon tile. Each lane writes and checks
all 64 words. A successful run prints `out0=0x00a5`, `out1=0x00a5` and
`scratch test passed`. A mismatch emits `0xffff` on the affected lane. A
short batch that expires before completion also exits with status 1.

```sh
python asic/slot16/weftline.py --scratch-test --scratch-seed 42
python asic/slot16/weftline.py --scratch-test --scratch-seed 42 --monitor
```

In the monitor, enter `mem 0 0 8`, `step 6000`, then `mem 0 0 8` to inspect
the first eight words before and after the diagnostic. `reset` retains both
banks. Changing the seed changes both lanes' initial contents. Each physical
SRAM word has unspecified power-up data.

### Custom programs

Each lane accepts a `.s16` assembly file or a `.hex` file with up to 256 whitespace-separated 16-bit hexadecimal words. Unused ROM words contain HALT. To boot the existing filter and telemetry programs with independent inputs, run this command from the repository root.

```sh
python asic/slot16/weftline.py --rom0 asic/slot16/firmware/filter.s16 --rom1 asic/slot16/firmware/telemetry.s16 --input0 10 20 30 --input1 0x10 0x11 --cycles 128
```

The filter emits 10, 30 and 50. Telemetry emits 0x45 and 0x44. To use an assembled image, run `python asic/slot16/asm16.py asic/slot16/firmware/filter.s16 filter.hex` and pass `--rom0 filter.hex`. Supplying either ROM or input flag disables the built-in sample sequence. Unspecified lanes boot their default firmware. A batch run executes the requested clock budget, prints final state, and returns status 1 if either lane faulted. Waiting for an input at the end of a run is normal.

## Emulation choice

The board reuses the cycle-level Slot16 model. That preserves alternating slots, mailbox stalls, and exact stream handshake edges while keeping firmware runs available on Windows, macOS and Linux. A QEMU system port would need a custom Slot16 CPU target, instruction translation, a machine and device models, and an explicit clock policy for the two contexts. Its debugger and wider device ecosystem would justify that work if firmware grows into a larger system. The current board runs bare-metal ROM images. [QEMU system emulation](https://www.qemu.org/docs/master/system/introduction.html) · [QEMU TCG internals](https://www.qemu.org/docs/master/devel/tcg.html)

The integration test checks the public CLI, custom ROM loading, fault exit, reset, and output backpressure. It replays all 16 ordered A:B transitions, including reverse wraparound, against an independent position/error scoreboard and the synthesizable RTL. Every accepted input and output must match the virtual board at the same clock edge. Run `python asic/slot16/test_weftline.py` with Icarus Verilog on PATH, or run the full `check.sh` suite.

<details>
<summary>Product rendering provenance</summary>

Built-in ImageGen generated the [Weftline 16 concept rendering](weftline-16-concept.png) with this prompt.

> Product concept photograph for Weftline 16. A single matte graphite embedded-processor package rests on a light warm-grey ceramic surface. Three-quarter macro view, fine laser-etched lettering “WEFTLINE 16” on the lid, a small interlaced two-thread emblem above the name, pin-one index dot, restrained copper and silver details. Precision industrial design, soft side light, generous negative space, landscape composition. Only the product name is printed. No circuit board, performance claims or captions.

</details>
