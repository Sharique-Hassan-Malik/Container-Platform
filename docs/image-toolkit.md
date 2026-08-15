# Architecture

## Components

```
Dockerfile ──▶ dockerfile.parse ──▶ [Stage, Stage, ...]
                                          │
                                    Builder.build
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
        COPY: hash inputs           RUN: snapshot,             metadata: mutate
        copy into staging root      execute, diff              ImageConfig only
              │                           │                           │
              └──────────┬────────────────┘                           │
                         ▼                                            │
                   layer.make_layer                                   │
              deterministic tar + gzip                                │
                         │                                            │
                    diff_id, digest                                   │
                         │                                            │
              ┌──────────┴──────────┐                                 │
              ▼                     ▼                                 ▼
       BuildCache[key]        store.put_blob                 config.diff_ids
       (chain key -> layer)   blobs/sha256/<hex>             config.history
                                     │                                │
                                     └────────────┬───────────────────┘
                                                  ▼
                                          manifest.Manifest
                                          index.json (tag -> manifest)

                        ImageStore ──copy_image──▶ ImageStore
                        (local)      only missing   (registry / node)
                                        blobs
                                          │
                                          ▼
                                    store.unpack ──▶ rootfs
                                          │
                                          ▼
                                  coldstart.measure
                          pull ▸ extract ▸ spawn ▸ load ▸ first token
```

## The two digests

Every layer is named twice and the names are not interchangeable:

| name | hashes | appears in | changes when |
|---|---|---|---|
| `diff_id` | the uncompressed tar | `config.rootfs.diff_ids` | the filesystem content changes |
| `digest` | the gzipped blob | `manifest.layers[]` | the content *or the compression* changes |

The split exists because the two consumers ask different questions. A runtime
assembling a root filesystem cares only what the files are, and must not see a
different image because someone re-compressed a blob at a different level. A
registry stores and verifies the bytes it actually transferred, so it must name
the compressed form. `make_layer` computes both in one pass; conflating them
produces a config that lists blobs no registry can serve.

## Why layers are built two different ways

`COPY` knows exactly which paths it wrote, so its layer is assembled directly
from that list. `RUN` does not, so the staging root is fingerprinted before and
after and the difference becomes the layer.

The snapshot is `(kind, mode, size, mtime_ns)` per path — deliberately not a
content hash. A build tree may hold a multi-gigabyte checkpoint that no step
touches, and hashing it around every `RUN` would dominate the build. The cost is
the blind spot every incremental build tool has: a rewrite that preserves both
size and timestamp is invisible. That is the same reason `make` occasionally
needs a `clean`.

Deletions found by the diff become `.wh.<name>` whiteout markers, because a tar
can only assert that a file exists. `extract_layer` interprets markers in a
first pass, before unpacking anything, so a layer that replaces a directory
with a file cannot delete what it just wrote.

## The cache key is a chain

```
key(step_n) = sha256( key(step_n-1) ‖ instruction_text ‖ input_hash )
key(step_0) = base manifest digest, or "scratch"
```

Folding in the parent key is what makes the cache correct rather than merely
fast. Without it, a step would hit on a base image it was never built against,
and the resulting layer would be applied to a filesystem it does not describe.

It also produces the behaviour `bench/layer_order.py` measures: a step that
*follows* a changed step misses even though its own inputs are untouched. That
is not a flaw to route around — it is why `COPY model.bin` belongs before
`COPY app/`.

`input_hash` differs by instruction on purpose:

- **COPY** hashes the content of every file it will copy, memoised on
  `(size, mtime_ns)` so a 500 MB checkpoint is read once per change, not once
  per build.
- **RUN** hashes nothing but its command string. This faithfully reproduces why
  `RUN apt-get update` serves a stale index forever. The fix is a better
  Dockerfile, and `lint()` says so rather than the builder silently guessing.

## Reproducibility is a caching feature

Three sources of nondeterminism are removed in `layer.py`:

1. **Directory order.** `os.walk` yields inode order, which differs between two
   directories holding identical files. Members are sorted by archive path.
2. **Metadata.** uid/gid become 0, user and group names become empty (a name
   forces a PAX header, whose own timestamps reintroduce the problem), mtimes
   are zeroed and modes are reduced to the executable bit.
3. **The gzip header.** It stores a timestamp entirely independently of the
   payload, so identical bytes compress to different blobs. `mtime=0` and an
   empty filename remove both variable fields.

