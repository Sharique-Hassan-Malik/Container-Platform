"""
Web dashboard for the task queue.

A minimal single-file HTTP server (no framework) exposing:

    GET /              → HTML dashboard page
    GET /api/stats     → JSON: queue sizes, worker count, pending acks
    GET /api/results   → JSON: recent task results
    GET /api/workers   → JSON: connected worker list

The HTML page uses polling (setInterval) to refresh the stats panel
every 2 seconds.  No WebSockets, no external JS libraries.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from taskqueue.config import DashboardConfig


class DashboardServer:
    """
    Serves the web dashboard.

    The dashboard reads live data from callback functions supplied at
    construction time, keeping it decoupled from any specific backend.

        stats_fn()    → dict  (queue sizes, worker count, etc.)
        results_fn()  → list  (recent TaskResult dicts)
        workers_fn()  → list  (worker info dicts)
    """

    def __init__(
        self,
        config:     DashboardConfig,
        stats_fn:   Callable[[], dict],
        results_fn: Callable[[], list],
        workers_fn: Callable[[], list] | None = None,
    ):
        self._config     = config
        self._stats_fn   = stats_fn
        self._results_fn = results_fn
        self._workers_fn = workers_fn or (lambda: [])

    def start(self, daemon: bool = True):
        """Start the HTTP server in a daemon thread."""
        server = self._build_server()
        t = threading.Thread(
            target=server.serve_forever,
            daemon=daemon,
            name="dashboard-http",
        )
        t.start()
        return server

    def serve_forever(self):
        """Block serving the dashboard (call from main thread)."""
        server = self._build_server()
        print(f"Dashboard at http://{self._config.host}:{self._config.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

    def _build_server(self) -> HTTPServer:
        stats_fn   = self._stats_fn
        results_fn = self._results_fn
        workers_fn = self._workers_fn

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass

            def do_GET(self):
                if self.path == "/":
                    self._send_html(_DASHBOARD_HTML)
                elif self.path == "/api/stats":
                    self._send_json(stats_fn())
                elif self.path == "/api/results":
                    self._send_json(results_fn())
                elif self.path == "/api/workers":
                    self._send_json(workers_fn())
                else:
                    self.send_response(404); self.end_headers()

            def _send_json(self, data):
                body = json.dumps(data, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str):
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)

        return HTTPServer((self._config.host, self._config.port), Handler)


# ---------------------------------------------------------------------------
# Embedded dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Task Queue Dashboard</title>
<style>
body{font-family:monospace;background:#1e1e2e;color:#cdd6f4;margin:0;padding:0}
h1{padding:14px 20px 4px;color:#cba6f7;margin:0;font-size:1.3em}
.subtitle{padding:0 20px 12px;color:#6c7086;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;padding:0 20px 20px}
.card{background:#313244;border-radius:8px;padding:16px}
.card h2{margin:0 0 10px;font-size:13px;color:#89dceb;text-transform:uppercase;letter-spacing:.05em}
.stat{font-size:2em;font-weight:bold;color:#cba6f7}
.label{font-size:11px;color:#6c7086;margin-top:3px}
table{border-collapse:collapse;width:calc(100%-40px);margin:0 20px 20px;font-size:12px}
th{background:#313244;padding:6px 10px;text-align:left;color:#89dceb}
td{padding:5px 10px;border-bottom:1px solid #313244}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px}
.SUCCESS{background:#1e4620;color:#a6e3a1}.FAILURE{background:#4a1e1e;color:#f38ba8}
.RUNNING{background:#3a3a1e;color:#f9e2af}.PENDING{background:#2a2a3e;color:#89b4fa}
.RETRY{background:#3a2e1e;color:#fab387}
#status{position:fixed;bottom:10px;right:16px;font-size:11px;color:#6c7086}
</style>
</head>
<body>
<h1>Task Queue Dashboard</h1>
<div class="subtitle" id="updated">Loading...</div>

<div class="grid" id="stats-grid">
  <div class="card"><h2>Queued</h2><div class="stat" id="s-queued">-</div><div class="label">tasks waiting</div></div>
  <div class="card"><h2>Workers</h2><div class="stat" id="s-workers">-</div><div class="label">connected</div></div>
  <div class="card"><h2>In-flight</h2><div class="stat" id="s-inflight">-</div><div class="label">unacknowledged</div></div>
  <div class="card"><h2>Completed</h2><div class="stat" id="s-done">-</div><div class="label">last 100</div></div>
</div>

<h2 style="padding:0 20px 8px;margin:0;font-size:13px;color:#89dceb;text-transform:uppercase">Recent Tasks</h2>
<table>
  <thead><tr><th>Task ID</th><th>Name</th><th>State</th><th>Worker</th><th>Duration</th></tr></thead>
  <tbody id="results-tbody"><tr><td colspan="5" style="color:#6c7086">Loading...</td></tr></tbody>
</table>
<div id="status">polling...</div>

<script>
function refresh() {
  fetch('/api/stats').then(r=>r.json()).then(data => {
    const total = Object.values(data.queues||{}).reduce((a,b)=>a+b,0);
    document.getElementById('s-queued').textContent   = total;
    document.getElementById('s-workers').textContent  = data.workers||0;
    document.getElementById('s-inflight').textContent = data.pending_acks||0;
    document.getElementById('updated').textContent    =
      'Last updated: ' + new Date().toLocaleTimeString();
  }).catch(()=>{});

  fetch('/api/results').then(r=>r.json()).then(results => {
    document.getElementById('s-done').textContent = results.length;
    const tbody = document.getElementById('results-tbody');
    tbody.innerHTML = results.map(r => {
      const dur = r.duration ? r.duration.toFixed(3)+'s' : '-';
      return '<tr>' +
        '<td style="color:#6c7086">' + r.task_id.slice(0,8) + '...</td>' +
        '<td>' + (r.name||'-') + '</td>' +
        '<td><span class="badge ' + r.state + '">' + r.state + '</span></td>' +
        '<td>' + (r.worker_id||'-') + '</td>' +
        '<td>' + dur + '</td>' +
        '</tr>';
    }).join('') || '<tr><td colspan="5" style="color:#6c7086">No results yet.</td></tr>';
  }).catch(()=>{});

  document.getElementById('status').textContent = 'last poll: ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
