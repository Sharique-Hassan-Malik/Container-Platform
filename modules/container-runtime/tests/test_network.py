"""Networking: a veth pair built from raw netlink, carrying a real TCP connection.

Two network namespaces owned by the same user namespace can be wired together
without any privilege at all. The test proves it end to end rather than
asserting that netlink returned zero: a server binds in one namespace, a client
connects from the other, and a byte crosses.
"""

from __future__ import annotations

import json
import os
import socket
import time

import pytest

from container_testcaps import requires_userns
from minicon import Bridge, Netlink, idmap, linux
from minicon.network import connect, host_networking_available

pytestmark = [requires_userns]


def in_userns(function, timeout: float = 60.0) -> dict:
    """Run `function` in a fresh user+net namespace, returning its JSON result."""
    read_fd, write_fd = os.pipe()
    real_uid, real_gid = os.getuid(), os.getgid()
    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            os.close(read_fd)
            linux.unshare(linux.CLONE_NEWUSER | linux.CLONE_NEWNET | linux.CLONE_NEWNS)
            idmap.write_maps(os.getpid(), idmap.root_mapping(real_uid, real_gid))
            os.write(write_fd, json.dumps(function()).encode())
        except BaseException as exc:  # noqa: BLE001
            os.write(write_fd, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode())
            status = 1
        finally:
            os.close(write_fd)
            os._exit(status)
    os.close(write_fd)
    buffer = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        buffer += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    result = json.loads(buffer or b"{}")
    if "error" in result:
        pytest.fail(result["error"])
    return result


def test_loopback_starts_down_and_can_be_raised():
    def probe():
        with Netlink() as netlink:
            before = netlink.addresses()
            netlink.set_link_up("lo")
            return {"before": before, "after": netlink.addresses(), "links": netlink.link_names()}

    result = in_userns(probe)
    assert list(result["links"].values()) == ["lo"]
    assert "lo" in result["after"]
    assert "127.0.0.1/8" in result["after"]["lo"]


def test_veth_pair_is_created_and_addressed():
    def probe():
        with Netlink() as netlink:
            netlink.create_veth("veth0", "veth1")
            netlink.add_address("veth0", "10.88.0.1", 24)
            netlink.add_address("veth1", "10.88.0.2", 24)
            netlink.set_link_up("veth0")
            netlink.set_link_up("veth1")
            return {"links": sorted(netlink.link_names().values()), "addresses": netlink.addresses()}

    result = in_userns(probe)
    assert result["links"] == ["lo", "veth0", "veth1"]
    assert result["addresses"]["veth0"] == ["10.88.0.1/24"]
    assert result["addresses"]["veth1"] == ["10.88.0.2/24"]


def test_creating_the_same_interface_twice_is_an_error():
    def probe():
        with Netlink() as netlink:
            netlink.create_veth("dup0", "dup1")
            try:
                netlink.create_veth("dup0", "dup2")
                return {"raised": False}
            except OSError as exc:
                return {"raised": True, "errno": exc.errno}

    result = in_userns(probe)
    assert result["raised"] is True
    assert result["errno"] == 17          # EEXIST


def test_rename_link():
    def probe():
        with Netlink() as netlink:
            netlink.create_veth("old0", "old1")
            netlink.rename_link("old0", "new0")
            return {"links": sorted(netlink.link_names().values())}

    assert in_userns(probe)["links"] == ["lo", "new0", "old1"]


def test_moving_an_interface_removes_it_from_this_namespace():
    def probe():
        with Netlink() as netlink:
            netlink.create_veth("vm0", "vm1")
            before = sorted(netlink.link_names().values())
            child = os.fork()
            if child == 0:
                # A second network namespace, owned by the same user namespace,
                # which is exactly what makes the move permitted.
                linux.unshare(linux.CLONE_NEWNET)
                time.sleep(3)
                os._exit(0)
            time.sleep(0.3)
            fd = os.open(f"/proc/{child}/ns/net", os.O_RDONLY)
            netlink.move_link_to_netns("vm1", fd)
            os.close(fd)
            after = sorted(netlink.link_names().values())
            os.waitpid(child, 0)
            return {"before": before, "after": after}

    result = in_userns(probe)
    assert result["before"] == ["lo", "vm0", "vm1"]
    assert result["after"] == ["lo", "vm0"]


def test_mtu_can_be_set():
    def probe():
        with Netlink() as netlink:
            netlink.create_veth("mtu0", "mtu1")
            netlink.set_mtu("mtu0", 1400)
            # Read back over netlink: a bare network namespace has no sysfs.
            return {"mtu": netlink.link_mtu("mtu0"), "peer": netlink.link_mtu("mtu1")}

    result = in_userns(probe)
    assert result["mtu"] == 1400
    assert result["peer"] == 1500      # the far end is a separate interface


def test_tcp_traffic_crosses_the_veth_pair():
    """The payoff: a byte moves between two namespaces over a link we built."""

    def probe():
        with Netlink() as netlink:
            netlink.set_link_up("lo")
            bridge = Bridge()
            host_side, container_side = bridge.allocate("c1")

            ready_r, ready_w = os.pipe()
            result_r, result_w = os.pipe()
            child = os.fork()
            if child == 0:
                # The container side: its own network namespace, still inside
                # this user namespace so netlink here is permitted.
                os.close(ready_w)
                os.close(result_r)
                try:
                    linux.unshare(linux.CLONE_NEWNET)
                    os.write(result_w, b"")
                    # Wait for the parent to hand us the far end of the pair.
                    os.read(ready_r, 1)
                    with Netlink() as inner:
                        inner.set_link_up("lo")
                        inner.add_address("eth0", container_side.address, container_side.prefix)
                        inner.set_link_up("eth0")
                    deadline = time.time() + 10
                    payload = b""
                    while time.time() < deadline and not payload:
                        try:
                            sock = socket.create_connection((host_side.address, 9099), timeout=2)
                            payload = sock.recv(16)
                            sock.close()
                        except OSError:
                            time.sleep(0.1)
                    os.write(result_w, payload or b"none")
                except BaseException as exc:  # noqa: BLE001
                    os.write(result_w, f"err:{exc}".encode()[:64])
                finally:
                    os.close(result_w)
                    os._exit(0)

            os.close(ready_r)
            os.close(result_w)
            time.sleep(0.3)
            fd = os.open(f"/proc/{child}/ns/net", os.O_RDONLY)
            connect(netlink, host_side, container_side, fd)
            os.close(fd)

            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host_side.address, 9099))
            server.listen(1)
            server.settimeout(15)
            os.write(ready_w, b"1")
            os.close(ready_w)

            connection, peer = server.accept()
            connection.sendall(b"across")
            connection.close()
            server.close()

            payload = os.read(result_r, 64)
            os.close(result_r)
            os.waitpid(child, 0)
            return {"received": payload.decode(errors="replace"), "peer": peer[0],
                    "host_addr": host_side.address, "container_addr": container_side.address}

    result = in_userns(probe, timeout=90)
    assert result["received"] == "across"
    assert result["peer"] == result["container_addr"]


def test_bridge_allocates_distinct_addresses():
    bridge = Bridge()
    first_host, first_container = bridge.allocate("a")
    second_host, second_container = bridge.allocate("b")
    addresses = {first_host.address, first_container.address, second_host.address, second_container.address}
    assert len(addresses) == 4
    with pytest.raises(ValueError, match="already attached"):
        bridge.allocate("a")


def test_host_networking_is_honestly_reported():
    available, reason = host_networking_available()
    if os.geteuid() != 0:
        assert available is False
        assert "slirp4netns" in reason or "CAP_NET_ADMIN" in reason
