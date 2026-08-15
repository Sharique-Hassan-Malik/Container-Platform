#!/usr/bin/env python3
"""Write a GPT-2-shaped checkpoint of a chosen size.

The `gpt2-124m` preset is byte-for-byte the size of real GPT-2 small in fp32
(497 MB), because that is the scale at which layer ordering stops being a style
argument and starts costing minutes per deploy. Smaller presets exist so the
test suite runs in seconds.

Weights are random. Cold start measures I/O and process startup; a trained
checkpoint of the same shape produces the same numbers and a 500 MB download.
"""

from __future__ import annotations

import argparse
import json
import os
import struct

import numpy as np

MAGIC = b"MMGPT001"
ALIGN = 4096

PRESETS = {
    "tiny":      dict(n_layer=2,  n_head=2,  n_embd=64,   vocab=512,   block=64),
    "small":     dict(n_layer=4,  n_head=4,  n_embd=256,  vocab=4096,  block=128),
    "medium":    dict(n_layer=6,  n_head=6,  n_embd=384,  vocab=16384, block=256),
    "gpt2-124m": dict(n_layer=12, n_head=12, n_embd=768,  vocab=50257, block=1024),
}


def plan(config: dict) -> list[dict]:
    """Lay out every tensor, in the order a forward pass touches it.

    Ordering by access is not cosmetic: with `--load mmap` it turns first-token
    page faults into a mostly sequential read instead of a scatter across the
    file.
    """
    n_layer, n_embd, vocab, block = config["n_layer"], config["n_embd"], config["vocab"], config["block"]
    specs: list[tuple[str, tuple[int, ...]]] = [
        ("wte", (vocab, n_embd)),
        ("wpe", (block, n_embd)),
    ]
    for i in range(n_layer):
        p = f"h.{i}."
        specs += [
            (p + "ln_1.w", (n_embd,)), (p + "ln_1.b", (n_embd,)),
            (p + "attn.qkv.w", (n_embd, 3 * n_embd)), (p + "attn.qkv.b", (3 * n_embd,)),
            (p + "attn.proj.w", (n_embd, n_embd)), (p + "attn.proj.b", (n_embd,)),
            (p + "ln_2.w", (n_embd,)), (p + "ln_2.b", (n_embd,)),
            (p + "mlp.fc.w", (n_embd, 4 * n_embd)), (p + "mlp.fc.b", (4 * n_embd,)),
            (p + "mlp.proj.w", (4 * n_embd, n_embd)), (p + "mlp.proj.b", (n_embd,)),
        ]
    specs += [("ln_f.w", (n_embd,)), ("ln_f.b", (n_embd,))]

    out, offset = [], 0
    for name, shape in specs:
        nbytes = int(np.prod(shape)) * 4
        out.append({"name": name, "shape": list(shape), "offset": offset, "dtype": "f32"})
        offset += nbytes
    return out


def write(path: str, preset: str, seed: int = 0) -> dict:
    config = dict(PRESETS[preset])
    tensors = plan(config)
    header = {**config, "name": preset, "tensors": tensors}
    blob = json.dumps(header, separators=(",", ":")).encode()
    data_offset = ALIGN * ((12 + len(blob) + ALIGN - 1) // ALIGN)

    rng = np.random.default_rng(seed)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(MAGIC)
        handle.write(struct.pack("<I", len(blob)))
        handle.write(blob)
        handle.write(b"\0" * (data_offset - handle.tell()))
        for spec in tensors:
            name = spec["name"]
            # LayerNorm gains start at 1 and biases at 0; everything else gets a
            # GPT-2-style N(0, 0.02). Getting this wrong makes the forward pass
            # overflow to inf, which would make the first-token timing a lie.
            if name.endswith(".b"):
                fill, scale = 0.0, 0.0
            elif name.startswith("ln_") or ".ln_" in name:
                fill, scale = 1.0, 0.0
            else:
                fill, scale = 0.0, 0.02

            # 256K floats at a time, so a 500 MB checkpoint never needs 500 MB of RAM.
            remaining = int(np.prod(spec["shape"]))
            while remaining:
                chunk = min(remaining, 1 << 18)
                if scale:
                    values = (rng.standard_normal(chunk) * scale).astype(np.float32)
                else:
                    values = np.full(chunk, fill, dtype=np.float32)
                handle.write(values.tobytes())
                remaining -= chunk
    return {"path": path, "bytes": os.path.getsize(path), "params": sum(int(np.prod(t["shape"])) for t in tensors)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="small")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    info = write(args.path, args.preset, args.seed)
    print(f"{info['path']}: {info['params'] / 1e6:.1f}M params, {info['bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
