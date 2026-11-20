"""Local web UI for jeli: serves static/, the tasks as JSON at GET /api/tasks, board edits at
POST /api/tasks/<id>, and the agent tracker's sessions at GET /api/agents.

Binds to 127.0.0.1 only, and guards against other sites in the same browser:
- every request must carry a Host header naming this server, which defeats DNS rebinding;
- writes must be JSON with an X-Jeli header, which a cross-site page can't send without a CORS
  preflight that this server never approves.
"""

import json
import re
import webbrowser
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agents
from store import OPEN_STATUSES, TaskError, move_task, read_tasks, update_task

STATIC_DIR = Path(__file__).resolve().parent / "static"
TASK_PATH = re.compile(r"^/api/tasks/(\d+)$")
MAX_BODY = 4096


class Handler(SimpleHTTPRequestHandler):
    def host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self) -> None:
        if not self.host_ok():
            self.send_json(403, {"error": "unexpected Host header"})
        elif self.path.split("?")[0] == "/api/tasks":
            self.send_tasks()
        elif self.path.split("?")[0] == "/api/agents":
            self.send_agents()
        else:
            super().do_GET()

    def do_HEAD(self) -> None:
        if not self.host_ok():
            self.send_json(403, {"error": "unexpected Host header"})
        else:
            super().do_HEAD()

    def do_POST(self) -> None:
        if not self.host_ok():
            return self.send_json(403, {"error": "unexpected Host header"})
        if self.headers.get("X-Jeli") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_json(403, {"error": "writes need Content-Type: application/json and X-Jeli: 1"})
        match = TASK_PATH.match(self.path)
        if not match:
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send_json(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            task, messages = self.edit(int(match.group(1)), body)
        except (ValueError, TypeError) as e:
            return self.send_json(400, {"error": f"bad request: {e}"})
        except TaskError as e:
            return self.send_json(409, {"error": str(e)})
        self.send_json(200, {"task": task, "messages": messages})

    def edit(self, task_id: int, body: dict) -> tuple[dict | None, list[str]]:
        """Apply a board edit: an optional status change plus an optional before/after position."""
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        status = body.get("status")
        if status is not None and status not in OPEN_STATUSES + ["done"]:
            raise ValueError(f"unknown status {status!r}")
        position = None
        for key in ("before", "after"):
            if body.get(key) is not None:
                position = {key: int(body[key])}
        if status is not None:
            return update_task(task_id, {"status": status}, position)
        if position is not None:
            move_task(task_id, position)
            return None, [f"moved #{task_id}"]
        raise ValueError("nothing to change; send status and/or before/after")

    def send_tasks(self) -> None:
        try:
            open_tasks, done_tasks = read_tasks()
        except TaskError as e:
            return self.send_json(409, {"error": str(e)})
        # The archive is in completion order; show the most recently finished first.
        self.send_json(200, {"open": open_tasks, "done": done_tasks[::-1]})

    def send_agents(self) -> None:
        """Every session including recently ended ones, in board order, with their tasks attached, and
        each task's most recently ended session from the whole retention window.
        A connection per request: sqlite connections can't be shared across the server's threads."""
        now = datetime.now()
        conn = agents.connect()
        try:
            sessions = agents.list_sessions(conn, now, ended_within=agents.RETENTION)
        finally:
            conn.close()
        try:
            open_tasks, done_tasks = read_tasks()
        except TaskError:
            open_tasks, done_tasks = [], []
        agents.attach_tasks(sessions, open_tasks, done_tasks, now)
        recent = agents.stamp(now - agents.RECENTLY_ENDED)
        shown = [s for s in sessions if s["state"] != "ended" or s["last_seen"] >= recent]
        self.send_json(200, {"sessions": shown, "last_agents": agents.last_agent_by_task(sessions)})

    def send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        # API responses are never cached. Static files are revalidated on every load, so an edit to
        # palette.css or index.html shows up on reload instead of a stale heuristically-cached copy.
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


def serve(port: int, open_browser: bool) -> None:
    handler = partial(Handler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"jeli UI at {url} (Ctrl-C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
