"""Wiring containers together with veth pairs.

A veth pair is two interfaces that are each other's wire. Create both in one
namespace, move one end into another, and the two namespaces have a link:

    ┌── bridge netns ──┐        ┌── container netns ──┐
    │  veth-abc-h      │◀══════▶│  eth0               │
    │  10.88.0.1/24    │        │  10.88.0.2/24       │
    └──────────────────┘        └─────────────────────┘

The honest boundary of a rootless runtime lives here. `CAP_NET_ADMIN` is held
over network namespaces **owned by a user namespace this process created**, so
containers can be wired to each other freely -- and cannot be wired to the
host's real network at all, because that namespace belongs to the initial user
namespace where an unprivileged process has no capabilities.

Rootless Docker and Podman work around this with `slirp4netns` or `pasta`: a
userspace TCP/IP stack on one end of a tun device, translating the container's
packets into ordinary sockets on the host. That is a second network stack, not
a smaller version of this one, and it is out of scope here -- so this module
does what genuinely can be done unprivileged, and says plainly what cannot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .netlink import Netlink, NetlinkError


@dataclass
class Endpoint:
    name: str
    address: str
    prefix: int = 24

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class Bridge:
    """A network namespace that other containers attach to.

    Not a Linux bridge device -- a namespace holding one veth end per member,
    each on the same subnet. That is enough for point-to-point links and keeps
    the code to netlink calls that work unprivileged. Wiring N members into a
    single broadcast domain needs a real `bridge` link, which is the natural
    next step and the same three netlink messages.
    """

    subnet: str = "10.88.0"
    prefix: int = 24
    members: dict[str, Endpoint] = field(default_factory=dict)
    _next_host: int = 1

    def allocate(self, name: str) -> tuple[Endpoint, Endpoint]:
        """Reserve an address pair for a new member."""
        if name in self.members:
            raise ValueError(f"{name} is already attached")
        host_side = Endpoint(f"veth{len(self.members)}h", f"{self.subnet}.{self._next_host}", self.prefix)
        self._next_host += 1
        container_side = Endpoint("eth0", f"{self.subnet}.{self._next_host}", self.prefix)
        self._next_host += 1
        self.members[name] = container_side
        return host_side, container_side


def connect(netlink: Netlink, host_side: Endpoint, container_side: Endpoint, netns_fd: int) -> None:
    """Create a veth pair and hand one end to the namespace behind `netns_fd`.

    Order is not negotiable: an interface loses its addresses when it moves
    between namespaces, so the far end must be configured *after* the move, by
    a netlink socket bound inside the target namespace. That is why this
    function configures only the near end.
    """
    # The peer is created with the name it will have *inside* the container, not
    # a temporary one. An interface keeps its name across a namespace move, and
    # renaming it afterwards would need a second netlink socket bound in the
    # target namespace. This works because the far end leaves immediately, so
    # the next container's `eth0` never collides with the previous one's.
    netlink.create_veth(host_side.name, container_side.name)
    netlink.move_link_to_netns(container_side.name, netns_fd)
    netlink.add_address(host_side.name, host_side.address, host_side.prefix)
    netlink.set_link_up(host_side.name)


def configure_inside(interface_from: str, endpoint: Endpoint, gateway: str | None = None) -> None:
    """Run inside the container's network namespace: rename, address, bring up."""
    with Netlink() as netlink:
        netlink.set_link_up("lo")
        netlink.add_address(interface_from, endpoint.address, endpoint.prefix)
        netlink.set_link_up(interface_from)
        if gateway:
            netlink.add_default_route(gateway)


def netns_fd(pid: int) -> int:
    return os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)


def host_networking_available() -> tuple[bool, str]:
    """Can this process attach a container to the host's real network?

    Unprivileged: no. Stated as a function rather than a comment so callers can
    branch on it and tests can assert the honest answer on this machine.
    """
    from .linux import CAP_NET_ADMIN, has_capability

    if os.geteuid() == 0:
        return True, "running as root in the initial user namespace"
    if has_capability(CAP_NET_ADMIN):
        return True, "CAP_NET_ADMIN is held"
    return False, (
        "unprivileged: CAP_NET_ADMIN is held only over network namespaces owned by a "
        "user namespace this process created. Containers can be wired to each other, "
        "but reaching the host network needs slirp4netns/pasta or real privilege."
    )
