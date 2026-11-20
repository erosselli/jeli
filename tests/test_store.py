"""Tests for the task store: store.py's rules for adding, matching, updating, archiving, deleting and
ordering tasks and subtasks.

Run from ~/jeli (store.py needs Python 3.10+, unlike the hook):
    uv run --with pytest --with pyyaml python -m pytest tests/test_store.py

The tests only go through store.py's public functions, so they hold for whatever storage backs
the store.

Covers:
- adding tasks, stages and statuses, and kind rules
- matching an existing task or subtask by PR or ticket, and reopening it
- a second PR on a ticket becoming a subtask, and promoting the parent's own PR
- subtasks: inherited fields, nesting limit, closing and reopening parents
- archiving in completion order, and reopened tasks going to the bottom
- deleting, and ids never being reused
- priority order: moving tasks and subtasks
- every field surviving a save and load
"""

import pytest

import store
from store import TaskError

# --- Fixtures ---


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_FILE", tmp_path / "tasks.db")
    return tmp_path


# --- Helpers ---


def stored() -> tuple[list[dict], list[dict]]:
    """The open tasks in priority order and the archive in completion order, as saved."""
    return store.read_tasks()


def ids(tasks: list[dict]) -> list[int]:
    return [t["id"] for t in tasks]


def get(task_id: int) -> dict:
    found = store.get_task(task_id)
    assert found is not None, f"#{task_id} isn't stored"
    return found[0]


def add(parent: int | None = None, **changes) -> dict:
    changes.setdefault("title", "a task")
    if parent is None:
        changes.setdefault("kind", "implement")
    return store.add_task(changes, parent_id=parent)[0]


def update(task_id: int, position: dict | None = None, **changes) -> list[str]:
    return store.update_task(task_id, changes, position)[1]


# --- Tests ---


class TestAddTask:
    def test_new_tasks_get_increasing_ids_and_go_to_the_bottom(self):
        first, second = add(title="first"), add(title="second")

        assert (first["id"], second["id"]) == (1, 2)
        assert ids(stored()[0]) == [1, 2]
        assert get(1)["status"] == "in_progress"

    def test_top_level_task_needs_a_kind(self):
        with pytest.raises(TaskError, match="--kind is required"):
            store.add_task({"title": "no kind"})

    def test_stage_sets_the_status_it_implies(self):
        task = add(stage="in_review")

        assert (task["status"], task["stage"]) == ("blocked", "in_review")

    def test_stage_from_another_kind_is_rejected(self):
        with pytest.raises(TaskError, match="isn't valid for implement tasks"):
            add(stage="reviewing")

    def test_stage_with_a_conflicting_status_is_rejected(self):
        with pytest.raises(TaskError, match="implies status blocked"):
            add(stage="in_review", status="in_progress")

    def test_other_kind_takes_any_stage_and_keeps_its_status(self):
        task = add(kind="other", stage="whatever I like")

        assert (task["status"], task["stage"]) == ("in_progress", "whatever I like")

    def test_ticket_is_uppercased(self):
        assert add(ticket="eng-12")["ticket"] == "ENG-12"


class TestMatching:
    def test_same_pr_updates_the_task_and_keeps_its_title(self):
        add(title="original", pr="o/r#1")

        task, messages = store.add_task({"title": "renamed", "kind": "review", "pr": "o/r#1", "stage": "in_review"})

        assert task["id"] == 1
        assert (task["title"], task["kind"], task["stage"]) == ("original", "implement", "in_review")
        assert messages == ["updated #1 (matched existing task)"]
        assert ids(stored()[0]) == [1]

    def test_same_ticket_matches_regardless_of_case(self):
        add(ticket="ENG-1")

        task = add(ticket="eng-1", branch="b")

        assert (task["id"], task["branch"]) == (1, "b")

    def test_matching_a_done_task_reopens_it_at_the_bottom(self):
        add(ticket="ENG-1")
        add(title="other")
        update(1, status="done")

        add(ticket="ENG-1")

        open_tasks, done_tasks = stored()
        assert ids(open_tasks) == [2, 1]
        assert done_tasks == []
        assert get(1)["status"] == "in_progress"

    def test_pr_matches_a_subtask_and_reopens_it(self):
        add(ticket="ENG-1")
        add(parent=1, title="sub", pr="o/r#2")
        update(2, status="done")

        task, messages = store.add_task({"pr": "o/r#2", "stage": "in_review"})

        assert (task["id"], task["status"]) == (2, "blocked")
        assert messages == ["updated #2 (matched existing subtask)"]

    @pytest.mark.parametrize("ref", [{"ticket": "ENG-1"}, {"pr": "o/r#1"}])
    def test_open_task_wins_over_an_archived_one(self, ref):
        add(**ref)
        update(1, status="done")
        add()
        update(2, **ref)

        task, parent = store.find_task(ref.get("ticket"), ref.get("pr"))

        assert (task["id"], parent) == (2, None)
        assert add(**ref)["id"] == 2

    def test_ticket_doesnt_match_subtasks(self):
        add(kind="implement", title="parent")
        store.add_task({"title": "sub", "ticket": "ENG-9"}, parent_id=1)

        assert add(ticket="ENG-9")["id"] == 3


