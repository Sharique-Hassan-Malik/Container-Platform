# Architecture — raft-kv

## Overview

raft-kv is a Raft-based distributed key-value store. The Raft consensus
algorithm ensures that a cluster of nodes agrees on the order of commands
even when minority partitions or node crashes occur. A deterministic key-value
state machine applies each committed command in order, producing identical
state on all nodes.

---

## System diagram

```
Client
  │  gRPC (KVService)
  ▼
┌──────────────────────────────────┐
│         KVServicer               │  server.py
│  Get / Set / Del / Leader        │
└──────────────┬───────────────────┘
               │ propose(command)
               ▼
┌──────────────────────────────────┐
│           RaftNode               │  raft/node.py
│  Election · Replication · Commit │
└────────┬─────────────────────────┘
         │ apply_fn(LogEntry)     gRPC (RaftService) to peers
         ▼                          ↕
┌──────────────────┐         ┌──────────────┐
│     KVStore      │         │  RaftServicer│  server.py
│  SET / DEL / GET │         │  RV / AE RPCs│
└──────────────────┘         └──────────────┘
         ▲
    RaftLog + PersistentState
    (disk, optional)
```

---

## Raft algorithm

This implementation follows the original paper closely.

### Persistent state (§5.1, Figure 2)

`current_term` and `voted_for` are written to disk with `fsync` before any
RPC response is sent, as required by the paper. The log is appended on every
`append()` call and rewritten on snapshot or truncation.

### Leader election (§5.2)

Each node maintains a `_last_contact` timestamp and an `_election_timeout`
drawn uniformly at random from [150, 300] ms. The single background thread
checks whether the timeout has elapsed on every 10 ms tick. When it has, the
node starts an election:

1. Increment term, vote for self, request votes from all peers concurrently.
2. Grant vote only if the candidate's log is at least as complete as ours
   (§5.4.1 log completeness check).
3. First node to accumulate a quorum (⌈(n+1)/2⌉) wins and becomes leader.
4. Any node that sees a higher term immediately steps down.

Random timeouts make split votes statistically rare; the paper proves safety
regardless of the timing outcome.

### Log replication (§5.3)

The leader's background thread sends AppendEntries at a fixed 40 ms heartbeat
interval. Each call carries:

- `prev_log_index`, `prev_log_term` — consistency check on the follower.
- `entries` — zero or more new entries (zero = heartbeat).
- `leader_commit` — leader's current commit index.

Followers reject AppendEntries if the consistency check fails, returning their
last known good index so the leader can back up `next_index` quickly.

### Commit rule (§5.3)

The leader advances `commit_index` to the highest log index N such that:

- `log.term_at(N) == current_term` (no committing entries from prior terms)
- A quorum of nodes has `match_index >= N`

Followers advance their own `commit_index` to `min(leader_commit, last_index)`
on receipt of each AppendEntries.

### Apply loop

A separate pass inside the main background thread applies all committed but
not-yet-applied entries to the KVStore in strict index order. This produces
the linearisable read semantics the paper guarantees.

---

## Components

### `raft/node.py` — RaftNode

The consensus core. All public methods are thread-safe. The single background
thread runs at 10 ms intervals:

- **Follower / Candidate**: checks election timeout; starts election if elapsed.
- **Leader**: sends heartbeats every 40 ms; drives replication and commit.
- **All roles**: applies committed entries to the state machine.

### `raft/log.py` — RaftLog

In-memory replicated log. Entries are 1-based. Index 0 is a virtual sentinel
representing "nothing". The log supports:

- `append` / `append_all` — add entries, persisted on append.
- `truncate_from(index)` — delete conflicting suffix (§5.3).
- `snapshot(last_index, last_term)` — compact prefix (log trimming).
- `entries_from(start)` — slice for replication.

### `raft/state.py` — PersistentState

Stores `current_term` and `voted_for`. Every write uses an atomic
`rename(tmp → final)` with `fsync` on the tmp file before rename, satisfying
Raft's durability requirement on a crash-safe filesystem.

### `store/kv.py` — KVStore

Deterministic state machine. Commands:

| Command       | Effect                        |
|---------------|-------------------------------|
| `SET k v`     | Store value v under key k     |
| `DEL k`       | Remove key k                  |
| `NOP`         | No-op (used for leader no-op) |

Snapshot / restore support is included for future log compaction.

### `rpc/server.py` — gRPC server

Hosts two services on one port:

- `RaftService` — `RequestVote` and `AppendEntries` for inter-node RPCs.
- `KVService` — `Get`, `Set`, `Del`, `Leader` for client requests.

`Set` and `Del` return a `leader_hint` address when the node is not the leader,
allowing the client to redirect automatically.

### `rpc/client.py` — gRPC client

- `make_send_rv` / `make_send_ae` produce callables injected into `RaftNode`
  so the node has no import dependency on gRPC.
- `KVClient` wraps the KVService stub with automatic leader-hint following.

---

## Why in-process RPC injection?

The `RaftNode` constructor accepts `send_rv` and `send_ae` as callables rather
than constructing gRPC stubs internally. This makes the node fully testable
without a network: tests wire the callables directly to other nodes' handler
methods. Production code uses `make_send_rv` / `make_send_ae` which open gRPC
channels. The design keeps business logic and transport completely separated.

---

## What Raft guarantees

| Property | Guarantee |
|----------|-----------|
| Election Safety | At most one leader per term |
| Leader Append-Only | A leader never overwrites its log |
| Log Matching | If two logs agree at index N, they agree on all entries ≤ N |
| Leader Completeness | A committed entry is present on all future leaders |
| State Machine Safety | All nodes apply the same sequence of commands |

---

## Files

```
raft_kv/
  __init__.py            — cluster bootstrap helpers
  raft/
    node.py              — RaftNode: election, replication, commit, apply
    log.py               — RaftLog: in-memory log with optional persistence
    state.py             — PersistentState: term + voted_for with fsync
  store/
    kv.py                — KVStore: SET / DEL / NOP state machine
  rpc/
    raft.proto           — Protocol Buffer definitions
    raft_pb2.py          — generated message classes
    raft_pb2_grpc.py     — generated service stubs
    server.py            — gRPC server (RaftServicer + KVServicer)
    client.py            — gRPC client (make_send_rv/ae + KVClient)

scripts/
  run_node.py            — launch a single node
  kv_cli.py              — interactive CLI client

tests/
  test_raft.py           — 28 tests: log, state, KV and consensus

docs/
  ARCHITECTURE.md        — this file
```
