# Raft-KV — Distributed Key-Value Store

> Part of the [Container Platform](../../README.md). Runs standalone from this
> folder, and composes with the other four modules through the `ctl` CLI.

A Raft consensus implementation paired with a linearisable key-value store.
Implements leader election, log replication, commit index advancement and
durable persistent state from the original Raft paper. Built with Python and
gRPC; no consensus library is used.

---

## What it does

A cluster of three or five nodes elects a leader, replicates every write
through the Raft log, and applies committed entries to an in-memory key-value
store in the same order on every node. The cluster tolerates ⌊(n−1)/2⌋ node
failures — a three-node cluster survives one failure, a five-node cluster
survives two.

---

## The hard parts

**Commit rule.** The leader may only commit entries from the *current* term.
Entries from prior terms are only safe to commit transitively, by committing
a later entry from the current term that follows them in the log. Getting this
wrong silently corrupts the state machine. The paper devotes §5.4.2 to it.

**Log consistency check.** Every AppendEntries carries `prev_log_index` and
`prev_log_term`. Followers reject the call if their log does not match at that
position, and the leader backs up `next_index` until it finds the last point
of agreement. The fast backup returns the follower's last good index so the
leader can recover in one round-trip rather than one entry at a time.

**Durable state.** `current_term` and `voted_for` must survive crashes. The
implementation writes an atomic `rename(tmp → final)` with `fsync` on the
tmp file before the rename. Skipping the fsync causes split-brain on a crash
between write and rename.

**Transport injection.** `RaftNode` accepts `send_rv` and `send_ae` as plain
callables. Tests wire them to direct Python method calls (no network, no
threads, deterministic). Production code uses gRPC stubs. This lets the full
consensus logic be tested without starting any servers.

---

## Architecture

```
Client → KVService (gRPC) → RaftNode.propose()
                                 │ replicate
                                 ▼
                          Peers via RaftService (gRPC)
                                 │ commit
                                 ▼
                          KVStore.apply(command)
```

See `docs/ARCHITECTURE.md` for the full algorithm walkthrough.

---

## Running a local cluster

Start three nodes in separate terminals:

```bash
python scripts/run_node.py --id n1 --addr 127.0.0.1:15001 \
    --peer n2=127.0.0.1:15002 --peer n3=127.0.0.1:15003

python scripts/run_node.py --id n2 --addr 127.0.0.1:15002 \
    --peer n1=127.0.0.1:15001 --peer n3=127.0.0.1:15003

python scripts/run_node.py --id n3 --addr 127.0.0.1:15003 \
    --peer n1=127.0.0.1:15001 --peer n2=127.0.0.1:15002
```

Then use the CLI client:

```bash
python scripts/kv_cli.py 127.0.0.1:15001 127.0.0.1:15002 127.0.0.1:15003

> set city Islamabad
ok
> get city
Islamabad
> del city
ok
> leader
127.0.0.1:15001 -> leader=n1
```

To test fault tolerance, kill one node — the remaining two will continue
serving reads and writes. Restart it and it will catch up automatically.

---

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

28 tests across log operations, persistent state, the KV state machine and
full in-process consensus (election, replication, commit, fault injection).
Tests run entirely in-process with no gRPC overhead, completing in under 3
seconds.

---

## Raft guarantees

| Property | Guarantee |
|----------|-----------|
| Election Safety | At most one leader per term |
| Leader Completeness | All committed entries appear on every future leader |
| State Machine Safety | Every node applies the same commands in the same order |
| Fault tolerance | Cluster survives ⌊(n−1)/2⌋ simultaneous node failures |

---

## Tech stack

Python 3.10+, gRPC / Protocol Buffers. No external consensus library.

---

## References

Ongaro, D. and Ousterhout, J. (2014). In Search of an Understandable Consensus
Algorithm. *USENIX Annual Technical Conference*.
https://raft.github.io/raft.pdf

Ongaro, D. (2014). Consensus: Bridging Theory and Practice (PhD thesis,
Stanford University). https://web.stanford.edu/~ouster/cgi-bin/papers/OngaroPhD.pdf
