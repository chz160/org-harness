# org-harness: design (Phase 1)

A small, domain-free engine that runs a team of `claude -p` agents from an **org definition** (`orgs/<name>/`) and a **goal**. All domain knowledge lives in config and prompt files; the engine only knows: roles, messages, tasks, states, gates, escalations.

## 1. Research findings (checked against code.claude.com docs, Oct 2026)

| Feature | Status | Fit |
|---|---|---|
| **Agent teams** (teammates, mailboxes, shared task list) | Experimental, off by default (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) | Covers ~50%. **Interactive only** (no `-p`/SDK), any-to-any messaging (no org-chart limits), tasks are only pending/in-progress/completed, one team per session, no resume, no per-teammate permissions at spawn, no nesting |
| **Subagents** (`.claude/agents/*.md`) | Stable | Good role *format* (tools, model, `maxTurns`, `isolation: worktree`, hooks). Single session; results funnel to the caller |
| **Hooks** | Stable | `PreToolUse` (exit 2 blocks) enforces ownership/tool rules; `TaskCompleted`/`TeammateIdle` only exist for native teams |
| **Headless `claude -p`** | Stable | `--resume <id>`, `--append-system-prompt-file`, `--allowedTools`, `--max-turns`, `--settings`, `--output-format json` |
| **Worktrees** | Stable | Per-role isolation |

**Billing.** `claude -p` uses your `/login` subscription credentials by default, so a Max plan covers it. Two traps: (1) if `ANTHROPIC_API_KEY` is set, `-p` *always* uses it (per-token billing), so the engine unsets it; (2) `--bare` ignores OAuth and **requires an API key**, so the engine never uses it. `--max-budget-usd` is a client-side estimate and meaningless on a subscription, so limits are in turns, wall-clock and ticks. Agents run at your plan's shared rate limits; many parallel agents burn the 5-hour window fast, so default parallelism is 3. *I did not verify Anthropic's usage policy for automated subscription use; please confirm it covers this before heavy use.*

**Chosen architecture:** the engine is a **tick-based scheduler over plain files**. Each agent is a resumable `claude -p` session. Agents act on the world only through one CLI (`python3 -m harness`, alias `./orgh`), which the engine checks against the org config on every call. Native agent teams were rejected because they can't be headless, can't enforce an org chart, and can't resume. A bespoke SDK/API orchestrator was rejected because it needs an API key. Subagents-only was rejected because it is the "everything funnels through one orchestrator" model you already dislike.

## 2. How it works

```
orgh run --org security-audit --goal "..." [--autonomy balanced]
  loop tick:
    for each role with work (unread msgs | owned task in an actionable state | lead at start):   # up to `parallel` at once
        claude -p "<digest: inbox + my tasks + board>" --resume <role session> \
          --append-system-prompt-file <generated: org context + role prompt + protocol> \
          --allowedTools <role tools + Bash(orgh *)> --settings <generated hooks> --max-turns N
    check stop conditions: goal tasks terminal | human decision pending | limits | stall/ping-pong
```

Agents have no direct access to state files. They call `orgh msg | task | review | escalate | decide | inbox`; `orgh` reads `HARNESS_RUN`/`HARNESS_ROLE` from env, validates against `org.yaml`, appends to the log, and returns a clear error ("role `x` may not message `y`; allowed: ..."). A generated `PreToolUse` hook (`orgh hook pretool`) denies `Edit/Write` outside the role's `owns` globs. Writes use lockfiles plus atomic rename (Windows-safe).

**Messaging.** Direct (`can_message` allowlist; messaging up your own reporting chain is always allowed), channel (`channels` membership), broadcast (`@all`). Delivery = append to `messages.jsonl` with `{id,ts,from,to,task,kind,text}`; unread messages wake the recipient next tick.
**Tasks.** `{id,title,owner,status,priority,deps,accept[],kind,parent,history[]}`. Leads create/assign, agents claim/handoff/block. A task whose deps aren't terminal can't leave the initial state.
**Workflow engine.** `orgh task move` is legal only if a `transitions` entry matches (from, to, actor) *and* every matching **gate** is satisfied. Gate: required sign-off role(s) + approval count for a transition (optionally per task `kind`). `orgh review reject --feedback` sends the task to the gate's `on_reject` state with the feedback attached as a message.
**Escalation.** `orgh escalate <category>` routes per `escalation.categories` (`to: <role|human>`). The autonomy level picks which categories reach the human (`ask-often` > `balanced` > `autonomous`); `always_human` categories ignore autonomy. A human-bound escalation pauses only the blocked tasks; other work continues.
**Safety.** Per-role `limits` (turns/minutes), run limits (ticks/minutes/parallel), **stall** (N ticks with no task/message state change), **ping-pong** (same pair exchanges N msgs with no transition). Either trips a pause and an escalation to the human. Roles with `workspace: worktree` get `git worktree` + branch `org/<run>/<role>`; a state flagged `merge: true` merges the owner's branch.
**Pluggable hooks.** `hooks:` maps events (`on_gate` can refuse a sign-off, `on_transition`, `on_finish`) to shell commands run in the org folder (e.g. run tests, call a scanner). This is the only place domain behaviour can plug in.

