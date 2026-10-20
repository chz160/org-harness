"""Shared task board, workflow transitions and review gates. All rules come from org.yaml."""
import os
import subprocess

from . import bus, store, worktree
from .org import HUMAN, HarnessError


def run_hook(ctx, event, **env):
    """Org-supplied shell command for `event`, run inside the org folder. Returns (ok, output).
    This is the only place domain behaviour plugs in (run tests, call a scanner, ...)."""
    cmd = (ctx.cfg.get("hooks") or {}).get(event)
    if not cmd:
        return True, ""
    e = dict(os.environ, HARNESS_RUN=str(ctx.run.path), HARNESS_EVENT=event,
             **{k.upper(): str(v) for k, v in env.items()})
    p = subprocess.run(cmd, shell=True, cwd=ctx.cfg["_dir"], env=e, capture_output=True, text=True, timeout=600)
    out = (p.stdout + p.stderr).strip()
    ctx.run.log("hook", f"{event} exit={p.returncode} {out[:120]}")
    return p.returncode == 0, out


def _path(tid):
    return f"tasks/{tid}.json"


def get(ctx, tid):
    t = ctx.run.read(_path(tid))
    if t is None:
        raise HarnessError(f"no task {tid}")
    return t


def all_tasks(ctx):
    d = ctx.run.p("tasks")
    return [ctx.run.read(f"tasks/{f.name}") for f in sorted(d.glob("T*.json"))] if d.exists() else []


def _save(ctx, t, who, note):
    t["history"].append({"ts": store.now(), "by": who, "note": note})
    ctx.run.write(_path(t["id"]), t)
    ctx.run.log(who, f"{t['id']}: {note}")


def create(ctx, who, title, owner=None, priority=2, deps=(), accept=(), kind=None, parent=None):
    ctx.check_actor(who)
    if owner and owner != HUMAN:
        if owner not in ctx.roles:
            raise HarnessError(f"unknown owner '{owner}'")
        if not ctx.may_assign(who, owner):
            raise HarnessError(f"role '{who}' may not assign work to '{owner}' (see can_assign)")
    if kind and kind not in (ctx.cfg.get("kinds") or []):
        raise HarnessError(f"unknown kind '{kind}'; kinds: {', '.join(ctx.cfg.get('kinds') or [])}")
    for d in deps:
        get(ctx, d)
    with ctx.run.lock():
        tid = ctx.run.next_id("task", "T")
        t = {"id": tid, "title": title, "owner": owner, "status": ctx.cfg["workflow"]["initial"],
             "priority": priority, "deps": list(deps), "accept": list(accept), "kind": kind,
             "parent": parent, "created_by": who, "blocked": None, "awaiting_human": None,
             "approvals": [], "history": []}
        _save(ctx, t, who, f"created, owner={owner}")
    if owner and owner != who:
        bus._deliver(ctx, who, [owner], f"New task {tid}: {title}", "assignment", tid)
    return t


def claim(ctx, who, tid):
    with ctx.run.lock():
        t = get(ctx, tid)
        if t["owner"] and t["owner"] != who:
            raise HarnessError(f"{tid} is owned by '{t['owner']}'; ask them or your lead to hand it off")
        t["owner"] = who
        _save(ctx, t, who, "claimed")
    return t


def handoff(ctx, who, tid, to, note=""):
    with ctx.run.lock():
        t = get(ctx, tid)
        if t["owner"] != who and who != HUMAN and not ctx.may_assign(who, t["owner"] or who):
            raise HarnessError(f"only the owner ('{t['owner']}') or their lead may hand off {tid}")
        if to not in ctx.roles or not (ctx.may_message(who, to) or ctx.may_assign(who, to)):
            raise HarnessError(f"role '{who}' may not hand work to '{to}'")
        t["owner"] = to
        _save(ctx, t, who, f"handed off to {to}" + (f": {note}" if note else ""))
    bus._deliver(ctx, who, [to], f"Handed to you: {tid} {t['title']}. {note}", "handoff", tid)
    return t


def set_flag(ctx, tid, **flags):
    with ctx.run.lock():
        t = get(ctx, tid)
        t.update(flags)
        _save(ctx, t, "engine", "flags " + ", ".join(f"{k}={v}" for k, v in flags.items()))


