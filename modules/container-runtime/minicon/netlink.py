"""A minimal rtnetlink client -- enough to build a container network.

`ip link add ... type veth` is a shell out to a program that opens an
`AF_NETLINK` socket and sends a structured message. Doing it directly removes
the dependency and, more usefully, makes the shape of the interface visible:
rtnetlink is a request/ack protocol over a socket, with every message a fixed
header followed by type-length-value attributes that nest.

    struct nlmsghdr   { u32 len; u16 type; u16 flags; u32 seq; u32 pid; }
    struct ifinfomsg  { u8 family; u8 pad; u16 type; i32 index; u32 flags; u32 change; }
    struct rtattr     { u16 len; u16 type; }  followed by payload, 4-byte aligned

The alignment is not optional and is the usual source of `EINVAL`: each
attribute's declared length excludes padding, while its position in the buffer
includes it.

Everything here runs unprivileged **inside a user namespace that owns the
target network namespace**. Creating a veth pair needs `CAP_NET_ADMIN`, and a
process holds that over namespaces its own user namespace created -- so two
containers can be wired to each other, while wiring one to the host's real
network still cannot be done without real privilege.
"""

from __future__ import annotations

import os
import socket
import struct

NETLINK_ROUTE = 0

# message types
RTM_NEWLINK, RTM_DELLINK, RTM_GETLINK = 16, 17, 18
RTM_NEWADDR, RTM_GETADDR = 20, 22
RTM_NEWROUTE = 24
NLMSG_ERROR, NLMSG_DONE = 2, 3

# message flags
NLM_F_REQUEST = 0x001
NLM_F_MULTI = 0x002
NLM_F_ACK = 0x004
NLM_F_ROOT = 0x100
NLM_F_MATCH = 0x200
NLM_F_EXCL = 0x200
NLM_F_CREATE = 0x400
NLM_F_DUMP = NLM_F_ROOT | NLM_F_MATCH

# link attributes
IFLA_ADDRESS, IFLA_IFNAME, IFLA_MTU = 1, 3, 4
IFLA_LINKINFO, IFLA_NET_NS_FD = 18, 28
IFLA_INFO_KIND, IFLA_INFO_DATA = 1, 2
VETH_INFO_PEER = 1

# address attributes
IFA_ADDRESS, IFA_LOCAL = 1, 2

# route attributes
RTA_DST, RTA_OIF, RTA_GATEWAY = 1, 4, 5
RT_TABLE_MAIN, RT_SCOPE_UNIVERSE, RT_SCOPE_LINK = 254, 0, 253
RTPROT_BOOT, RTN_UNICAST = 3, 1

IFF_UP = 0x1

AF_UNSPEC, AF_INET = 0, 2


def align(length: int) -> int:
    return (length + 3) & ~3


def attribute(kind: int, payload: bytes) -> bytes:
    """One rtattr. Declared length excludes padding; buffer position includes it."""
    header = struct.pack("=HH", len(payload) + 4, kind)
    body = header + payload
    return body + b"\0" * (align(len(body)) - len(body))


