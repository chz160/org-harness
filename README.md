# org-harness

A small, domain-free engine that runs a team of Claude Code agents shaped like **any organisation you define**. You write an org (`orgs/<name>/org.yaml` plus optional prompt files), give it a goal, and the agents plan, divide the work, message each other along the org chart, review each other's work through gates, and come to you only for real decisions.

The engine knows nothing about game studios, security audits or software teams. Roles, workflows, gates, escalation rules and vocabulary all live in the org folder. Adding a new kind of org takes new config and prompt files, never code (`tests/run_proof.py` checks this).

Design and rationale: [docs/DESIGN.md](docs/DESIGN.md). Making your own org: [docs/CREATE_AN_ORG.md](docs/CREATE_AN_ORG.md).

## Requirements

- Python 3.10+ and `pip install -r requirements.txt` (PyYAML is the only dependency)
- Claude Code (`claude`) logged in with your subscription (`/login`)
- git, only if you use worktrees

**Billing:** agents run as `claude -p` sessions on your logged-in Max subscription. The engine removes `ANTHROPIC_API_KEY` from agents' environment (when set, `claude -p` always bills per token) and never uses `--bare` (it requires an API key). Limits are turns and minutes, because dollar budgets mean nothing on a subscription. Agents share your plan's rate limits, so default parallelism is 3. Check Anthropic's usage policy for automated use of subscription logins before heavy use.

## Quick start

```bash
python3 -m harness validate dev-team              # check an org definition (also: ./orgh, orgh.cmd on Windows)
python3 -m harness run --org dev-team --goal "Add a /health endpoint" --workdir /path/to/your/repo
```

Orgs shipped as examples: `dev-team`, `game-studio`, `security-audit`, and a blank `_template`.

Useful `run` options: `--autonomy ask-often|balanced|autonomous` (overrides the org default), `--parallel N`, `--max-ticks N`, `--workdir DIR` (the project the agents work on; keep `runs/` outside it), `--auto-merge` (see worktrees), `--run-id NAME`.

## How a run works

1. Your goal goes to the roles that report to you. Each agent is a resumable `claude -p` session, woken when it has unread messages or its tasks change.
2. Agents act only through the `python3 -m harness` commands (`msg`, `task`, `review`, `escalate`, `decide`). The engine checks every call against `org.yaml` and returns a clear error if the role isn't allowed.
3. Tasks move through the org's workflow. A gated transition happens only when the required roles sign off; a rejection returns the task with feedback.
4. A run ends when every task is terminal (`done`), or pauses at: `needs-human`, `stalled`, `loop-detected`, `paused`, `time-limit`, `max-ticks`, or `idle` (nobody has work). `runs/<org>/<run-id>/summary.md` says what's done, blocked, and needs you.

## Watching a run

```bash
python3 -m harness --run runs/dev-team/<run-id> watch     # live event log (or: tail -f runs/.../events.log)
python3 -m harness --run runs/dev-team/<run-id> status    # board + anything waiting on you
python3 -m harness --run runs/dev-team/<run-id> summary
```

Everything is plain files under `runs/<org>/<run-id>/`: `run.json`, `tasks/T001.json`, `messages.jsonl` (sender, recipient, time, task, kind), `decisions/ADR-001.md`, `events.log`, `agents/<role>.log` (agent transcripts), `summary.md`.

## Intervening (you are the human role)

You act as the org's human (whatever it calls you). Pass `--run` and the command:

| Do this | Command |
|---|---|
| Answer an escalation | `reply M012 "Go with option B"` |
| Approve a gated task yourself | `review approve T003 --note "ok"` (or `reject --note "..."`) |
| Veto a task | `veto T003 --note "out of scope"` |
| Redirect everyone | `redirect "Focus on X, drop Y"` (optionally `--to ROLE`) |
| Reassign work | `task handoff T003 --to ROLE` |
| Pause | `pause` (stops after the current tick) |

Agents escalate only for real decisions. Which categories reach you depends on the autonomy level; categories in the org's `always_human` list always do.

## Resuming

`python3 -m harness resume runs/dev-team/<run-id>` clears a pause and continues from the files. Agents continue their own sessions (`--resume`), so context survives. Do this after replying to an escalation, after a stall or loop pause once you've redirected, or after a crash.

## Safety rails

- **Tools:** each role's `tools` list is an allowlist (the agent runs in `dontAsk` mode, so anything else is denied). `org.deny_tools` is an org-wide denylist.
- **File ownership:** a role with `owns: [globs]` can only edit matching paths (enforced by a `PreToolUse` hook).
- **Limits:** per role `max_turns` / `max_minutes`; per run `max_ticks`, `max_minutes`, `parallel`.
- **Loops:** `stall_ticks` (no task progress) and `pingpong` (two roles trading messages without progress) pause the run and escalate to you.
- **Worktrees:** `workspace: worktree` gives a role its own git branch and checkout under the run folder. Branches are merged into the `--workdir` checkout only with `--auto-merge`, when a task enters a state flagged `merge: true`. Start such runs on an integration branch, not your main branch.

## Tests

`python3 tests/run_proof.py` runs the same engine through all shipped orgs, plus loop/stall detection, human intervention and worktree merging, using a scripted stand-in for `claude` (no tokens). It also asserts that no role name from any org appears in `harness/*.py`.
