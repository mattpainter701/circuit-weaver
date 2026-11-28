"""Integer reference model of the Heddle hardwired language-model tile.

The model file is the single source of truth for the weights. This module
defines their meaning. `build_rtl.py` turns the same file into logic, and the
testbenches compare that logic against this code row by row and pin by pin.
Only the Python standard library is used.
"""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = HERE / "model.json"
PUSH, STEP, CLEAR, SEED = range(4)
LFSR_BITS = 31
LFSR_MASK = (1 << LFSR_BITS) - 1


class Model:
    """Validated view of a model file."""

    def __init__(self, data):
        if data.get("schema") != 1:
            raise ValueError("unsupported model schema")
        self.alphabet = data["alphabet"]
        if len(self.alphabet) != 32 or len(set(self.alphabet)) != 32:
            raise ValueError("the alphabet must hold 32 distinct symbols")
        self.context = int(data["context"])
        self.embed_bits = int(data["embed_bits"])
        self.embedding = [int(word, 16) for word in data["embedding"]]
        if len(self.embedding) != 32 or max(self.embedding) >> self.embed_bits:
            raise ValueError("malformed embedding table")
        self.layers = []
        inputs = self.context * self.embed_bits
        for layer in data["layers"]:
            if layer["inputs"] != inputs:
                raise ValueError("layer input width does not match the previous layer")
            rows = [(int(row["pos"], 16), int(row["neg"], 16), int(row["c"])) for row in layer["rows"]]
            for pos, neg, _ in rows:
                if pos & neg or (pos | neg) >> inputs:
                    raise ValueError("a weight must be exactly one of -1, 0 or +1")
            self.layers.append((inputs, rows))
            inputs = len(rows)
        if len(self.layers) < 2 or inputs != 32:
            raise ValueError("need at least one hidden layer and a 32-row output layer")
        self.gumbel = [int(value) for value in data["sampler"]["gumbel"]]
        self.seed = int(data["sampler"]["lfsr_seed"])
        if len(self.gumbel) != 32 or min(self.gumbel) < 0 or not 0 < self.seed <= LFSR_MASK:
            raise ValueError("malformed sampler")
        self.sha256 = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @classmethod
    def load(cls, path=MODEL):
        return cls(json.loads(Path(path).read_text()))

    @property
    def rows(self):
        return sum(len(rows) for _, rows in self.layers)

    @property
    def weights(self):
        return sum(inputs * len(rows) for inputs, rows in self.layers)

    @property
    def nonzero(self):
        return sum(bin(pos | neg).count("1") for _, rows in self.layers for pos, neg, _ in rows)


def row_value(row, x, inputs):
    """Signed pre-activation of one row for the input bit vector x."""
    pos, neg, c = row
    return 2 * bin((pos & x) | (neg & ~x & ((1 << inputs) - 1))).count("1") + c


def lfsr_advance(state):
    """Five steps of the Fibonacci LFSR x^31 + x^28 + 1."""
    for _ in range(5):
        state = ((state << 1) | (((state >> 30) ^ (state >> 27)) & 1)) & LFSR_MASK
    return state


def lfsr_stir(state, value):
    """Shift five host-supplied bits into the generator, never reaching zero."""
    return ((state << 5) | (value & 31)) & LFSR_MASK or 1


class Chip:
    """Architectural state of the core. One method call is one accepted request."""

    def __init__(self, model):
        self.model = model
        self.clear()

    def clear(self):
        self.ctx = [0] * self.model.context
        self.lfsr = self.model.seed
        self.token = 0
        self.valid = False

    def seed(self, value):
        self.lfsr = lfsr_stir(self.lfsr, value)

    def context_bits(self):
        bits = 0
        for position, token in enumerate(self.ctx):
            bits |= self.model.embedding[token] << (position * self.model.embed_bits)
        return bits

    def infer(self, sample=False, trace=None):
        """Evaluate every row in hardware order and return the chosen token."""
        x = self.context_bits()
        for inputs, rows in self.model.layers[:-1]:
            fired = 0
            for index, row in enumerate(rows):
                value = row_value(row, x, inputs)
                if trace is not None:
                    trace.append(value)
                fired |= (value >= 0) << index
            x = fired
        inputs, rows = self.model.layers[-1]
        best = winner = None
        for index, row in enumerate(rows):
            value = row_value(row, x, inputs)
            if trace is not None:
                trace.append(value)
            score = value + (self.model.gumbel[self.lfsr & 31] if sample else 0)
            self.lfsr = lfsr_advance(self.lfsr)
            if best is None or score > best:
                best, winner = score, index
        self.token, self.valid = winner, True
        return winner

    def push(self, token, sample=False, trace=None):
        """Append a host token to the context and predict its successor."""
        self.ctx = self.ctx[1:] + [token & 31]
        return self.infer(sample, trace)

    def step(self, sample=False, trace=None):
        """Append the last prediction to the context and predict again."""
        return self.push(self.token, sample, trace)


def generate(model, prompt="", count=400, sample=True, seed_bits=()):
    """Return text produced autoregressively after an optional prompt."""
    chip = Chip(model)
    for value in seed_bits:
        chip.seed(value)
    token = None
    for char in prompt.lower():
        if char in model.alphabet:
            token = chip.push(model.alphabet.index(char), sample)
    if token is None:
        token = chip.step(sample)
    out = [model.alphabet[token]]
    for _ in range(count - 1):
        out.append(model.alphabet[chip.step(sample)])
    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description="Run the Heddle virtual chip")
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--count", type=int, default=400)
    parser.add_argument("--greedy", action="store_true", help="disable the on-chip sampler")
    parser.add_argument("--seed", type=int, nargs="*", default=[], help="5-bit values stirred into the sampler")
    parser.add_argument("--info", action="store_true")
    args = parser.parse_args()
    model = Model.load(args.model)
    if args.info:
        print(f"rows {model.rows}  weights {model.weights}  nonzero {model.nonzero}  sha256 {model.sha256}")
        return
    print(args.prompt + generate(model, args.prompt, args.count, not args.greedy, args.seed))


if __name__ == "__main__":
    main()
