"""
Custom binary protocol over TCP.

Every message is framed as:

    [0:4]   magic b"TSKQ"        (4 bytes)
    [4:8]   payload length       (uint32, big-endian)
    [8:]    JSON payload          (UTF-8)

The length-prefix framing lets the receiver know exactly how many bytes
to read before deserialising, which avoids the "read until delimiter"
fragmentation problems of newline-based protocols.

A connection can carry an arbitrary sequence of messages in both directions.
"""

from __future__ import annotations

import json
import socket
import struct

from taskqueue.config import MAGIC, HEADER_SIZE, MAX_MESSAGE_BYTES, Message


class FramingError(Exception):
    pass


class ConnectionClosed(Exception):
    pass


# ---------------------------------------------------------------------------
# Encode / decode individual frames
# ---------------------------------------------------------------------------

def encode_message(msg: Message) -> bytes:
    """Serialise a Message to a framed byte string."""
    body = json.dumps(msg.to_dict(), separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise FramingError(f"Message too large: {len(body)} bytes")
    header = MAGIC + struct.pack(">I", len(body))
    return header + body


def decode_frame(data: bytes) -> Message:
    """Deserialise a complete frame (header + body) into a Message."""
    if len(data) < HEADER_SIZE:
        raise FramingError("Frame too short")
    if data[:4] != MAGIC:
        raise FramingError(f"Bad magic: {data[:4]!r}")
    length = struct.unpack_from(">I", data, 4)[0]
    body   = data[HEADER_SIZE: HEADER_SIZE + length]
    try:
        return Message.from_dict(json.loads(body.decode("utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise FramingError(f"Malformed message body: {exc}") from exc


# ---------------------------------------------------------------------------
# Socket-level read helpers
# ---------------------------------------------------------------------------

def recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket, raising ConnectionClosed on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed("Connection closed by remote end")
        buf += chunk
    return buf


def read_message(sock: socket.socket) -> Message:
    """Read one complete framed message from a socket."""
    header = recv_exactly(sock, HEADER_SIZE)
    if header[:4] != MAGIC:
        raise FramingError(f"Bad magic: {header[:4]!r}")
    length = struct.unpack_from(">I", header, 4)[0]
    if length > MAX_MESSAGE_BYTES:
        raise FramingError(f"Message too large: {length}")
    body = recv_exactly(sock, length)
    try:
        return Message.from_dict(json.loads(body.decode("utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise FramingError(f"Malformed message: {exc}") from exc


def write_message(sock: socket.socket, msg: Message):
    """Send one framed message over a socket."""
    sock.sendall(encode_message(msg))
