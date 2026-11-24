"""Compare complete RTL transitions against the independent interpreter."""
import random
import subprocess
import tempfile
from pathlib import Path

from asm16 import assemble, write_image
from reference import Machine

HERE = Path(__file__).resolve().parent


def assembler_checks() -> None:
    assert assemble("start: LDI r7, -256\nST r0, [r7 - 32]\nBNZ r7,start") == [0x1F00, 0x61E0, 0x7FFD]
    for bad in ("HALT r0", "ADD r8,r0", "LDI r0,256", "LD r0,[r1+32]", "BNZ r2,missing",
                "l: HALT\nl: HALT", "HALT\n" * 257):
        try:
            assemble(bad)
        except ValueError:
            continue
        raise AssertionError(f"assembler accepted {bad[:40]!r}")


def randomized_bank(rng: random.Random) -> list[int]:
    # r7 stays zero as a base. A forward branch rejoins the random stream.
    words = ["LDI r0,1"]
    for _ in range(150):
        d, s = rng.randrange(7), rng.randrange(8)
        choices = [f"{op} r{d},r{s}" for op in ("ADD", "SUB", "AND", "XOR")]
        choices += [f"LDI r{d},{rng.randrange(-256,256)}", f"SHL r{d}", f"VALID r{d}",
                    f"IN r{d}", f"OUT r{d}", f"BNZ r{d},1",
                    f"LD r{d},[r7+{rng.randrange(32)}]", f"ST r{d},[r7+{rng.randrange(32)}]"]
        words.append(rng.choice(choices))
    return assemble("\n".join(words))


def trial(directory: Path, banks: list[list[int]], seed: int, cycles: int, coverage: set) -> None:
    rng = random.Random(seed)
    machine = Machine(banks)
    for lane, ctx in enumerate(machine.contexts):
        ctx.scratch = [0x5A00 + lane * 64 + i for i in range(64)]
    write_image(machine.rom[0] + machine.rom[1], directory / "rom.hex")
    events, expected = [], []
    pending = [None, None]
    for edge in range(cycles):
        reset = edge == cycles // 2
        for lane in range(2):
            if pending[lane] is None and rng.randrange(5) == 0:
                pending[lane] = rng.randrange(65536)
        valid = sum(1 << i for i in range(2) if pending[i] is not None)
        inputs = (pending[0] or 0) | (pending[1] or 0) << 16
        ready = rng.randrange(4)
        events.append(f"{int(reset):x} {valid:x} {inputs:x} {ready:x}\n")
        slot = machine.slot
        ctx = machine.contexts[slot]
        active = not (reset or ctx.halted or ctx.faulted)
        op = machine.rom[slot][ctx.pc] >> 12
        pins = machine.step(reset, valid, inputs, ready)
        if active:
            outcome = "retire" if pins[1] else "fault" if ctx.faulted else "wait"
            coverage.add((op, outcome))
        if reset:
            pending = [None, None]
        elif pins[2] & valid:
            pending[slot] = None
        contexts = machine.contexts
        row = [*pins, machine.slot, sum(int(c.halted) << i for i, c in enumerate(contexts)),
               sum(int(c.faulted) << i for i, c in enumerate(contexts))]
        for c in contexts:
            row.extend((c.pc, int(c.inbox is not None), c.inbox or 0))
        for c in contexts:
            row.extend(c.regs)
        for c in contexts:
            row.extend(c.scratch)
        expected.append(row)
    (directory / "events.txt").write_text("".join(events))
    raw = subprocess.check_output(["vvp", "trace"], cwd=directory, text=True)
    rows = [line for line in raw.splitlines() if line and line[0] in "01" and " " in line]
    if len(rows) != cycles:
        raise AssertionError(f"seed {seed}: expected {cycles} trace rows, got {len(rows)}")
    for edge, (line, wanted) in enumerate(zip(rows, expected)):
        actual = [int(value, 16) for value in line.split()]
        if actual != wanted:
            raise AssertionError(f"seed {seed} edge {edge}: RTL {actual} reference {wanted}")


def main() -> None:
    assembler_checks()
    rng = random.Random(934)
    coverage = set()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        subprocess.run(["iverilog", "-g2012", "-s", "trace_tb", "-o", directory / "trace",
                        HERE / "slot16.sv", HERE / "trace_tb.sv"], check=True)
        total = 0
        for seed in range(12):
            trial(directory, [randomized_bank(rng), randomized_bank(rng)], seed, 1000, coverage)
            total += 1000
        producer = assemble("LDI r1,1\nSEND r1\nSEND r1\nRECV r0\nHALT")
        consumer = assemble("IN r3\nIN r3\nRECV r1\nRECV r2\nSEND r1\nHALT")
        for seed, banks in enumerate(([producer, consumer], [consumer, producer],
                                     [[0xF000], assemble("LD r0,[r0-1]")],
                                     [assemble("LDI r2,-1\nST r0,[r2+1]"), [0]])):
            trial(directory, banks, seed, 100, coverage)
            total += 100
        firmware = [assemble((HERE / "firmware" / f"{name}.s16").read_text()) for name in ("filter", "telemetry")]
        trial(directory, firmware, 42, 1000, coverage)
        total += 1000
    required = ({(op, "retire") for op in range(15)} | {(op, "wait") for op in (8, 9, 10, 11)}
                | {(op, "fault") for op in (5, 6, 15)})
    if required - coverage:
        raise AssertionError(f"uncovered transitions {required - coverage}")
    print(f"{total} complete edge traces matched; all required instruction outcomes observed")


if __name__ == "__main__":
    main()
