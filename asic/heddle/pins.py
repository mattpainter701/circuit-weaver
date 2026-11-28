"""Cycle-accurate pin model of the Heddle tile and a bit-banged host driver.

`Tile.clock` advances one rising clock edge and mirrors every register in
`tt_um_heddle.v`. `Host` drives it the way a bridge or microcontroller would,
which makes this file both the virtual board and the source of the pin
vectors replayed against the RTL and the gate netlist.
"""
from heddle import CLEAR, PUSH, SEED, STEP, Chip

OP_NOP, OP_PUSH, OP_STEP, OP_CLEAR, OP_SEED, OP_CFG = range(6)
SCK_DIVIDE = 8  # tile clocks per SCK period, the fastest supported rate


class Tile:
    def __init__(self, model):
        self.model = model
        self.rows = model.rows
        self.reset()

    def reset(self):
        self.chip = Chip(self.model)
        self.busy = 0
        self.left = 0
        self.pending = None
        self.out_valid = 0
        self.out_token = 0
        self.sck_s, self.cs_s, self.go_s = 0b000, 0b111, 0b000
        self.mosi_s = self.sample_s = 0b00
        self.tx = self.rx = self.nbit = 0
        self.overrun = self.cfg_sample = self.sample_q = self.toggle = self.valid_q = 0

    def outputs(self, uio_in, ena=1):
        uo = self.toggle << 7 | self.busy << 6 | self.out_valid << 5 | self.out_token
        return uo, (self.tx >> 7) << 2, (1 if ena and not uio_in & 1 else 0) << 2

    def clock(self, ui, uio_in, rst_n=1, ena=1):
        """Apply one rising edge with the given pin levels and return the new outputs."""
        if not rst_n or not ena:
            self.reset()
            return self.outputs(uio_in, ena)
        cs_active = not self.cs_s >> 1 & 1
        cs_start = self.cs_s >> 2 & 1 and cs_active
        sck_rise = cs_active and not self.sck_s >> 2 & 1 and self.sck_s >> 1 & 1
        sck_fall = cs_active and self.sck_s >> 2 & 1 and not self.sck_s >> 1 & 1
        go_rise = not self.go_s >> 2 & 1 and self.go_s >> 1 & 1
        cmd = self.rx << 1 | self.mosi_s >> 1
        cmd_done = sck_rise and self.nbit == 7
        op = cmd >> 5
        spi_req = cmd_done and op in (OP_PUSH, OP_STEP, OP_CLEAR, OP_SEED)
        req_valid = spi_req or go_rise
        if spi_req:
            kind, token = {OP_PUSH: PUSH, OP_STEP: STEP, OP_CLEAR: CLEAR, OP_SEED: SEED}[op], cmd & 31
        else:
            kind, token = (PUSH if ui >> 6 & 1 else STEP), ui & 31
        status = self.busy << 7 | self.out_valid << 6 | self.overrun << 5 | self.out_token
        load_status = cs_start or (sck_fall and self.nbit == 0)
        busy, out_valid = self.busy, self.out_valid

        # Wrapper registers, all computed from the pre-edge state.
        nxt = {}
        nxt["sck_s"] = (self.sck_s << 1 | uio_in >> 3 & 1) & 7
        nxt["cs_s"] = (self.cs_s << 1 | uio_in & 1) & 7
        nxt["go_s"] = (self.go_s << 1 | ui >> 5 & 1) & 7
        nxt["mosi_s"] = (self.mosi_s << 1 | uio_in >> 1 & 1) & 3
        nxt["sample_s"] = (self.sample_s << 1 | ui >> 7 & 1) & 3
        if not busy:
            nxt["sample_q"] = self.sample_s >> 1 | self.cfg_sample
        nxt["valid_q"] = out_valid
        if out_valid and not self.valid_q:
            nxt["toggle"] = self.toggle ^ 1
        if cs_start:
            nxt["nbit"] = 0
        elif sck_rise:
            nxt["rx"] = cmd & 0x7F
            nxt["nbit"] = (self.nbit + 1) & 7
        if load_status:
            nxt["tx"] = status
        elif sck_fall:
            nxt["tx"] = self.tx << 1 & 0xFF
        if cmd_done and op == OP_CFG:
            nxt["cfg_sample"] = cmd & 1
        if cs_start:
            nxt["overrun"] = 0
        if (req_valid and busy) or (spi_req and go_rise):
            nxt["overrun"] = 1

        # Core: a request is evaluated when accepted and delivered one edge per row later.
        if busy:
            self.left -= 1
            if self.left == 0:
                self.out_token, self.out_valid, self.busy = self.pending, 1, 0
        elif req_valid:
            if kind == CLEAR:
                self.chip.clear()
                self.out_valid = self.out_token = 0
            elif kind == SEED:
                self.chip.seed(token)
            else:
                # sample_q is rewritten on every idle edge, so the run sees the value this edge latches
                sample = bool(self.sample_s >> 1 | self.cfg_sample)
                self.pending = self.chip.push(token, sample) if kind == PUSH else self.chip.step(sample)
                self.busy, self.left, self.out_valid = 1, self.rows, 0
        for name, value in nxt.items():
            setattr(self, name, value)
        return self.outputs(uio_in, ena)


