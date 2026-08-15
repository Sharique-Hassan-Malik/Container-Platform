#!/usr/bin/env python3
"""
Launch a single Raft-KV node.

Usage:
    python scripts/run_node.py --id n1 --addr 127.0.0.1:15001 \
        --peer n2=127.0.0.1:15002 --peer n3=127.0.0.1:15003

All nodes must be started with the same peer list. Start all three in
separate terminals.
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from raft_kv.raft.node  import RaftNode
from raft_kv.store.kv   import KVStore
from raft_kv.rpc.client import make_send_rv, make_send_ae
from raft_kv.rpc.server import build_server

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(name)s %(levelname)s %(message)s",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id",       required=True, help="Unique node ID (e.g. n1)")
    ap.add_argument("--addr",     required=True, help="host:port to listen on")
    ap.add_argument("--peer",     action="append", default=[],
                    metavar="ID=HOST:PORT", help="Peer id=addr (repeat for each peer)")
    ap.add_argument("--data-dir", default=None,   help="Directory for persistent state")
    args = ap.parse_args()

    peers: dict[str, str] = {}
    for p in args.peer:
        pid, paddr = p.split("=", 1)
        peers[pid] = paddr

    store = KVStore()
    node  = RaftNode(
        node_id  = args.id,
        peers    = peers,
        apply_fn = lambda entry: store.apply(entry.command),
        send_rv  = make_send_rv(peers),
        send_ae  = make_send_ae(peers),
        data_dir = args.data_dir,
    )
    server = build_server(node, store, peers, args.addr)
    server.start()

    print(f"[{args.id}] listening on {args.addr}")
    print(f"[{args.id}] peers: {peers}")

    def shutdown(sig, frame):
        print(f"\n[{args.id}] shutting down")
        node.stop()
        server.stop(grace=1)
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        time.sleep(1)
        print(f"[{args.id}] role={node.role.name:<9} "
              f"term={node.current_term} "
              f"commit={node.commit_index} "
              f"leader={node.leader_id or '?'} "
              f"keys={len(store)}")


if __name__ == "__main__":
    main()
