"""Core of the jeli task tracker: the database, matching, stages, and task mutations.

Tasks live in data/tasks.db (SQLite). Top-level tasks that aren't done are the open list, in
priority order; done ones are the archive, in completion order. Subtasks sit under their parent in
their own order. Each change runs in one write transaction and touches only the tasks it needs, so
the CLI, the UI and concurrent Claude sessions can't clobber each other. Set JELI_DATA_DIR to use
another data directory (useful for testing).
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml

DATA_DIR = Path(os.environ.get("JELI_DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_FILE = DATA_DIR / "tasks.db"

KINDS = ["implement", "review", "investigate", "release", "other"]
OPEN_STATUSES = ["todo", "in_progress", "blocked"]
# Stages per kind, each mapped to the status it implies. "other" takes any stage and leaves status alone.
STAGES = {
    "implement": {
        "planning": "in_progress",
        "coding": "in_progress",
        "pr_draft": "in_progress",
        "in_review": "blocked",
        "addressing_feedback": "in_progress",
        "approved": "in_progress",
    },
    "review": {
        "reviewing": "in_progress",
        "waiting_on_author": "blocked",
        "re_review": "in_progress",
    },
    "investigate": {
        "digging": "in_progress",
        "writing_up": "in_progress",
        "waiting_on_info": "blocked",
    },
    "release": {
        "cutting": "in_progress",
        "rc_testing": "in_progress",
        "verifying": "in_progress",
    },
}
FIELD_ORDER = [
    "id", "title", "kind", "status", "stage", "ticket", "pr", "repo", "branch",
    "due", "done_when", "created", "updated", "links", "subtasks",
]
# A subtask inherits these from its parent, so they're never stored on it.
PARENT_ONLY_FIELDS = ("kind", "ticket", "repo")
# Keys accepted in a `changes` dict, besides "links" (a list to append).
SCALAR_FIELDS = ("title", "kind", "status", "stage", "ticket", "pr", "repo", "branch", "due", "done_when")


class TaskError(Exception):
    """A request that breaks a tracker rule. The message is meant to be shown as-is."""


# position orders siblings, gaps allowed. Top-level tasks share one sequence that status splits into
# the open list and the archive; a task moving between the two goes after every other top-level
# task, which is the bottom of the open list or the end of the archive. AUTOINCREMENT keeps ids
# from being reused after a delete.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    title     TEXT,
    kind      TEXT,
    status    TEXT NOT NULL,
    stage     TEXT,
    ticket    TEXT,
    pr        TEXT,
    repo      TEXT,
    branch    TEXT,
    due       TEXT,
    done_when TEXT,
    created   TEXT,
    updated   TEXT
);
CREATE TABLE IF NOT EXISTS task_links (
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    url      TEXT NOT NULL,
    PRIMARY KEY (task_id, position)
);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id, position);
CREATE INDEX IF NOT EXISTS tasks_pr ON tasks(pr);
CREATE INDEX IF NOT EXISTS tasks_ticket ON tasks(ticket);
"""
COLUMNS = ("title", "kind", "status", "stage", "ticket", "pr", "repo", "branch", "due", "done_when", "created", "updated")


# --- Database ---