## 3. `org.yaml` schema (validated on load: unknown keys, dangling role refs, unreachable states, gates on missing transitions, cycles in `reports_to` all fail with `file:path: message`)

```yaml
org:   {name, description, autonomy: ask-often|balanced|autonomous, context: [rules.md, ...]}
human: {title: "Studio Head", reports: [producer], approves: [scope_change, irreversible]}
roles:
  <id>:
    title: str; prompt: prompts/<id>.md      # optional persona file
    responsibilities: str
    reports_to: <role>|human
    can_message: [<role>|"@reports"|"@channel:<c>"|"@all"]
    model: sonnet|opus|...; tools: [Read, Grep, "Bash(npm test *)"]
    owns: ["src/gameplay/**"]; workspace: shared|worktree
    decides: [<category>]                    # what it may resolve alone
    limits: {max_turns: 40, max_minutes: 20}
channels: {eng: [role, ...], all: "*"}
kinds: [code, art, finding]                  # optional task kinds
workflow:
  states: {todo: {}, in-progress: {}, review: {}, done: {terminal: true, merge: true}}
  initial: todo
  transitions: [{from: in-progress, to: review, by: [owner]}, {from: review, to: done, by: [@gate]}]
gates:
  - {name: qa, transition: "review->done", kinds: [code], signoff: [qa-lead], approvals: 1, on_reject: in-progress}
escalation:
  categories: {scope_change: {to: producer}, irreversible: {to: human}, ...}
  always_human: [irreversible]
  autonomy: {ask-often: all, balanced: [scope_change, irreversible], autonomous: []}
limits: {parallel: 3, max_ticks: 200, max_minutes: 240, stall_ticks: 6, pingpong: 8}
hooks:  {on_gate: hooks/run_tests.sh}
```

## 4. Walkthroughs (same engine, zero code differences)

**Game studio.** Goal: "Prototype a dash-and-dodge mechanic." Only the Producer wakes first; it writes ADR-001 (scope) and creates tasks: spec (Creative Director), code (Technical Director, dep: spec). Game Designer writes the spec in `design/**`; Gameplay Engineer, in a worktree, implements in `src/gameplay/**` once the dep clears, then moves `in-progress→review`. The `qa` gate needs QA Lead sign-off before `review→done` (`hooks/run_tests.sh` runs on the gate); a rejection returns the task with feedback and wakes the engineer. An Art Director request for a new model is a `scope_change`: the Producer decides at `balanced`, the Studio Head also sees it at `ask-often`. All tasks `done` ends the run with `summary.md`.

**Security audit.** Goal: "Audit repo X; scope in `scope.md`." Workflow is `scoped→testing→finding→verified→reported`. Engagement Lead wakes first, records scope/ROE as ADR-001, and creates a `scoped` task per surface for Recon, AppSec and Infra reviewers. Their `tools` are read-only plus allow-listed scanners (`Bash(semgrep *)`) and `owns` is only `findings/**`, so the target can't be edited. Evidence is required by the org's finding template and checked by the gate hook. `finding→verified` is gated on the Verifier alone; the Remediation Engineer fixes in a worktree and the Verifier re-confirms. Out-of-scope or destructive actions are `always_human`: the task pauses and asks the Engagement Owner at any autonomy. Report Writer owns `report/**`; `verified→reported` needs the Lead's sign-off.

## 5. File layout

```
harness/            engine: config.py (load+validate) store.py (files+locks) bus.py (messages) board.py (tasks+workflow+gates)
                    sched.py (ticks, limits, loop detect) agent.py (claude -p) cli.py (orgh) hooks.py   # target <1k LOC; dep: PyYAML only
orgs/<name>/        org.yaml  prompts/*.md  context/*.md  hooks/*   (+ _template/, game-studio/, security-audit/, dev-team/)
runs/<org>/<run-id>/  run.json  tasks/T-001.json  messages.jsonl  decisions/ADR-001.md  events.log  sessions/<role>.id  wt/<role>/  summary.md
```

Everything is inspectable and resumable (`orgh resume <run-id>` re-reads files and `--resume`s sessions); `tail -f events.log` is the live view and `orgh status` prints the board. A local web dashboard is deferred unless you want it.

## 6. Decisions for you

1. **Python + PyYAML** (Windows-friendly, one dep). Node is the alternative.
2. Defaulting `parallel: 3` and no API key anywhere. OK?
3. `orgh` as a Bash-invoked CLI rather than an MCP server: simpler and testable. MCP is a later swap behind the same interface.
4. Phase 2 proof task: "write a one-paragraph spec and have it reviewed", run through `dev-team` and `security-audit` configs.
