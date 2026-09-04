# Model Image Toolkit

> Part of the [Container Platform](../../README.md). Runs standalone from this
> folder, and composes with the other four modules through the `ctl` CLI.

OCI container images for model servers, built from scratch: deterministic tar
layers, content-addressed blobs, a Dockerfile parser with multi-stage builds, a
cache that misses for the right reasons, and cold-start measurements that
survive scrutiny.

No Docker daemon, no registry client, no container library. A layer is a
gzipped tar, a name is a SHA-256, and a manifest is JSON — the format is small
enough to implement, and implementing it is the only way to see why packaging
decisions cost what they cost.

---

## Background

A model server is an awkward thing to put in a container. Ordinary application
images are tens of megabytes of code that changes hourly. A model image is
hundreds of megabytes to tens of gigabytes of weights that change weekly,
wrapped around a few kilobytes of handler code that changes hourly. Every
default in the packaging toolchain is tuned for the first case.

That mismatch produces four questions with non-obvious answers:

- **Which order should layers go in?** Everyone repeats "put the stuff that
  changes least first". Almost nobody can say what the alternative actually
  costs, or in which currency.
- **Should weights live in the image at all?** "Keep images small, fetch the
  model from object storage at startup" is common advice. It is sometimes
  right, and the condition is measurable.
- **Does compressing weights help?** gzip is the default for every layer.
  Trained float32 tensors are close to incompressible.
- **What is a cold start made of?** "Startup takes 8 seconds" is not actionable.
  Pull, extract, load and first-token are fixed by four unrelated changes.

This project answers all four by building the images and timing them.

---

## The headline result

Two Dockerfiles, identical files, identical unpacked filesystem. The only
difference is whether `COPY model.bin` comes before or after `COPY app/`. Then
one line of application code changes and the image is rebuilt and pushed to a
registry that already holds the previous version.

65 MB checkpoint, `slim` base:

| order | reproducible | cache hits | rebuilt | build time | re-pushed |
|---|---|---:|---:|---:|---:|
| weights first | yes | 1/2 | 3.0 KB | **2.33 s** | **4.0 KB** |
| weights first | no | 1/2 | 3.0 KB | 2.46 s | 4.0 KB |
| weights last | yes | 0/2 | 60.2 MB | 6.08 s | 4.0 KB |
| weights last | no | 0/2 | 60.2 MB | 6.42 s | **60.2 MB** |

`python3 bench/layer_order.py --preset medium`

The two knobs turn out to control **different costs**, which is why running all
four cells is worth more than running the two anyone expects:

- **Ordering decides whether the weights layer is rebuilt.** Cache keys chain
  forward, so a `COPY` that follows a changed `COPY` misses even though its own
  inputs are untouched. That is 3.75 s of CPU per build, every build.
- **Reproducibility decides whether a rebuild is also re-uploaded.** A rebuilt
  layer whose content is unchanged keeps its digest, so the registry already
  has it and the push is free. Remove determinism — let tar record real mtimes
  and gzip stamp the wall clock — and the same rebuild re-sends 60.2 MB.

Fixing only one of them fixes half the problem. The common advice covers the
first and is silent on the second, which is the more expensive one.

At real scale the same run is no longer a rounding error. 474.7 MB checkpoint —
the fp32 size of GPT-2 small:

| order | reproducible | rebuilt | build time | re-pushed |
|---|---|---:|---:|---:|
| weights first | yes | 3.0 KB | **9.18 s** | **4.0 KB** |
| weights last | yes | 439.9 MB | 34.72 s | 4.0 KB |
| weights last | no | 439.9 MB | 35.43 s | **439.9 MB** |

`python3 bench/layer_order.py --preset gpt2-124m`

One line of Python costs either 9 seconds, or 35 seconds, or 35 seconds plus
440 MB over the wire — decided entirely by two lines of Dockerfile and one
`mtime=0`.

---

## Cold start, phase by phase