def connect() -> sqlite3.Connection:
    """Open the database, creating it and its schema if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(write: bool = True) -> Iterator[sqlite3.Connection]:
    """A connection inside one transaction, rolled back if the block raises. A write transaction
    takes the write lock up front; a read one sees a single consistent snapshot."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def fetch(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[dict]:
    """The top-level tasks matching `where`, in position order, each with its links and subtasks."""
    rows = conn.execute(
        f"WITH top AS (SELECT id FROM tasks WHERE parent_id IS NULL AND ({where})) "
        "SELECT * FROM tasks WHERE id IN (SELECT id FROM top) OR parent_id IN (SELECT id FROM top) "
        # Parents sort first, so a subtask's parent is always built before it.
        "ORDER BY parent_id IS NOT NULL, position",
        params,
    ).fetchall()
    links: dict[int, list[str]] = {}
    ids = [row["id"] for row in rows]
    if ids:
        query = f"SELECT task_id, url FROM task_links WHERE task_id IN ({', '.join('?' * len(ids))}) ORDER BY task_id, position"
        for row in conn.execute(query, ids):
            links.setdefault(row["task_id"], []).append(row["url"])
    by_id: dict[int, dict] = {}
    top: list[dict] = []
    for row in rows:
        task = {"id": row["id"]} | {c: row[c] for c in COLUMNS if row[c] is not None}
        if row["id"] in links:
            task["links"] = links[row["id"]]
        by_id[row["id"]] = task
        if row["parent_id"] is None:
            top.append(task)
        else:
            by_id[row["parent_id"]].setdefault("subtasks", []).append(task)
    return top


def get(conn: sqlite3.Connection, task_id: int) -> tuple[dict, dict | None] | None:
    """(task, parent) for a task or subtask; parent is None for a top-level task, and has every subtask."""
    row = conn.execute("SELECT parent_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    if row["parent_id"] is None:
        return fetch(conn, "id = ?", (task_id,))[0], None
    parent = fetch(conn, "id = ?", (row["parent_id"],))[0]
    return next(s for s in parent["subtasks"] if s["id"] == task_id), parent


def matching_id(conn: sqlite3.Connection, ticket: str | None, pr: str | None) -> int | None:
    """Match on PR first (tasks and subtasks), then on ticket (top-level tasks only). Open tasks win
    over archived ones, then priority order."""
    if pr:
        row = conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id WHERE t.pr = ? "
            "ORDER BY COALESCE(p.status, t.status) = 'done', COALESCE(p.position, t.position), "
            "t.parent_id IS NOT NULL, t.position LIMIT 1",
            (pr,),
        ).fetchone()
        if row:
            return row["id"]
    if ticket:
        row = conn.execute(
            "SELECT id FROM tasks WHERE parent_id IS NULL AND ticket = ? ORDER BY status = 'done', position LIMIT 1",
            (ticket,),
        ).fetchone()
        if row:
            return row["id"]
    return None


def bottom(conn: sqlite3.Connection, parent_id: int | None) -> int:
    """A position after every sibling under parent_id (None for top-level tasks)."""
    return conn.execute(
        "SELECT COALESCE(MAX(position) + 1, 0) FROM tasks WHERE parent_id IS ?", (parent_id,)
    ).fetchone()[0]


def values(task: dict) -> tuple:
    """The task's column values; empty strings clear a field, so they're stored as NULL."""
    return tuple(task.get(c) if task.get(c) != "" else None for c in COLUMNS)


def insert(conn: sqlite3.Connection, task: dict, parent_id: int | None, position: int) -> None:
    """Store a new task, under its own id if it has one, else the next one, which is set on task."""
    cursor = conn.execute(
        f"INSERT INTO tasks (id, parent_id, position, {', '.join(COLUMNS)}) VALUES ({', '.join('?' * (len(COLUMNS) + 3))})",
        (task.get("id"), parent_id, position, *values(task)),
    )
    task["id"] = cursor.lastrowid
    save_links(conn, task)


