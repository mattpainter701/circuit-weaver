"""Make an independent, pin-level oracle for RTL and gate simulation."""
import argparse
from pathlib import Path

from build_rom import build
from reference import Machine
from weftline import load_bank


def generate(output: Path) -> None:
    build(output)
    images = [[load_bank(output / f"{name}.hex") for name in pair]
              for pair in [("encoder", "diagnostics"), ("scratch0", "scratch1")]]
    modes = [Machine(banks).rom for banks in images]
    cpu = Machine(images[0])
    mode = 0
    latch = [0, 0]
    vectors = []
    outputs = [[], []]

    def edge(enable: int, rst_n: int, pins: int) -> bool:
        nonlocal mode
        reset = not (enable and rst_n)
        flags = sum((int(ctx.halted) << (3+i)) | (int(ctx.faulted) << (5+i))
                    for i, ctx in enumerate(cpu.contexts))
        _, _, rx, tx, word = cpu.step(reset, (pins >> 2) & 1, pins & 3, 3)
        value = latch[(pins >> 4) & 1] >> (8 if pins & 8 else 0)
        status = (mode << 7) | flags | (tx << 1) | (rx & 1)
        vectors.append(f"{enable} {rst_n} {pins:02x} {value & 255:02x} {status:02x} {255*enable:02x}\n")
        if reset:
            mode = (pins >> 5) & 1
            cpu.rom = modes[mode]
            latch[:] = [0, 0]
        if tx:
            lane = tx.bit_length() - 1
            latch[lane] = word
            outputs[lane].append(word)
        return bool(rx & (pins >> 2) & 1)

    # Abandon a memory sweep mid-flight, then reboot and test both banks.
    edge(1, 0, 32)
    for i in range(200):
        edge(1, 1, i & 24)
    edge(0, 1, 32)
    for i in range(6000):
        edge(1, 1, (i % 4) << 3)
        if all(ctx.halted for ctx in cpu.contexts):
            break
    assert outputs == [[165], [165]] and all(ctx.halted for ctx in cpu.contexts)
    for pins in (0, 8, 16, 24):
        edge(1, 1, pins)

    edge(1, 0, 0)
    outputs = [[], []]
    # First reverse step wraps; the final diagonal transition increments errors.
    samples = [0, 2, 0, 1, 3, 2, 0, 3]
    index = 0
    for i in range(1000):
        pins = ((i % 4) << 3) | (samples[index] | 4 if index < len(samples) else 0)
        index += edge(1, 1, pins)
    assert outputs == [[0, 65535, 0, 1, 2, 3, 4, 4], [0, 0, 0, 0, 0, 0, 0, 1]]
    (output / "pins.txt").write_text("".join(vectors), encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
