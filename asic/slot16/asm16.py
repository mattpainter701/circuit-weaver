"""Two-pass assembler for one Slot16 program bank. Run with SOURCE OUTPUT."""
import argparse
import re
from pathlib import Path

OPS = "HALT LDI ADD SUB XOR LD ST BNZ SEND RECV IN OUT VALID SHL AND".split()
REG = re.compile(r"r([0-7])", re.I)
LABEL = re.compile(r"[a-zA-Z_]\w*")


def register(token: str) -> int:
    match = REG.fullmatch(token)
    if not match:
        raise ValueError(f"invalid register {token!r}")
    return int(match[1])


def signed(value: int, bits: int) -> int:
    limit = 1 << (bits - 1)
    if not -limit <= value < limit:
        raise ValueError(f"{value} outside signed {bits}-bit field")
    return value % (2 * limit)


def encode(line: str, pc: int, labels: dict[str, int]) -> int:
    mnemonic, *tail = line.split(maxsplit=1)
    op = mnemonic.upper()
    if op not in OPS:
        raise ValueError(f"unknown instruction {op}")
    args = [part.strip() for part in tail[0].split(",")] if tail else []
    count = 0 if op == "HALT" else 2 if op in {"LDI", "ADD", "SUB", "XOR", "LD", "ST", "BNZ", "AND"} else 1
    if len(args) != count:
        raise ValueError(f"{op} expects {count} operands")
    word = OPS.index(op) << 12
    if not args:
        return word
    word |= register(args[0]) << 9
    if len(args) == 1:
        return word
    operand = args[1]
    if op in {"LDI", "BNZ"}:
        if op == "BNZ" and operand in labels:
            offset = labels[operand] - pc - 1
        else:
            offset = int(operand, 0)
        return word | signed(offset, 9)
    if op in {"LD", "ST"}:
        match = re.fullmatch(r"\[\s*(r[0-7])\s*(?:([+-])\s*(\d+))?\s*\]", operand, re.I)
        if not match:
            raise ValueError(f"invalid address {operand!r}")
        offset = int((match[2] or "+") + (match[3] or "0"))
        return word | register(match[1]) << 6 | signed(offset, 6)
    return word | register(operand) << 6


def assemble(source: str) -> list[int]:
    labels = {}
    lines = []
    for number, line in enumerate(source.splitlines(), 1):
        line = line.partition("#")[0].strip()
        if ":" in line:
            label, line = (part.strip() for part in line.split(":", 1))
            if not LABEL.fullmatch(label) or label in labels or len(lines) >= 256:
                raise ValueError(f"line {number}: invalid or duplicate label {label!r}")
            labels[label] = len(lines)
        if line:
            lines.append((number, line))
    if len(lines) > 256:
        raise ValueError("program exceeds 256 words")
    words = []
    for pc, (number, line) in enumerate(lines):
        try:
            words.append(encode(line, pc, labels))
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from exc
    return words


def write_image(words: list[int], path: Path) -> None:
    path.write_text("".join(f"{word:04x}\n" for word in words), encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        write_image(assemble(args.source.read_text()), args.output)
    except ValueError as error:
        parser.error(str(error))
