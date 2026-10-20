"""Tick scheduler. A role wakes when it has unread messages or its owned work changed since it
last ran. The run ends when every task is terminal, a human decision is pending, or nobody has work."""
import concurrent.futures as cf
import json
import time

from . import agent, board, bus
from .org import HUMAN


def _sig(ctx, role):
    tasks = board.all_tasks(ctx)
    state = {t["id"]: t["status"] for t in tasks}
    mine = [(t["id"], t["status"], t["owner"], all(ctx.is_terminal(state.get(d, "")) for d in t["deps"]))
            for t in tasks
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


def _progress(ctx):
    """A number that changes whenever any task changes state, owner or flag, or an ADR is added."""
    n = sum(len(t["history"]) for t in board.all_tasks(ctx))
    d = ctx.run.p("decisions")
    return n + (len(list(d.glob("*.md"))) if d.exists() else 0)


def _pingpong(ctx, since_msg, limit):
    """Pair of roles that exchanged `limit`+ direct messages since the last progress."""
    pairs = {}
    for m in ctx.run.read_jsonl("messages.jsonl")[since_msg:]:
        if m["kind"] == "dm":
            k = tuple(sorted((m["from"], m["to"])))
            pairs[k] = pairs.get(k, 0) + 1
    return next((k for k, v in pairs.items() if v >= limit), None)


def _trip(ctx, category, text):
    bus._deliver(ctx, "engine", [HUMAN], text, "escalation", None, extra={"category": category})


def loop(ctx, max_ticks=50, parallel=3, runner=agent.run_turn):
    lim = ctx.cfg.get("limits", {})
    deadline = time.time() + lim["max_minutes"] * 60 if lim.get("max_minutes") else None
    stall_n, pp_n = lim.get("stall_ticks", 6), lim.get("pingpong", 8)
    last, idle_ticks, mark = _progress(ctx), 0, len(ctx.run.read_jsonl("messages.jsonl"))
    for tick in range(1, max_ticks + 1):
        if ctx.run.p("state", "paused").exists():
            return "paused"
        if finished(ctx):
            return "done"
        if deadline and time.time() > deadline:
            return "time-limit"
        if bus.open_escalations(ctx) and not any(wants_wake(ctx, r) for r in ctx.roles):
            return "needs-human"
        wake = [r for r in ctx.roles if wants_wake(ctx, r)]
        if not wake:
            return "idle"
        with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
            list(ex.map(lambda r: runner(ctx, r, tick), wake))
        for r in wake:  # remember post-run state so a role only re-wakes on outside changes
            ctx.run.write(f"state/sig_{r}.json", _sig(ctx, r))
        now = _progress(ctx)
        if now != last:
            last, idle_ticks, mark = now, 0, len(ctx.run.read_jsonl("messages.jsonl"))
            continue
        idle_ticks += 1
        pair = _pingpong(ctx, mark, pp_n)
        if pair:
            _trip(ctx, "loop", f"{pair[0]} and {pair[1]} have exchanged {pp_n}+ messages with no task progress. Redirect them or decide for them.")
            return "loop-detected"
        if idle_ticks >= stall_n:
            _trip(ctx, "stall", f"{idle_ticks} ticks passed with no task progress. Roles woken last: {', '.join(wake)}.")
            return "stalled"
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
