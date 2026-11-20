"""Tests for the agent tracker: agents.py's state machine and read side, and the agent_hook.py entrypoint.

Run from ~/jeli on the hook's Python, so a 3.10+ construct in agents.py fails here first:
    uv run --python /usr/bin/python3 --with pytest python -m pytest tests

Covers:
- turn states: working, your_turn, waiting_bg
- blocking on permission prompts and questions, and what clears a block
- when a session started needing me, across blocked and your_turn
- subagent events, which run under the parent's session
- session lifecycle: end, stragglers, resume, /clear task inheritance, retention
- task linking from jeli output, and attaching tasks on read, incl. waiting on others and aging out to idle
- concurrent hook processes
- the hook entrypoint never failing a session
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

import agents

JELI_DIR = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 4, 12, 0, 0)

# --- Fixtures ---


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "DATA_DIR", tmp_path)
    monkeypatch.setattr(agents, "DB_FILE", tmp_path / "agents.db")
    # Test cwds aren't real checkouts; a test that needs a branch sets one through this.
    monkeypatch.setattr(agents, "git_info", lambda cwd: (None, None))
    return tmp_path


@pytest.fixture
def conn():
    connection = agents.connect()
    yield connection
    connection.close()


class Clock:
    def __init__(self):
        self.now = T0

    def tick(self, seconds: float = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def send(conn, clock):
    """Apply one hook event, a second after the previous one. The default pid is this live process, so
    reading the sessions doesn't end them as gone."""

    def send(event: str, session_id: str = "s1", pid: int | None = os.getpid(), at: datetime | None = None, **fields):
        payload = {"hook_event_name": event, "session_id": session_id, "cwd": "/work/repo", **fields}
        agents.apply_event(conn, payload, at or clock.tick(), pid)

    return send


def row(conn, session_id: str = "s1") -> dict | None:
    found = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(found) if found else None


def bash(command: str = "ls") -> dict:
    return {"command": command, "description": f"run {command}"}


def tool_call(send, tool_use_id: str, tool_input: dict, tool_name: str = "Bash", **fields) -> None:
    send("PreToolUse", tool_name=tool_name, tool_input=tool_input, tool_use_id=tool_use_id, **fields)


# --- Turn states ---


