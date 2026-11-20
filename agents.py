"""Core of the jeli agent tracker: one row per Claude Code session, kept current by agent_hook.py.

Claude Code runs the hook on every hook event (see ~/.claude/settings.json), and the hook hands the
payload to apply_event, which moves the session through these states:

    idle        session started, no prompt yet
    working     running a turn
    blocked     waiting on a permission prompt or an AskUserQuestion
    your_turn   turn over, nothing running in the background
    waiting_bg  turn over, but a background subagent is still running (shells and crons don't count)
    ended       SessionEnd fired

On read, attach_tasks adjusts two states, neither of which is stored:
- a your_turn session whose task is blocked (in review, waiting on its author, waiting on info)
  shows as waiting_others: its turn is over, but the next move is someone else's
- any other your_turn session left alone for IDLE_AFTER shows as idle: I've walked away from it

blocked and your_turn both need me; needs_you_since says when the session last moved into one of
them from any other state, so going from a prompt straight to your_turn keeps the time it started.

Hooks run async, so several hook processes for one session can overlap. Every event is applied in
one BEGIN IMMEDIATE transaction, and transitions that depend on a specific tool call match it by
tool_use_id instead of trusting arrival order. Standard library only: the hook runs on the system
Python 3.9, which has no pyyaml. Set JELI_DATA_DIR to use another data directory, like store.py.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("JELI_DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_FILE = DATA_DIR / "agents.db"

STATES = ("idle", "working", "blocked", "your_turn", "waiting_bg", "ended")
NEEDS_YOU = ("blocked", "your_turn")
# Ended sessions are deleted this long after they end, so a task can still offer to resume the
# last session that worked on it. Claude Code deletes transcripts after 30 days by default
# (cleanupPeriodDays), and a session can't be resumed without one.
RETENTION = timedelta(days=30)
# Ended sessions are listed as agents for this long after they end.
RECENTLY_ENDED = timedelta(hours=24)
# A /clear ends one session and starts another in the same cwd; the new one inherits the task link
# if the old one ended at most this long before.
CLEAR_LINK_WINDOW = timedelta(seconds=30)
# A your_turn session untouched this long shows as idle.
IDLE_AFTER = timedelta(hours=2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    state          TEXT NOT NULL,
    waiting_kind   TEXT,
    waiting_text   TEXT,
    waiting_since  TEXT,
    waiting_tool_use_id TEXT,
    inflight       TEXT NOT NULL DEFAULT '{}',
    task_id        INTEGER,
    cwd            TEXT,
    repo           TEXT,
    branch         TEXT,
    prompt         TEXT,
    last_tool      TEXT,
    last_message   TEXT,
    background     TEXT NOT NULL DEFAULT '[]',
    claude_pid     INTEGER,
    started        TEXT NOT NULL,
    state_since    TEXT NOT NULL,
    needs_you_since TEXT,
    last_seen      TEXT NOT NULL,
    ended_reason   TEXT,
    name           TEXT,
    project_dir    TEXT
)
"""


def connect() -> sqlite3.Connection:
    """Open the database, creating it (owner-only) and its schema if needed."""
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The database, its -wal and -shm files hold prompts and commands, so keep them owner-only.
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        add_missing_columns(conn)
    finally:
        os.umask(old_umask)
    return conn


# Columns added after the table was first created, with how to fill them in for existing rows. A
# session already waiting on me gets its state_since as needs_you_since, the best guess at when it
# started waiting; a name arrives with the session's next prompt.
ADDED_COLUMNS = {
    "needs_you_since": (
        f"UPDATE sessions SET needs_you_since = state_since WHERE state IN ({', '.join(repr(s) for s in NEEDS_YOU)})"
    ),
    "name": None,
    "project_dir": None,
}


