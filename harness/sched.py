"""Tick scheduler. A role wakes when it has unread messages or its owned work changed since it
last ran. The run ends when every task is terminal, a human decision is pending, or nobody has work."""
import concurrent.futures as cf
import json
import time

from . import agent, board, bus
from .org import HUMAN


def _sig(ctx, role):
    mine = [(t["id"], t["status"], t["owner"]) for t in board.all_tasks(ctx)
            if t["owner"] == role and not ctx.is_terminal(t["status"]) and not t["blocked"] and not t["awaiting_human"]]
    return json.dumps(mine)


def wants_wake(ctx, role):
    if bus.inbox(ctx, role, mark_read=False):
        return True
    sig = _sig(ctx, role)
    return sig != "[]" and sig != ctx.run.read(f"state/sig_{role}.json")


def finished(ctx):
    tasks = board.all_tasks(ctx)
    return bool(tasks) and all(ctx.is_terminal(t["status"]) for t in tasks)


def start(ctx):
    """Deliver the goal to whoever reports to the human."""
    for r in ctx.reports_of(HUMAN):
        bus._deliver(ctx, HUMAN, [r], ctx.meta["goal"], "goal")


def loop(ctx, max_ticks=50, parallel=3, runner=agent.run_turn):
    started = time.time()
    for tick in range(1, max_ticks + 1):
        if finished(ctx):
            return "done"
        if bus.open_escalations(ctx) and not any(wants_wake(ctx, r) for r in ctx.roles):
            return "needs-human"
        wake = [r for r in ctx.roles if wants_wake(ctx, r)]
        if not wake:
            return "idle"
        with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
            list(ex.map(lambda r: runner(ctx, r, tick), wake))
        for r in wake:  # remember post-run state so a role only re-wakes on outside changes
            ctx.run.write(f"state/sig_{r}.json", _sig(ctx, r))
    return "max-ticks"


def summary(ctx, status):
    tasks = board.all_tasks(ctx)
    done = [t for t in tasks if ctx.is_terminal(t["status"])]
    blocked = [t for t in tasks if t["blocked"]]
    open_ = [t for t in tasks if not ctx.is_terminal(t["status"]) and not t["blocked"]]
    esc = bus.open_escalations(ctx)
    msgs = ctx.run.read_jsonl("messages.jsonl")
    adrs = sorted(p.name for p in ctx.run.p("decisions").glob("*.md")) if ctx.run.p("decisions").exists() else []
    L = [f"# Run summary: {ctx.meta['org']} / {ctx.meta['id']}", f"Goal: {ctx.meta['goal']}", f"End state: **{status}**", "",
         f"## Done ({len(done)})", board.render(ctx, done), f"## In progress / waiting ({len(open_)})", board.render(ctx, open_),
         f"## Blocked ({len(blocked)})", board.render(ctx, blocked),
         f"## Needs the {ctx.title(HUMAN)} ({len(esc)})"]
    L += [f"- {m['id']} from {m['from']} [{m['category']}]: {m['text']}  -> `reply {m['id']} \"...\"`" for m in esc] or ["(nothing)"]
    L += ["", f"## Decisions ({len(adrs)})"] + [f"- {a}" for a in adrs] + ["", f"{len(msgs)} messages logged in messages.jsonl"]
    text = "\n".join(L)
    ctx.run.p("summary.md").write_text(text, encoding="utf-8")
    return text