Six scenarios, one stopwatch. Page cache evicted before every run; transfers
charged at 200 Mbps wherever bytes move, including the weights-at-startup fetch.

| scenario | image | moved | transfer | extract | spawn | load | first tok | **total** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in image, cold node | 95.3 MB | 95.3 MB | 3995 | 2127 | 213 | 157 | 286 | **6779** |
| in image, base cached | 95.3 MB | 60.2 MB | 2524 | 2058 | 189 | 165 | 306 | **5243** |
| in image, all cached | 95.3 MB | 1.5 KB | 0 | 2108 | 177 | 176 | 272 | **2733** |
| in image, gzip level 0 | 100.1 MB | 1.5 KB | 0 | 1743 | 167 | 160 | 362 | **2431** |
| in image, mmap load | 95.3 MB | 60.2 MB | 2524 | 2114 | 200 | 19 | 415 | **5272** |
| at startup, any start | 35.1 MB | 65.0 MB | 2726 | 1197 | 180 | 231 | 246 | **4581** |

All times in ms. `python3 bench/coldstart.py --preset medium`

**Weights at startup wins the first start and loses every one after it.** On a
node holding only the base layer, fetching the checkpoint at startup is 4581 ms
against 5243 ms for weights-in-image — the small image skips gzip on 65 MB of
incompressible data. But in-image weights are a *layer*: the next container on
that node moves 1.5 KB and starts in 2733 ms, while the at-startup path
re-downloads 65 MB on every start, including every scale-from-zero. Break-even
is two starts per node. Object storage wins only when the model changes far
more often than containers start.

**Compressing weights is close to pure loss.** Comparing the two fully-cached
rows, where extract is the only phase that differs: gzip level 6 yields a
95.3 MB image extracting in 2108 ms; level 0 yields 100.1 MB extracting in
1743 ms. Deflate buys 4.8 MB — 5% of the image — and charges 365 ms of CPU for
it on *every container start*. The default compression level is tuned for
source code.

**mmap does not make loading faster, it makes it a different phase.** Load
drops 165 ms → 19 ms and first token rises 306 ms → 415 ms, for a total that is
slightly worse. A dashboard tracking "model load time" would record a 9x
improvement no user experiences. mmap pays when the process does not touch
every weight; the first forward pass touches all of them.

One honest caveat: the "all cached" row still pays 2108 ms of extract, because
`unpack` always re-materialises the rootfs. A real runtime keeps the unpacked
snapshot and a genuine container *restart* skips that phase entirely. The row
models a fresh container from fully cached layers, not a restart.

---

## Base images, and the healthcheck that never runs

There is no Docker here and no registry to pull `python:3.12-slim` from, so the
three bases are constructed from this host's own interpreter the way those
images are constructed underneath: copy the binary, close over its shared
libraries with `ldd`, decide how much standard library survives. Each is
smoke-tested by running numpy out of it before it becomes an image.

| base | unpacked | compressed | files | `/bin/sh` |
|---|---:|---:|---:|:---:|
| full | 155.2 MB | 47.0 MB | 1604 | yes |
| slim | 111.0 MB | 35.1 MB | 1023 | yes |
| distroless | 104.9 MB | 33.5 MB | 692 | **no** |

The same model server on each: 62.0 MB, 50.0 MB (−19%), 48.5 MB (−22%). The
distroless saving over slim is small because numpy is 40 MB and no amount of
stdlib trimming touches it — worth knowing before treating distroless as a size
strategy.

The reason to care is the other column:

| base | healthcheck form | verdict |
|---|---|---|
| full | exec | runs |
| full | shell | runs |
| slim | exec | runs |
| slim | shell | runs |
| distroless | exec | runs |
| distroless | **shell** | **never runs** — no `/bin/sh` in the image |

`python3 bench/base_images.py`

A shell-form `HEALTHCHECK CMD curl ...` is rewritten by the runtime to
`/bin/sh -c "..."`. Distroless images have no shell. The image builds, the
manifest validates, the container starts and serves traffic — and the
healthcheck can never succeed, so an orchestrator kills the pod on a loop while
the application logs nothing at all. Write healthchecks in exec form.

