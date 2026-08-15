#!/usr/bin/env python3
"""The payload: a GPT-2-shaped transformer that loads real weights and emits a token.

This exists so `pull -> extract -> load -> first token` measures something. The
arithmetic is a genuine forward pass in numpy over a genuine on-disk checkpoint;
only the weight *values* are random, because cold start is a question about
bytes, page faults and process startup, and those do not care what the weights
mean.

Two loading strategies are exposed because they move cost between phases rather
than removing it:

    --load eager   read the file into arrays. All I/O lands in `load`.
    --load mmap    map it and let the first forward pass fault pages in.
                   `load` looks instant; the cost reappears in `first token`.

Reporting only one of them is how a cold-start graph gets to lie.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import time

import numpy as np

MAGIC = b"MMGPT001"
ALIGN = 4096
EVENT_PREFIX = "@@IMAGEKIT@@ "


def emit(event: str, **fields) -> None:
    """Phase marker read by imagekit.coldstart (and by #19's runtime)."""
    sys.stdout.write(EVENT_PREFIX + json.dumps({"event": event, "t": time.time(), **fields}) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Checkpoint format
# ---------------------------------------------------------------------------


def read_header(path: str) -> tuple[dict, int]:
    with open(path, "rb") as handle:
        magic = handle.read(8)
        if magic != MAGIC:
            raise ValueError(f"{path}: not a checkpoint (magic {magic!r})")
        (header_len,) = struct.unpack("<I", handle.read(4))
        header = json.loads(handle.read(header_len))
    # Tensor data starts on a page boundary so mmap hands numpy the kernel's
    # own pages instead of forcing a misaligned copy.
    data_offset = ALIGN * ((12 + header_len + ALIGN - 1) // ALIGN)
    return header, data_offset


def load_tensors(path: str, mode: str) -> dict[str, np.ndarray]:
    header, data_offset = read_header(path)
    tensors: dict[str, np.ndarray] = {}
    if mode == "mmap":
        for spec in header["tensors"]:
            tensors[spec["name"]] = np.memmap(
                path,
                dtype=np.float32,
                mode="r",
                offset=data_offset + spec["offset"],
                shape=tuple(spec["shape"]),
            )
        return tensors
    with open(path, "rb") as handle:
        handle.seek(data_offset)
        blob = handle.read()
    for spec in header["tensors"]:
        count = int(np.prod(spec["shape"]))
        start = spec["offset"]
        arr = np.frombuffer(blob, dtype=np.float32, count=count, offset=start)
        tensors[spec["name"]] = arr.reshape(tuple(spec["shape"]))
    return tensors


# ---------------------------------------------------------------------------
# Forward pass
# ---------------------------------------------------------------------------


def layer_norm(x: np.ndarray, weight: np.ndarray, bias: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    mean = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    return (x - mean) / np.sqrt(var + eps) * weight + bias


def gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + np.tanh(0.7978845608 * (x + 0.044715 * x**3)))


def softmax(x: np.ndarray) -> np.ndarray:
    shifted = x - x.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def forward(tensors: dict[str, np.ndarray], config: dict, ids: list[int]) -> np.ndarray:
    n_layer, n_head, n_embd = config["n_layer"], config["n_head"], config["n_embd"]
    head_dim = n_embd // n_head
    seq = len(ids)

    x = tensors["wte"][ids] + tensors["wpe"][:seq]
    causal = np.triu(np.full((seq, seq), -1e9, dtype=np.float32), k=1)

    for i in range(n_layer):
        p = f"h.{i}."
        h = layer_norm(x, tensors[p + "ln_1.w"], tensors[p + "ln_1.b"])
        qkv = h @ tensors[p + "attn.qkv.w"] + tensors[p + "attn.qkv.b"]
        q, k, v = np.split(qkv, 3, axis=-1)
        q = q.reshape(seq, n_head, head_dim).transpose(1, 0, 2)
        k = k.reshape(seq, n_head, head_dim).transpose(1, 0, 2)
        v = v.reshape(seq, n_head, head_dim).transpose(1, 0, 2)
        scores = (q @ k.transpose(0, 2, 1)) / np.sqrt(head_dim) + causal
        attended = (softmax(scores) @ v).transpose(1, 0, 2).reshape(seq, n_embd)
        x = x + attended @ tensors[p + "attn.proj.w"] + tensors[p + "attn.proj.b"]

        h = layer_norm(x, tensors[p + "ln_2.w"], tensors[p + "ln_2.b"])
        h = gelu(h @ tensors[p + "mlp.fc.w"] + tensors[p + "mlp.fc.b"])
        x = x + h @ tensors[p + "mlp.proj.w"] + tensors[p + "mlp.proj.b"]

    x = layer_norm(x, tensors["ln_f.w"], tensors["ln_f.b"])
    return x[-1] @ tensors["wte"].T  # weight tying: lm_head is wte transposed


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", default="model.bin", help="path inside the rootfs")
    parser.add_argument(
        "--fetch-from",
        default=None,
        help="weights-at-startup: copy the checkpoint from here before loading, "
        "standing in for an S3/GCS download at container start",
    )
    parser.add_argument("--load", choices=["eager", "mmap"], default="eager")
    parser.add_argument("--prompt", default="the model server is")
    parser.add_argument("--serve", action="store_true", help="stay up after the first token")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    emit("started", pid=os.getpid())

    weights = args.weights
    fetched_bytes = 0
    if args.fetch_from:
        # Deliberately inside the `load` phase: with weights-at-startup, the
        # download is not part of the image pull, it is part of every single
        # container start -- including the ones that scale-to-zero causes.
        os.makedirs(os.path.dirname(os.path.abspath(weights)) or ".", exist_ok=True)
        shutil.copyfile(args.fetch_from, weights)
        fetched_bytes = os.path.getsize(weights)

    header, _ = read_header(weights)
    tensors = load_tensors(weights, args.load)
    emit("loaded", bytes=os.path.getsize(weights), mode=args.load, fetched=fetched_bytes)

    ids = list(args.prompt.encode()[: header["block"]]) or [0]
    logits = forward(tensors, header, ids)
    token = int(np.argmax(logits))
    emit("first_token", token=token, logit=float(logits[token]))

    if args.serve:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                healthy = self.path in ("/healthz", "/health", "/readyz")
                self.send_response(200 if healthy else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": healthy, "model": header["name"]}).encode())

            def log_message(self, *_):
                pass

        emit("serving", port=args.port)
        HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