def add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older version up to SCHEMA."""

    def missing() -> list[str]:
        present = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        return [c for c in ADDED_COLUMNS if c not in present]

    if not missing():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Checked again under the lock: another hook process may have added them meanwhile.
        for column in missing():
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
            if ADDED_COLUMNS[column]:
                conn.execute(ADDED_COLUMNS[column])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def clip(text: str | None, limit: int = 300) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(name: str | None, tool_input: dict | None) -> str:
    """One line saying what a tool call does, e.g. "Bash: run the backend tests" or "Edit: store.py"."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        detail = tool_input.get("description") or tool_input.get("command")
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        detail = Path(path).name if path else None
    elif name in ("Grep", "Glob"):
        detail = tool_input.get("pattern")
    elif name == "Agent":
        detail = tool_input.get("description")
    elif name == "Skill":
        detail = tool_input.get("skill")
    elif name == "AskUserQuestion":
        detail = question_text(tool_input)
    else:
        detail = None
    return clip(f"{name}: {detail}" if detail else name or "tool", 120)


def question_text(tool_input: dict) -> str | None:
    questions = tool_input.get("questions") or []
    return questions[0].get("question") if questions and isinstance(questions[0], dict) else None


def git_info(cwd: str | None) -> tuple[str | None, str | None]:
    """(repo as owner/name, branch) for cwd, or Nones outside a git checkout."""
    if not cwd or not Path(cwd).is_dir():
        return None, None

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", cwd, *args], check=False, capture_output=True, text=True, timeout=2
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    remote = git("remote", "get-url", "origin") or ""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote)
    return (match.group(1) if match else None), (branch if branch != "HEAD" else None)


JELI_LINKING_COMMAND = re.compile(r"jeli\.py\s+(add|update)\b")
JELI_TASK_ID = re.compile(r"^id: (\d+)$", re.MULTILINE)


def linked_task(tool_input: dict, tool_response) -> int | None:
    """The task id when a Bash call ran `jeli.py add` or `jeli.py update`, read from the task it prints.
    A fallback: jeli.py links the session itself through CLAUDE_CODE_SESSION_ID, which survives the
    output being piped through tail or grep."""
    if not JELI_LINKING_COMMAND.search(str(tool_input.get("command", ""))):
        return None
    stdout = tool_response.get("stdout", "") if isinstance(tool_response, dict) else str(tool_response or "")
    match = JELI_TASK_ID.search(stdout)
    return int(match.group(1)) if match else None


def input_key(tool_input) -> str:
    """Identifies a tool call's input, so a PermissionRequest (which has no tool_use_id) can be matched
    to the PreToolUse that preceded it."""
    return json.dumps(tool_input, sort_keys=True, default=str)


COLUMNS = (
    "state", "waiting_kind", "waiting_text", "waiting_since", "waiting_tool_use_id", "inflight",
    "task_id", "cwd", "repo", "branch", "prompt", "last_tool", "last_message", "background",
    "claude_pid", "started", "state_since", "needs_you_since", "last_seen", "ended_reason", "name", "project_dir",
)


class Session:
    """A session row being changed by one event. JSON columns are decoded for the duration."""

    def __init__(self, row: dict, now: str):
        self.row = row
        self.now = now
        self.inflight = json.loads(row["inflight"])

    def __getitem__(self, key):
        return self.row[key]

    def __setitem__(self, key, value):
        self.row[key] = value

    def set_state(self, state: str) -> None:
        if self.row["state"] == state:
            return
        if state not in NEEDS_YOU:
            self.row["needs_you_since"] = None
        elif self.row["state"] not in NEEDS_YOU:
            self.row["needs_you_since"] = self.now
        self.row["state"] = state
        self.row["state_since"] = self.now

    def block(self, kind: str, text: str | None, tool_use_id: str | None) -> None:
        self.row.update(waiting_kind=kind, waiting_text=clip(text), waiting_tool_use_id=tool_use_id)
        if self.row["state"] != "blocked":
            self.row["waiting_since"] = self.now
        self.set_state("blocked")

    def unblock(self) -> None:
        self.row.update(waiting_kind=None, waiting_text=None, waiting_since=None, waiting_tool_use_id=None)

    def locate(self, cwd: str | None) -> None:
        self.row["cwd"] = cwd or self.row["cwd"]
        self.row["repo"], self.row["branch"] = git_info(self.row["cwd"])

    def encoded(self) -> list:
        self.row["inflight"] = json.dumps(self.inflight)
        return [self.row[c] for c in COLUMNS]


def new_row(session_id: str, now: str, claude_pid: int | None) -> dict:
    row = dict.fromkeys(COLUMNS)
    row.update(
        session_id=session_id, state="idle", inflight="{}", background="[]", claude_pid=claude_pid,
        started=now, state_since=now, last_seen=now,
    )
    return row


