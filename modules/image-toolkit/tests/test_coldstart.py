"""End-to-end: build an image around the real payload and time its cold start."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from imagekit import BuildCache, Builder, ImageStore, drop_page_cache, measure

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")

pytest.importorskip("numpy", reason="the payload is a numpy forward pass")

DOCKERFILE = """
FROM scratch
COPY model.bin /app/model.bin
COPY app /app
USER 10001:10001
HEALTHCHECK --interval=10s CMD ["python3", "/app/serve.py", "--help"]
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A tiny but genuine image: real checkpoint, real forward pass."""
    tmp = tmp_path_factory.mktemp("coldstart")
    context = tmp / "ctx"
    (context / "app").mkdir(parents=True)
    shutil.copy2(os.path.join(EXAMPLES, "serve.py"), context / "app" / "serve.py")
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "make_model.py"), str(context / "model.bin"), "--preset", "tiny"],
        check=True, capture_output=True,
    )
    store = ImageStore(str(tmp / "images"))
    builder = Builder(store, str(context), cache=BuildCache(str(tmp / "cache.json")))
    result = builder.build(DOCKERFILE, "payload:v1")
    return store, result, str(tmp)


def test_the_image_is_well_formed(built):
    _, result, _ = built
    assert result.findings == []
    assert result.config.user == "10001:10001"
    assert result.config.healthcheck is not None
    assert result.config.entrypoint[0] == "python3"


def test_cold_start_reports_every_phase(built):
    store, _, tmp = built
    result = measure(store, "payload:v1", os.path.join(tmp, "cs1"))
    for phase in (result.extract_s, result.spawn_s, result.load_s, result.first_token_s):
        assert phase > 0.0
    assert result.pull_bytes > 0
    assert result.layers == 2
    assert result.token != ""
    # The measured total is the sum of its parts, not an independent stopwatch.
    assert abs(result.total_s - (result.pull_s + result.extract_s + result.spawn_s
                                 + result.load_s + result.first_token_s)) < 1e-9


def test_warm_layers_are_not_pulled_again(built):
    store, _, tmp = built
    layers = [d.digest for d in store.get_manifest("payload:v1").layers]
    cold = measure(store, "payload:v1", os.path.join(tmp, "cs2"))
    warm = measure(store, "payload:v1", os.path.join(tmp, "cs3"), warm_layers=layers)
    assert warm.pull_bytes < cold.pull_bytes
    assert warm.pull_bytes_reused > 0


def test_modelled_pull_scales_with_bandwidth(built):
    store, _, tmp = built
    slow = measure(store, "payload:v1", os.path.join(tmp, "cs4"), bandwidth_mbps=10)
    fast = measure(store, "payload:v1", os.path.join(tmp, "cs5"), bandwidth_mbps=1000)
    assert slow.pull_modelled_s > fast.pull_modelled_s * 50


@pytest.fixture(scope="module")
def built_small(tmp_path_factory):
    """A 17 MB checkpoint. The tiny one loads in ~7 ms, where timing is noise."""
    tmp = tmp_path_factory.mktemp("coldstart-small")
    context = tmp / "ctx"
    (context / "app").mkdir(parents=True)
    shutil.copy2(os.path.join(EXAMPLES, "serve.py"), context / "app" / "serve.py")
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "make_model.py"), str(context / "model.bin"), "--preset", "small"],
        check=True, capture_output=True,
    )
    store = ImageStore(str(tmp / "images"))
    Builder(store, str(context), cache=BuildCache(str(tmp / "cache.json"))).build(DOCKERFILE, "payload:v1")
    return store, str(tmp)


def test_mmap_moves_cost_out_of_load(built_small):
    store, tmp = built_small
    eager = measure(store, "payload:v1", os.path.join(tmp, "cs6"), extra_args=["--load", "eager"])
    mapped = measure(store, "payload:v1", os.path.join(tmp, "cs7"), extra_args=["--load", "mmap"])
    # Both must still produce a token; only the phase charged differs. An eager
    # load reads all 17 MB before reporting 'loaded'; mmap reports immediately
    # and faults the same pages in during the forward pass.
    assert eager.token and mapped.token
    assert mapped.load_s < eager.load_s * 0.5
    assert mapped.first_token_s > mapped.load_s


def test_weights_at_startup_is_charged_as_transfer(built, tmp_path):
    store, _, tmp = built
    weights = os.path.join(tmp, "ctx", "model.bin")
    result = measure(
        store, "payload:v1", os.path.join(tmp, "cs8"),
        extra_args=["--fetch-from", weights, "--weights", "$ROOTFS/tmp/fetched.bin"],
    )
    assert result.fetch_bytes == os.path.getsize(weights)
    assert result.fetch_modelled_s > 0
    assert result.total_modelled_s > result.pull_modelled_s + result.extract_s


def test_a_payload_that_never_reports_is_an_error(built, tmp_path):
    store, _, tmp = built
    with pytest.raises(RuntimeError, match="never reported"):
        measure(store, "payload:v1", os.path.join(tmp, "cs9"), extra_args=["--weights", "/nonexistent.bin"])


def test_drop_page_cache_walks_a_tree(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a").write_bytes(b"x" * 65536)
    (root / "sub" / "b").write_bytes(b"y" * 65536)
    assert drop_page_cache(str(root)) == 2
    assert drop_page_cache(str(tmp_path / "missing")) == 0
