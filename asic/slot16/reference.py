"""Independent cycle-level Slot16 interpreter. The caller supplies both ROM banks."""
from dataclasses import dataclass, field


@dataclass
class Context:
    pc: int = 0
    regs: list[int] = field(default_factory=lambda: [0] * 8)
    scratch: list[int] = field(default_factory=lambda: [0] * 64)
    halted: bool = False
    faulted: bool = False
    inbox: int | None = None


def signed_field(word: int, width: int) -> int:
    sign = 1 << (width - 1)
    return ((word & (2 * sign - 1)) ^ sign) - sign


class Machine:
    def __init__(self, banks: list[list[int]]):
        if len(banks) != 2 or any(len(bank) > 256 for bank in banks):
            raise ValueError("expected two ROM banks of up to 256 words each")
        self.rom = [bank + [0] * (256 - len(bank)) for bank in banks]
        self.contexts = [Context(), Context()]
        self.slot = 0

    def step(self, reset: bool, valid: int, inputs: int, ready: int) -> tuple[int, ...]:
        """Advance one edge and return pre-edge address, retire and stream strobes/data."""
        lane = self.slot
        ctx = self.contexts[lane]
        peer = self.contexts[1 - lane]
        addr = lane * 256 + ctx.pc
        if reset:
            self.slot = 0
            self.contexts = [Context(scratch=c.scratch) for c in self.contexts]
            return addr, 0, 0, 0, 0
        self.slot = 1 - lane
        if ctx.halted or ctx.faulted:
            return addr, 0, 0, 0, 0

        word = self.rom[lane][ctx.pc]
        op = word >> 12
        dest = (word >> 9) & 7
        source = ctx.regs[(word >> 6) & 7]
        old = ctx.regs[dest]
        next_pc = ctx.pc + 1
        mask = 1 << lane
        input_strobe = mask if op == 10 else 0
        output_strobe = mask if op == 11 else 0
        pins = (input_strobe, output_strobe, old if output_strobe else 0)
        if (op == 8 and peer.inbox is not None or op == 9 and ctx.inbox is None
                or input_strobe and not valid & mask or output_strobe and not ready & mask):
            return addr, 0, *pins
        effective = source + signed_field(word, 6)
        if op == 15 or op in (5, 6) and not 0 <= effective < 64:
            ctx.faulted = True
            return addr, 0, *pins
        if op == 0:
            ctx.halted = True
        elif op == 1:
            ctx.regs[dest] = signed_field(word, 9) & 65535
        elif op == 2:
            ctx.regs[dest] = (old + source) & 65535
        elif op == 3:
            ctx.regs[dest] = (old - source) & 65535
        elif op == 4:
            ctx.regs[dest] = old ^ source
        elif op == 5:
            ctx.regs[dest] = ctx.scratch[effective]
        elif op == 6:
            ctx.scratch[effective] = old
        elif op == 7 and old:
            next_pc += signed_field(word, 9)
        elif op == 8:
            peer.inbox = old
        elif op == 9:
            ctx.regs[dest] = ctx.inbox
            ctx.inbox = None
        elif op == 10:
            ctx.regs[dest] = (inputs >> (16 * lane)) & 65535
        elif op == 12:
            ctx.regs[dest] = int(bool(valid & mask))
        elif op == 13:
            ctx.regs[dest] = (old << 1) & 65535
        elif op == 14:
            ctx.regs[dest] = old & source
        ctx.pc = next_pc % 256
        return addr, 1, *pins