class Host:
    """Drives the pins and records one vector per clock."""

    def __init__(self, tile):
        self.tile = tile
        self.ui = 0
        self.uio = 0b0001  # CS high, SCK and MOSI low
        self.rst_n = self.ena = 1
        self.vectors = []
        self.uo = 0
        self.uio_out = 0

    def tick(self, count=1):
        for _ in range(count):
            self.uo, self.uio_out, oe = self.tile.clock(self.ui, self.uio, self.rst_n, self.ena)
            self.vectors.append((self.ui, self.uio, self.rst_n, self.ena, self.uo, self.uio_out, oe))

    def reset(self, clocks=4):
        self.rst_n = 0
        self.tick(clocks)
        self.rst_n = 1
        self.tick(2)

    def pin(self, bit, level, bus="uio"):
        value = getattr(self, bus)
        setattr(self, bus, value | 1 << bit if level else value & ~(1 << bit))

    # ---- SPI port -------------------------------------------------------
    def spi(self, *commands, divide=SCK_DIVIDE):
        """Send command bytes in one chip-select frame and return the status bytes read."""
        half = divide // 2
        self.pin(0, 0)
        self.tick(half)
        replies = []
        for command in commands:
            reply = 0
            for bit in range(7, -1, -1):
                self.pin(1, command >> bit & 1)
                self.tick(half)
                reply = reply << 1 | self.uio_out >> 2 & 1
                self.pin(3, 1)
                self.tick(half)
                self.pin(3, 0)
            replies.append(reply)
        self.tick(half)
        self.pin(0, 1)
        self.tick(half)
        return replies

    def status(self):
        return self.spi(OP_NOP << 5)[0]

    def wait(self, limit=64):
        """Poll until the tile is idle and return the status byte."""
        for _ in range(limit):
            status = self.status()
            if not status & 0x80:
                return status
        raise TimeoutError("tile stayed busy")

    def spi_token(self, command):
        self.spi(command)
        status = self.wait()
        if not status & 0x40:
            raise RuntimeError("no result after a completed request")
        return status & 31

    # ---- parallel port --------------------------------------------------
    def strobe(self, token=None):
        """Pulse go. A token selects push; None feeds the last result back."""
        self.ui = self.ui & 0x80 | (0x40 | token if token is not None else 0)
        self.tick(2)
        self.pin(5, 1, "ui")
        self.tick(4)
        self.pin(5, 0, "ui")
        for _ in range(self.tile.rows + 8):
            self.tick()
            if self.uo & 0x20 and not self.uo & 0x40:
                return self.uo & 31
        raise TimeoutError("no parallel result")
