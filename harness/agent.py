"""Runs one agent turn as a resumable `claude -p` session. Subscription auth is used (never
--bare, and ANTHROPIC_API_KEY is removed from the child env so -p cannot silently bill per token)."""
import json
import os
import pathlib
import subprocess
import sys

from . import board, bus
from .org import HUMAN

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = "python" if os.name == "nt" else "python3"
CLI = f"{PY} -m harness"


def system_prompt(ctx, role):
    cfg, r = ctx.cfg, ctx.roles[role]
    org_dir = pathlib.Path(cfg["_dir"])
    parts = [f"You are {r['title']} ({role}) in the organisation '{cfg['org']['name']}'. "
             f"{cfg['org'].get('description', '')}\nYour human is the {ctx.title(HUMAN)}. Autonomy level: {ctx.autonomy}."]
    if r.get("responsibilities"):
        parts.append("## Your responsibilities\n" + r["responsibilities"].strip())
    if r.get("prompt"):
        parts.append((org_dir / r["prompt"]).read_text(encoding="utf-8"))
    for f in cfg["org"].get("context", []):
        parts.append(f"## Org context: {f}\n" + (org_dir / f).read_text(encoding="utf-8"))
    wf = cfg["workflow"]
    trans = "\n".join(f"  {t['from']} -> {t['to']}  (by: {', '.join(t.get('by', ['any']))})" for t in wf["transitions"])
    gates = "\n".join(f"  {g['transition']}: needs {g.get('approvals', 1)} approval(s) from {', '.join(g['signoff'])}; rejection returns to '{g['on_reject']}'"
                      for g in cfg.get("gates") or []) or "  (none)"
    people = "\n".join(f"  {k}: {v['title']}" for k, v in ctx.roles.items())
    cats = "\n".join(f"  {k} -> {v['to']}" for k, v in (cfg.get("escalation", {}).get("categories") or {}).items()) or "  (none)"
    parts.append(f"""## How this organisation works
Everything you do to coordinate happens through this command (run it with Bash): `{CLI} <command>`
  msg <role|@reports|@lead|@all|@channel:name> "text" [--task T001]   message someone (only permitted recipients)
  inbox                                  read new messages
  task list | show T001 | new "title" --owner ROLE [--dep T001] [--accept "criterion"] [--kind K] [--priority 1-3]
  task claim T001 | handoff T001 --to ROLE | block T001 --reason "..." | unblock T001 | move T001 STATE
  review approve|reject T001 --note "specific feedback"      (only if you are a sign-off role for that gate)
  escalate CATEGORY "what you need decided" [--task T001]    when blocked or outside your authority
  decide "title" --context "..." --decision "..." [--task T001]   record a decision (ADR) so it survives sessions
Roles:
{people}
Workflow states: {', '.join(wf['states'])} (initial: {wf['initial']})
Allowed transitions:
{trans}
Review gates:
{gates}
Escalation categories:
{cats}
Rules: act only through the command above; the engine rejects anything outside your role. Finish by
saying what you did and what you are waiting on. Do not do other roles' work; message or assign instead.""")
    return "\n\n".join(parts)


def digest(ctx, role, tick):
    msgs = bus.inbox(ctx, role)
    mine = [t for t in board.all_tasks(ctx) if t["owner"] == role and not ctx.is_terminal(t["status"])]
    lines = [f"Tick {tick}. Goal: {ctx.meta['goal']}", "", "New messages:"]
    lines += [f"  {m['id']} from {m['from']}{' re ' + m['task'] if m['task'] else ''} [{m['kind']}]: {m['text']}" for m in msgs] or ["  (none)"]
    lines += ["", "Your open tasks:", board.render(ctx, mine), "", "Whole board:", board.render(ctx), "",
              "Do what your role requires now, using the command from your instructions."]
    return "\n".join(lines)


def run_turn(ctx, role, tick):
    r = ctx.roles[role]
    d = ctx.run.p("agents")
    d.mkdir(exist_ok=True)
    prompt_f = d / f"{role}.system.md"
    prompt_f.write_text(system_prompt(ctx, role), encoding="utf-8")
    settings_f = d / f"{role}.settings.json"
    settings_f.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                          "hooks": [{"type": "command", "command": f"{CLI} hook pretool"}]}]}}), encoding="utf-8")
    tools = list(r.get("tools", [])) + [f"Bash({CLI} *)"]
    lim = r.get("limits", {})
    cmd = [os.environ.get("HARNESS_CLAUDE_BIN", "claude"), "-p", digest(ctx, role, tick),
           "--append-system-prompt-file", str(prompt_f), "--allowedTools", ",".join(tools),
           "--settings", str(settings_f), "--max-turns", str(lim.get("max_turns", 30)),
           "--output-format", "json", "--permission-mode", "dontAsk"]
    if r.get("model"):
        cmd += ["--model", r["model"]]
    sid_f = f"agents/{role}.session"
    sid = ctx.run.p(sid_f).read_text().strip() if ctx.run.p(sid_f).exists() else None
    if sid:
        cmd += ["--resume", sid]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update(HARNESS_RUN=str(ctx.run.path), HARNESS_ROLE=role,
               PYTHONPATH=str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""))
    ctx.run.log("engine", f"wake {role} (tick {tick})")
    try:
        p = subprocess.run(cmd, cwd=ctx.meta.get("workdir") or os.getcwd(), env=env, capture_output=True,
                           text=True, timeout=lim.get("max_minutes", 20) * 60)
    except subprocess.TimeoutExpired:
        ctx.run.log("engine", f"{role} timed out")
        return
    out = p.stdout.strip()
    try:
        res = json.loads(out)
        if res.get("session_id"):
            ctx.run.p(sid_f).write_text(res["session_id"])
        out = res.get("result", out)
    except ValueError:
        pass
    ctx.run.append(f"agents/{role}.log", f"--- tick {tick} exit={p.returncode}\n{out}\n{p.stderr[-500:]}")
    if p.returncode != 0:
        ctx.run.log("engine", f"{role} exited {p.returncode}: {p.stderr[-200:].strip()}")
