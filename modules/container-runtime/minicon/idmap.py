"""User-namespace ID mapping -- and the reason rootless containers are awkward.

A user namespace is a translation table between IDs inside it and IDs outside.
Writing that table is governed by one rule that decides everything about what a
rootless runtime can do:

  * With `CAP_SETUID` in the *parent* namespace you may write any mapping.
  * Without it you may write **exactly one line**, and it must map your own
    effective UID.

One line means one UID. A container can therefore be root, or it can be uid
10001, but it cannot be root that later drops to uid 10001 -- there is no second
mapping for the second identity to land in. Every unprivileged container runtime
hits this, and the standard escape is the setuid helper `newuidmap`, which reads
the administrator's `/etc/subuid` grant and installs a whole range on your
behalf.

Three strategies, in increasing order of what they need from the host:

    ROOT     0 -> your uid            no privileges; container is root
    SINGLE   N -> your uid            no privileges; container is exactly uid N
    SUBID    0..65535 -> subuid range needs newuidmap(1) from the uidmap package

`plan_for_user()` picks one from an image's `USER` directive and explains, in
the failure message, precisely which of the three was unavailable and why.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdRange:
    """One line of a uid_map/gid_map file: `<inside> <outside> <count>`."""

    inside: int
    outside: int
    count: int

    def render(self) -> str:
        return f"{self.inside} {self.outside} {self.count}"


@dataclass
class IdMapping:
    uids: list[IdRange]
    gids: list[IdRange]
    strategy: str = "root"

    @property
    def needs_helper(self) -> bool:
        """More than one line, or a line not naming our own ID, needs newuidmap."""
        return len(self.uids) > 1 or len(self.gids) > 1

    def container_root_uid(self) -> int:
        return self.uids[0].inside

    def maps(self, inside_uid: int) -> bool:
        return any(r.inside <= inside_uid < r.inside + r.count for r in self.uids)

    def describe(self) -> str:
        return (
            f"{self.strategy}: uid {', '.join(r.render() for r in self.uids)} | "
            f"gid {', '.join(r.render() for r in self.gids)}"
        )


def root_mapping(uid: int | None = None, gid: int | None = None) -> IdMapping:
    """Container uid 0 <- your uid. The only mapping available with no helper."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return IdMapping([IdRange(0, uid, 1)], [IdRange(0, gid, 1)], "root")