class TestTurnStates:
    def test_session_start_is_idle_until_a_prompt(self, conn, send):
        send("SessionStart", source="startup")
        assert row(conn)["state"] == "idle"
        send("UserPromptSubmit", prompt="fix the  flaky\ntest")
        assert row(conn)["state"] == "working"
        assert row(conn)["prompt"] == "fix the flaky test"

    def test_tool_call_records_what_it_does(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", {"file_path": "/work/repo/store.py"}, tool_name="Edit")
        assert row(conn)["last_tool"] == "Edit: store.py"

    def test_stop_with_nothing_running_is_your_turn(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", last_assistant_message="Done.", background_tasks=[])
        assert row(conn)["state"] == "your_turn"
        assert row(conn)["last_message"] == "Done."

    def test_stop_with_running_background_work_is_waiting_bg(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send(
            "Stop",
            background_tasks=[
                {"id": "b1", "type": "subagent", "status": "running", "description": "self-review"},
                {"id": "b2", "type": "shell", "status": "completed", "description": "old build"},
            ],
            session_crons=["*/5 * * * * check CI"],
        )
        found = row(conn)
        assert found["state"] == "waiting_bg"
        assert json.loads(found["background"]) == [
            {"type": "subagent", "description": "self-review"},
            {"type": "cron", "description": "*/5 * * * * check CI"},
        ]

    def test_stop_with_only_finished_background_work_is_your_turn(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", background_tasks=[{"id": "b1", "type": "subagent", "status": "completed", "description": "x"}])
        assert row(conn)["state"] == "your_turn"

    def test_stop_with_only_shells_and_crons_running_is_your_turn_and_lists_them(self, conn, send):
        # A dev server or a /loop never wakes the agent, so the session needs me.
        send("UserPromptSubmit", prompt="go")
        send(
            "Stop",
            background_tasks=[{"id": "b1", "type": "shell", "status": "running", "description": "jeli ui server"}],
            session_crons=["*/5 * * * * check CI"],
        )
        found = row(conn)
        assert found["state"] == "your_turn"
        assert [b["type"] for b in json.loads(found["background"])] == ["shell", "cron"]

    def test_task_notification_keeps_the_last_real_prompt(self, conn, send):
        send("UserPromptSubmit", prompt="run the self-review in the background")
        send("Stop", background_tasks=[{"id": "b1", "type": "subagent", "status": "running", "description": "x"}])
        send("UserPromptSubmit", prompt="<task-notification>b1 completed</task-notification>")
        found = row(conn)
        assert found["state"] == "working"
        assert found["prompt"] == "run the self-review in the background"

    def test_stop_refreshes_location_from_the_payload_cwd(self, conn, send, monkeypatch):
        monkeypatch.setattr(agents, "git_info", lambda cwd: ("ethyca/fidesplus", "main") if cwd == "/work/fp" else (None, None))
        send("UserPromptSubmit", prompt="go")
        send("CwdChanged", new_cwd="/elsewhere")
        assert row(conn)["cwd"] == "/elsewhere"
        send("Stop", cwd="/work/fp")
        found = row(conn)
        assert (found["cwd"], found["repo"], found["branch"]) == ("/work/fp", "ethyca/fidesplus", "main")


# --- Blocking ---


class TestBlocking:
    def test_permission_request_blocks_on_the_matching_call(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("ls"))
        tool_call(send, "t2", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        found = row(conn)
        assert found["state"] == "blocked"
        assert found["waiting_kind"] == "permission"
        assert found["waiting_text"] == "Bash: run rm -rf build"
        assert found["waiting_tool_use_id"] == "t2"

    def test_other_calls_finishing_leave_the_block(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("ls"))
        tool_call(send, "t2", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        send("PostToolUse", tool_name="Bash", tool_input=bash("ls"), tool_use_id="t1")
        tool_call(send, "t3", bash("pwd"))
        assert row(conn)["state"] == "blocked"

    @pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure", "PermissionDenied"])
    def test_the_blocked_call_finishing_unblocks(self, conn, send, event):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        send(event, tool_name="Bash", tool_input=bash("rm -rf build"), tool_use_id="t1")
        found = row(conn)
        assert found["state"] == "working"
        assert (found["waiting_kind"], found["waiting_since"], found["waiting_tool_use_id"]) == (None, None, None)

    def test_ask_user_question_blocks_as_a_question(self, conn, send):
        question = {"questions": [{"question": "Which port?", "header": "Port", "options": []}]}
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "q1", question, tool_name="AskUserQuestion")
        send("PermissionRequest", tool_name="AskUserQuestion", tool_input=question)
        found = row(conn)
        assert (found["state"], found["waiting_kind"], found["waiting_text"]) == ("blocked", "question", "Which port?")

    def test_typing_a_reply_unblocks(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        send("UserPromptSubmit", prompt="no, keep build")
        found = row(conn)
        assert (found["state"], found["waiting_kind"]) == ("working", None)
        assert json.loads(found["inflight"]) == {}

    def test_notification_blocks_when_permission_request_was_missed(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Notification", notification_type="permission_prompt", message="Claude needs your permission to use Bash")
        found = row(conn)
        assert (found["state"], found["waiting_text"]) == ("blocked", "Claude needs your permission to use Bash")

    def test_notification_keeps_the_more_specific_block(self, conn, send, clock):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        blocked_at = agents.stamp(clock.now)
        send("Notification", notification_type="permission_prompt", message="Claude needs your permission")
        found = row(conn)
        assert (found["waiting_text"], found["waiting_tool_use_id"]) == ("Bash: run rm -rf build", "t1")
        assert found["waiting_since"] == blocked_at

    def test_idle_notification_does_not_block(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop")
        send("Notification", notification_type="idle_prompt", message="Claude is waiting for your input")
        assert row(conn)["state"] == "your_turn"

    def test_fallback_block_clears_on_the_next_top_level_tool_only(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Notification", notification_type="permission_prompt", message="needs permission")
        send("PostToolUse", tool_name="Read", tool_input={}, tool_use_id="s1", agent_id="a1")
        assert row(conn)["state"] == "blocked"
        send("PostToolUse", tool_name="Bash", tool_input=bash(), tool_use_id="t1")
        assert row(conn)["state"] == "working"

    def test_stop_clears_a_block(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Notification", notification_type="permission_prompt", message="needs permission")
        send("Stop")
        assert (row(conn)["state"], row(conn)["waiting_kind"]) == ("your_turn", None)


# --- Needs you since ---


class TestNeedsYouSince:
    def test_ending_a_turn_starts_the_wait_and_a_prompt_clears_it(self, conn, send, clock):
        send("UserPromptSubmit", prompt="go")
        assert row(conn)["needs_you_since"] is None
        send("Stop", background_tasks=[])
        assert row(conn)["needs_you_since"] == agents.stamp(clock.now)
        send("UserPromptSubmit", prompt="next")
        assert row(conn)["needs_you_since"] is None

    def test_moving_between_blocked_and_your_turn_keeps_the_start(self, conn, send, clock):
        send("UserPromptSubmit", prompt="go")
        tool_call(send, "t1", bash("rm -rf build"))
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm -rf build"))
        blocked_at = agents.stamp(clock.now)
        send("Stop", background_tasks=[])
        found = row(conn)
        assert (found["state"], found["needs_you_since"]) == ("your_turn", blocked_at)
        assert found["state_since"] != blocked_at

    def test_waiting_on_a_background_subagent_doesnt_count(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", background_tasks=[{"id": "b1", "type": "subagent", "status": "running"}])
        assert row(conn)["needs_you_since"] is None

    def test_database_from_before_the_column_gets_it_on_connect(self, conn):
        # An older database: the table without the added columns, holding one waiting and one working session.
        conn.execute("DROP TABLE sessions")
        original = agents.SCHEMA.replace("    needs_you_since TEXT,\n", "")
        original = re.sub(r",\s*name\s+TEXT,\s*project_dir\s+TEXT", "", original)
        assert all(c not in original for c in agents.ADDED_COLUMNS)
        conn.execute(original)
        for session_id, state in (("s1", "your_turn"), ("s2", "working")):
            conn.execute(
                "INSERT INTO sessions (session_id, state, started, state_since, last_seen) VALUES (?, ?, ?, ?, ?)",
                (session_id, state, "2026-11-04T11:00:00", "2026-11-04T11:30:00", "2026-11-04T11:30:00"),
            )
        agents.connect().close()
        assert row(conn, "s1")["needs_you_since"] == "2026-11-04T11:30:00"
        assert row(conn, "s2")["needs_you_since"] is None
        assert (row(conn, "s1")["name"], row(conn, "s1")["project_dir"]) == (None, None)


# --- Subagents ---


class TestSubagents:
    def test_subagent_tools_leave_the_parent_waiting_on_it(self, conn, send):
        send("UserPromptSubmit", prompt="review in the background")
        tool_call(send, "t1", {"description": "self-review"}, tool_name="Agent")
        send("Stop", background_tasks=[{"id": "b1", "type": "subagent", "status": "running", "description": "self-review"}])
        tool_call(send, "a-t1", bash("git diff"), agent_id="a1", agent_type="general-purpose")
        send("PostToolUse", tool_name="Bash", tool_input=bash("git diff"), tool_use_id="a-t1", agent_id="a1")
        found = row(conn)
        assert found["state"] == "waiting_bg"
        assert found["last_tool"] == "Agent: self-review"

    def test_subagent_permission_prompt_blocks_the_session(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", background_tasks=[{"id": "b1", "type": "subagent", "status": "running", "description": "x"}])
        tool_call(send, "a-t1", bash("rm x"), agent_id="a1")
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm x"), agent_id="a1")
        assert (row(conn)["state"], row(conn)["waiting_tool_use_id"]) == ("blocked", "a-t1")
        send("PostToolUse", tool_name="Bash", tool_input=bash("rm x"), tool_use_id="a-t1", agent_id="a1")
        assert row(conn)["state"] == "waiting_bg"

    def test_foreground_subagent_permission_prompt_goes_back_to_working(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", background_tasks=[{"id": "b1", "type": "shell", "status": "running", "description": "x"}])
        send("UserPromptSubmit", prompt="now review it")
        tool_call(send, "a-t1", bash("rm x"), agent_id="a1")
        send("PermissionRequest", tool_name="Bash", tool_input=bash("rm x"), agent_id="a1")
        send("PostToolUse", tool_name="Bash", tool_input=bash("rm x"), tool_use_id="a-t1", agent_id="a1")
        assert row(conn)["state"] == "working"

    def test_subagent_stop_does_not_end_the_turn(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("SubagentStop", agent_id="a1", last_assistant_message="summary")
        assert row(conn)["state"] == "working"


# --- Session lifecycle ---


class TestLifecycle:
    def test_project_dir_stays_where_the_session_started(self, conn, send):
        send("SessionStart", source="startup", cwd="/work")
        send("CwdChanged", old_cwd="/work", new_cwd="/work/repo/sub", cwd="/work/repo/sub")
        send("Stop", background_tasks=[], cwd="/work/repo/sub")
        assert (row(conn)["cwd"], row(conn)["project_dir"]) == ("/work/repo/sub", "/work")

    def test_claude_project_dir_wins_over_the_first_cwd(self, conn, clock):
        # The tracker can first hear of a session after it has moved, e.g. one started before the hook.
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/repo", "prompt": "go"}
        agents.apply_event(conn, payload, clock.tick(), os.getpid(), "/work")
        assert row(conn)["project_dir"] == "/work"

    def test_name_i_gave_the_session_is_kept_until_renamed(self, conn, send):
        send("SessionStart", source="resume", session_title="jeli")
        assert row(conn)["name"] == "jeli"
        send("Stop", background_tasks=[])  # events without a title leave the name alone
        assert row(conn)["name"] == "jeli"
        send("UserPromptSubmit", prompt="go", session_title="jeli-agents")
        assert row(conn)["name"] == "jeli-agents"

    @pytest.mark.parametrize("before", ["your_turn", "working"])
    def test_compacting_keeps_the_state(self, conn, send, before):
        send("UserPromptSubmit", prompt="go")
        if before == "your_turn":
            send("Stop", background_tasks=[])
        send("PreCompact", trigger="auto")
        send("SessionStart", source="compact")
        assert row(conn)["state"] == before

    def test_session_end_ends_and_clears_waiting(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("Stop", background_tasks=[{"id": "b1", "type": "shell", "status": "running", "description": "x"}])
        send("SessionEnd", reason="prompt_input_exit")
        found = row(conn)
        assert (found["state"], found["ended_reason"], found["background"]) == ("ended", "prompt_input_exit", "[]")

    def test_events_after_session_end_are_ignored(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("SessionEnd", reason="other")
        ended = row(conn)
        send("SubagentStop", agent_id="a1")
        send("PostToolUse", tool_name="Bash", tool_input=bash(), tool_use_id="t1")
        assert row(conn) == ended

    def test_session_end_for_an_unknown_session_adds_nothing(self, conn, send):
        send("SessionEnd", session_id="never-seen", reason="other")
        assert row(conn, "never-seen") is None

    def test_resume_revives_an_ended_session(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("SessionEnd", reason="other")
        send("SessionStart", source="resume", pid=5151)
        found = row(conn)
        assert (found["state"], found["ended_reason"], found["claude_pid"]) == ("idle", None, 5151)

    def test_event_without_pid_keeps_the_known_one(self, conn, send):
        send("SessionStart", source="startup", pid=5151)
        send("UserPromptSubmit", prompt="go", pid=None)
        assert row(conn)["claude_pid"] == 5151

    def test_unknown_event_only_refreshes_last_seen(self, conn, send, clock):
        send("UserPromptSubmit", prompt="go")
        before = row(conn)
        send("PreCompact", trigger="auto")
        after = row(conn)
        assert after.pop("last_seen") == agents.stamp(clock.now)
        before.pop("last_seen")
        assert after == before

    @pytest.mark.parametrize("payload", [{"hook_event_name": "Stop"}, {"session_id": "s1"}])
    def test_payload_without_session_or_event_is_ignored(self, conn, payload):
        agents.apply_event(conn, payload, T0, 1)
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0

    def test_clear_inherits_the_task_of_the_session_it_replaced(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        agents.link(conn, "s1", 66)
        send("SessionEnd", reason="clear")
        send("SessionStart", session_id="s2", source="clear")
        assert row(conn, "s2")["task_id"] == 66

    @pytest.mark.parametrize(
        "end_reason, cwd, delay",
        [
            ("other", "/work/repo", 1),  # an exit, not a /clear
            ("clear", "/work/other", 1),  # a /clear in another session's checkout
            ("clear", "/work/repo", 31),  # outside CLEAR_LINK_WINDOW
        ],
    )
    def test_clear_does_not_inherit_from_unrelated_sessions(self, conn, send, clock, end_reason, cwd, delay):
        send("UserPromptSubmit", prompt="go")
        agents.link(conn, "s1", 66)
        send("SessionEnd", reason=end_reason)
        send("SessionStart", session_id="s2", source="clear", cwd=cwd, at=clock.tick(delay))
        assert row(conn, "s2")["task_id"] is None

    def test_session_start_deletes_sessions_ended_past_retention(self, conn, send, clock):
        send("UserPromptSubmit", session_id="old", prompt="go")
        send("SessionEnd", session_id="old", reason="other")
        send("UserPromptSubmit", session_id="live", prompt="go")
        send("SessionStart", session_id="new", source="startup", at=clock.tick((agents.RETENTION + timedelta(hours=1)).total_seconds()))
        assert row(conn, "old") is None
        assert row(conn, "live") is not None


# --- Task linking ---


class TestTaskLinking:
    @pytest.mark.parametrize(
        "command, stdout, expected",
        [
            ("~/jeli/jeli.py add --title x --kind other", "added #12\nid: 12\ntitle: x\n", 12),
            ("~/jeli/jeli.py update 12 --stage coding", "updated #12\nid: 12\n", 12),
            ("~/jeli/jeli.py show 12", "id: 12\ntitle: x\n", None),  # looking at a task isn't working on it
            ("~/jeli/jeli.py add --title x", "error: --kind is required", None),
            ("echo 'id: 9'", "id: 9\n", None),
        ],
    )
    def test_linked_task(self, command, stdout, expected):
        assert agents.linked_task({"command": command}, {"stdout": stdout}) == expected

    def test_jeli_add_links_the_session(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        command = {"command": "~/jeli/jeli.py add --title x --kind other"}
        tool_call(send, "t1", command)
        send("PostToolUse", tool_name="Bash", tool_input=command, tool_use_id="t1", tool_response={"stdout": "id: 12\n"})
        assert row(conn)["task_id"] == 12


# --- Concurrency ---


class TestConcurrency:
    def test_overlapping_hook_processes_lose_no_updates(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        errors = []

        def hook(n: int) -> None:
            own = agents.connect()
            try:
                payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Read",
                           "tool_input": {"file_path": f"/f{n}"}, "tool_use_id": f"t{n}"}
                agents.apply_event(own, payload, T0 + timedelta(seconds=60), os.getpid())
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                errors.append(e)
            finally:
                own.close()

        threads = [threading.Thread(target=hook, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert sorted(json.loads(row(conn)["inflight"])) == sorted(f"t{n}" for n in range(20))


# --- Read side ---


def dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class TestListSessions:
    def test_most_in_need_of_me_first_then_longest_waiting(self, conn, send):
        send("SessionStart", session_id="idle", source="startup")
        send("UserPromptSubmit", session_id="working", prompt="go")
        send("UserPromptSubmit", session_id="bg", prompt="go")
        send("Stop", session_id="bg", background_tasks=[{"type": "subagent", "status": "running", "description": "x"}])
        send("UserPromptSubmit", session_id="turn-late", prompt="go")
        send("UserPromptSubmit", session_id="turn-early", prompt="go")
        send("Stop", session_id="turn-early")
        send("Stop", session_id="turn-late")
        send("UserPromptSubmit", session_id="blocked", prompt="go")
        send("Notification", session_id="blocked", notification_type="permission_prompt", message="?")
        send("UserPromptSubmit", session_id="ended", prompt="go")
        send("SessionEnd", session_id="ended", reason="other")
        sessions = agents.list_sessions(conn, T0 + timedelta(minutes=5))
        assert [s["session_id"] for s in sessions] == ["blocked", "turn-early", "turn-late", "bg", "working", "idle"]
        assert sessions[3]["background"] == [{"type": "subagent", "description": "x"}]
        assert "inflight" not in sessions[0]

    def test_ended_sessions_only_within_the_asked_window(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("SessionEnd", reason="other")
        later = T0 + timedelta(hours=25)
        assert agents.list_sessions(conn, later) == []
        assert agents.list_sessions(conn, later, ended_within=timedelta(hours=24)) == []
        assert [s["state"] for s in agents.list_sessions(conn, later, ended_within=timedelta(hours=26))] == ["ended"]

    def test_session_whose_claude_process_died_is_ended_as_gone(self, conn, send):
        pid = dead_pid()
        send("UserPromptSubmit", session_id="crashed", prompt="go", pid=pid)
        send("Notification", session_id="crashed", notification_type="permission_prompt", message="?", pid=pid)
        send("UserPromptSubmit", session_id="live", prompt="go", pid=os.getpid())
        send("UserPromptSubmit", session_id="no-pid", prompt="go", pid=None)
        agents.list_sessions(conn, T0 + timedelta(minutes=1))
        crashed = row(conn, "crashed")
        assert (crashed["state"], crashed["ended_reason"], crashed["waiting_kind"]) == ("ended", "gone", None)
        assert row(conn, "live")["state"] == "working"
        assert row(conn, "no-pid")["state"] == "working"

    def test_reading_deletes_sessions_ended_past_retention(self, conn, send):
        send("UserPromptSubmit", prompt="go")
        send("SessionEnd", reason="other")
        agents.list_sessions(conn, T0 + agents.RETENTION - timedelta(hours=1))
        assert row(conn) is not None
        agents.list_sessions(conn, T0 + agents.RETENTION + timedelta(hours=1))
        assert row(conn) is None

    def test_last_agent_by_task_is_the_most_recently_ended(self):
        def session(sid, state, task_id, ended):
            return {"session_id": sid, "state": state, "project_dir": "/work", "cwd": f"/work/{sid}", "last_seen": ended,
                    "task": {"id": task_id} if task_id else None}

        sessions = [
            session("older", "ended", 1, "2026-11-01T10:00:00"),
            session("newer", "ended", 1, "2026-11-03T10:00:00"),
            session("live", "your_turn", 1, "2026-11-04T10:00:00"),  # live agents show on their own
            session("no-task", "ended", None, "2026-11-04T10:00:00"),
        ]
        assert agents.last_agent_by_task(sessions) == {
            1: {"session_id": "newer", "project_dir": "/work", "cwd": "/work/newer", "ended": "2026-11-03T10:00:00"},
        }


class TestFindSession:
    @pytest.fixture(autouse=True)
    def sessions(self, send):
        for session_id in ("abc123", "abd456"):
            send("SessionStart", session_id=session_id, source="startup")

    def test_unique_prefix_finds_the_session(self, conn):
        assert agents.find_session(conn, "abc") == "abc123"

    @pytest.mark.parametrize("prefix, message", [("zzz", "no session matches"), ("ab", "matches 2 sessions")])
    def test_missing_or_ambiguous_prefix_raises(self, conn, prefix, message):
        with pytest.raises(LookupError, match=message):
            agents.find_session(conn, prefix)


class TestAttachTasks:
    OPEN: ClassVar[list[dict]] = [
        {"id": 40, "title": "approvals", "kind": "implement", "ticket": "ENG-6061", "status": "in_progress",
         "branch": "feature-branch",
         "subtasks": [{"id": 66, "title": "story 4", "status": "in_progress", "stage": "coding",
                       "branch": "story-4", "pr": "ethyca/fidesplus#7029"}]},
        {"id": 62, "title": "patch release", "kind": "release", "status": "in_progress", "branch": "hotfix"},
        {"id": 70, "title": "pr up", "kind": "implement", "status": "blocked", "stage": "in_review"},
    ]
    DONE: ClassVar[list[dict]] = [{"id": 12, "title": "old work", "kind": "review", "status": "done", "branch": "old-branch"}]

    def attach(self, **session) -> dict:
        session = {
            "task_id": None, "branch": None, "state": "working", "state_since": agents.stamp(T0),
            "needs_you_since": None, **session,
        }
        agents.attach_tasks([session], self.OPEN, self.DONE, T0 + timedelta(minutes=5))
        return session

    def test_branch_match_on_a_subtask_carries_the_parents_kind_and_ticket(self):
        session = self.attach(branch="story-4")
        assert session["task_source"] == "branch"
        assert session["task"] == {
            "id": 66, "title": "story 4", "kind": "implement", "status": "in_progress", "stage": "coding",
            "ticket": "ENG-6061", "pr": "ethyca/fidesplus#7029", "parent": {"id": 40, "title": "approvals"},
        }

    def test_explicit_link_wins_over_branch(self):
        session = self.attach(task_id=12, branch="hotfix")
        assert (session["task"]["id"], session["task_source"]) == (12, "link")

    def test_archived_task_is_not_matched_by_branch(self):
        session = self.attach(branch="old-branch")
        assert (session["task"], session["task_source"]) == (None, None)

    def test_link_to_a_deleted_task_shows_no_task(self):
        session = self.attach(task_id=999, branch="hotfix")
        assert (session["task"], session["task_source"]) == (None, "link")

    def test_my_turn_on_a_blocked_task_is_waiting_on_others(self):
        session = self.attach(task_id=70, state="your_turn", needs_you_since="2026-11-04T12:00:00")
        assert (session["state"], session["needs_you_since"]) == ("waiting_others", None)

    @pytest.mark.parametrize(
        "state, task_id",
        [
            ("blocked", 70),  # a permission prompt still needs me, whatever the task's status
            ("your_turn", 62),  # the task isn't blocked
        ],
    )
    def test_other_sessions_keep_their_state(self, state, task_id):
        session = self.attach(task_id=task_id, state=state, needs_you_since="2026-11-04T12:00:00")
        assert (session["state"], session["needs_you_since"]) == (state, "2026-11-04T12:00:00")

    @pytest.mark.parametrize(
        "state, task_id, age, shown",
        [
            ("your_turn", 62, agents.IDLE_AFTER + timedelta(seconds=1), "idle"),
            ("your_turn", 62, agents.IDLE_AFTER, "your_turn"),
            ("your_turn", 70, timedelta(days=3), "waiting_others"),  # waiting on others doesn't age out
            ("blocked", None, timedelta(days=3), "blocked"),  # stuck on me, however old
        ],
    )
    def test_my_turn_left_alone_long_enough_shows_as_idle(self, state, task_id, age, shown):
        since = T0 + timedelta(minutes=5) - age
        session = self.attach(task_id=task_id, state=state, state_since=agents.stamp(since), needs_you_since=agents.stamp(since))
        assert session["state"] == shown
        if shown == "idle":
            assert session["needs_you_since"] is None


# --- Hook entrypoint and CLI ---


def run_hook(data_dir: Path, stdin: str) -> subprocess.CompletedProcess:
    # The hook runs as Claude Code runs it: on the system Python, outside any venv.
    return subprocess.run(
        ["/usr/bin/python3", str(JELI_DIR / "agent_hook.py")], input=stdin, capture_output=True, text=True,
        env={**os.environ, "JELI_DATA_DIR": str(data_dir)}, timeout=10, check=False,
    )


class TestHook:
    def test_payload_is_applied_silently(self, data_dir, conn):
        result = run_hook(data_dir, json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hi"}))
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert row(conn)["state"] == "working"
        assert not (data_dir / "agents" / "raw.jsonl").exists()

    def test_failure_exits_0_and_is_logged(self, data_dir):
        result = run_hook(data_dir, "not json")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        log = data_dir / "agents" / "hook-errors.log"
        assert "JSONDecodeError" in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600

    def test_raw_log_is_appended_only_while_it_exists(self, data_dir):
        raw = data_dir / "agents" / "raw.jsonl"
        raw.parent.mkdir()
        raw.touch()
        run_hook(data_dir, json.dumps({"hook_event_name": "Stop", "session_id": "s1"}))
        assert json.loads(raw.read_text())["event"] == "Stop"


def run_cli(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(JELI_DIR / "jeli.py"), "agents", *args], capture_output=True, text=True,
        env={**os.environ, "JELI_DATA_DIR": str(data_dir), "CLAUDE_CODE_SESSION_ID": ""}, timeout=60, check=False,
    )


class TestCli:
    @pytest.fixture
    def tracked(self, data_dir, conn, send):
        # Task #1, added with no session to link it to.
        subprocess.run(
            [str(JELI_DIR / "jeli.py"), "add", "--kind", "release", "--title", "a very long patch release title " * 3],
            capture_output=True, timeout=60, check=True,
            env={**os.environ, "JELI_DATA_DIR": str(data_dir), "CLAUDE_CODE_SESSION_ID": ""},
        )
        # Real time, so the process check and retention in the CLI treat these as fresh.
        now = datetime.now()
        for session_id in ("aaaa1111", "bbbb2222"):
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": "go"}
            agents.apply_event(conn, payload, now, os.getpid())

    def test_list_shows_linked_and_unlinked_sessions(self, data_dir, tracked):
        assert run_cli(data_dir, "link", "aaaa", "1").stdout == "linked aaaa1111 to #1\n"
        lines = run_cli(data_dir).stdout.splitlines()
        assert lines[0].startswith("aaaa1111 [working 0m] #1 a very long patch release title")
        assert lines[0].endswith("…")
        assert lines[2] == "bbbb2222 [working 0m] (no task)"

    @pytest.mark.parametrize("command", [["add", "--title", "patch", "--kind", "release"], ["update", "1", "--stage", "cutting"]])
    def test_add_and_update_link_the_calling_session_even_when_piped(self, data_dir, tracked, conn, command):
        # Piping through grep drops the `id:` line the hook reads; the session id from the environment doesn't.
        result = subprocess.run(
            f"{JELI_DIR / 'jeli.py'} {' '.join(command)} | grep -c title", shell=True, capture_output=True, text=True,
            env={**os.environ, "JELI_DATA_DIR": str(data_dir), "CLAUDE_CODE_SESSION_ID": "aaaa1111"}, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert row(conn, "aaaa1111")["task_id"] == (2 if command[0] == "add" else 1)
        assert row(conn, "bbbb2222")["task_id"] is None

    def test_link_to_an_unknown_task_fails(self, data_dir, tracked, conn):
        result = run_cli(data_dir, "link", "aaaa", "99")
        assert (result.returncode, result.stderr) == (1, "no task #99\n")
        assert row(conn, "aaaa1111")["task_id"] is None
