# Slot16 verification and controller results

Both cores met the declared 64-clock control deadline in all 56 scenarios. Slot16 finished in 9–10 clocks, leaving at least 54 clocks of margin. PicoRV32 took 30–64 clocks and reached the deadline in the busiest open-sink case. These results apply to the firmware, memory timing and scheduling policies below.

## Reproduce the evidence

Run from `asic/slot16` on Linux with Python 3.10+, Icarus Verilog, Yosys with ABC, curl and GNU RV32-capable binutils (`binutils-riscv64-unknown-elf` on Ubuntu). The assembler and interpreter use the Python standard library.

```sh
python3 asm16.py firmware/filter.s16 /tmp/filter.hex
sh check.sh
curl -fLsS -o /tmp/picorv32.v https://raw.githubusercontent.com/YosysHQ/picorv32/ef203c2b0a3fb793280f5114941416c425c5b461/picorv32.v
python3 benchmark.py /tmp/picorv32.v --json /tmp/controller-trials.json
```

The assembler accepts labels, signed immediates, `LD r1, [r6 + 3]` style addressing and `#` comments. It emits one hex word per instruction and rejects oversized banks and out-of-range fields. The benchmark verifies the upstream RTL digest, assembles both Slot16 banks, assembles and links the RV32I firmware, and simulates both real cores. CI publishes the per-sample latencies, misses and telemetry totals as the `slot16-controller-trials` artifact.

## ISA verification

The independent Python machine decodes raw instruction words and steps one clock at a time. The differential runner checks 13,400 edges from 12 seeded pairs of randomized programs, four directed pairs, and the two controller images. It compares pre-edge fetch, retirement and stream pins, followed by the new slot, PCs, status, all registers, valid mailbox contents and every scratchpad word. Both machines start with the same distinct scratchpad test values. Reset must retain them. The runner checks that all 15 legal instructions retire, each blocking operation waits, and invalid loads, stores and opcodes fault. These finite simulations supplement the existing directed boundary tests.

The Yosys SAT check takes an **arbitrary starting machine state**, symbolic instruction and endpoint signals, and quantifies over a fixed but arbitrary watched register, lane and scratchpad address. The first step captures the prior state. Assertions in the second step check the transition. An arbitrary starting state makes these local properties applicable on every edge of a longer execution.

- The active lane alternates except at reset. Reset restores the specified PCs, registers and flags, empties mailboxes and retains scratchpad data.
- A blocked or stopped instruction preserves PCs, register files, scratchpad, mailbox state and status. A fault preserves the same state except for the issuing lane's sticky fault flag. No blocked or faulting operation transfers an I/O word.
- Only a retired store in the owner lane may alter a scratchpad word. Handshakes and retirement agree with an independently calculated range and availability test.

The proof covers the listed safety properties. Progress still depends on the environment eventually supplying or accepting data. The differential test checks arithmetic and payload values. In a local sensitivity check, removing the bank selector or allowing blocked instructions to advance each produced a formal counterexample.

## Workload contract

The synthetic peripheral releases 64 unsigned 16-bit samples, one every 128 clocks, with a deadline 64 clocks after each release, inclusive. A two-tap FIR computes `(sample + previous_sample) modulo 65536`, starting with a zero previous sample in local memory. The second task XORs each incoming telemetry word with 85 and forwards it in order. An independent scoreboard checks every control result and every telemetry word, including those delivered after the measurement window.

Both cores have 1 KiB of local instruction storage, 256 bytes of local data storage, a one-word pending input per stream and one output holding register per stream. Both use the same peripheral harness. Burst data waits at the external producer until accepted. The control sink stays ready. Latency runs from sample release to **external sink transfer**, including the output holding register. Throughput counts only transfers during the fixed 8,192-clock window. After the window the telemetry sink opens and all outstanding words must drain in order.

Two control start alignments exercise alternate clock phases. For each alignment the harness runs one idle-peer trial and 27 loaded trials combining burst sizes of 1, 8 or 32 every 512 clocks, telemetry release offsets of 0, 17 or 63 clocks, and three sink conditions. Those conditions are always ready, blocked for the first 384 of every 512 clock edges, and blocked for the full measurement window. Both cores receive identical scenarios.

The reference core is the pinned default PicoRV32 used by the original echo experiment. Its RV32I dispatcher checks control first, then services at most one telemetry word per pass. A status bit lets it defer telemetry while the output buffer is full. Its instruction ROM and data RAM have zero added wait states. Slot16 uses its two hardware contexts and blocking instructions. These implementation choices affect the results. An interrupt-driven Ibex implementation, physical clock frequencies and energy remain separate comparisons. [PicoRV32 revision](https://github.com/YosysHQ/picorv32/tree/ef203c2b0a3fb793280f5114941416c425c5b461)

## Cycle measurements

Each cell with four values gives minimum, median, 95th percentile and maximum latency in clocks, using nearest-rank percentiles. The open-sink group includes idle trials. Each core produced **3,584 correct control outputs and zero deadline misses**.

| Telemetry sink | Control samples per core | Slot16 latency | PicoRV32 latency |
|---|---:|---:|---:|
| Open | 1,280 | 9 / 9 / 10 / 10 | 30 / 41 / 60 / 64 |
| Periodically blocked | 1,152 | 9 / 9 / 10 / 10 | 30 / 41 / 49 / 56 |
| Blocked throughout | 1,152 | 9 / 9 / 10 / 10 | 30 / 40 / 49 / 50 |

For the largest burst at offset zero and control alignment zero, the producer offered 512 telemetry words in 8,192 clocks. The measured throughput in words per window was as follows.

| Sink condition | Slot16 delivered / deferred | PicoRV32 delivered / deferred |
|---|---:|---:|
| Open | 512 / 0 | 195 / 317 |
| Periodically blocked | 272 / 240 | 56 / 456 |
| Blocked throughout | 0 / 512 | 0 / 512 |

The Slot16 control program uses **8 of 256 instruction words** and **1 of 64 scratchpad words**. Telemetry uses **6 instruction words** and **zero scratchpad words**. Together the programs occupy 28 bytes of ROM. The RV32I firmware occupies 60 bytes of ROM and one 32-bit RAM word. Both implementations keep additional transient state in registers.

For this two-task workload the measured margin and code fit support retaining the current slot allocation and bank sizes for an FPGA trial. Telemetry buffering is an integration requirement. A source unable to retain deferred words would need a FIFO sized for its burst and stall envelope or a specified drop policy. A real peripheral's deadline in microseconds still requires a measured clock frequency and interface timing.
