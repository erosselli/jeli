#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Command line for the jeli task tracker. The data and its rules live in store.py."""

import argparse
import os
import sys
from datetime import date, datetime

import agents
from server import serve
from store import (
    KINDS,
    OPEN_STATUSES,
    SCALAR_FIELDS,
    STAGES,
    TaskError,
    add_task,
    delete_task,
    emit,
    find_task,
    get_task,
    list_tasks,
    move_task,
    read_tasks,
    update_task,
)


def changes_from(args: argparse.Namespace) -> dict:
    changes = {field: getattr(args, field, None) for field in SCALAR_FIELDS}
    changes["links"] = getattr(args, "link", None)
    return changes


def link_session(task_id: int) -> None:
    """Link the Claude Code session running this command to the task it added or updated, for the
    agent tracker. Claude Code sets CLAUDE_CODE_SESSION_ID in every command it runs; outside Claude
    Code this does nothing."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return
    conn = agents.connect()
    try:
        agents.link(conn, session_id, task_id)
    finally:
        conn.close()


def cmd_add(args: argparse.Namespace) -> None:
    task, messages = add_task(changes_from(args), parent_id=args.parent)
    link_session(task["id"])
    emit("\n".join(messages), task)


def cmd_update(args: argparse.Namespace) -> None:
    task, messages = update_task(args.id, changes_from(args))
    link_session(task["id"])
    emit("\n".join(messages), task)


def cmd_done(args: argparse.Namespace) -> None:
    args.status = "done"
    cmd_update(args)


def cmd_delete(args: argparse.Namespace) -> None:
    # The removed task is printed in full, so a mistaken delete can be re-added by hand.
    task, messages = delete_task(args.id, with_subtasks=args.with_subtasks)
    emit("\n".join(messages), task)


def cmd_move(args: argparse.Namespace) -> None:
    if args.top or args.bottom:
        position = {"top": True} if args.top else {"bottom": True}
    else:
        position = {"before": args.before} if args.before is not None else {"after": args.after}
    move_task(args.id, position)
    cmd_list(argparse.Namespace(done=False, status=None, kind=None))


def format_line(t: dict, indent: str = "") -> str:
    refs = " ".join(str(t[k]) for k in ("ticket", "pr") if t.get(k))
    due = f" due {t['due']}" if t.get("due") else ""
    kind = f" {t['kind']}:" if t.get("kind") else ""
    state = f"{t.get('status')}/{t['stage']}" if t.get("stage") else t.get("status")
    return f"{indent}#{t['id']} [{state}]{kind} {t.get('title')}  {refs}{due}".rstrip()


def cmd_list(args: argparse.Namespace) -> None:
    tasks = list_tasks(archived=args.done)
    if args.status:
        tasks = [t for t in tasks if t.get("status") == args.status]
    if args.kind:
        tasks = [t for t in tasks if t.get("kind") == args.kind]
    if not tasks:
        print("no tasks")
    for t in tasks:
        print(format_line(t))
        for sub in t.get("subtasks", []):
            print(format_line(sub, indent="    "))


def cmd_find(args: argparse.Namespace) -> None:
    found = find_task(args.ticket, args.pr)
    if found is None:
        print("no match")
        return
    task, parent = found
    label = "done" if (parent or task).get("status") == "done" else "open"
    emit(f"{label}:" + (f" subtask of #{parent['id']}" if parent else ""), task)


def cmd_show(args: argparse.Namespace) -> None:
    found = get_task(args.id)
    if found is None:
        sys.exit(f"no task #{args.id}")
    task, parent = found
    emit(f"subtask of #{parent['id']}: {parent.get('title')}" if parent else "", task)


def ago(since: str | None, now: datetime) -> str:
    if not since:
        return "?"
    minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes < 24 * 60 else f"{minutes // (24 * 60)}d"


def agent_activity(s: dict) -> str:
    """The second line of an agent in `jeli agents`: what it's doing or waiting on."""
    if s["state"] == "blocked":
        return f"{s['waiting_kind']}: {s['waiting_text'] or '?'}"
    if s["state"] == "waiting_bg":
        return "waiting on: " + "; ".join(t["description"] or t["type"] for t in s["background"])
    if s["state"] in ("your_turn", "waiting_others"):
        # Shells and crons left running don't make it the agent's turn, but are worth knowing about.
        running = "; ".join(t["description"] or t["type"] for t in s["background"])
        return (f"[running: {running}] " if running else "") + (s["last_message"] or "")
    if s["state"] == "ended":
        return f"ended ({s['ended_reason'] or '?'})"
    return s["last_tool"] or s["prompt"] or ""