class TestSecondPrOnATicket:
    def test_parent_pr_is_promoted_to_a_subtask_next_to_the_new_one(self):
        add(ticket="ENG-1", pr="o/r#1", branch="b1", stage="in_review")

        task, messages = store.add_task({"title": "second", "ticket": "ENG-1", "pr": "o/r#2"})

        parent = get(1)
        assert [(s["id"], s.get("pr")) for s in parent["subtasks"]] == [(2, "o/r#1"), (3, "o/r#2")]
        promoted = parent["subtasks"][0]
        assert (promoted["title"], promoted["branch"], promoted["stage"], promoted["status"]) == (
            "o/r#1", "b1", "in_review", "blocked",
        )
        assert (parent["status"], "pr" in parent, "stage" in parent, "branch" in parent) == (
            "in_progress", False, False, False,
        )
        assert task["id"] == 3
        assert messages == ["added #3 as subtask of #1", "moved o/r#1 from #1 into subtask #2"]

    def test_task_with_no_pr_or_subtasks_just_gets_the_pr(self):
        add(ticket="ENG-1")

        task = add(ticket="ENG-1", pr="o/r#2")

        assert (task["id"], task["pr"]) == (1, "o/r#2")
        assert "subtasks" not in get(1)

    def test_task_with_subtasks_gets_another_one(self):
        add(ticket="ENG-1", pr="o/r#1")
        add(ticket="ENG-1", pr="o/r#2")

        add(ticket="ENG-1", pr="o/r#3")

        assert [s["pr"] for s in get(1)["subtasks"]] == ["o/r#1", "o/r#2", "o/r#3"]


class TestSubtasks:
    def test_subtask_uses_its_parents_kind_and_never_stores_parent_fields(self):
        add(kind="review")

        sub = add(parent=1, title="sub", kind="implement", ticket="ENG-5", repo="r", stage="waiting_on_author")

        assert (sub["status"], sub["stage"]) == ("blocked", "waiting_on_author")
        assert not {"kind", "ticket", "repo"} & sub.keys()

    def test_subtasks_cant_have_subtasks(self):
        add()
        add(parent=1)

        with pytest.raises(TaskError, match="subtasks can't have subtasks"):
            add(parent=2)

    def test_unknown_parent_is_rejected(self):
        with pytest.raises(TaskError, match="no task #7"):
            add(parent=7)

    def test_parent_cant_close_with_open_subtasks(self):
        add()
        add(parent=1)

        with pytest.raises(TaskError, match="still has open subtasks: #2"):
            update(1, status="done")

    def test_closing_a_subtask_says_whats_left(self):
        add()
        add(parent=1)
        add(parent=1)

        assert update(2, status="done") == ["updated #2", "#1 has 1 open subtask(s) left"]
        assert update(3, status="done") == [
            "updated #3", "all subtasks of #1 are done; close it if no more work is expected",
        ]
        assert ids(stored()[0]) == [1]

    def test_reopening_a_subtask_reopens_its_archived_parent(self):
        add(title="parent")
        add(title="other")
        add(parent=1)
        update(3, status="done")
        update(1, status="done")

        update(3, status="in_progress")

        open_tasks, done_tasks = stored()
        assert (ids(open_tasks), done_tasks) == ([2, 1], [])
        assert get(1)["status"] == "in_progress"

    def test_adding_a_subtask_reopens_an_archived_parent(self):
        add()
        update(1, status="done")

        add(parent=1)

        assert ids(stored()[0]) == [1]


class TestUpdateTask:
    def test_status_alone_drops_a_stage_that_implies_something_else(self):
        add(stage="in_review")

        update(1, status="in_progress")

        assert ("stage" in get(1), get(1)["status"]) == (False, "in_progress")

    def test_status_matching_the_stage_keeps_it(self):
        add(stage="in_review")

        update(1, status="blocked")

        assert get(1)["stage"] == "in_review"

    def test_new_stage_needs_the_current_kind_or_the_new_one(self):
        add(kind="review")

        with pytest.raises(TaskError, match="isn't valid for review tasks"):
            update(1, stage="coding")
        update(1, kind="implement", stage="coding")

        assert (get(1)["kind"], get(1)["stage"]) == ("implement", "coding")

    def test_links_are_appended_once(self):
        add(links=["a"])

        update(1, links=["b", "a"])

        assert get(1)["links"] == ["a", "b"]

    def test_empty_string_clears_a_field(self):
        add(due="2026-12-01")

        update(1, due="")

        assert "due" not in get(1)

    def test_unknown_task_is_rejected(self):
        with pytest.raises(TaskError, match="no task #4"):
            update(4, status="done")


