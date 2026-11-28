"""Train the Heddle character model and write `model.json`.

The network reads the last four characters as one-hot signs, has two hidden
layers of sign units and uses weights restricted to -1, 0 and +1. Training
keeps real-valued shadow weights and passes gradients straight through the
quantizers. The saved model contains integers only.

The corpus is the prose of this repository's Markdown files at a pinned
commit, so the script needs no network access. NumPy is the only dependency.
Floating-point training is not bit-reproducible across machines; the committed
`model.json` is the authority for the silicon.
"""
import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORPUS_REV = "a2f3e5a3d2862e68b9580bf458a2246abfe5aa68"
ALPHABET = " abcdefghijklmnopqrstuvwxyz.,-0\n"
CONTEXT = 4
HIDDEN = (96, 48)
SKIP = ("src/circuit_weaver/_bundled_skills", "samples/", "tests/")


def corpus(rev=CORPUS_REV):
    """Return one normalized string per Markdown file: prose only, 32 symbols."""
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True).stdout

    names = sorted(
        name for name in git("ls-tree", "-r", "--name-only", rev).decode().split("\n")
        if name.endswith(".md") and not name.startswith(SKIP)
    )
    docs = []
    for name in names:
        kept, code = [], False
        for line in git("show", f"{rev}:{name}").decode("utf-8", "replace").split("\n"):
            if line.strip().startswith("```"):
                code = not code
                continue
            if code or line.lstrip().startswith(("|", "<", "!", "    ")):
                continue
            line = re.sub(r"`[^`]*`", " ", line)
            line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            kept.append(re.sub(r"https?://\S+", " ", line))
        text = re.sub(r"[0-9]+", "0", "\n".join(kept).lower())
        text = "".join(char if char in ALPHABET else " " for char in text)
        text = re.sub(r" +", " ", text)
        docs.append(re.sub(r" ?\n[ \n]*", "\n", text).strip())
    return docs