def cmd_agents(args: argparse.Namespace) -> None:
    conn = agents.connect()
    try:
        if args.agents_command in ("link", "unlink", "forget"):
            session_id = agents.find_session(conn, args.session)
            if args.agents_command == "link":
                if get_task(args.task) is None:
                    raise TaskError(f"no task #{args.task}")
                agents.link(conn, session_id, args.task)
                print(f"linked {session_id[:8]} to #{args.task}")
            elif args.agents_command == "unlink":
                agents.link(conn, session_id, None)
                print(f"unlinked {session_id[:8]}")
            else:
                agents.forget(conn, session_id)
                print(f"forgot {session_id[:8]}")
            return
        now = datetime.now()
        sessions = agents.list_sessions(conn, now, ended_within=agents.RECENTLY_ENDED if args.all else None)
    finally:
        conn.close()
    open_tasks, done_tasks = read_tasks()
    agents.attach_tasks(sessions, open_tasks, done_tasks, now)
    if not sessions:
        print("no agents")
    for s in sessions:
        task = s["task"]
        if task:
            title = task["title"] or ""
            label = f"#{task['id']} {title if len(title) <= 60 else title[:59] + '…'}"
        else:
            label = "(no task)"
        if task and s["task_source"] == "branch":
            label += " (by branch)"
        where = "@".join(x for x in ((s["repo"] or "").split("/")[-1], s["branch"]) if x) or s["cwd"] or ""
        age = ago(s["waiting_since"] if s["state"] == "blocked" else s["state_since"], now)
        print(f"{s['session_id'][:8]} [{s['state']} {age}] {label}  {where}".rstrip())
        print(f"    {agent_activity(s)[:150]}")


def cmd_ui(args: argparse.Namespace) -> None:
    serve(args.port, open_browser=not args.no_browser)


def add_field_args(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--title", required=required)
    parser.add_argument("--kind", choices=KINDS, help="required for top-level tasks; subtasks inherit it")
    parser.add_argument("--status", choices=OPEN_STATUSES + ["done"])
    stage_help = "; ".join(f"{k}: {', '.join(v)}" for k, v in STAGES.items()) + "; other: any text"
    parser.add_argument("--stage", help=f"sets the status too. {stage_help}")
    parser.add_argument("--ticket", help="Jira key, e.g. ENG-1234")
    parser.add_argument("--pr", help="owner/repo#N, e.g. ethyca/fidesplus#5800")
    parser.add_argument("--repo")
    parser.add_argument("--branch")
    # An empty string clears the field: store.ordered() drops empty values when saving.
    parser.add_argument(
        "--due",
        type=lambda s: "" if s.lower() == "none" else date.fromisoformat(s).isoformat(),
        help="YYYY-MM-DD, or 'none' to clear",
    )
    parser.add_argument("--done-when", dest="done_when", help="what finishing looks like; for `other` tasks")
    parser.add_argument("--link", action="append", help="repeatable")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jeli", description="Local task tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a task or subtask, or update the one matching --pr/--ticket")
    add_field_args(add, required=True)
    add.add_argument("--parent", type=int, help="add as a subtask of this task id")
    add.set_defaults(func=cmd_add)

    update = sub.add_parser("update", help="change fields on a task")
    update.add_argument("id", type=int)
    add_field_args(update, required=False)
    update.set_defaults(func=cmd_update)

    done = sub.add_parser("done", help="mark a task done and archive it")
    done.add_argument("id", type=int)
    done.set_defaults(func=cmd_done)

    delete = sub.add_parser("delete", help="remove a task or subtask for good (open or archived)")
    delete.add_argument("id", type=int)
    delete.add_argument("--with-subtasks", action="store_true", help="required to delete a task that has subtasks")
    delete.set_defaults(func=cmd_delete)

    lst = sub.add_parser("list", help="list open tasks in priority order (or archived with --done)")
    lst.add_argument("--done", action="store_true")
    lst.add_argument("--status", choices=OPEN_STATUSES + ["done"])
    lst.add_argument("--kind", choices=KINDS)
    lst.set_defaults(func=cmd_list)

    move = sub.add_parser("move", help="reorder a task (list order is priority, top first)")
    move.add_argument("id", type=int)
    where = move.add_mutually_exclusive_group(required=True)
    where.add_argument("--top", action="store_true")
    where.add_argument("--bottom", action="store_true")
    where.add_argument("--before", type=int, metavar="ID")
    where.add_argument("--after", type=int, metavar="ID")
    move.set_defaults(func=cmd_move)

    find = sub.add_parser("find", help="look up a task by ticket or PR")
    find.add_argument("--ticket")
    find.add_argument("--pr")
    find.set_defaults(func=cmd_find)

    show = sub.add_parser("show", help="print one task")
    show.add_argument("id", type=int)
    show.set_defaults(func=cmd_show)

    agents_parser = sub.add_parser("agents", help="list Claude Code sessions, most in need of me first")
    agents_parser.add_argument("--all", action="store_true", help="include sessions that ended in the last 24 hours")
    agents_parser.set_defaults(func=cmd_agents, agents_command="list")
    agents_sub = agents_parser.add_subparsers(dest="agents_command")
    agent_link = agents_sub.add_parser("link", help="link a session to a task")
    agent_link.add_argument("session", help="session id or a unique prefix of it")
    agent_link.add_argument("task", type=int)
    agent_unlink = agents_sub.add_parser("unlink", help="drop a session's task link")
    agent_unlink.add_argument("session")
    agent_forget = agents_sub.add_parser("forget", help="delete a session from the tracker")
    agent_forget.add_argument("session")

    ui = sub.add_parser("ui", help="serve the task board on 127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    try:
        args.func(args)
    except (TaskError, LookupError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