Multi-stage does what it promises: building on the full base and shipping on
distroless leaves 13.5 MB of build tooling behind, and the shipped manifest
references no builder layer except the one artifact copied across.

---

## Usage

```bash
python3 -m imagekit build -t serve:v1 --store ./images .   # build from ./Dockerfile
python3 -m imagekit inspect serve:v1 --store ./images      # manifest + config JSON
python3 -m imagekit lint serve:v1 --store ./images         # packaging findings
python3 -m imagekit push serve:v1 --store ./images --to ./registry
python3 -m imagekit unpack serve:v1 ./rootfs --store ./images
```

As a library:

```python
from imagekit import ImageStore, Builder, copy_image, measure

store = ImageStore("./images")
result = Builder(store, context=".").build(open("Dockerfile").read(), "serve:v1")
print(result.summary())

stats = copy_image(store, ImageStore("./registry"), "serve:v1")
print(stats)                                    # 2 blobs / 60.2 MB transferred, ...

cold = measure(store, "serve:v1", "./work")
print(cold.table())                             # pull / extract / spawn / load / first token
```

A `Dockerfile` this builder understands:

```dockerfile
FROM python-slim:base
WORKDIR /app
COPY model.bin /app/model.bin          # big and stable: first
COPY app /app                          # small and volatile: last
USER 10001:10001
HEALTHCHECK --interval=10s CMD ["python3", "/app/healthz.py"]
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
```

`FROM`, `COPY` (with `--from` and `--chown`), `RUN`, `ENV`, `ARG`, `WORKDIR`,
`USER`, `ENTRYPOINT`, `CMD`, `EXPOSE`, `LABEL`, `HEALTHCHECK`, `STOPSIGNAL`,
line continuations, comments, and multi-stage builds. `ADD` is deliberately
absent: a `COPY` that also fetches URLs and auto-extracts tarballs is two
behaviours nobody wants implicitly.

## The lint pass

Every check is something that costs real minutes on a real rollout:

```
image runs as root: no USER instruction (a compromised handler owns the container)
no HEALTHCHECK: an orchestrator cannot tell 'process alive' from 'model loaded'
shell-form ENTRYPOINT: PID 1 is /bin/sh, which does not forward SIGTERM --
  every shutdown becomes a 10s SIGKILL timeout
layer 2 (60.2 MB) sits behind smaller layer(s) [1]: editing any of those
  invalidates this one too, re-pushing 60.2 MB for a source-code change.
  Copy weights before code.
```

`build --strict` exits non-zero on any finding.

## Running it

```bash
python3 -m pytest tests/ -q            # 91 tests, ~11 s, stdlib + numpy only
python3 bench/layer_order.py --preset medium
python3 bench/coldstart.py  --preset medium
python3 bench/base_images.py
```

Benchmarks cache their fixtures under `.bench/`; `--fresh` discards them.
`--preset gpt2-124m` reproduces every measurement against a 497 MB checkpoint,
the fp32 size of real GPT-2 small.

Requires Python 3.10+ and numpy (the payload runs a genuine transformer forward
pass, which is what makes "first token" a real phase). `imagekit` itself is
standard library only.

## What this is not

The `RUN` executor runs build commands on the host with the staging root as its
working directory — it is **not a sandbox**, and says so. The cold-start runner
executes the entrypoint on the host against the unpacked rootfs rather than
inside it, using the host interpreter. Both are stated in the code and in
`ARCHITECTURE.md` rather than papered over, and both are exactly what
`container-runtime` fixes: it provides namespaces, cgroups and a real
`pivot_root`, plugs into `RunExecutor`, and reports the same four cold-start
phases plus its own setup cost.

Weights in the example checkpoints are randomly initialised. Cold start measures
bytes, page faults and process startup; a trained checkpoint of the same shape
produces the same timings and a larger download.

## License

MIT