def apply_event(
    conn: sqlite3.Connection, payload: dict, now: datetime, claude_pid: int | None, project_dir: str | None = None
) -> None:
    """Apply one hook payload to its session's row. Unknown events only refresh last_seen.

    project_dir is the directory Claude Code was started in (CLAUDE_PROJECT_DIR), which a session
    can only be resumed from: Claude Code files its transcript under it. Unlike cwd, it doesn't
    follow the session into subdirectories or worktrees. Without it, a new session's first cwd
    stands in."""
    session_id = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not session_id or not event:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        found = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if found is None and event == "SessionEnd":
            conn.execute("COMMIT")
            return
        if found is not None and found["state"] == "ended" and event != "SessionStart":
            # Stragglers after SessionEnd, e.g. a late SubagentStop, don't revive the session.
            conn.execute("COMMIT")
            return
        session = Session(dict(found) if found else new_row(session_id, stamp(now), claude_pid), stamp(now))
        if found is None:
            session.locate(payload.get("cwd"))
            session["project_dir"] = payload.get("cwd")
        session["project_dir"] = project_dir or session["project_dir"]
        HANDLERS.get(event, lambda s, p, c: None)(session, payload, conn)
        session["last_seen"] = stamp(now)
        # Only sessions I've named (/rename) carry a title, on SessionStart and UserPromptSubmit.
        if payload.get("session_title"):
            session["name"] = clip(payload["session_title"], 80)
        # A resumed session runs in a new claude process, so the pid follows the latest event.
        session["claude_pid"] = claude_pid or session["claude_pid"]
        values = session.encoded()
        conn.execute(
            f"INSERT INTO sessions (session_id, {', '.join(COLUMNS)}) VALUES (?{', ?' * len(COLUMNS)}) "
            f"ON CONFLICT(session_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in COLUMNS)}",
            [session_id, *values],
        )
        if event == "SessionStart":
            cutoff = stamp(now - RETENTION)
            conn.execute("DELETE FROM sessions WHERE state = 'ended' AND last_seen < ?", (cutoff,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# Handlers take (session, payload, conn). Events from a subagent carry an agent_id and run under the
# parent's session_id; they never change the parent's state, except that a permission prompt raised
# inside a subagent still blocks on the user.


def on_session_start(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    s["ended_reason"] = None
    # Compacting carries on the same conversation, mid-turn or between turns, so the state stands.
    if p.get("source") != "compact":
        s.set_state("idle")
    s.locate(p.get("cwd"))
    if p.get("source") == "clear" and s["task_id"] is None:
        since = stamp(datetime.fromisoformat(s.now) - CLEAR_LINK_WINDOW)
        previous = conn.execute(
            "SELECT task_id FROM sessions WHERE cwd = ? AND state = 'ended' AND ended_reason = 'clear' "
            "AND last_seen >= ? AND session_id != ? ORDER BY last_seen DESC LIMIT 1",
            (s["cwd"], since, s.row["session_id"]),
        ).fetchone()
        if previous is not None:
            s["task_id"] = previous["task_id"]


def on_user_prompt(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    prompt = p.get("prompt") or ""
    # A finished background task re-invokes Claude through a <task-notification> pseudo-prompt; that
    # isn't something the user typed, so the prompt line keeps the last real one.
    if not prompt.lstrip().startswith("<task-notification>"):
        s["prompt"] = clip(prompt)
    # Typing a reply also answers (rejects) a pending permission prompt, with no tool event for it.
    s.unblock()
    s.inflight = {}
    # Background work is only reported by Stop, so whatever the last one reported is stale now.
    s["background"] = "[]"
    s.set_state("working")


def on_pre_tool(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    tool_use_id = p.get("tool_use_id")
    if tool_use_id:
        s.inflight[tool_use_id] = input_key(p.get("tool_input"))
    if p.get("agent_id"):
        return
    s["last_tool"] = describe_tool(p.get("tool_name"), p.get("tool_input"))
    if s["state"] != "blocked":
        s.set_state("working")


def on_permission_request(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    key = input_key(p.get("tool_input"))
    # The most recent in-flight call with the same input is the one being asked about.
    tool_use_id = next((t for t, k in reversed(list(s.inflight.items())) if k == key), None)
    tool_name, tool_input = p.get("tool_name"), p.get("tool_input") or {}
    if tool_name == "AskUserQuestion":
        s.block("question", question_text(tool_input), tool_use_id)
    else:
        s.block("permission", describe_tool(tool_name, tool_input), tool_use_id)


def on_notification(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    # PermissionRequest normally gets here first; this only covers a missed one.
    if p.get("notification_type") == "permission_prompt" and s["state"] != "blocked":
        s.block("permission", p.get("message"), None)


def on_post_tool(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    tool_use_id = p.get("tool_use_id")
    s.inflight.pop(tool_use_id, None)
    if p.get("tool_name") == "Bash":
        task_id = linked_task(p.get("tool_input") or {}, p.get("tool_response"))
        if task_id is not None:
            s["task_id"] = task_id
    if s["state"] == "blocked":
        # Only the call that was blocked unblocks the session; a fallback block with no known call
        # is cleared by the next top-level tool finishing.
        waiting_on = s["waiting_tool_use_id"]
        if tool_use_id == waiting_on or (waiting_on is None and not p.get("agent_id")):
            s.unblock()
            # A background subagent can block after the turn ended; approving it goes back to waiting.
            ended_turn = p.get("agent_id") and waits_on_background(json.loads(s["background"]))
            s.set_state("waiting_bg" if ended_turn else "working")
    elif not p.get("agent_id"):
        s.set_state("working")


def on_stop(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    s["last_message"] = clip(p.get("last_assistant_message"))
    running = [
        {"type": t.get("type"), "description": clip(t.get("description"), 120)}
        for t in p.get("background_tasks") or []
        if t.get("status") == "running"
    ]
    running += [{"type": "cron", "description": clip(str(c), 120)} for c in p.get("session_crons") or []]
    s["background"] = json.dumps(running)
    s.unblock()
    s.inflight = {}
    # The payload's cwd is current; CwdChanged doesn't fire when the shell is reset to the start dir.
    s.locate(p.get("cwd"))
    s.set_state("waiting_bg" if waits_on_background(running) else "your_turn")


def waits_on_background(background: list[dict]) -> bool:
    """Whether the session will pick up again on its own. Only a subagent always finishes and wakes
    it; a background shell may be a server that never exits, and crons are recurring jobs I set up,
    so those are listed on the session but leave it my turn."""
    return any(t.get("type") == "subagent" for t in background)


def on_stop_failure(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    s["last_message"] = clip(p.get("error") or p.get("last_assistant_message") or "turn failed")
    s.unblock()
    s.inflight = {}
    s.set_state("your_turn")


def on_cwd_changed(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    s.locate(p.get("new_cwd"))


def on_session_end(s: Session, p: dict, conn: sqlite3.Connection) -> None:
    s["ended_reason"] = p.get("reason")
    s["background"] = "[]"
    s.unblock()
    s.inflight = {}
    s.set_state("ended")


# Read side: what the CLI and the board show.

# Most urgent first: blocked on me, my turn, then everything that can run without me.
STATE_ORDER = {"blocked": 0, "your_turn": 1, "waiting_bg": 2, "working": 3, "idle": 4, "ended": 5}


def process_alive(pid: int | None) -> bool:
    if not pid:
        return True  # unknown pid: assume alive rather than end a session on a guess
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(conn: sqlite3.Connection, now: datetime) -> None:
    """End sessions whose claude process is gone (crashed or killed, so no SessionEnd), and delete
    sessions that ended longer than RETENTION ago."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute("SELECT session_id, claude_pid FROM sessions WHERE state != 'ended'").fetchall():
            if not process_alive(row["claude_pid"]):
                conn.execute(
                    "UPDATE sessions SET state = 'ended', ended_reason = 'gone', state_since = ?, "
                    "waiting_kind = NULL, waiting_text = NULL, waiting_since = NULL, waiting_tool_use_id = NULL "
                    "WHERE session_id = ?",
                    (stamp(now), row["session_id"]),
                )
        conn.execute("DELETE FROM sessions WHERE state = 'ended' AND last_seen < ?", (stamp(now - RETENTION),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def list_sessions(conn: sqlite3.Connection, now: datetime, ended_within: timedelta | None = None) -> list[dict]:
    """Sessions in board order: by STATE_ORDER, then longest in that state first. Ended sessions are
    left out, or with ended_within, kept if they ended at most that long ago."""
    reap(conn, now)
    rows = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
    for row in rows:
        row["background"] = json.loads(row["background"])
        del row["inflight"]
    cutoff = stamp(now - ended_within) if ended_within is not None else None
    rows = [r for r in rows if r["state"] != "ended" or (cutoff is not None and r["last_seen"] >= cutoff)]
    return sorted(rows, key=lambda r: (STATE_ORDER.get(r["state"], 9), r["waiting_since"] or r["state_since"]))


def find_session(conn: sqlite3.Connection, prefix: str) -> str:
    """The full session id starting with prefix; raises LookupError if none or several match."""
    ids = [r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ?", (prefix + "%",))]
    if len(ids) != 1:
        raise LookupError(f"no session matches {prefix!r}" if not ids else f"{prefix!r} matches {len(ids)} sessions")
    return ids[0]


def link(conn: sqlite3.Connection, session_id: str, task_id: int | None) -> None:
    conn.execute("UPDATE sessions SET task_id = ? WHERE session_id = ?", (task_id, session_id))


def forget(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def attach_tasks(sessions: list[dict], open_tasks: list[dict], done_tasks: list[dict], now: datetime) -> None:
    """Add a "task" summary to each session, from its explicit link or, failing that, from the open
    task or subtask whose branch is the session's branch (task_source says which).

    Then adjust your_turn sessions, which clears needs_you_since since nothing is waiting on me:
    on a blocked task one becomes waiting_others, and once the task moves on it's your_turn again
    with its original needs_you_since; otherwise one whose turn ended IDLE_AFTER ago becomes idle.
    A blocked session keeps its state however old: it's stuck on me."""
    by_id, by_branch = {}, {}
    for tasks, is_open in ((done_tasks, False), (open_tasks, True)):
        for task in tasks:
            for item, parent in [(task, None)] + [(sub, task) for sub in task.get("subtasks", [])]:
                summary = {
                    "id": item["id"],
                    "title": item.get("title"),
                    "kind": (parent or item).get("kind"),
                    "status": item.get("status"),
                    "stage": item.get("stage"),
                    "ticket": (parent or item).get("ticket"),
                    "pr": item.get("pr"),
                    "parent": {"id": parent["id"], "title": parent.get("title")} if parent else None,
                }
                by_id[item["id"]] = summary
                if is_open and item.get("branch") and item.get("status") != "done":
                    by_branch.setdefault(item["branch"], summary)
    for session in sessions:
        if session["task_id"] is not None:
            session["task"], session["task_source"] = by_id.get(session["task_id"]), "link"
        elif session.get("branch") in by_branch:
            session["task"], session["task_source"] = by_branch[session["branch"]], "branch"
        else:
            session["task"], session["task_source"] = None, None
        if session["state"] != "your_turn":
            continue
        if (session["task"] or {}).get("status") == "blocked":
            session["state"], session["needs_you_since"] = "waiting_others", None
        elif session["state_since"] < stamp(now - IDLE_AFTER):
            session["state"], session["needs_you_since"] = "idle", None


def last_agent_by_task(sessions: list[dict]) -> dict[int, dict]:
    """For each task with an ended session attached, the session that ended most recently: enough
    for the board to offer resuming it."""
    last: dict[int, dict] = {}
    for s in sessions:
        task = s.get("task")
        if s["state"] != "ended" or not task:
            continue
        if task["id"] not in last or s["last_seen"] > last[task["id"]]["ended"]:
            last[task["id"]] = {
                "session_id": s["session_id"], "project_dir": s["project_dir"], "cwd": s["cwd"], "ended": s["last_seen"],
            }
    return last


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PreToolUse": on_pre_tool,
    "PermissionRequest": on_permission_request,
    "Notification": on_notification,
    "PostToolUse": on_post_tool,
    "PostToolUseFailure": on_post_tool,
    "PermissionDenied": on_post_tool,
    "Stop": on_stop,
    "StopFailure": on_stop_failure,
    "CwdChanged": on_cwd_changed,
    "SessionEnd": on_session_end,
}
