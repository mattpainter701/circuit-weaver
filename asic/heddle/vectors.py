"""Write the stimulus and expected results for the Heddle testbenches.

`core.hex` holds requests with the reference value of every row, `pins.hex`
holds one pin vector per clock from the host scenarios, and `--rom` compares a
dump of the generated weight logic with the model file bit for bit.
"""
import argparse
import random
from pathlib import Path

from build_rtl import placed_rows, shape
from heddle import CLEAR, PUSH, SEED, STEP, Chip, Model
from pins import OP_CFG, OP_CLEAR, OP_NOP, OP_PUSH, OP_SEED, OP_STEP, Host, Tile


def core_vectors(model, output, requests=1500, seed=7):
    """Random requests. Each inference lists the value of every row in order."""
    rng = random.Random(seed)
    chip = Chip(model)
    lines = []
    inferences = 0
    for number in range(requests):
        roll = rng.random()
        kind = CLEAR if roll < 0.02 else SEED if roll < 0.06 else STEP if roll < 0.5 else PUSH
        token, sample = rng.randrange(32), int(rng.random() < 0.5)
        if number < 64:  # walk every token through the embedding first
            kind, token = PUSH, number % 32
        lines.append(f"{kind << 6 | sample << 5 | token:03x}")
        if kind == CLEAR:
            chip.clear()
        elif kind == SEED:
            chip.seed(token)
        else:
            trace = []
            result = chip.push(token, bool(sample), trace) if kind == PUSH else chip.step(bool(sample), trace)
            lines.extend(f"{value & 0xFFFF:04x}" for value in trace)
            lines.append(f"{result:02x}")
            inferences += 1
    (output / "core.hex").write_text("\n".join(lines) + "\n")
    return len(lines), inferences


def pin_scenario(model):
    """Exercise both ports the way a host would and check each result against the reference."""
    tile = Tile(model)
    host = Host(tile)
    ref = Chip(model)
    index = model.alphabet.index
    host.reset()

    # Parallel port, greedy then sampled.
    for char in "the circuit ":
        assert host.strobe(index(char)) == ref.push(index(char))
    for _ in range(8):
        assert host.strobe() == ref.step()
    host.pin(7, 1, "ui")
    host.tick(4)
    for _ in range(8):
        assert host.strobe() == ref.step(True)
    host.pin(7, 0, "ui")
    host.tick(4)

    # SPI port: clear, prompt, stir the sampler, enable sampling, generate.
    host.spi(OP_CLEAR << 5)
    ref.clear()
    assert host.wait() == 0
    for char in "weave":
        assert host.spi_token(OP_PUSH << 5 | index(char)) == ref.push(index(char))
    for value in (0x15, 0x0A, 0x1F):
        host.spi(OP_SEED << 5 | value)
        ref.seed(value)
    host.spi(OP_CFG << 5 | 1)
    for _ in range(12):
        assert host.spi_token(OP_STEP << 5) == ref.step(True)
    host.spi(OP_CFG << 5)
    for _ in range(4):
        assert host.spi_token(OP_STEP << 5, ) == ref.step()

    # A second command inside the same frame arrives while busy and is dropped.
    assert model.rows > 80, "the overrun scenario assumes an inference outlasts one SPI byte"
    host.spi(OP_STEP << 5, OP_STEP << 5)
    expected = ref.step()
    status = host.status()
    assert status & 0x20, "the dropped command must raise the overrun flag"
    status = host.wait()
    assert not status & 0x20 and status & 31 == expected, "the flag clears once read"

    # A go strobe during a run is dropped too, and unknown opcodes only read status.
    host.spi(OP_STEP << 5)
    expected = ref.step()
    host.pin(5, 1, "ui")
    host.tick(6)
    host.pin(5, 0, "ui")
    assert host.status() & 0x20
    assert host.wait() & 31 == expected
    assert host.spi(6 << 5 | 3, 7 << 5 | 9, OP_NOP << 5)[2] & 0x1F == expected

    # Slower clock, an abandoned frame, then a clean command.
    assert host.spi(OP_STEP << 5, divide=22) and host.wait() & 31 == ref.step()
    host.pin(0, 0)
    host.tick(4)
    for level in (1, 0, 1):
        host.pin(1, level)
        host.tick(4)
        host.pin(3, 1)
        host.tick(4)
        host.pin(3, 0)
    host.tick(4)
    host.pin(0, 1)
    host.tick(6)
    assert host.spi_token(OP_STEP << 5) == ref.step()

    # Reset in the middle of a run, disable, and start again.
    host.spi(OP_PUSH << 5 | index("a"))
    host.tick(9)
    host.reset()
    ref.clear()
    assert host.uo == 0
    host.ena = 0
    host.tick(3)
    host.ena = 1
    host.tick(3)
    for char in "loom":
        assert host.spi_token(OP_PUSH << 5 | index(char)) == ref.push(index(char))
    for _ in range(6):
        assert host.strobe() == ref.step()
    host.tick(4)
    return host.vectors


def write_pins(model, output):
    vectors = pin_scenario(model)
    lines = []
    for ui, uio, rst_n, ena, uo, uio_out, oe in vectors:
        lines.append(f"{ena << 41 | rst_n << 40 | ui << 32 | uio << 24 | uo << 16 | uio_out << 8 | oe:011x}")
    (output / "pins.hex").write_text("\n".join(lines) + "\n")
    return len(vectors)


def check_rom(model, dump):
    """Compare `rom_tb` output with the model. Every row and flag must match."""
    rows = list(placed_rows(model))
    seen = [line.split() for line in Path(dump).read_text().splitlines() if line.startswith("ROW ")]
    if len(seen) != len(rows):
        raise SystemExit(f"dumped {len(seen)} rows, the model has {len(rows)}")
    bits = shape(model)["CB"]
    for number, (line, (pos, neg, c, layer_end, is_out)) in enumerate(zip(seen, rows)):
        got = (int(line[1], 16), int(line[2], 16), int(line[3], 16), int(line[4]), int(line[5]))
        if got != (pos, neg, c & ((1 << bits) - 1), int(layer_end), int(is_out)):
            raise SystemExit(f"row {number} of the weight logic differs from the model")
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--rom", type=Path, help="compare this weight-logic dump instead of writing vectors")
    args = parser.parse_args()
    model = Model.load(args.model) if args.model else Model.load()
    if args.rom:
        print(f"weight logic matches the model in all {check_rom(model, args.rom)} rows")
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        entries, inferences = core_vectors(model, args.output)
        clocks = write_pins(model, args.output)
        dims = shape(model)
        (args.output / "tb_params.vh").write_text(
            f"`define TB_CORE_ENTRIES {entries}\n`define TB_PIN_CLOCKS {clocks}\n`define TB_ROWS {dims['R']}\n"
        )
        print(f"core: {inferences} inferences, {inferences * dims['R']} row values; pins: {clocks} clocks")
