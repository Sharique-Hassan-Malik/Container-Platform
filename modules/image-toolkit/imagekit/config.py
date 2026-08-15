"""The image config blob: what the runtime needs in order to start the process.

This is the JSON document at `manifest.config`, and its digest is what everyone
calls the "image ID". It carries two unrelated things:

  * ``rootfs.diff_ids`` -- the ordered filesystem identity (uncompressed digests)
  * ``config``          -- the process spec: env, entrypoint, user, workdir

Healthcheck deserves a note. It is *not* in the OCI image-spec `config` object;
it is a Docker extension that the OCI spec tolerates because unknown fields are
preserved. It is written where Docker expects it, so `docker inspect` finds it,
and #19's runtime reads the same field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .digest import Descriptor, canonical_json, digest_of

MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
EPOCH = "1970-01-01T00:00:00Z"


@dataclass
class HealthCheck:
    """Docker-compatible healthcheck. Intervals are nanoseconds on the wire."""

    test: list[str]
    interval_s: float = 30.0
    timeout_s: float = 5.0
    start_period_s: float = 0.0
    retries: int = 3

    def to_json(self) -> dict[str, Any]:
        return {
            "Test": list(self.test),
            "Interval": int(self.interval_s * 1e9),
            "Timeout": int(self.timeout_s * 1e9),
            "StartPeriod": int(self.start_period_s * 1e9),
            "Retries": self.retries,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "HealthCheck":
        return cls(
            test=list(obj["Test"]),
            interval_s=obj.get("Interval", 30_000_000_000) / 1e9,
            timeout_s=obj.get("Timeout", 5_000_000_000) / 1e9,
            start_period_s=obj.get("StartPeriod", 0) / 1e9,
            retries=int(obj.get("Retries", 3)),
        )


@dataclass
class HistoryEntry:
    created_by: str
    empty_layer: bool = False
    created: str = EPOCH
    comment: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"created": self.created, "created_by": self.created_by}
        if self.empty_layer:
            out["empty_layer"] = True
        if self.comment:
            out["comment"] = self.comment
        return out


@dataclass
class ImageConfig:
    """The runnable half of an image."""

    env: dict[str, str] = field(default_factory=dict)
    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    user: str = ""
    working_dir: str = "/"
    exposed_ports: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    healthcheck: HealthCheck | None = None
    stop_signal: str = ""

    architecture: str = "amd64"
    os_name: str = "linux"
    created: str = EPOCH

    diff_ids: list[str] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)

    def copy(self) -> "ImageConfig":
        return ImageConfig(
            env=dict(self.env),
            entrypoint=list(self.entrypoint),
            cmd=list(self.cmd),
            user=self.user,
            working_dir=self.working_dir,
            exposed_ports=list(self.exposed_ports),
            labels=dict(self.labels),
            healthcheck=self.healthcheck,
            stop_signal=self.stop_signal,
            architecture=self.architecture,
            os_name=self.os_name,
            created=self.created,
            diff_ids=list(self.diff_ids),
            history=list(self.history),
        )

    def runs_as_root(self) -> bool:
        """True when nothing forced a non-root user.

        An empty ``User`` means uid 0, which is the default nobody chooses on
        purpose. `build.py` surfaces this as a lint finding rather than silently
        producing a root-by-default image.
        """
        return self.user in ("", "0", "root", "0:0", "root:root")

    def to_json(self) -> dict[str, Any]:
        # Env is a list of "K=V" on the wire, sorted so the blob is stable.
        process: dict[str, Any] = {
            "Env": [f"{k}={v}" for k, v in sorted(self.env.items())],
            "WorkingDir": self.working_dir,
        }
        if self.entrypoint:
            process["Entrypoint"] = list(self.entrypoint)
        if self.cmd:
            process["Cmd"] = list(self.cmd)
        if self.user:
            process["User"] = self.user
        if self.exposed_ports:
            process["ExposedPorts"] = {p: {} for p in sorted(self.exposed_ports)}
        if self.labels:
            process["Labels"] = dict(sorted(self.labels.items()))
        if self.healthcheck is not None:
            process["Healthcheck"] = self.healthcheck.to_json()
        if self.stop_signal:
            process["StopSignal"] = self.stop_signal

        return {
            "created": self.created,
            "architecture": self.architecture,
            "os": self.os_name,
            "config": process,
            "rootfs": {"type": "layers", "diff_ids": list(self.diff_ids)},
            "history": [h.to_json() for h in self.history],
        }

    def serialise(self) -> tuple[bytes, Descriptor]:
        blob = canonical_json(self.to_json())
        return blob, Descriptor(MEDIA_CONFIG, digest_of(blob), len(blob))

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "ImageConfig":
        process = obj.get("config", {}) or {}
        env: dict[str, str] = {}
        for item in process.get("Env", []) or []:
            key, _, value = item.partition("=")
            env[key] = value
        health = process.get("Healthcheck")
        return cls(
            env=env,
            entrypoint=list(process.get("Entrypoint", []) or []),
            cmd=list(process.get("Cmd", []) or []),
            user=process.get("User", ""),
            working_dir=process.get("WorkingDir", "/") or "/",
            exposed_ports=sorted((process.get("ExposedPorts", {}) or {}).keys()),
            labels=dict(process.get("Labels", {}) or {}),
            healthcheck=HealthCheck.from_json(health) if health else None,
            stop_signal=process.get("StopSignal", ""),
            architecture=obj.get("architecture", "amd64"),
            os_name=obj.get("os", "linux"),
            created=obj.get("created", EPOCH),
            diff_ids=list(obj.get("rootfs", {}).get("diff_ids", [])),
            history=[
                HistoryEntry(
                    created_by=h.get("created_by", ""),
                    empty_layer=bool(h.get("empty_layer", False)),
                    created=h.get("created", EPOCH),
                    comment=h.get("comment", ""),
                )
                for h in obj.get("history", [])
            ],
        )
