# Architecture

## The three processes

`unshare(CLONE_NEWPID)` does not move the caller into the new PID namespace —
only its children go there. So the process that becomes PID 1 must be a
*grandchild* of the runtime, and the whole shape follows from that one rule.

```
runtime (P)                 child (C)                    init (G)
────────────────────────────────────────────────────────────────────────────
resolve image
unpack layers → cache
create cgroup
fork ────────────────────▶  join cgroup leaf
                            unshare(user|mnt|pid|net|uts|ipc|cgroup)
               ◀── "R" ─────┤
write uid_map / gid_map
├── "G" ───────────────────▶ (now uid 0 in its own user namespace)
                            fork ──────────────────────▶ PID 1
                                                         mount overlay
                                                         write /etc/{hosts,hostname}
                                                         mount /proc /dev /sys /tmp
                                                         pivot_root, detach old root
                                                         sethostname, lo up
                            ◀────── {"overlay_s": ...} ──┤  readiness + timings
                            relay signals ──────────────▶ exec entrypoint
                            waitpid(G) ◀────────────────
waitpid(C) ◀── exit status ─┤
```

**Why C exists.** Purely because of the PID-namespace rule — but it earns its
keep. C sits outside the container's PID namespace and inside its user
namespace, which makes it the only process that can both observe PID 1 exiting
and be signalled by the runtime. It must *relay* signals rather than act on
them: dying itself would orphan PID 1, leaving the container running with
nothing supervising it while `kill()` reported success.

**Why P writes the maps.** An unprivileged process may write exactly one
mapping line, and it must name its own UID. After `unshare(CLONE_NEWUSER)` C's
UID has already become `nobody`, so it can no longer name it. P still can. This
is also why the "R"/"G" handshake exists at all: C must wait, because between
the unshare and the map write it has no privileges in its own namespace.

**Why PID 1 reports its own timings.** `overlay_s`, `mounts_s` and `pivot_s`
happen inside namespaces the parent cannot observe. They come back as JSON over
the readiness pipe and are recorded as a *breakdown of* `setup_s`, not as
additions to it.

## The root filesystem is an overlay, not a copy

```
merged/   ← what the container sees, after pivot_root
  upper/  ← this container's writes, deleted with it
  lower/  ← the image's layers, read-only, shared by every container
  work/   ← overlayfs scratch (see the cleanup note below)
```

OCI lists layers bottom-first; overlayfs takes them **top-first**. Reversing
the list is a one-line detail with a silent failure mode — get it wrong and the
base image wins every conflict with the application layer, which looks like a
stale build rather than a bug.

Layers are unpacked once into `<workspace>/layers/<digest>/`, keyed by digest
rather than by image, so two images sharing a base share the unpacked bytes on
disk. Each container then adds one empty `upper/`. That is the entire mechanism
behind the density result.

### Whiteout translation

OCI and overlayfs both express "deleted in this layer" and disagree about how:

| OCI tar | overlayfs |
|---|---|
| `.wh.<name>` | character device 0:0 named `<name>` |
| `.wh..wh..opq` | xattr `user.overlay.opaque="y"` on the directory |

Creating the device node needs `CAP_MKNOD`, which an unprivileged process holds
*inside a user namespace it created*. So unpacking runs in a short-lived
namespace helper (`run_in_userns`), the same trick `podman unshare` exposes.
Nodes made this way cannot be opened, which does not matter — an overlayfs
whiteout is a marker the filesystem inspects, never a device anyone reads.

The `user.` xattr prefix rather than `trusted.` goes with mounting the overlay
`userxattr`; the trusted namespace needs real `CAP_SYS_ADMIN`.

### The cleanup trap

overlayfs creates `workdir/work` itself, with kernel credentials: root:root,
mode 000. The unprivileged process that created the parent directory cannot
remove it. `OverlayRoot.cleanup()` therefore re-enters a user namespace to
delete it — the same helper used for unpacking. Skipping this leaks a directory
per container, which is exactly the kind of thing that is invisible until a
node runs out of inodes.

## `pivot_root`, not `chroot`

`chroot` changes one process's idea of a path. A chrooted process still holding
a descriptor to a directory outside the jail can walk back out. `pivot_root`
changes the mount tree, so the old root becomes a mount that can be detached
outright — and then it is genuinely gone.

The sequence is fixed by the kernel's preconditions, each of which returns
`EINVAL` if skipped:

1. `new_root` must be a *mount point* → bind-mount it onto itself.
2. `put_old` must live inside `new_root`.
3. After pivoting, `chdir("/")` or the process keeps a cwd on the old root.
4. Detach the old root lazily (`MNT_DETACH`) and remove the directory. Until
   this happens the host filesystem is still reachable at `/.oldroot` and the
   container is not isolated at all.