def parse_attributes(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    offset = 0
    while offset + 4 <= len(data):
        length, kind = struct.unpack_from("=HH", data, offset)
        if length < 4 or offset + length > len(data):
            break
        out[kind] = data[offset + 4: offset + length]
        offset += align(length)
    return out


class NetlinkError(OSError):
    pass


class Netlink:
    """A netlink socket bound to the caller's current network namespace.

    Which namespace that is matters more than anything else here: the socket
    is bound at construction, so a `Netlink` created before `setns` talks to
    the old namespace forever. Constructing one inside the target namespace is
    the entire mechanism by which a container configures its own interfaces.
    """

    def __init__(self):
        self.socket = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE)
        self.socket.bind((0, 0))
        self.sequence = 0

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "Netlink":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- transport -----------------------------------------------------------

    def request(self, message_type: int, flags: int, payload: bytes, *, expect_reply: bool = False) -> list[bytes]:
        self.sequence += 1
        header_len = 16 + len(payload)
        header = struct.pack("=IHHII", header_len, message_type, flags | NLM_F_REQUEST, self.sequence, 0)
        self.socket.send(header + payload)
        return self._read_replies(self.sequence, expect_reply)

    def _read_replies(self, sequence: int, expect_reply: bool) -> list[bytes]:
        replies: list[bytes] = []
        while True:
            data = self.socket.recv(65536)
            offset = 0
            while offset + 16 <= len(data):
                length, kind, flags, seq, _pid = struct.unpack_from("=IHHII", data, offset)
                body = data[offset + 16: offset + length]
                offset += align(length)
                if seq != sequence:
                    continue
                if kind == NLMSG_ERROR:
                    (code,) = struct.unpack_from("=i", body, 0)
                    if code == 0:
                        return replies          # a zero "error" is the ACK
                    raise NetlinkError(-code, f"netlink: {os.strerror(-code)}")
                if kind == NLMSG_DONE:
                    return replies
                replies.append(body)
                if not (flags & NLM_F_MULTI) and expect_reply:
                    return replies
            if not expect_reply and not replies:
                continue

    # -- links ---------------------------------------------------------------

    def link_index(self, name: str) -> int:
        """Look up an interface index by name, the way `if_nametoindex` does."""
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, 0, 0, 0) + attribute(IFLA_IFNAME, name.encode() + b"\0")
        replies = self.request(RTM_GETLINK, NLM_F_ACK, payload, expect_reply=True)
        if not replies:
            raise NetlinkError(19, f"no such interface: {name}")
        _family, _pad, _type, index, _flags, _change = struct.unpack_from("=BBHiII", replies[0], 0)
        return index

    def link_names(self) -> dict[int, str]:
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, 0, 0, 0)
        out: dict[int, str] = {}
        for body in self.request(RTM_GETLINK, NLM_F_DUMP, payload, expect_reply=True):
            _f, _p, _t, index, _fl, _ch = struct.unpack_from("=BBHiII", body, 0)
            attrs = parse_attributes(body[16:])
            if IFLA_IFNAME in attrs:
                out[index] = attrs[IFLA_IFNAME].rstrip(b"\0").decode()
        return out

    def create_veth(self, name: str, peer: str) -> None:
        """Create a veth pair: two interfaces that are each other's wire.

        The nesting is three deep, and expresses "make a link of kind veth
        whose driver-specific data contains a second, complete link":

            IFLA_LINKINFO
              IFLA_INFO_KIND  = "veth"
              IFLA_INFO_DATA
                VETH_INFO_PEER
                  ifinfomsg (empty)
                  IFLA_IFNAME = <peer>
        """
        peer_spec = struct.pack("=BBHiII", 0, 0, 0, 0, 0, 0) + attribute(IFLA_IFNAME, peer.encode() + b"\0")
        info_data = attribute(IFLA_INFO_DATA, attribute(VETH_INFO_PEER, peer_spec))
        link_info = attribute(IFLA_LINKINFO, attribute(IFLA_INFO_KIND, b"veth\0") + info_data)
        payload = (
            struct.pack("=BBHiII", AF_UNSPEC, 0, 0, 0, 0, 0)
            + attribute(IFLA_IFNAME, name.encode() + b"\0")
            + link_info
        )
        self.request(RTM_NEWLINK, NLM_F_CREATE | NLM_F_EXCL | NLM_F_ACK, payload)

    def move_link_to_netns(self, name: str, netns_fd: int) -> None:
        """Move an interface into another network namespace by file descriptor.

        This is why veth pairs exist: one end stays, one end moves, and the two
        namespaces now have a wire between them. A moved interface loses its
        addresses, so addressing must happen after the move, not before.
        """
        index = self.link_index(name)
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, index, 0, 0) + attribute(
            IFLA_NET_NS_FD, struct.pack("=I", netns_fd)
        )
        self.request(RTM_NEWLINK, NLM_F_ACK, payload)

    def set_link_up(self, name: str) -> None:
        index = self.link_index(name)
        # `change` is a mask: only the bits it names are touched.
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, index, IFF_UP, IFF_UP)
        self.request(RTM_NEWLINK, NLM_F_ACK, payload)

    def set_mtu(self, name: str, mtu: int) -> None:
        index = self.link_index(name)
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, index, 0, 0) + attribute(IFLA_MTU, struct.pack("=I", mtu))
        self.request(RTM_NEWLINK, NLM_F_ACK, payload)

    def link_mtu(self, name: str) -> int:
        """Read an interface's MTU from netlink rather than /sys.

        A fresh network namespace usually has no sysfs mounted, so
        `/sys/class/net/<name>/mtu` does not exist -- but the attribute is in
        every RTM_GETLINK reply regardless.
        """
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, 0, 0, 0) + attribute(IFLA_IFNAME, name.encode() + b"\0")
        replies = self.request(RTM_GETLINK, NLM_F_ACK, payload, expect_reply=True)
        if not replies:
            raise NetlinkError(19, f"no such interface: {name}")
        attrs = parse_attributes(replies[0][16:])
        if IFLA_MTU not in attrs:
            raise NetlinkError(61, f"no MTU reported for {name}")
        return struct.unpack("=I", attrs[IFLA_MTU])[0]

    def rename_link(self, name: str, new_name: str) -> None:
        index = self.link_index(name)
        payload = struct.pack("=BBHiII", AF_UNSPEC, 0, 0, index, 0, 0) + attribute(
            IFLA_IFNAME, new_name.encode() + b"\0"
        )
        self.request(RTM_NEWLINK, NLM_F_ACK, payload)

    # -- addresses and routes ------------------------------------------------

    def add_address(self, name: str, address: str, prefix: int) -> None:
        index = self.link_index(name)
        packed = socket.inet_aton(address)
        payload = (
            struct.pack("=BBBBi", AF_INET, prefix, 0, RT_SCOPE_UNIVERSE, index)
            + attribute(IFA_LOCAL, packed)
            + attribute(IFA_ADDRESS, packed)
        )
        self.request(RTM_NEWADDR, NLM_F_CREATE | NLM_F_EXCL | NLM_F_ACK, payload)

    def add_default_route(self, gateway: str) -> None:
        payload = struct.pack(
            "=BBBBBBBBI", AF_INET, 0, 0, 0, RT_TABLE_MAIN, RTPROT_BOOT, RT_SCOPE_UNIVERSE, RTN_UNICAST, 0
        ) + attribute(RTA_GATEWAY, socket.inet_aton(gateway))
        self.request(RTM_NEWROUTE, NLM_F_CREATE | NLM_F_ACK, payload)

    def addresses(self, name: str | None = None) -> dict[str, list[str]]:
        names = self.link_names()
        payload = struct.pack("=BBBBi", AF_INET, 0, 0, 0, 0)
        out: dict[str, list[str]] = {}
        for body in self.request(RTM_GETADDR, NLM_F_DUMP, payload, expect_reply=True):
            _family, prefix, _flags, _scope, index = struct.unpack_from("=BBBBi", body, 0)
            attrs = parse_attributes(body[8:])
            raw = attrs.get(IFA_LOCAL) or attrs.get(IFA_ADDRESS)
            if raw is None or index not in names:
                continue
            out.setdefault(names[index], []).append(f"{socket.inet_ntoa(raw)}/{prefix}")
        return {k: v for k, v in out.items() if name is None or k == name}
