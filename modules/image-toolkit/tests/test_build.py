"""The builder: cache chaining, COPY semantics, RUN diffs, multi-stage, lint."""

from __future__ import annotations

import os

import pytest

from imagekit import BuildCache, Builder, BuildError, ImageStore, copy_image
from imagekit.build import LARGE_LAYER_BYTES, diff_snapshots, snapshot


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "ctx"
    (root / "app").mkdir(parents=True)
    (root / "app" / "serve.py").write_text("print('serve')\n")
    (root / "app" / "handler.py").write_text("VERSION = 1\n")
    (root / "model.bin").write_bytes(b"\x01\x02\x03\x04" * 2048)
    return str(root)


@pytest.fixture
def store(tmp_path):
    return ImageStore(str(tmp_path / "images"))


def build(store, context, dockerfile, tag, cache=None, **kwargs):
    builder = Builder(store, context, cache=cache or BuildCache(os.path.join(store.root, "c.json")), **kwargs)
    return builder.build(dockerfile, tag)


SIMPLE = """
FROM scratch
COPY model.bin /app/model.bin
COPY app /app
ENV MODEL=/app/model.bin
USER 10001
ENTRYPOINT ["python3", "/app/serve.py"]
"""


# ---------------------------------------------------------------------------
# COPY semantics
# ---------------------------------------------------------------------------


def test_copy_directory_places_contents_not_the_directory(store, context, tmp_path):
    build(store, context, SIMPLE, "app:v1")
    root = str(tmp_path / "root")
    store.unpack("app:v1", root)
    # `COPY app /app` must give /app/serve.py, never /app/app/serve.py.
    assert os.path.exists(os.path.join(root, "app", "serve.py"))
    assert not os.path.exists(os.path.join(root, "app", "app"))


def test_copy_file_to_explicit_path(store, context, tmp_path):
    build(store, context, "FROM scratch\nCOPY model.bin /weights.bin\n", "f:v1")
    root = str(tmp_path / "root")
    store.unpack("f:v1", root)
    assert os.path.getsize(os.path.join(root, "weights.bin")) == 4 * 2048


def test_copy_file_into_a_directory_destination(store, context, tmp_path):
    build(store, context, "FROM scratch\nCOPY model.bin /data/\n", "f:v1")
    root = str(tmp_path / "root")
    store.unpack("f:v1", root)
    assert os.path.exists(os.path.join(root, "data", "model.bin"))


def test_missing_copy_source_fails_the_build(store, context):
    with pytest.raises(BuildError, match="source not found"):
        build(store, context, "FROM scratch\nCOPY nope.bin /x\n", "f:v1")


def test_dockerignore_excludes_files(store, context, tmp_path):
    with open(os.path.join(context, ".dockerignore"), "w") as handle:
        handle.write("app/handler.py\n")
    build(store, context, "FROM scratch\nCOPY app /app\n", "f:v1")
    root = str(tmp_path / "root")
    store.unpack("f:v1", root)
    assert os.path.exists(os.path.join(root, "app", "serve.py"))
    assert not os.path.exists(os.path.join(root, "app", "handler.py"))


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_rebuild_with_no_changes_hits_every_layer(store, context, tmp_path):
    cache_path = str(tmp_path / "cache.json")
    first = build(store, context, SIMPLE, "app:v1", cache=BuildCache(cache_path))
    second = build(store, context, SIMPLE, "app:v1", cache=BuildCache(cache_path))
    assert first.cache_hits == 0
    assert second.cache_hits == len(second.layer_steps)
    assert second.bytes_rebuilt == 0


def test_identical_builds_produce_an_identical_manifest(store, context, tmp_path):
    cache_path = str(tmp_path / "cache.json")
    a = build(store, context, SIMPLE, "app:a", cache=BuildCache(cache_path))
    b = build(store, context, SIMPLE, "app:b", cache=BuildCache(cache_path))
    assert a.manifest.serialise()[1].digest == b.manifest.serialise()[1].digest


def test_changing_a_later_copy_keeps_the_earlier_layer(store, context, tmp_path):
    cache_path = str(tmp_path / "cache.json")
    build(store, context, SIMPLE, "app:v1", cache=BuildCache(cache_path))
    with open(os.path.join(context, "app", "handler.py"), "w") as handle:
        handle.write("VERSION = 2\n")
    second = build(store, context, SIMPLE, "app:v2", cache=BuildCache(cache_path))
    hits = {step.instruction.split()[1]: step.cached for step in second.layer_steps}
    assert hits["model.bin"] is True
    assert hits["app"] is False


