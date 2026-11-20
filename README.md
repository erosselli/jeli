# jeli

A local task tracker and session board for [Claude Code](https://claude.com/claude-code).

jeli keeps a list of the work you do with Claude Code (tickets, PR reviews, investigations, releases) and shows what every open Claude Code session is doing and which ones are waiting on you. Claude logs and updates tasks itself through a skill, so the tracker stays current without manual bookkeeping. Everything runs locally; nothing leaves your machine.

## Features

- **Task tracker**: a CLI over SQLite with kinds, stages, subtasks, priority order and an archive. Tasks are matched by Jira ticket or PR, so the same piece of work is never logged twice.
- **Claude Code skill**: tells Claude when to log a task, how to move it through its stages, and when to close it.
- **Session tracking**: a Claude Code hook records each session's state (working, waiting on a permission prompt, waiting for a reply, ...) and links it to the task it's working on.
- **Board**: a local web page with a Tasks tab and an Agents tab. It chimes when a session has been waiting on you for a while, and offers a one-click resume command for the last session that worked on a task.

## Requirements

- macOS (the hook runs on the system `/usr/bin/python3`, 3.9)
- [uv](https://docs.astral.sh/uv/) (`jeli.py` is a uv script; its only dependency, pyyaml, is installed automatically)
- Claude Code

## Installation

### 1. Clone the repo to `~/jeli`

The hook command and the skill both refer to `~/jeli`, so clone it there (or update those paths).

```bash
git clone git@github.com:erosselli/jeli.git ~/jeli
~/jeli/jeli.py list    # creates ~/jeli/data/tasks.db and prints "no tasks"
```

### 2. Install the skill

```bash
mkdir -p ~/.claude/skills && cp -r ~/jeli/skills/tasks ~/.claude/skills/tasks
```

The skill's instructions are written in the first person, from the point of view of the person using it. Edit them to match your own workflow, for example when a review counts as done.

### 3. Tell Claude to use it

Add the section in [`CLAUDE.example.md`](CLAUDE.example.md) to `~/.claude/CLAUDE.md`. Without it, the skill only runs when you invoke `/tasks`; with it, Claude logs a task at the start of every unit of work.

### 4. Add the hooks

Only needed for the Agents tab. Merge the `hooks` section of [`settings.example.json`](settings.example.json) into `~/.claude/settings.json`, keeping any hooks you already have (or ask Claude to merge it for you). It runs the same script on every hook event, asynchronously, so it doesn't slow sessions down.

Sessions pick up the hooks when they start; restart any sessions that are already open.

### 5. Open the board

```bash
~/jeli/jeli.py ui
```

This serves the board at http://127.0.0.1:8765/ and opens it in your browser. You can also ask Claude to "open the tasks UI".

## Tasks

A task is a unit of work, such as implementing a ticket, reviewing a PR, an investigation or a release. One-off questions don't get a task.

Each task has a kind and a stage. Setting a stage also sets the status it implies, so most updates only pass `--stage`:

| Kind | Stages → status |
| --- | --- |
| `implement` | `planning`, `coding`, `pr_draft`, `addressing_feedback`, `approved` → in_progress; `in_review` → blocked |
| `review` | `reviewing`, `re_review` → in_progress; `waiting_on_author` → blocked |
| `investigate` | `digging`, `writing_up` → in_progress; `waiting_on_info` → blocked |
| `release` | `cutting`, `rc_testing`, `verifying` → in_progress |
| `other` | any short text; set `--done-when` to describe the end point |

Statuses are `todo`, `in_progress`, `blocked` and `done`.

### Rules

- **Matching**: `add` with a `--pr` or `--ticket` that's already tracked updates the existing task instead of creating a new one, and reopens it if it was done.
- **Subtasks**: one level deep. When a ticket gets a second PR, the first PR moves into a subtask alongside the new one. A review of a PR stack is a parent task with one subtask per PR.
- **Priority**: open tasks are kept in priority order, top first. New and reopened tasks go to the bottom. Reorder them with `move` or by dragging cards on the board.
- **Archive**: done tasks move to the archive in the order they were finished. A parent can't be closed while it has open subtasks.
- **Formats**: tickets are Jira keys (`ENG-1234`) and PRs are `owner/repo#N`, since matching is exact.

### Commands

Run any command with `--help` for its options.

```bash
jeli.py add --title "Review PR 5800" --kind review --stage reviewing --pr acme/api#5800
jeli.py update 12 --stage waiting_on_author     # also --status, --branch, --pr, --due, --link, ...
jeli.py done 12
jeli.py list                                    # open tasks; --done for the archive
jeli.py show 12
jeli.py find --pr acme/api#5800                 # or --ticket ENG-1234
jeli.py move 12 --top                           # or --bottom, --before ID, --after ID
jeli.py delete 12                               # permanent; --with-subtasks if it has any
```

## Agents

The hook keeps one record per Claude Code session. The Agents tab groups sessions by what they need from you:

| Group | States |
| --- | --- |
| **Needs you** | `blocked`: a permission prompt or question is waiting. `your_turn`: the turn is over. |
| **Running** | `working`, or `waiting_bg`: the turn is over but a background subagent is still running. |
| **Waiting on others** | The turn is over, but the session's task is blocked (in review, waiting on its author, ...). |
| **Quiet** | `idle`: just started, or left in `your_turn` for 2 hours. Also sessions that ended in the last 24 hours. |

Each card shows the session's task, repo and branch, how long it has been in its current state, and what it's doing or last said. Sessions renamed with `/rename` show their name.

The board plays a chime when a session has been in **Needs you** for 2 minutes, so a quick back-and-forth doesn't trigger it on every turn. There's a mute button in the header.

### Linking sessions to tasks

A session is linked to a task when:

- Claude runs `jeli.py add` or `jeli.py update` in it (the CLI reads `CLAUDE_CODE_SESSION_ID`), or
- you link it manually with `jeli.py agents link <session> <task>`, or
- its git branch matches the branch of an open task (shown as "by branch").

### Resuming sessions

When a task has no live session, its card shows the last session that worked on it, with a **copy resume** button that copies `cd <dir> && claude --resume <session-id>`. Ended sessions are kept for 30 days, matching Claude Code's default transcript retention; a session can't be resumed once its transcript is gone.

### Commands

```bash
jeli.py agents                       # live sessions, most urgent first; --all adds ones that ended in the last 24 hours
jeli.py agents link <session> <task> # <session> can be a unique prefix of the session id
jeli.py agents unlink <session>
jeli.py agents forget <session>
```

## Data

All data lives in `~/jeli/data/`, which is gitignored:

- `tasks.db`: tasks, subtasks and links.
- `agents.db`: sessions. It contains prompts, commands and Claude's latest messages, so it's created readable by your user only.

Both are SQLite databases in WAL mode. Every change runs in a single write transaction, so the CLI, the board and concurrent sessions can all write safely. Set `JELI_DATA_DIR` to use a different directory.

To debug the hook, create `~/jeli/data/agents/raw.jsonl`. While the file exists, the hook appends every raw event payload to it. Delete the file to stop logging; it grows quickly.

## Development

```bash
cd ~/jeli
uv run --with pytest --with pyyaml python -m pytest tests/test_store.py
# The hook runs on the system Python 3.9, so its tests run there too:
uv run --python /usr/bin/python3 --with pytest python -m pytest tests/test_agents.py
uvx ruff check .
```

| File | What it does |
| --- | --- |
| `jeli.py` | CLI entry point |
| `store.py` | Task database and the tracker's rules |
| `agents.py` | Session database and state machine |
| `agent_hook.py` | Claude Code hook entry point (standard library only) |
| `server.py` | Board server (binds to 127.0.0.1) |
| `static/` | Board page; all colors come from `palette.css`, previewed in `palette.html` |
| `skills/tasks/` | The Claude Code skill |
| `CLAUDE.example.md`, `settings.example.json` | The `CLAUDE.md` section and hooks to install |

The board server loads the code once at startup, so restart `jeli.py ui` after changing `store.py`, `agents.py` or `server.py`. The hook runs fresh on every event and needs no restart.