def dataset(docs, block=2000):
    """Sliding windows. Every tenth block of each file is held out."""
    train, held = [], []
    for doc in docs:
        ids = np.array([ALPHABET.index(char) for char in doc], dtype=np.int64)
        for start in range(0, len(ids), block):
            lead = np.zeros(CONTEXT, dtype=np.int64) if start == 0 else ids[start - CONTEXT:start]
            (held if (start // block) % 10 == 9 else train).append(np.concatenate([lead, ids[start:start + block]]))

    def windows(blocks):
        view = [np.lib.stride_tricks.sliding_window_view(b, CONTEXT + 1) for b in blocks if len(b) > CONTEXT]
        both = np.concatenate(view)
        return both[:, :CONTEXT], both[:, CONTEXT]

    return windows(train), windows(held)


def ternary(weights):
    """Zero the small shadow weights of each row and keep the sign of the rest."""
    cut = 0.7 * np.abs(weights).mean(axis=1, keepdims=True)
    return (np.sign(weights) * (np.abs(weights) > cut)).astype(np.float32)


def train(train_set, steps, seed, batch=512, rate=1e-2, log=None):
    rng = np.random.default_rng(seed)
    x_all, y_all = train_set
    onehot = (2 * np.eye(32) - 1).astype(np.float32)
    dims = [CONTEXT * 32, *HIDDEN]
    hidden = [rng.normal(0, dims[i] ** -0.5, (dims[i + 1], dims[i])).astype(np.float32) for i in range(len(HIDDEN))]
    shift = [np.zeros(width, np.float32) for width in HIDDEN]
    out = rng.normal(0, dims[-1] ** -0.5, (32, dims[-1])).astype(np.float32)
    bias, scale = np.zeros(32, np.float32), np.array([0.3], np.float32)
    params = [*hidden, *shift, out, bias, scale]
    first = [np.zeros_like(p) for p in params]
    second = [np.zeros_like(p) for p in params]
    for step in range(1, steps + 1):
        pick = rng.integers(0, len(x_all), batch)
        tokens, target = x_all[pick], y_all[pick]
        acts, cache = [onehot[tokens].reshape(batch, -1)], []
        for weights, offset in zip(hidden, shift):
            quant = ternary(weights)
            pre = acts[-1] @ quant.T
            spread = pre.std(0) + 1e-5
            norm = (pre - pre.mean(0)) / spread + offset
            cache.append((quant, spread, norm))
            acts.append(np.where(norm >= 0, 1, -1).astype(np.float32))
        quant_out = ternary(out)
        raw = acts[-1] @ quant_out.T
        logits = scale[0] * raw + bias
        logits -= logits.max(1, keepdims=True)
        prob = np.exp(logits)
        prob /= prob.sum(1, keepdims=True)
        loss = -np.log(prob[np.arange(batch), target] + 1e-12).mean()
        delta = prob
        delta[np.arange(batch), target] -= 1
        delta /= batch
        grads_out = [scale[0] * (delta.T @ acts[-1]), delta.sum(0), np.array([(delta * raw).sum()], np.float32)]
        back = scale[0] * (delta @ quant_out)
        grads_w, grads_s = [None] * len(HIDDEN), [None] * len(HIDDEN)
        for layer in reversed(range(len(HIDDEN))):
            quant, spread, norm = cache[layer]
            through = back * (np.abs(norm) <= 1)  # straight-through estimate of the sign unit
            grads_s[layer] = through.sum(0)
            through = through / spread
            grads_w[layer] = through.T @ acts[layer]
            back = through @ quant
        grads = [*grads_w, *grads_s, *grads_out]
        now = rate * 0.5 * (1 + math.cos(math.pi * (step - 1) / steps))
        for param, grad, m, v in zip(params, grads, first, second):  # Adam
            m *= 0.9
            m += 0.1 * grad
            v *= 0.999
            v += 0.001 * grad * grad
            param -= now * (m / (1 - 0.9 ** step)) / (np.sqrt(v / (1 - 0.999 ** step)) + 1e-8)
        for weights in (*hidden, out):
            np.clip(weights, -1.5, 1.5, out=weights)
        if log and step % 5000 == 0:
            log(f"  seed {seed} step {step} loss {loss / math.log(2):.3f} bits")

    # Fold the normalization into one integer threshold per unit, using the quantized net itself.
    x = onehot[x_all[:300000]].reshape(-1, CONTEXT * 32).astype(np.int64)
    layers = []
    for weights, offset in zip(hidden, shift):
        quant = ternary(weights).astype(np.int64)
        pre = x @ quant.T
        threshold = np.ceil(pre.mean(0) - offset * (pre.std(0) + 1e-5)).astype(np.int64)
        layers.append((quant, -threshold))
        x = np.where(pre - threshold >= 0, 1, -1)
    layers.append((ternary(out).astype(np.int64), np.round(bias / scale[0]).astype(np.int64)))
    return layers, float(scale[0])


def logits(layers, tokens):
    x = np.where(np.arange(32) == tokens[..., None], 1, -1).reshape(len(tokens), -1)
    for weights, constant in layers[:-1]:
        x = np.where(x @ weights.T + constant >= 0, 1, -1)
    return x @ layers[-1][0].T + layers[-1][1]


def bits_per_char(layers, data, scales):
    """Best cross-entropy over the candidate logit scales, in bits, and that scale."""
    tokens, target = data
    values = logits(layers, tokens).astype(np.float64)
    best = (math.inf, None)
    for scale in scales:
        z = scale * values
        z -= z.max(1, keepdims=True)
        loss = -(z - np.log(np.exp(z).sum(1, keepdims=True)))[np.arange(len(target)), target].mean() / math.log(2)
        best = min(best, (loss, scale))
    return best


def export(layers, scale, facts):
    """Integer model in the hardware's terms: v = 2 * matches + c for each row."""
    data = {
        "schema": 1,
        "alphabet": ALPHABET,
        "context": CONTEXT,
        "embed_bits": 32,
        "embedding": [f"{1 << token:08x}" for token in range(32)],
        "layers": [],
        "sampler": {
            "lfsr_seed": 0x2C0FFEE5,
            # Gumbel quantiles in logit units: adding one to each score and taking the
            # maximum draws a token from the softmax of the scores.
            "gumbel": [round((-math.log(-math.log((i + 0.5) / 32)) + math.log(-math.log(0.5 / 32))) / scale)
                       for i in range(32)],
        },
        "training": facts,
    }
    for weights, constant in layers:
        rows = []
        for row, c in zip(weights, constant):
            pos = sum(1 << j for j in np.flatnonzero(row > 0).tolist())
            neg = sum(1 << j for j in np.flatnonzero(row < 0).tolist())
            rows.append({"pos": f"{pos:x}", "neg": f"{neg:x}", "c": int(c) - int(np.count_nonzero(row))})
        data["layers"].append({"inputs": int(weights.shape[1]), "rows": rows})
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=60000)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    parser.add_argument("--output", type=Path, default=HERE / "model.json")
    args = parser.parse_args()
    docs = corpus()
    digest = hashlib.sha256("\x00".join(docs).encode()).hexdigest()
    train_set, held = dataset(docs)
    best = None
    by_seed = {}
    for seed in args.seeds:
        layers, scale = train(train_set, args.steps, seed, log=print)
        loss, fitted = bits_per_char(layers, held, [scale * f for f in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5)])
        print(f"seed {seed}: held-out {loss:.3f} bits per character at logit scale {fitted:.4f}", flush=True)
        by_seed[str(seed)] = round(loss, 4)
        if best is None or loss < best[0]:
            best = (loss, fitted, seed, layers)
    loss, fitted, seed, layers = best
    counts = np.bincount(train_set[1], minlength=32) / len(train_set[1])
    sample = (train_set[0][:200000], train_set[1][:200000])
    facts = {
        "corpus_rev": CORPUS_REV,
        "corpus_sha256": digest,
        "corpus_chars": int(sum(len(doc) for doc in docs)),
        "train_windows": int(len(train_set[1])),
        "held_out_windows": int(len(held[1])),
        "steps": args.steps,
        "seed": seed,
        "seeds_tried": args.seeds,
        "held_out_bits_per_char": round(loss, 4),
        "train_bits_per_char": round(bits_per_char(layers, sample, [fitted])[0], 4),
        "unigram_bits_per_char": round(float(-(counts * np.log2(counts + 1e-12)).sum()), 4),
        "logit_scale": round(fitted, 6),
        "held_out_by_seed": by_seed,
    }
    args.output.write_text(json.dumps(export(layers, fitted, facts), indent=1) + "\n")
    print(f"wrote {args.output} from seed {seed}: {facts['held_out_bits_per_char']} bits per character")


if __name__ == "__main__":
    main()
