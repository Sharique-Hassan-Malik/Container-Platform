"""Cross-module tests — what is only true because these five are one repo.

Each module's own behaviour is tested inside its own folder. What is tested
here is composition: that the control plane really runs on the Raft store from
`modules/raft-kv`, that backend selection reports honestly, and that a module
still works when run from its own directory with nothing else installed.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ctl import backends, plane  # noqa: E402
from ctl.paths import MODULES_ROOT, MODULES  # noqa: E402

RAFT = pytest.mark.skipif(
    importlib.util.find_spec("grpc") is None, reason="grpcio not installed"
)


def _deploy(control, name: str, image: str, replicas: int, timeout: float = 30.0,
            *, complete: bool = False):
    from orchestrator import deployment_spec, pod_spec

    control.apply_deployment(name, deployment_spec(
        replicas=replicas,
        template={"labels": {"app": name}, "spec": pod_spec(image)},
        selector={"app": name},
        max_surge=1, max_unavailable=0,
    ))
    # `wait_available` is satisfied by the *old* replicas during a rolling
    # update — which is the point of a rolling update. Waiting for the rollout
    # to finish is a different question, and needs `wait_complete`.
    assert control.wait_available(name, replicas, timeout=timeout)
    if complete:
        assert control.wait_complete(name, timeout=timeout)


class TestLayout:
    def test_every_declared_module_exists(self):
        for name in MODULES:
            assert (MODULES_ROOT / name).is_dir(), f"{name} is missing"

    def test_every_module_has_a_readme_and_tests(self):
        for name in MODULES:
            folder = MODULES_ROOT / name
            assert (folder / "README.md").is_file(), f"{name} has no README"
            assert (folder / "tests").is_dir(), f"{name} ships no tests"

    def test_every_backend_names_a_module_that_exists(self):
        for backend in backends.BACKENDS:
            assert (MODULES_ROOT / backend.module).is_dir()


class TestBackendSelection:
    def test_unknown_backend_is_a_clear_error(self):
        with pytest.raises(KeyError, match="unknown store backend"):
            backends.get("store", "etcd")

    def test_every_backend_answers_the_capability_check(self):
        for backend in backends.BACKENDS:
            usable, reason = backend.check()
            assert isinstance(usable, bool)
            # An unusable backend must explain itself — that is the whole point.
            assert usable or reason, f"{backend.name} is unavailable with no reason"

    def test_auto_prefers_a_real_backend_when_one_works(self):
        for seam in ("store", "runtime"):
            chosen = backends.best(seam)
            usable, _ = chosen.check()
            assert usable
            better = [
                b for b in backends.BACKENDS
                if b.seam == seam and b.rank < chosen.rank and b.check()[0]
            ]
            assert not better, f"{seam}: {better} works but was not chosen"

    def test_explicitly_naming_a_dead_backend_raises_rather_than_downgrading(self, monkeypatch):
        monkeypatch.setitem(backends._CHECKS, "raft", lambda: (False, "pretend it is missing"))
        with pytest.raises(RuntimeError, match="unavailable"):
            backends.build_store("raft")


class TestControlPlaneOverBackends:
    def test_memory_store_and_simulated_runtime(self):
        with plane.start(store="memory", runtime="simulated", nodes=3) as cluster:
            _deploy(cluster.plane, "serve", "serve:v1", 4)
            assert len(cluster.plane.store.list("Pod")) == 4
            assert cluster.store_backend.name == "memory"

    def test_pods_are_spread_across_nodes(self):
        with plane.start(store="memory", runtime="simulated", nodes=3) as cluster:
            _deploy(cluster.plane, "serve", "serve:v1", 3)
            nodes = {
                pod.spec.get("nodeName") or pod.status.get("nodeName")
                for pod in cluster.plane.store.list("Pod")
            }
            assert len(nodes) == 3, f"expected one pod per node, got {nodes}"

    def test_rolling_update_keeps_capacity(self):
        with plane.start(store="memory", runtime="simulated", nodes=3) as cluster:
            _deploy(cluster.plane, "serve", "serve:v1", 4, complete=True)
            _deploy(cluster.plane, "serve", "serve:v2", 4, complete=True)
            images = {pod.spec["image"] for pod in cluster.plane.store.list("Pod")}
            assert images == {"serve:v2"}

    @RAFT
    def test_control_plane_runs_on_the_raft_store(self):
        """The integration this repository exists for.

        Before these were one repo, using the Raft store meant pip-installing
        another repository from GitHub.
        """
        with plane.start(store="raft", runtime="simulated", nodes=3, raft_size=3) as cluster:
            assert cluster.store_backend.name == "raft"
            assert len(cluster.members) == 3
            _deploy(cluster.plane, "serve", "serve:v1", 3, timeout=45.0)
            assert len(cluster.plane.store.list("Pod")) == 3

    @RAFT
    def test_writes_reach_every_raft_member(self):
        with plane.start(store="raft", runtime="simulated", nodes=2, raft_size=3) as cluster:
            _deploy(cluster.plane, "serve", "serve:v1", 2, timeout=45.0)
            deadline = time.time() + 15.0
            while time.time() < deadline:
                replicated = [
                    len([k for k in member.store.snapshot() if k.startswith("Pod/")])
                    for member in cluster.members
                ]
                if all(count >= 2 for count in replicated):
                    break
                time.sleep(0.2)
            else:
                pytest.fail(f"pods did not replicate to every member: {replicated}")


class TestStandalone:
    """Each module runs from its own folder — the reason for the layout."""

    HELP = {
        "container-runtime": [sys.executable, "-m", "minicon", "--help"],
        "image-toolkit": [sys.executable, "-m", "imagekit", "--help"],
        "taskqueue": [sys.executable, "tq.py", "--help"],
    }

    @pytest.mark.parametrize("module", sorted(HELP))
    def test_module_cli_runs_from_its_own_folder(self, module):
        completed = subprocess.run(
            self.HELP[module], cwd=MODULES_ROOT / module,
            capture_output=True, text=True, timeout=120,
        )
        assert completed.returncode == 0, completed.stderr

    def test_orchestrator_finds_its_siblings_without_the_platform(self):
        """Run from the orchestrator's own directory, with no ctl on the path."""
        script = (
            "import orchestrator, sys;"
            "found = orchestrator.add_siblings();"
            "print(','.join(found))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=MODULES_ROOT / "orchestrator",
            capture_output=True, text=True, timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        assert "raft-kv" in completed.stdout
        assert "container-runtime" in completed.stdout


class TestCli:
    def test_status_reports_every_backend(self, capsys):
        from ctl import cli

        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        for backend in backends.BACKENDS:
            assert backend.name in out

    def test_modules_listing_covers_the_repo(self, capsys):
        from ctl import cli

        assert cli.main(["modules"]) == 0
        out = capsys.readouterr().out
        for name in MODULES:
            assert name in out

    def test_delegation_reaches_the_module_parser(self, capsys):
        from ctl import cli

        with pytest.raises(SystemExit):
            cli.main(["image", "--help"])
        assert "imagekit" in capsys.readouterr().out
