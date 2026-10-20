# Create your own org

1. `cp -r orgs/_template orgs/my-org`
2. Edit `orgs/my-org/org.yaml`. Keys the template leaves commented out are optional.
3. `python3 -m harness validate my-org` and fix what it reports. Errors name the exact path, for example `roles.qa.reports_to: unknown role 'x'`.
4. `python3 -m harness run --org my-org --goal "..."`

Do not edit anything under `harness/`. If an idea needs code, look for a config option or an org hook first.

## What to decide, in order

**Roles.** One entry per role: `title`, `responsibilities`, `reports_to` (another role or `human`), `can_message`, `tools`.
- `can_message` is who a role may message directly: role ids, `@reports`, `@all`, or `@channel:<name>`. Messaging the role you report to is always allowed. Anyone else must go through a lead or an escalation.
- `can_assign` is whom the role may give tasks to (same selectors). Leads need it; workers don't.
- `tools` is the allowlist, for example `[Read, Grep, Glob, "Bash(npm test *)"]`. Leave out `Edit`/`Write` for read-only roles.
- `owns: ["dir/**"]` restricts edits to those paths. `workspace: worktree` gives the role its own git branch.
- `prompt: prompts/role.md` adds a persona file. `model` and `limits: {max_turns, max_minutes}` are per role.

**Human.** `human.title` is what you are called; `human.reports` lists the roles that receive the goal.

**Workflow.** `states` (mark finish states `terminal: true`), `initial`, and `transitions` (`from`, `to`, `by`). `by` is a list of role ids, or `owner`, or `any`. Different orgs use different state names; the engine doesn't care.

**Gates.** Make "done" mean something: `{name, transition: "a->b", signoff: [roles], approvals: 1, on_reject: some_state}`. Add `kinds: [...]` to apply it only to some task kinds (declare them in the top-level `kinds`). The task owner can't review their own task.

**Escalation.** `categories` maps a name to where it goes (`to: role|human`). `always_human` lists categories that reach you at any autonomy level. `autonomy` lists, per level, which categories reach you; `all` means everything. A role with `decides: [category]` may settle that category alone.

**Limits.** `parallel`, `max_ticks`, `max_minutes`, `stall_ticks`, `pingpong`.

**Hooks.** `hooks: {on_gate: "./check.sh", on_transition: ..., on_finish: ...}` run shell commands from your org folder with `HARNESS_RUN`, `TASK`, `GATE`, `WHO` in the environment. `on_gate` runs before a sign-off is recorded; a non-zero exit refuses it with the command's output. This is where domain-specific behaviour (run tests, run a scanner) belongs.

**Context files.** `org.context: [context/rules.md]` are given to every agent: style guides, rules of engagement, standards.

## A good first org

Two or three roles, three states, one gate. Run a tiny goal with `--autonomy ask-often` and read `events.log` and `agents/*.log` to see how your prompts land. Tighten `can_message` and `tools` after it works.
