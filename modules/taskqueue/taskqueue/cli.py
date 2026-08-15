"""Task queue CLI.

Commands:
    broker      Start the TCP broker
    worker      Start a worker process pool
    submit      Submit a task and (optionally) wait for the result
    stats       Print broker queue statistics
    dashboard   Start the web dashboard
"""

import argparse
import json
import sys
import time

from taskqueue.config import BrokerConfig, WorkerConfig, BackendConfig, DashboardConfig


def cmd_broker(args):
    from taskqueue.broker.server import BrokerServer
    cfg = BrokerConfig(
        host=args.host, port=args.port,
        ack_timeout=args.ack_timeout,
        persist_path=args.persist,
    )
    print(f"Starting broker on {cfg.host}:{cfg.port}  (persist={cfg.persist_path})")
    BrokerServer(cfg).start()


def cmd_worker(args):
    import importlib
    from taskqueue.worker.worker import Worker

    # Import task modules so the registry is populated
    for mod in args.import_module:
        importlib.import_module(mod)

    cfg = WorkerConfig(
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        queues=args.queues,
        concurrency=args.concurrency,
    )
    print(
        f"Starting worker (id={cfg.worker_id}) "
        f"queues={cfg.queues} concurrency={cfg.concurrency}"
    )
    Worker(cfg, task_modules=args.import_module).start()


def cmd_submit(args):
    from taskqueue.client.client import TaskClient
    cfg    = BrokerConfig(host=args.broker_host, port=args.broker_port)
    client = TaskClient(cfg)

    kwargs = {}
    if args.kwargs:
        kwargs = json.loads(args.kwargs)
    positional = [json.loads(a) for a in args.args]

    ar = client.publish(
        args.name,
        args=positional,
        kwargs=kwargs,
        queue=args.queue,
        priority=args.priority,
    )
    print(f"Submitted: {ar.task_id}")

    if args.wait:
        print("Waiting for result...")
        result = ar.get(timeout=args.timeout)
        if result is None:
            print("Timed out waiting for result.")
            sys.exit(1)
        print(f"State    : {result.state.value}")
        print(f"Result   : {result.result}")
        if result.error:
            print(f"Error    : {result.error}")
        print(f"Duration : {result.duration:.3f}s")
        print(f"Worker   : {result.worker_id}")

    client.close()


def cmd_stats(args):
    from taskqueue.client.client import TaskClient
    cfg    = BrokerConfig(host=args.broker_host, port=args.broker_port)
    client = TaskClient(cfg)
    stats  = client.stats()
    print(json.dumps(stats, indent=2))
    client.close()


def cmd_dashboard(args):
    from taskqueue.backend.sqlite_backend import ResultBackend
    from taskqueue.dashboard.server import DashboardServer
    from taskqueue.client.client import TaskClient

    backend = ResultBackend(BackendConfig(persist_path=args.results_db))
    broker_cfg = BrokerConfig(host=args.broker_host, port=args.broker_port)

    def stats_fn():
        try:
            client = TaskClient(broker_cfg)
            s = client.stats()
            client.close()
            return s
        except Exception:
            return {}

    def results_fn():
        results = backend.all_recent(100)
        return [
            {
                "task_id":   r.task_id,
                "name":      "",
                "state":     r.state.value,
                "worker_id": r.worker_id,
                "duration":  r.duration,
            }
            for r in results
        ]

    cfg = DashboardConfig(host=args.host, port=args.port)
    DashboardServer(cfg, stats_fn=stats_fn, results_fn=results_fn).serve_forever()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tq", description="Distributed task queue")
    sub = p.add_subparsers(dest="command", required=True)

    # broker
    bp = sub.add_parser("broker", help="Start the TCP broker")
    bp.add_argument("--host",        default="127.0.0.1")
    bp.add_argument("--port",        type=int, default=6380)
    bp.add_argument("--ack-timeout", type=float, default=30.0, dest="ack_timeout")
    bp.add_argument("--persist",     default="broker.db")
    bp.set_defaults(func=cmd_broker)

    # worker
    wp = sub.add_parser("worker", help="Start a worker process pool")
    wp.add_argument("--broker-host", default="127.0.0.1", dest="broker_host")
    wp.add_argument("--broker-port", type=int, default=6380, dest="broker_port")
    wp.add_argument("--queues",      nargs="+", default=["default"])
    wp.add_argument("--concurrency", type=int, default=4)
    wp.add_argument("-I", "--import-module", action="append", default=[],
                    dest="import_module", metavar="MODULE",
                    help="Import this module to register tasks (repeatable)")
    wp.set_defaults(func=cmd_worker)

    # submit
    sp = sub.add_parser("submit", help="Submit a task")
    sp.add_argument("name", help="Task name (e.g. myapp.tasks.add)")
    sp.add_argument("args", nargs="*", help="Positional args as JSON")
    sp.add_argument("--kwargs",       default=None, help="Keyword args as JSON object")
    sp.add_argument("--queue",        default="default")
    sp.add_argument("--priority",     type=int, default=5)
    sp.add_argument("--wait",         action="store_true", help="Wait for result")
    sp.add_argument("--timeout",      type=float, default=30.0)
    sp.add_argument("--broker-host",  default="127.0.0.1", dest="broker_host")
    sp.add_argument("--broker-port",  type=int, default=6380, dest="broker_port")
    sp.set_defaults(func=cmd_submit)

    # stats
    stp = sub.add_parser("stats", help="Print broker statistics")
    stp.add_argument("--broker-host", default="127.0.0.1", dest="broker_host")
    stp.add_argument("--broker-port", type=int, default=6380, dest="broker_port")
    stp.set_defaults(func=cmd_stats)

    # dashboard
    dp = sub.add_parser("dashboard", help="Start the web dashboard")
    dp.add_argument("--host",        default="127.0.0.1")
    dp.add_argument("--port",        type=int, default=8888)
    dp.add_argument("--broker-host", default="127.0.0.1", dest="broker_host")
    dp.add_argument("--broker-port", type=int, default=6380, dest="broker_port")
    dp.add_argument("--results-db",  default="results.db",  dest="results_db")
    dp.set_defaults(func=cmd_dashboard)

    args = p.parse_args(argv)
    args.func(args)
    return 0