The payoff is not aesthetic. A rebuilt-but-unchanged layer keeps its digest, so
the registry already has it and the push is free. `make_layer(deterministic=False)`
exists so the benchmark can build the other variant and show the difference:
ordering decides whether a layer is *rebuilt*, reproducibility decides whether a
rebuild is also *re-uploaded*.

## `copy_image` is both push and pull

```python
for descriptor in manifest.layers + [manifest.config]:
    if dst.has_blob(descriptor.digest):   # HEAD /v2/<name>/blobs/<digest>
        skip
    else:
        dst.put_blob(src.get_blob(...))   # POST + PUT
```

A registry has no separate download path; a pull is this function with the
stores swapped. Writing it once makes `TransferStats` the honest unit of
measurement for every caching claim in the README — the bytes a destination
accepts are exactly what a deploy costs.

## Cold start reports four phases, and two of them are modelled

```
pull ──▶ extract ──▶ spawn ──▶ load ──▶ first token
```

Aggregate "startup time" hides which phase to attack: pulling is fixed by layer
caching, extracting by compression choice, loading by file format, first token
by the model. Two measurement decisions keep the numbers from flattering the
implementation:

**The page cache is evicted between extract and load.** Everything `unpack` just
wrote is resident in RAM. Without eviction an eager load measures `memcpy` and
an mmap load takes only minor faults — and both get printed as disk numbers.
`drop_page_cache` fsyncs each file and calls `POSIX_FADV_DONTNEED`, which needs
no privileges and is the per-file half of `echo 3 > /proc/sys/vm/drop_caches`.

**Transfers are modelled, not simulated.** `pull_s` is a real local blob copy,
which is not a network fetch and must not be reported as one. `pull_modelled_s`
is measured bytes divided by a stated bandwidth, and is labelled as arithmetic
everywhere it appears. A weights-at-startup fetch is charged at the same rate,
because otherwise "keep your images small" wins by moving bytes onto a link the
benchmark pretends is free.

## What this deliberately is not

**The RUN executor is not a sandbox.** `HostExecutor` runs build commands on the
host with the staging root as the working directory. Absolute paths reach the
real filesystem. This is stated rather than hidden, and `RunExecutor` is a
Protocol so `mini-container-runtime` (#19) drops in as the isolated
implementation — which is the honest ordering, since a container runtime is a
larger project than a build tool.

**The cold-start runner does not contain the process.** It executes the image's
entrypoint on the host with the unpacked rootfs as cwd, rewriting in-image
absolute paths to their host locations (`_rebase`). The interpreter is the
host's, so the base image's own interpreter is never exercised here. Both
caveats disappear in #19, which enters the root for real and reports the same
four phases plus its own setup cost.

**Base images are built from this host.** With no Docker and no registry to pull
`python:3.12-slim` from, `examples/mkbase.py` constructs bases the way those
images are constructed underneath: copy an interpreter, close over its shared
libraries with `ldd`, and choose how much stdlib survives. The distroless
variant traces which modules the payload actually imports at runtime, because
static import analysis misses everything imported inside a function — and a
distroless image built from static analysis is one that crashes on first
request.

## Layout

| path | role |
|---|---|
| `imagekit/digest.py` | content addressing, descriptors, chain IDs, canonical JSON |
| `imagekit/layer.py` | deterministic tar and gzip, whiteouts, extraction |
| `imagekit/config.py` | image config blob, healthcheck, history |
| `imagekit/manifest.py` | manifest and index documents |
| `imagekit/store.py` | OCI layout on disk, `copy_image`, `TransferStats` |
| `imagekit/dockerfile.py` | parser: continuations, flags, stages, argv forms |
| `imagekit/build.py` | builder, cache chain, snapshots, lint |
| `imagekit/coldstart.py` | phase timing, page-cache eviction, transfer modelling |
| `imagekit/cli.py` | `build` / `inspect` / `ls` / `unpack` / `push` / `lint` |
| `examples/mkbase.py` | base root filesystems from the host interpreter |
| `examples/make_model.py` | GPT-2-shaped checkpoints, `tiny` to `gpt2-124m` |
| `examples/serve.py` | the payload: real forward pass, phase markers |
| `bench/layer_order.py` | ordering x reproducibility, 2x2 |
| `bench/coldstart.py` | six scenarios, four phases |
| `bench/base_images.py` | base sizes, the healthcheck trap, multi-stage |
