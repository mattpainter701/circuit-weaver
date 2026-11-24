"""Boot a pair of Slot16 firmware images on the Weftline 16 virtual board."""
import argparse
import re
from collections import deque
from pathlib import Path

from asm16 import assemble
from reference import Machine

FIRMWARE = Path(__file__).resolve().parent / "firmware"
DEMO = (0, 1, 3, 2, 0, 0, 2, 1, 3)
COMMANDS = "step [clocks] | input LANE WORD... | ready LANE 0|1 | status | reset | quit"


def load_bank(path: Path) -> list[int]:
    source = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".s16":
        return assemble(source)
    if path.suffix.lower() != ".hex":
        raise ValueError(f"{path}: expected .s16 assembly or .hex ROM words")
    words = source.split()
    if len(words) > 256 or any(not re.fullmatch(r"[0-9a-fA-F]{1,4}", word) for word in words):
        raise ValueError(f"{path}: expected up to 256 hexadecimal 16-bit words")
    return [int(word, 16) for word in words]


def number(token: str, minimum: int, maximum: int | None = None) -> int:
    value = int(token, 0)
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"value out of range: {token}")
    return value


class Board:
    def __init__(self, banks: list[list[int]]):
        self.cpu = Machine(banks)
        self.inputs: list[deque[int]] = [deque(), deque()]
        self.reset()

    def reset(self) -> None:
        self.cpu.step(True, 0, 0, 0)
        self.cycle = 0
        self.ready = [True, True]
        for queue in self.inputs:
            queue.clear()

    def pins(self) -> tuple[int, int, int]:
        valid = int(bool(self.inputs[0])) | (int(bool(self.inputs[1])) << 1)
        words = sum(queue[0] << (16 * index) for index, queue in enumerate(self.inputs) if queue)
        return valid, words, int(self.ready[0]) | (int(self.ready[1]) << 1)

    def tick(self) -> tuple[int, int, int] | None:
        lane = self.cpu.slot
        edge = self.cycle
        valid, inputs, ready = self.pins()
        _, _, rx, tx, word = self.cpu.step(False, valid, inputs, ready)
        if rx & valid:
            self.inputs[lane].popleft()
        self.cycle += 1
        return (edge, lane, word) if tx & ready else None

    def describe(self, lane: int) -> str:
        ctx = self.cpu.contexts[lane]
        op = self.cpu.rom[lane][ctx.pc] >> 12
        if ctx.faulted:
            return "faulted"
        if ctx.halted:
            return "halted"
        if op == 8 and self.cpu.contexts[1 - lane].inbox is not None:
            return "waiting for peer mailbox space"
        if op == 9 and ctx.inbox is None:
            return "waiting for mailbox input"
        if op == 10 and not self.inputs[lane]:
            return "waiting for stream input"
        if op == 11 and not self.ready[lane]:
            return "waiting for output ready"
        return "runnable"

    def status(self) -> None:
        print(f"clock={self.cycle} next_lane={self.cpu.slot}")
        for lane, ctx in enumerate(self.cpu.contexts):
            regs = " ".join(f"{word:04x}" for word in ctx.regs)
            mail = "empty" if ctx.inbox is None else f"0x{ctx.inbox:04x}"
            print(f"lane={lane} pc=0x{ctx.pc:02x} {self.describe(lane)} "
                  f"input_depth={len(self.inputs[lane])} ready={int(self.ready[lane])} inbox={mail} regs=[{regs}]")

    def run(self, clocks: int) -> None:
        for _ in range(clocks):
            event = self.tick()
            if event is not None:
                edge, lane, word = event
                print(f"clock={edge} out{lane}=0x{word:04x} ({word})")


def monitor(board: Board) -> None:
    print(COMMANDS)
    while True:
        try:
            parts = input("weftline> ").split()
        except EOFError:
            return
        try:
            if not parts:
                continue
            match parts:
                case ["quit"]:
                    return
                case ["help"]:
                    print(COMMANDS)
                case ["status"]:
                    board.status()
                case ["reset"]:
                    board.reset()
                case ["step"] | ["step", _]:
                    board.run(number(parts[1], 0) if len(parts) == 2 else 1)
                    board.status()
                case ["input", target, first, *rest]:
                    lane = number(target, 0, 1)
                    values = [number(word, 0, 65535) for word in [first, *rest]]
                    board.inputs[lane].extend(values)
                case ["ready", target, state]:
                    board.ready[number(target, 0, 1)] = bool(number(state, 0, 1))
                case _:
                    raise ValueError(COMMANDS)
        except ValueError as exc:
            print(f"error: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom0", type=Path, help="lane 0 assembly or hex image")
    parser.add_argument("--rom1", type=Path, help="lane 1 assembly or hex image")
    parser.add_argument("--input0", nargs="+", metavar="WORD", help="queue lane 0 input words")
    parser.add_argument("--input1", nargs="+", metavar="WORD", help="queue lane 1 input words")
    parser.add_argument("--cycles", metavar="N", help="batch clock budget (default 512)")
    parser.add_argument("--monitor", action="store_true", help="open the interactive monitor")
    args = parser.parse_args()
    if args.monitor and args.cycles is not None:
        parser.error("--cycles applies to batch runs; use step in the monitor")
    try:
        clocks = number(args.cycles or "512", 0)
        board = Board([load_bank(args.rom0 or FIRMWARE / "encoder.s16"),
                       load_bank(args.rom1 or FIRMWARE / "encoder_diagnostics.s16")])
        for lane, words in enumerate([args.input0, args.input1]):
            if words:
                board.inputs[lane].extend(number(word, 0, 65535) for word in words)
        if not any([args.rom0, args.rom1, args.input0, args.input1, args.monitor]):
            board.inputs[0].extend(DEMO)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.monitor:
        monitor(board)
    else:
        board.run(clocks)
    board.status()
    return int(any(ctx.faulted for ctx in board.cpu.contexts))


if __name__ == "__main__":
    raise SystemExit(main())