def test_changing_an_earlier_copy_invalidates_the_later_one(store, context, tmp_path):
    """The cache key chains, so a later step misses even with identical inputs."""
    reordered = """
FROM scratch
COPY app /app
COPY model.bin /app/model.bin
"""
    cache_path = str(tmp_path / "cache.json")
    build(store, context, reordered, "app:v1", cache=BuildCache(cache_path))
    with open(os.path.join(context, "app", "handler.py"), "w") as handle:
        handle.write("VERSION = 2\n")
    second = build(store, context, reordered, "app:v2", cache=BuildCache(cache_path))
    assert second.cache_hits == 0


def test_reproducible_rebuild_costs_nothing_to_push(store, context, tmp_path):
    """A rebuilt layer with unchanged content keeps its digest, so a push skips it."""
    reordered = "FROM scratch\nCOPY app /app\nCOPY model.bin /app/model.bin\n"
    registry = ImageStore(str(tmp_path / "registry"))
    cache_path = str(tmp_path / "cache.json")

    build(store, context, reordered, "app:v1", cache=BuildCache(cache_path))
    copy_image(store, registry, "app:v1")

    with open(os.path.join(context, "app", "handler.py"), "w") as handle:
        handle.write("VERSION = 2\n")
    second = build(store, context, reordered, "app:v2", cache=BuildCache(cache_path))
    pushed = copy_image(store, registry, "app:v2")

    model_layer = [s for s in second.layer_steps if "model.bin" in s.instruction][0]
    assert model_layer.cached is False           # it was rebuilt
    assert model_layer.layer_digest not in pushed.sent_digests   # but not re-sent


def test_nondeterministic_rebuild_does_cost_a_push(store, context, tmp_path):
    reordered = "FROM scratch\nCOPY app /app\nCOPY model.bin /app/model.bin\n"
    registry = ImageStore(str(tmp_path / "registry"))
    cache_path = str(tmp_path / "cache.json")

    build(store, context, reordered, "app:v1", cache=BuildCache(cache_path), deterministic=False)
    copy_image(store, registry, "app:v1")

    with open(os.path.join(context, "app", "handler.py"), "w") as handle:
        handle.write("VERSION = 2\n")
    import time

    time.sleep(1.05)  # gzip mtime has one-second resolution
    second = build(store, context, reordered, "app:v2", cache=BuildCache(cache_path), deterministic=False)
    pushed = copy_image(store, registry, "app:v2")

    model_layer = [s for s in second.layer_steps if "model.bin" in s.instruction][0]
    assert model_layer.layer_digest in pushed.sent_digests


def test_metadata_steps_produce_no_layer(store, context):
    result = build(store, context, SIMPLE, "app:v1")
    assert len(result.layer_steps) == 2
    assert len(result.steps) == 5


def test_corrupt_cache_file_is_survivable(store, context, tmp_path):
    cache_path = str(tmp_path / "cache.json")
    with open(cache_path, "w") as handle:
        handle.write("{not json")
    result = build(store, context, SIMPLE, "app:v1", cache=BuildCache(cache_path))
    assert len(result.manifest.layers) == 2


# ---------------------------------------------------------------------------
# RUN and snapshots
# ---------------------------------------------------------------------------


def test_run_layer_captures_created_files(store, context, tmp_path):
    dockerfile = (
        "FROM scratch\n"
        "COPY app /app\n"
        "RUN python3 -c \"open('generated.txt','w').write('x')\"\n"
    )
    build(store, context, dockerfile, "r:v1")
    root = str(tmp_path / "root")
    store.unpack("r:v1", root)
    assert os.path.exists(os.path.join(root, "generated.txt"))


def test_run_layer_records_deletions_as_whiteouts(store, context, tmp_path):
    dockerfile = (
        "FROM scratch\n"
        "COPY app /app\n"
        "RUN python3 -c \"import os; os.unlink('app/handler.py')\"\n"
    )
    build(store, context, dockerfile, "r:v1")
    root = str(tmp_path / "root")
    store.unpack("r:v1", root)
    assert os.path.exists(os.path.join(root, "app", "serve.py"))
    assert not os.path.exists(os.path.join(root, "app", "handler.py"))