def save(conn: sqlite3.Connection, task: dict) -> None:
    """Store an existing task's fields and links, not its subtasks. A top-level task that became done,
    or stopped being done, goes to the end of the archive or the bottom of the open list."""
    row = conn.execute("SELECT parent_id, status FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    conn.execute(f"UPDATE tasks SET {', '.join(f'{c} = ?' for c in COLUMNS)} WHERE id = ?", (*values(task), task["id"]))
    if row["parent_id"] is None and (row["status"] == "done") != (task.get("status") == "done"):
        conn.execute("UPDATE tasks SET position = ? WHERE id = ?", (bottom(conn, None), task["id"]))
    save_links(conn, task)


def save_links(conn: sqlite3.Connection, task: dict) -> None:
    conn.execute("DELETE FROM task_links WHERE task_id = ?", (task["id"],))
    conn.executemany(
        "INSERT INTO task_links (task_id, position, url) VALUES (?, ?, ?)",
        [(task["id"], i, url) for i, url in enumerate(task.get("links") or [])],
    )


def reorder(conn: sqlite3.Connection, task: dict, parent: dict | None, position: dict) -> None:
    """Place task (see `place`) among its parent's subtasks, or among the open top-level tasks."""
    if parent is not None:
        rows = conn.execute("SELECT id FROM tasks WHERE parent_id = ? ORDER BY position", (parent["id"],))
        scope = f"subtasks of #{parent['id']}"
    else:
        rows = conn.execute("SELECT id FROM tasks WHERE parent_id IS NULL AND status != 'done' ORDER BY position")
        scope = "open top-level tasks"
    siblings = [{"id": row["id"]} for row in rows]
    place(siblings, next(s for s in siblings if s["id"] == task["id"]), position, scope)
    conn.executemany("UPDATE tasks SET position = ? WHERE id = ?", [(i, s["id"]) for i, s in enumerate(siblings)])


# --- Reading ---


def read_tasks() -> tuple[list[dict], list[dict]]:
    """The open tasks in priority order and the archive in completion order."""
    with transaction(write=False) as conn:
        tasks = fetch(conn, "1")
    return [t for t in tasks if t["status"] != "done"], [t for t in tasks if t["status"] == "done"]


def list_tasks(archived: bool = False) -> list[dict]:
    """The open tasks in priority order, or the archive in completion order."""
    with transaction(write=False) as conn:
        return fetch(conn, "status = 'done'" if archived else "status != 'done'")


def get_task(task_id: int) -> tuple[dict, dict | None] | None:
    """(task, parent) for a task or subtask, or None; parent is None for a top-level task."""
    with transaction(write=False) as conn:
        return get(conn, task_id)


def find_task(ticket: str | None, pr: str | None) -> tuple[dict, dict | None] | None:
    """(task, parent) for the task `add` would match on this ticket or PR, or None."""
    with transaction(write=False) as conn:
        task_id = matching_id(conn, ticket, pr)
        return get(conn, task_id) if task_id is not None else None


# --- Rules ---


def ordered(task: dict) -> dict:
    if task.get("subtasks"):
        task["subtasks"] = [ordered(s) for s in task["subtasks"]]
    known = {k: task[k] for k in FIELD_ORDER if task.get(k) not in (None, [], "")}
    extra = {k: v for k, v in task.items() if k not in FIELD_ORDER}
    return known | extra


def today() -> str:
    return date.today().isoformat()


def emit(prefix: str, task: dict) -> None:
    if prefix:
        print(prefix)
    print(yaml.safe_dump(ordered(task), sort_keys=False, allow_unicode=True, width=1000), end="")


def resolve_status(kind: str | None, stage: str | None, status: str | None) -> str | None:
    """Validate stage against kind and return the status to set, if any."""
    if stage is None:
        return status
    if kind not in STAGES:
        return status
    if stage not in STAGES[kind]:
        raise TaskError(f"stage '{stage}' isn't valid for {kind} tasks; use one of: {', '.join(STAGES[kind])}")
    derived = STAGES[kind][stage]
    if status is not None and status != derived:
        raise TaskError(f"stage '{stage}' implies status {derived}, not {status}; pass only --stage")
    return derived


def apply_changes(task: dict, changes: dict, kind: str | None, is_subtask: bool = False) -> None:
    stage = changes.get("stage")
    status = resolve_status(kind, stage, changes.get("status"))
    for field in ("title", "kind", "ticket", "pr", "repo", "branch", "due", "done_when"):
        value = changes.get(field)
        if value is not None and not (is_subtask and field in PARENT_ONLY_FIELDS):
            task[field] = value
    if stage is not None:
        task["stage"] = stage
    if status is not None:
        task["status"] = status
        current = task.get("stage")
        # A status set on its own drops a stage that implies something else, so the two never disagree.
        if status == "done" or (stage is None and STAGES.get(kind, {}).get(current, status) != status):
            task.pop("stage", None)
    for link in changes.get("links") or []:
        task.setdefault("links", [])
        if link not in task["links"]:
            task["links"].append(link)
    task["updated"] = today()


def normalize(changes: dict) -> dict:
    if changes.get("ticket"):
        changes["ticket"] = changes["ticket"].upper()
    return changes


def open_subtasks(task: dict) -> list[dict]:
    return [s for s in task.get("subtasks", []) if s.get("status") != "done"]


def reopen(task: dict) -> None:
    if task.get("status") == "done":
        task["status"] = "in_progress"
        task["updated"] = today()


def subtask_hint(parent: dict) -> str:
    remaining = open_subtasks(parent)
    if remaining:
        return f"#{parent['id']} has {len(remaining)} open subtask(s) left"
    return f"all subtasks of #{parent['id']} are done; close it if no more work is expected"


def place(siblings: list[dict], task: dict, position: dict, scope: str) -> None:
    """Move task within siblings to position: {"top": True}, {"bottom": True}, {"before": id} or {"after": id}."""
    anchor_id = position.get("before", position.get("after"))
    if anchor_id == task["id"]:
        return
    if anchor_id is not None and not any(t["id"] == anchor_id for t in siblings):
        raise TaskError(f"#{anchor_id} isn't among the {scope}")
    siblings.remove(task)
    if position.get("top"):
        siblings.insert(0, task)
    elif anchor_id is None:
        siblings.append(task)
    else:
        anchor = next(t for t in siblings if t["id"] == anchor_id)
        siblings.insert(siblings.index(anchor) + (0 if "before" in position else 1), task)


# --- Changes ---


def add_subtask(conn: sqlite3.Connection, parent: dict, changes: dict) -> tuple[dict, list[str]]:
    """Add a subtask under parent. If parent still carries its own PR, promote it to a subtask first.
    The caller saves parent, whose PR, stage and branch may have moved."""
    messages = []
    subtasks = parent.setdefault("subtasks", [])
    if parent.get("pr") and not subtasks:
        promoted = {
            "title": parent["pr"],
            "status": parent.get("status", "in_progress"),
            "stage": parent.pop("stage", None),
            "pr": parent.pop("pr"),
            "branch": parent.pop("branch", None),
            "created": parent.get("created", today()),
            "updated": today(),
        }
        insert(conn, promoted, parent["id"], bottom(conn, parent["id"]))
        subtasks.append(promoted)
        # The stage belonged to the promoted PR; the parent is just in progress now.
        parent["status"] = "in_progress"
        messages.append(f"moved {promoted['pr']} from #{parent['id']} into subtask #{promoted['id']}")
    sub = {"created": today(), "status": "in_progress"}
    apply_changes(sub, changes, kind=parent.get("kind"), is_subtask=True)
    insert(conn, sub, parent["id"], bottom(conn, parent["id"]))
    subtasks.append(sub)
    messages.insert(0, f"added #{sub['id']} as subtask of #{parent['id']}")
    return sub, messages


def add_task(changes: dict, parent_id: int | None = None) -> tuple[dict, list[str]]:
    """Add a task or subtask, or update the existing one that matches its PR/ticket."""
    changes = normalize(dict(changes))
    with transaction() as conn:
        messages = []
        match_id = matching_id(conn, changes.get("ticket"), changes.get("pr"))
        match = get(conn, match_id) if match_id is not None else None

        if parent_id is not None:
            found = get(conn, parent_id)
            if found is None:
                raise TaskError(f"no task #{parent_id}")
            parent, grandparent = found
            if grandparent is not None:
                raise TaskError(f"#{parent['id']} is a subtask of #{grandparent['id']}; subtasks can't have subtasks")
        elif (
            match
            and match[1] is None
            and changes.get("pr")
            and match[0].get("pr") != changes["pr"]
            and (match[0].get("pr") or match[0].get("subtasks"))
        ):
            # Ticket matched a task, but this is a different PR for it: that's a new subtask.
            parent = match[0]
            match = None
        else:
            parent = None

        if match:
            task, owner = match
            messages.append(f"updated #{task['id']} (matched existing {'subtask' if owner else 'task'})")
            changes["title"] = changes["kind"] = None
            if owner:
                reopen(owner)
                save(conn, owner)
            reopen(task)
            apply_changes(task, changes, kind=(owner or task).get("kind"), is_subtask=owner is not None)
            save(conn, task)
        elif parent is not None:
            reopen(parent)
            task, sub_messages = add_subtask(conn, parent, changes)
            save(conn, parent)
            messages += sub_messages
        else:
            if not changes.get("kind"):
                raise TaskError("--kind is required for a top-level task")
            task = {"created": today(), "status": "in_progress"}
            apply_changes(task, changes, kind=changes["kind"])
            insert(conn, task, None, bottom(conn, None))
            messages.append(f"added #{task['id']}")
    return task, messages


def update_task(task_id: int, changes: dict, position: dict | None = None) -> tuple[dict, list[str]]:
    """Change fields on a task or subtask, then optionally place it (see `place`) among its open siblings."""
    changes = normalize(dict(changes))
    with transaction() as conn:
        found = get(conn, task_id)
        if found is None:
            raise TaskError(f"no task #{task_id}")
        task, parent = found
        if parent is None and changes.get("status") == "done" and (remaining := open_subtasks(task)):
            ids = ", ".join(f"#{s['id']}" for s in remaining)
            raise TaskError(f"#{task['id']} still has open subtasks: {ids}. Close them first.")
        kind = parent.get("kind") if parent else (changes.get("kind") or task.get("kind"))
        apply_changes(task, changes, kind=kind, is_subtask=parent is not None)
        save(conn, task)
        if parent is not None and task.get("status") != "done" and parent.get("status") == "done":
            reopen(parent)
            save(conn, parent)
        if position and task.get("status") != "done":
            reorder(conn, task, parent, position)
    message = f"updated #{task['id']}"
    if parent is None and task.get("status") == "done":
        message += " (archived)"
    messages = [message]
    if parent is not None and task.get("status") == "done":
        messages.append(subtask_hint(parent))
    return task, messages


def delete_task(task_id: int, with_subtasks: bool = False) -> tuple[dict, list[str]]:
    """Remove a task or subtask, open or archived, for good. Ids are never reused.

    A task with subtasks is only deleted along with them, and only when with_subtasks is set.
    """
    with transaction() as conn:
        found = get(conn, task_id)
        if found is None:
            raise TaskError(f"no task #{task_id}")
        task, parent = found
        if parent is None and task.get("subtasks") and not with_subtasks:
            ids = ", ".join(f"#{s['id']}" for s in task["subtasks"])
            raise TaskError(f"#{task_id} has subtasks ({ids}); pass --with-subtasks to delete them too")
        # Subtasks and links go with it (ON DELETE CASCADE).
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if parent is not None:
            parent["subtasks"].remove(task)
            if not parent["subtasks"]:
                parent.pop("subtasks")
            parent["updated"] = today()
            save(conn, parent)
    messages = [f"deleted #{task_id}"]
    if parent is not None and parent.get("status") != "done" and parent.get("subtasks"):
        messages.append(subtask_hint(parent))
    return task, messages


def move_task(task_id: int, position: dict) -> None:
    """Reorder an open task or subtask among its siblings; see `place` for position."""
    with transaction() as conn:
        found = get(conn, task_id)
        if found is None or (found[1] or found[0]).get("status") == "done":
            raise TaskError(f"no open task #{task_id}")
        reorder(conn, *found, position)
