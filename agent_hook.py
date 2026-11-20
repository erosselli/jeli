"""Claude Code hook for the jeli agent tracker: applies each hook payload to data/agents.db.

Registered for every hook event in ~/.claude/settings.json and run with the system Python, so it
uses the standard library only. The hook's parent process is the session's claude process, which
is how the tracker later tells a crashed session from a quiet one.

While data/agents/raw.jsonl exists, every raw payload is also appended to it, for working out what
an event carries; delete the file to stop. The hook never prints and always exits 0, so a failure
here can't affect a session; failures are appended to data/agents/hook-errors.log instead.
"""

import fcntl
import json
import os
import sys
import traceback
from datetime import datetime

from agents import DATA_DIR, apply_event, connect

RAW_LOG = DATA_DIR / "agents" / "raw.jsonl"
ERROR_LOG = DATA_DIR / "agents" / "hook-errors.log"


def log_error() -> None:
    try:
        ERROR_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(ERROR_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {traceback.format_exc()}\n")
    except OSError:
        pass


def log_raw(payload: dict, now: datetime) -> None:
    if not RAW_LOG.exists():
        return
    record = {"ts": now.isoformat(timespec="milliseconds"), "event": payload.get("hook_event_name"), "payload": payload}
    with open(RAW_LOG, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record, default=str) + "\n")


def main() -> None:
    payload = json.loads(sys.stdin.read())
    now = datetime.now()
    log_raw(payload, now)
    conn = connect()
    try:
        apply_event(conn, payload, now, os.getppid(), os.environ.get("CLAUDE_PROJECT_DIR"))
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - nothing may escape into the session; the log keeps the trace
        log_error()
    sys.exit(0)