def test_failing_run_fails_the_build(store, context):
    with pytest.raises(BuildError, match="exit 3"):
        build(store, context, "FROM scratch\nRUN exit 3\n", "r:v1")


def test_snapshot_diff_reports_changes_and_removals(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    (root / "keep").write_text("a")
    (root / "gone").write_text("b")
    before = snapshot(str(root))
    (root / "gone").unlink()
    (root / "new").write_text("c")
    changed, removed = diff_snapshots(before, snapshot(str(root)))
    assert "new" in changed and "gone" in removed and "keep" not in changed


# ---------------------------------------------------------------------------
# multi-stage
# ---------------------------------------------------------------------------


def test_multistage_ships_only_the_copied_artifact(store, context, tmp_path):
    dockerfile = """
FROM scratch AS builder
COPY model.bin /build/model.bin
COPY app /build

FROM scratch
COPY --from=builder /build/model.bin /app/model.bin
ENTRYPOINT ["python3", "/app/serve.py"]
"""
    result = build(store, context, dockerfile, "m:v1")
    assert result.stages_built == 2
    root = str(tmp_path / "root")
    store.unpack("m:v1", root)
    assert os.path.exists(os.path.join(root, "app", "model.bin"))
    assert not os.path.exists(os.path.join(root, "build"))
    assert not os.path.exists(os.path.join(root, "app", "serve.py"))


def test_copy_from_unknown_stage_is_an_error(store, context):
    with pytest.raises(BuildError, match="no such stage"):
        build(store, context, "FROM scratch\nCOPY --from=ghost /x /y\n", "m:v1")


def test_copy_from_stage_does_not_reach_the_host_filesystem(store, context):
    """An absolute --from path must resolve inside the stage, not on the host."""
    with pytest.raises(BuildError, match="source not found"):
        build(
            store,
            context,
            "FROM scratch AS builder\nCOPY app /build\n\nFROM scratch\nCOPY --from=builder /etc/hostname /h\n",
            "m:v1",
        )


def test_building_on_a_built_base_inherits_its_layers(store, context, tmp_path):
    build(store, context, "FROM scratch\nCOPY model.bin /app/model.bin\n", "base:v1")
    child = build(store, context, "FROM base:v1\nCOPY app /app\n", "child:v1")
    assert len(child.manifest.layers) == 2
    root = str(tmp_path / "root")
    store.unpack("child:v1", root)
    assert os.path.exists(os.path.join(root, "app", "model.bin"))
    assert os.path.exists(os.path.join(root, "app", "serve.py"))


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def test_lint_flags_root_and_missing_healthcheck(store, context):
    result = build(store, context, "FROM scratch\nCOPY app /app\nENTRYPOINT [\"python3\"]\n", "l:v1")
    joined = " ".join(result.findings)
    assert "runs as root" in joined
    assert "no HEALTHCHECK" in joined


def test_lint_flags_shell_form_entrypoint(store, context):
    result = build(store, context, "FROM scratch\nCOPY app /app\nENTRYPOINT python3 /app/serve.py\n", "l:v1")
    assert any("shell-form ENTRYPOINT" in f for f in result.findings)


def test_lint_is_quiet_on_a_well_formed_image(store, context):
    dockerfile = """
FROM scratch
COPY model.bin /app/model.bin
COPY app /app
USER 10001:10001
HEALTHCHECK CMD ["python3", "-c", "1"]
ENTRYPOINT ["python3", "/app/serve.py"]
"""
    assert build(store, context, dockerfile, "l:v1").findings == []


def test_lint_flags_a_large_layer_behind_a_small_one(store, context, tmp_path):
    with open(os.path.join(context, "big.bin"), "wb") as handle:
        handle.write(os.urandom(LARGE_LAYER_BYTES + (1 << 20)))
    dockerfile = "FROM scratch\nCOPY app /app\nCOPY big.bin /app/big.bin\nENTRYPOINT [\"x\"]\n"
    result = build(store, context, dockerfile, "l:v1")
    assert any("Copy weights before code" in f for f in result.findings)

    ordered = "FROM scratch\nCOPY big.bin /app/big.bin\nCOPY app /app\nENTRYPOINT [\"x\"]\n"
    result = build(store, context, ordered, "l:v2")
    assert not any("Copy weights before code" in f for f in result.findings)