class TestArchive:
    def test_done_task_is_archived_without_its_stage(self):
        add(stage="coding")

        assert update(1, status="done") == ["updated #1 (archived)"]

        open_tasks, done_tasks = stored()
        assert (open_tasks, ids(done_tasks)) == ([], [1])
        assert "stage" not in done_tasks[0]

    def test_archive_is_in_completion_order(self):
        add(), add(), add()

        for task_id in (2, 3, 1):
            update(task_id, status="done")

        assert ids(stored()[1]) == [2, 3, 1]

    def test_reopened_task_goes_to_the_bottom(self):
        add(), add(), add()
        update(1, status="done")

        update(1, status="todo")

        assert ids(stored()[0]) == [2, 3, 1]


class TestDeleteTask:
    @pytest.mark.parametrize("archived", [False, True])
    def test_deletes_open_and_archived_tasks(self, archived):
        add(), add()
        if archived:
            update(1, status="done")

        task, messages = store.delete_task(1)

        assert (task["id"], messages) == (1, ["deleted #1"])
        assert (ids(stored()[0]), stored()[1]) == ([2], [])

    def test_task_with_subtasks_needs_with_subtasks(self):
        add()
        add(parent=1)

        with pytest.raises(TaskError, match=r"#1 has subtasks \(#2\)"):
            store.delete_task(1)
        store.delete_task(1, with_subtasks=True)

        assert stored() == ([], [])

    def test_deleting_the_last_subtask_leaves_no_subtask_list(self):
        add()
        add(parent=1)

        store.delete_task(2)

        assert "subtasks" not in get(1)

    def test_deleting_a_subtask_says_whats_left(self):
        add()
        add(parent=1)
        add(parent=1)

        assert store.delete_task(2)[1] == ["deleted #2", "#1 has 1 open subtask(s) left"]

    def test_ids_are_never_reused(self):
        add(), add()
        store.delete_task(2)

        assert add()["id"] == 3

    def test_unknown_task_is_rejected(self):
        with pytest.raises(TaskError, match="no task #1"):
            store.delete_task(1)


class TestMoveTask:
    @pytest.mark.parametrize(
        ("position", "expected"),
        [
            ({"top": True}, [3, 1, 2, 4]),
            ({"bottom": True}, [1, 2, 4, 3]),
            ({"before": 2}, [1, 3, 2, 4]),
            ({"after": 4}, [1, 2, 4, 3]),
            ({"before": 3}, [1, 2, 3, 4]),
        ],
    )
    def test_moves_a_task_among_open_tasks(self, position, expected):
        add(), add(), add(), add()

        store.move_task(3, position)

        assert ids(stored()[0]) == expected

    def test_moves_a_subtask_among_its_siblings(self):
        add()
        add(parent=1), add(parent=1), add(parent=1)

        store.move_task(4, {"top": True})

        assert ids(get(1)["subtasks"]) == [4, 2, 3]

    def test_anchor_must_be_a_sibling(self):
        add()
        add(parent=1)
        add()

        with pytest.raises(TaskError, match="#3 isn't among the subtasks of #1"):
            store.move_task(2, {"before": 3})

    def test_archived_task_cant_move(self):
        add()
        update(1, status="done")

        with pytest.raises(TaskError, match="no open task #1"):
            store.move_task(1, {"top": True})

    def test_update_can_place_the_task_too(self):
        add(), add(), add()

        update(3, position={"before": 1}, status="todo")

        assert ids(stored()[0]) == [3, 1, 2]


class TestStorage:
    def test_every_field_survives_a_save_and_load(self):
        fields = {
            "title": "t", "kind": "implement", "stage": "coding", "ticket": "ENG-1", "pr": "o/r#1",
            "repo": "o/r", "branch": "b", "due": "2026-12-01", "done_when": "it's done", "links": ["x", "y"],
        }
        add(**fields)
        add(title="parent")
        store.add_task({"title": "sub", "stage": "in_review", "pr": "o/r#9", "links": ["z"]}, parent_id=2)

        task = get(1)
        assert {k: task[k] for k in fields} == fields
        assert task["status"] == "in_progress"
        assert {"created", "updated"} <= task.keys()
        sub = get(2)["subtasks"][0]
        assert (sub["title"], sub["stage"], sub["status"], sub["pr"], sub["links"]) == (
            "sub", "in_review", "blocked", "o/r#9", ["z"],
        )

