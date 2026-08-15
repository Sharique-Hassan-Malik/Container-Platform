#!/usr/bin/env python3
"""
Interactive key-value client.

Usage:
    python scripts/kv_cli.py 127.0.0.1:15001 127.0.0.1:15002 127.0.0.1:15003

Commands:
    get <key>
    set <key> <value>
    del <key>
    leader
    quit
"""

import sys
import readline
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from raft_kv.rpc.client import KVClient


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: kv_cli.py <addr> [<addr> ...]")
        sys.exit(1)

    client = KVClient(sys.argv[1:])
    print("raft-kv client  (get / set / del / leader / quit)")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        parts = line.split(None, 2)
        cmd   = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "get" and len(parts) >= 2:
            v = client.get(parts[1])
            print(v if v is not None else "(not found)")
        elif cmd == "set" and len(parts) >= 3:
            ok = client.set(parts[1], parts[2])
            print("ok" if ok else "error")
        elif cmd == "del" and len(parts) >= 2:
            ok = client.delete(parts[1])
            print("ok" if ok else "error")
        elif cmd == "leader":
            import grpc
            from raft_kv.rpc import raft_pb2 as pb, raft_pb2_grpc as rpc
            for addr in sys.argv[1:]:
                try:
                    with grpc.insecure_channel(addr) as ch:
                        r = rpc.KVServiceStub(ch).Leader(pb.LeaderRequest(), timeout=1)
                    print(f"{addr} -> leader={r.leader_id or '?'}")
                except Exception as e:
                    print(f"{addr} -> error: {e}")
        else:
            print("commands: get <key> | set <key> <value> | del <key> | leader | quit")


if __name__ == "__main__":
    main()