Two ordering constraints fall out of this and are easy to get backwards:

- **`MS_REC | MS_PRIVATE` on `/` first.** systemd mounts `/` shared, so a fresh
  mount namespace still forwards every mount and unmount to its parent. Without
  this the container's `/proc` appears on the host and the container's unmounts
  take the host's mounts with them.
- **Read-only root comes *after* the pivot.** Sealing `/` first makes
  `pivot_root` fail: it has to `mkdir` the directory the old root is parked in,
  and that `mkdir` lands on the root being sealed. `remount_root_readonly()` is
  a separate call for exactly this reason.

`/proc` is the other non-obvious dependency: mounting it requires being in a
**PID** namespace, not a mount namespace. Without one the kernel refuses, and
without `/proc` almost nothing in userspace runs — which makes PID isolation
effectively mandatory rather than optional.

## cgroup v2

Namespaces control what a process can *see*; cgroups control what it can
*consume*. They are independent subsystems, and conflating them is how people
end up with "containers" that OOM the host.

Two v2 rules shape the code:

**No internal processes.** A cgroup may hold processes or distribute
controllers to children, never both. Hence `<name>/` for policy and
`<name>/leaf/` for processes.

**Controllers are handed down explicitly.** A child sees only what its parent
wrote into `cgroup.subtree_control`, and a parent can only hand down what it
was given. Delegation is a chain from the root, which is why an unprivileged
runtime works inside `user@<uid>.service` — systemd delegates that subtree —
and nowhere else. `delegated_root()` prefers it precisely because the caller's
own cgroup is often missing the `cpu` controller, and a CPU limit that silently
does nothing is worse than one that reports it could not be set.

Process migration needs write access to the destination *and* to the common
ancestor of source and destination. Running under `user@<uid>.service`
satisfies both; running from a login session scope does not, and fails with
`EACCES` for a reason no error message explains.

`add_process` runs **before** `unshare`, so that `CLONE_NEWCGROUP` makes PID 1
see its own cgroup as `/` rather than the host's full path.

## rtnetlink from scratch

```
struct nlmsghdr  { u32 len; u16 type; u16 flags; u32 seq; u32 pid; }
struct ifinfomsg { u8 family; u8 pad; u16 type; i32 index; u32 flags; u32 change; }
struct rtattr    { u16 len; u16 type; }  payload, padded to 4 bytes
```

The alignment is the usual source of `EINVAL`: an attribute's declared length
*excludes* padding while its position in the buffer *includes* it.

Creating a veth pair nests three deep — "make a link of kind veth whose
driver-specific data contains a second, complete link":

```
IFLA_LINKINFO
  IFLA_INFO_KIND = "veth"
  IFLA_INFO_DATA
    VETH_INFO_PEER
      ifinfomsg (empty)
      IFLA_IFNAME = <peer>
```

A `Netlink` socket is bound to whatever network namespace existed at
construction, which is the entire mechanism by which a container configures its
own interfaces: construct one *inside* the target namespace.

An interface loses its addresses when it moves between namespaces, so the far
end is created already carrying the name it will have inside the container
(`eth0`) and addressed afterwards by a socket bound in that namespace. Renaming
after the move would need a second socket there anyway.

## Layout

| path | role |
|---|---|
| `minicon/linux.py` | the six syscalls, mount flags, support detection |
| `minicon/idmap.py` | uid/gid mapping strategies and the one-line rule |
| `minicon/mounts.py` | overlay root, pseudo-filesystems, `pivot_root` |
| `minicon/cgroups.py` | v2 delegation, limits, usage, `cgroup.kill` |
| `minicon/netlink.py` | rtnetlink: links, veth, addresses, routes |
| `minicon/network.py` | veth wiring, and the honest privilege boundary |
| `minicon/image.py` | OCI layout reader, whiteout translation, layer cache |
| `minicon/container.py` | the three-process lifecycle, PID 1, phases |
| `minicon/cli.py` | `run` / `images` / `inspect` / `ps` / `check` |
| `bench/startup.py` | phase breakdown, warm start, density |

## What this is not

- **No seccomp or LSM confinement.** A container here has full syscall access
  as its mapped user. Real runtimes ship a seccomp profile blocking ~40
  syscalls; that is a separate, largely orthogonal piece of work.
- **No host networking.** Structurally impossible unprivileged; see the README.
- **No `exec` into a running container.** `setns` into another process's user
  namespace has its own permission rules, and implementing it half-correctly
  would be worse than not having it.
- **No image pull.** #18 produces the layouts; this consumes them from disk.