def block(ctx, who, tid, reason):
    t = get(ctx, tid)
    set_flag(ctx, tid, blocked=reason)
    lead = ctx.roles[who]["reports_to"] if who != HUMAN else None
    if lead and lead != HUMAN:
        bus.send(ctx, who, lead, f"{tid} blocked: {reason}", task=tid, kind="blocked")
    return t


def unblock(ctx, who, tid):
    set_flag(ctx, tid, blocked=None)


def _gate_for(ctx, t, frm, to):
    for g in ctx.cfg.get("gates") or []:
        if g["transition"] == f"{frm}->{to}" and (not g.get("kinds") or t["kind"] in g["kinds"]):
            return g
    return None


def _check_transition(ctx, who, t, to):
    frm = t["status"]
    tr = next((x for x in ctx.cfg["workflow"]["transitions"] if x["from"] == frm and x["to"] == to), None)
    if not tr:
        legal = [x["to"] for x in ctx.cfg["workflow"]["transitions"] if x["from"] == frm]
        raise HarnessError(f"{t['id']}: no transition {frm} -> {to}. From '{frm}' you can go to: {', '.join(legal) or 'nowhere (terminal)'}")
    by = tr.get("by", ["any"])
    if who != HUMAN and "any" not in by and who not in by and not ("owner" in by and t["owner"] == who):
        raise HarnessError(f"role '{who}' may not move {frm} -> {to} (allowed: {', '.join(by)})")
    if t["blocked"]:
        raise HarnessError(f"{t['id']} is blocked: {t['blocked']}. Unblock it first")
    if t["awaiting_human"]:
        raise HarnessError(f"{t['id']} is waiting on a decision from the {ctx.cfg['human']['title']}")
    if frm == ctx.cfg["workflow"]["initial"]:
        waiting = [d for d in t["deps"] if not ctx.is_terminal(get(ctx, d)["status"])]
        if waiting:
            raise HarnessError(f"{t['id']} depends on unfinished tasks: {', '.join(waiting)}")
    g = _gate_for(ctx, t, frm, to)
    if g:
        have = len({a["by"] for a in t["approvals"] if a["gate"] == g["name"]})
        if have < g.get("approvals", 1):
            raise HarnessError(f"gate '{g['name']}' not passed for {t['id']}: {have}/{g.get('approvals', 1)} approvals. "
                               f"Ask for a review from: {', '.join(g['signoff'])}")
    return g


def move(ctx, who, tid, to):
    with ctx.run.lock():
        t = get(ctx, tid)
        _check_transition(ctx, who, t, to)
        _apply_move(ctx, who, t, to)
    _after_move(ctx, t)
    return t


def _after_move(ctx, t):
    """Entering a state: run the org's transition hook, tell the sign-off roles a review is due, and
    merge the owner's branch if the state is flagged `merge` and the run used --auto-merge."""
    run_hook(ctx, "on_transition", task=t["id"], status=t["status"], owner=t["owner"] or "")
    owner = t["owner"]
    if ctx.states[t["status"]].get("merge") and ctx.meta.get("auto_merge") and owner in ctx.roles:
        err = worktree.merge(ctx, owner)
        if err:
            set_flag(ctx, t["id"], blocked=f"merge failed: {err}")
            bus._deliver(ctx, "engine", [owner], f"{t['id']} could not be merged: {err}", "blocked", t["id"])
    for g in ctx.cfg.get("gates") or []:
        if g["transition"].startswith(t["status"] + "->") and (not g.get("kinds") or t["kind"] in g["kinds"]):
            bus._deliver(ctx, "engine", [r for r in g["signoff"] if r != t["owner"]],
                         f"{t['id']} '{t['title']}' awaits your review (gate '{g['name']}'). "
                         f"Accept: {'; '.join(t['accept']) or 'n/a'}", "review-request", t["id"])


def _apply_move(ctx, who, t, to):
    frm = t["status"]
    t["status"] = to
    t["approvals"] = []
    _save(ctx, t, who, f"{frm} -> {to}")


