---
name: tasks
description: Log and update my work in the jeli task tracker (~/jeli). Use at the start of any unit of work (planning or implementing a ticket/PR, PR code review, investigation/RCA, release) alongside whatever skill does the work, and when it ends. Also for /tasks list, done, show.
---

# Tasks

Keep my jeli tasks (`~/jeli/data/tasks.db`) in sync with the work I ask Claude to do. Always go through the script, never edit the database by hand:

```bash
~/jeli/jeli.py <command> ...    # add | update | done | delete | move | list | find | show; --help on each
```

`delete ID` removes a task for good, so only run it when I ask for a task to be deleted. Dropped or finished work gets `done`, not `delete`. It prints the removed task in full; show me that output.

## Input

$ARGUMENTS

If arguments were passed (`list`, `done 12`, `show 12`, ...), run the matching command, print the result, and stop. With no arguments, run `list`.

For `ui`, or when I ask to see or open the tasks UI: run `~/jeli/jeli.py ui` in the background (it keeps running and opens my browser), then tell me the URL it prints. If the port is already in use, the UI is probably already running; give me `http://127.0.0.1:8765/` instead of starting another one.

## What gets a task

A unit of work, not a single question:

| kind | keywords | examples |
| --- | --- | --- |
| `implement` | implement, plan, build, fix, fix ticket, start on, work on, pick up, refactor, migrate, upgrade, address PR comments, fix CI, resolve conflicts, update the branch, self-review | working on a ticket, planning a ticket before coding it, building a feature, fixing a bug, updating a PR branch, self-review before opening a PR |
| `review` | review, re-review, look over, check this PR | reviewing someone else's PR |
| `investigate` | investigate, RCA, root cause, debug, why is, dig into, triage, repro | debugging an incident, digging into a perf problem, triaging a vuln or dependency alert |
| `release` | release, hotfix, patch release, cut, tag, rc, verify release | sprint or patch release, tag pushes, release verification |
| `other` | design doc, spike, write up, set up, tooling | anything else clearly multi-step |

Keywords are hints, not exact matches. Go by what the work is: planning a ticket and self-reviewing my own changes are still `implement`, because they're steps of implementing it, and the same task carries through to the PR. Don't log one-off questions, lookups, or explanations. If you can't tell whether something counts, ask me in one line before logging it.

## Logging a task

At the start of the work, before anything else:

1. `~/jeli/jeli.py find --ticket ENG-1234` (or `--pr owner/repo#N`) if the work has a ticket or PR.
2. `~/jeli/jeli.py add --title "..." --kind <kind> --stage <stage>` with every field you know: `--ticket`, `--pr`, `--repo`, `--branch`, `--due`, `--link`. If `find` matched, `add` reuses that task (and reopens it if it was done), so call it either way.
3. Tell me in one line: `Logged #12: Review PR 5800 (consent cache TTL)`, or `Picked up #12` when it matched.

Formats: tickets are Jira keys (`ENG-1234`), PRs are always `owner/repo#N` (`ethyca/fidesplus#5800`), since matching is exact. Titles are short and say what the work is, not the ticket summary verbatim.

## Priority

The order of open tasks is my priority order, top first, and `list` prints them in that order. The same goes for subtasks within a parent. New and reopened tasks go to the bottom.

- Reorder with `move ID --top | --bottom | --before X | --after X`, never by editing the database.
- If I say something is urgent, a priority, or should come before/after another task, move it when logging it.
- Don't reorder anything on your own otherwise.

## Stages

`stage` says exactly where a task is; `status` is the general state. Setting `--stage` sets the status too, so set the stage and don't pass `--status` alongside it. Subtasks use their parent's kind's stages.

| kind | stage → status |
| --- | --- |
| `implement` | `planning`, `coding`, `pr_draft`, `addressing_feedback`, `approved` → in_progress; `in_review` → blocked |
| `review` | `reviewing`, `re_review` → in_progress; `waiting_on_author` → blocked |
| `investigate` | `digging`, `writing_up` → in_progress; `waiting_on_info` → blocked |
| `release` | `cutting`, `rc_testing`, `verifying` → in_progress |
| `other` | any short text; status is left alone |

Always log a task with the stage it's at, and move it along as the work moves: a PR opened as a draft → `pr_draft`, marked ready for review → `in_review`, review comments came in → `addressing_feedback`, I posted a review with comments → `waiting_on_author`. A plain `--status` with no stage (e.g. `blocked` on something outside the list) drops a stage that contradicts it.

## Subtasks

A task can have subtasks, one level deep (a subtask can't have its own). Subtasks share the parent's `kind`, `ticket`, and `repo`, so don't pass those.

- **implement**: one subtask per PR when a ticket ships in more than one PR. A ticket with a single PR keeps it on the parent. When a second PR comes along, `add --ticket ENG-1234 --pr owner/repo#N` creates the subtask on its own and moves the parent's first PR into a subtask too. The moved subtask gets the PR as its title, so give it a real one with `update ID --title "..."`.
- **review**: one subtask per PR when I'm reviewing a stack. Log the stack as the parent (`add --title "Review <stack>" --kind review`), then `add --parent ID --pr ...` for each PR.

Anything else that splits into separate pieces I'd track on their own can use subtasks too, with `add --parent ID`.

## While working

Update the task when its state changes, not on every step. There are no notes: findings and decisions belong in chat, the PR, or the ticket, not in the tracker.

- the work moves to a new stage → `update ID --stage ...`
- a branch or PR gets created → `update ID --branch ...` / `--pr ...` (with the matching stage)
- the work is blocked on something no stage covers → `update ID --status blocked`, back to `in_progress` when it unblocks

## Done, per kind

Close the task yourself as soon as its end point is reached, and tell me in one line what happened: `Closed #12: PR merged`.

- **implement**: the PR is merged. Opening the PR isn't done; set the stage (`pr_draft` / `in_review`) and leave it open. If the work turns out to need no code change, or I drop it, that's done too (say which when you tell me).
- **review**: I approved the PR (or it merged). Delivering findings isn't done: if I left comments, the author still has to address them and I still have to re-review, so set the stage to `waiting_on_author`, and to `re_review` when I pick up the re-review.
- **a subtask**: same rule as its parent's kind, applied to that one PR.
- **a parent with subtasks**: when its last subtask closes, the script says so. Close the parent if no more work is expected (no other PRs planned for the ticket, no more PRs in the stack); otherwise leave it open. The script won't let you close a parent while it has open subtasks.
- **investigate**: the root cause is identified and written up (in chat, a doc, or a ticket comment), or I decide to stop pursuing it.
- **release**: the release is published and `verify-release` has passed.
- **other**: whatever its `done_when` says. Always log an `other` task with `--done-when "..."` (a short outcome, e.g. "action items page shared with the team") so there's something to close against; ask me if it isn't obvious.

When running `list`, check open tasks and subtasks that have a PR against GitHub, then apply the parent rule above:

- `implement`: `gh pr view <N> --repo <owner/repo> --json state`; close it if `MERGED`.
- `review`: `gh pr view <N> --repo <owner/repo> --json state,reviews`; close it if `MERGED` or if my latest review (login from `gh api user --jq .login`) is `APPROVED`.