def single_mapping(inside_uid: int, inside_gid: int, uid: int | None = None, gid: int | None = None) -> IdMapping:
    """Container uid N <- your uid, for one specific N.

    Still one line, so still allowed without a helper -- the kernel requires the
    *outside* ID to be yours, and says nothing about the inside one. This is how
    an image with `USER 10001` can run rootless, at the cost of uid 0 not
    existing inside the container at all.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return IdMapping([IdRange(inside_uid, uid, 1)], [IdRange(inside_gid, gid, 1)], "single")


def subid_mapping(username: str | None = None, size: int = 65536) -> IdMapping:
    """A full range from /etc/subuid, installed by newuidmap.

    This is what gives a rootless container both a uid 0 and a working `USER`.
    It needs the `uidmap` package, because installing more than one line
    requires CAP_SETUID that an unprivileged process does not have.
    """
    username = username or _username()
    uid_start, uid_count = _read_subid("/etc/subuid", username)
    gid_start, gid_count = _read_subid("/etc/subgid", username)
    count = min(size, uid_count, gid_count)
    return IdMapping(
        # Line 1 maps container root to us; line 2 maps everything above it into
        # the delegated range, which is the pair that makes USER work.
        uids=[IdRange(0, os.getuid(), 1), IdRange(1, uid_start, count - 1)],
        gids=[IdRange(0, os.getgid(), 1), IdRange(1, gid_start, count - 1)],
        strategy="subid",
    )


def helper_available() -> bool:
    return bool(shutil.which("newuidmap") and shutil.which("newgidmap"))


def subid_available(username: str | None = None) -> bool:
    try:
        _read_subid("/etc/subuid", username or _username())
        _read_subid("/etc/subgid", username or _username())
    except (OSError, LookupError):
        return False
    return True


def plan_for_user(user: str, *, allow_helper: bool = True) -> IdMapping:
    """Choose a mapping for an image's `USER` directive, or explain why not.

    `USER 10001:10001` with no helper installed is the interesting case: SUBID
    is unavailable, ROOT would silently run the workload as root -- which is the
    exact thing the directive asked not to happen -- so SINGLE is chosen, and
    the caller is told that uid 0 will not exist inside.
    """
    inside_uid, inside_gid = parse_user(user)
    if inside_uid == 0:
        return root_mapping()
    if allow_helper and helper_available() and subid_available():
        return subid_mapping()
    return single_mapping(inside_uid, inside_gid)


def parse_user(user: str) -> tuple[int, int]:
    """`"10001:10002"` / `"10001"` / `""` -> (uid, gid). Names are not resolved.

    Resolving a name would mean reading the *image's* /etc/passwd, which does
    not exist yet at the point the mapping has to be decided.
    """
    text = (user or "0").strip()
    if not text or text in ("root", "root:root"):
        return 0, 0
    uid_text, _, gid_text = text.partition(":")
    try:
        uid = int(uid_text)
    except ValueError as exc:
        raise ValueError(f"USER {user!r}: names cannot be resolved before the rootfs exists") from exc
    gid = int(gid_text) if gid_text else uid
    return uid, gid


# ---------------------------------------------------------------------------
# writing the maps
# ---------------------------------------------------------------------------


def write_maps(pid: int, mapping: IdMapping) -> None:
    """Install the mapping on a process that has already unshared CLONE_NEWUSER.

    `setgroups` must be denied before `gid_map` can be written without
    privilege. The reason is a genuine escalation: a process that could drop
    groups inside a namespace could shed a group used for *negative*
    permissions -- a file mode of `rwx---rwx` denies the group and allows
    everyone else -- and gain access it did not have.
    """
    if mapping.needs_helper:
        _write_maps_via_helper(pid, mapping)
        return
    try:
        _write(f"/proc/{pid}/setgroups", "deny")
    except OSError:
        # Absent on kernels before 3.19, where the escalation does not exist.
        pass
    _write(f"/proc/{pid}/uid_map", "\n".join(r.render() for r in mapping.uids) + "\n")
    _write(f"/proc/{pid}/gid_map", "\n".join(r.render() for r in mapping.gids) + "\n")


def _write_maps_via_helper(pid: int, mapping: IdMapping) -> None:
    if not helper_available():
        raise RuntimeError(
            f"mapping strategy {mapping.strategy!r} needs {len(mapping.uids)} uid ranges, but installing "
            "more than one requires CAP_SETUID or the newuidmap/newgidmap helpers, "
            "which are not installed (Debian/Ubuntu package: uidmap). "
            "Use strategy 'root' or 'single' instead."
        )
    for command, ranges in (("newuidmap", mapping.uids), ("newgidmap", mapping.gids)):
        argv = [command, str(pid)]
        for entry in ranges:
            argv += [str(entry.inside), str(entry.outside), str(entry.count)]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr.strip()}")


def helper_argv(command: str, pid: int, ranges: list[IdRange]) -> list[str]:
    """The exact argv `_write_maps_via_helper` would run. Exposed for testing."""
    argv = [command, str(pid)]
    for entry in ranges:
        argv += [str(entry.inside), str(entry.outside), str(entry.count)]
    return argv


def _write(path: str, data: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def _read_subid(path: str, username: str) -> tuple[int, int]:
    uid = str(os.getuid())
    with open(path) as handle:
        for line in handle:
            parts = line.strip().split(":")
            if len(parts) == 3 and parts[0] in (username, uid):
                return int(parts[1]), int(parts[2])
    raise LookupError(f"no entry for {username!r} in {path}")


def _username() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name