def review(ctx, who, tid, verdict, note=""):
    """Gate sign-off. approve: counts toward the gate and fires the transition once satisfied.
    reject: sends the task to the gate's on_reject state and delivers feedback to the owner."""
    if verdict == "approve":  # org check (e.g. run the tests) happens outside the state lock
        t0 = get(ctx, tid)
        g0 = next((g for g in ctx.cfg.get("gates") or [] if g["transition"].startswith(t0["status"] + "->")
                   and (not g.get("kinds") or t0["kind"] in g["kinds"]) and (who in g["signoff"] or who == HUMAN)), None)
        if g0:
            ok, out = run_hook(ctx, "on_gate", task=tid, gate=g0["name"], who=who, kind=t0["kind"] or "",
                               workdir=worktree.base_dir(ctx))
            if not ok:
                raise HarnessError(f"gate '{g0['name']}' check failed, approval not recorded: {out[-400:]}")
    with ctx.run.lock():
        t = get(ctx, tid)
        before = t["status"]
        gates = [g for g in ctx.cfg.get("gates") or []
                 if g["transition"].startswith(t["status"] + "->") and (not g.get("kinds") or t["kind"] in g["kinds"])]
        if who == t["owner"] and who != HUMAN:
            raise HarnessError(f"{tid}: you own this task, so you cannot review it. Ask another sign-off role")
        g = next((g for g in gates if who in g["signoff"] or who == HUMAN), None)
        if not g:
            need = sorted({r for g in gates for r in g["signoff"]})
            raise HarnessError(f"{tid} in '{t['status']}' has no review for role '{who}'. "
                               + (f"Reviewers: {', '.join(need)}" if need else "No gate applies in this state."))
        if verdict == "reject":
            if not note:
                raise HarnessError("a rejection needs specific feedback: --note \"...\"")
            t["status"] = g["on_reject"]
            t["approvals"] = []
            _save(ctx, t, who, f"gate '{g['name']}' REJECTED -> {g['on_reject']}: {note}")
            owner = t["owner"]
        else:
            t["approvals"] = [a for a in t["approvals"] if a["by"] != who] + [{"gate": g["name"], "by": who, "ts": store.now(), "note": note}]
            _save(ctx, t, who, f"gate '{g['name']}' approved" + (f": {note}" if note else ""))
            have = len({a["by"] for a in t["approvals"]})
            if have >= g.get("approvals", 1) or who == HUMAN:  # a human approval always satisfies the gate
                _apply_move(ctx, who, t, g["transition"].split("->")[1])
            owner = None
    if verdict == "reject" and owner:  # feedback always reaches the owner, whatever the DM rules say
        bus._deliver(ctx, who, [owner], f"{tid} sent back by gate '{g['name']}': {note}", "feedback", tid)
    if t["status"] != before:
        _after_move(ctx, t)
    return t


def veto(ctx, who, tid, note):
    """Human stops a task: it is blocked with the reason and the owner is told."""
    if who != HUMAN:
        raise HarnessError("only the " + ctx.cfg["human"]["title"] + " can veto")
    t = get(ctx, tid)
    set_flag(ctx, tid, blocked=f"vetoed by {ctx.title(HUMAN)}: {note}")
    if t["owner"]:
        bus._deliver(ctx, HUMAN, [t["owner"]], f"{tid} vetoed: {note}", "veto", tid)
    return t


def decide(ctx, who, title, context, decision, task=None):
    with ctx.run.lock():
        did = ctx.run.next_id("adr", "ADR-")
        body = (f"# {did}: {title}\n\n- Date: {store.now()}\n- Decided by: {ctx.title(who)} ({who})\n"
                + (f"- Task: {task}\n" if task else "") + f"\n## Context\n{context}\n\n## Decision\n{decision}\n")
        f = ctx.run.p("decisions", f"{did}.md")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
        ctx.run.log(who, f"decision {did}: {title}")
    return did


def render(ctx, tasks=None):
    tasks = all_tasks(ctx) if tasks is None else tasks
    lines = []
    for t in tasks:
        flag = (" BLOCKED:" + t["blocked"]) if t["blocked"] else (" AWAITING-HUMAN" if t["awaiting_human"] else "")
        dep = f" deps={','.join(t['deps'])}" if t["deps"] else ""
        lines.append(f"{t['id']} [{t['status']}] p{t['priority']} owner={t['owner'] or '-'} {t['title']}{dep}{flag}")
    return "\n".join(lines) or "(no tasks)"
