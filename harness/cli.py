"""`python -m harness ...` : one entry point for the human and for agents. Agents get HARNESS_RUN and
HARNESS_ROLE in their environment, so they never pass identity; the human uses --as human (default)."""
import argparse
import os
import pathlib
import sys

from . import agent, board, bus, config, hooks, sched, store
from .org import HUMAN, Ctx, HarnessError

REPO = pathlib.Path(__file__).resolve().parent.parent


def find_org(name):
    for c in (pathlib.Path(name), REPO / "orgs" / name):
        if (c / "org.yaml").is_file():
            return c
    raise HarnessError(f"org '{name}' not found (looked for {name}/org.yaml and orgs/{name}/org.yaml)")


def parser():
    p = argparse.ArgumentParser(prog="python -m harness")
    p.add_argument("--run", default=os.environ.get("HARNESS_RUN"), help="run directory")
    p.add_argument("--as", dest="who", default=os.environ.get("HARNESS_ROLE", HUMAN))
    s = p.add_subparsers(dest="cmd", required=True)

    def add(name, *args, **kw):
        sp = s.add_parser(name, **kw)
        for a, k in args:
            sp.add_argument(*a if isinstance(a, tuple) else (a,), **k)
        return sp

    add("validate", ("org", {}))
    add("run", ("--org", {"required": True}), ("--goal", {"required": True}), ("--autonomy", {}),
        ("--parallel", {"type": int}), ("--max-ticks", {"type": int, "default": 50}),
        ("--root", {"default": "runs"}), ("--run-id", {}), ("--workdir", {}),
        ("--no-agents", {"action": "store_true", "help": "set up the run and exit"}))
    add("resume", ("path", {}), ("--max-ticks", {"type": int, "default": 50}), ("--parallel", {"type": int}))
    add("msg", ("target", {}), ("text", {}), ("--task", {}))
    add("inbox", ("--peek", {"action": "store_true"}))
    t = s.add_parser("task").add_subparsers(dest="sub", required=True)
    n = t.add_parser("new")
    n.add_argument("title")
    n.add_argument("--owner")
    n.add_argument("--priority", type=int, default=2)
    n.add_argument("--dep", action="append", default=[])
    n.add_argument("--accept", action="append", default=[])
    n.add_argument("--kind")
    n.add_argument("--parent")
    for name in ("claim", "show", "unblock"):
        t.add_parser(name).add_argument("id")
    h = t.add_parser("handoff")
    h.add_argument("id")
    h.add_argument("--to", required=True)
    h.add_argument("--note", default="")
    b = t.add_parser("block")
    b.add_argument("id")
    b.add_argument("--reason", required=True)
    m = t.add_parser("move")
    m.add_argument("id")
    m.add_argument("state")
    t.add_parser("list")
    add("review", ("verdict", {"choices": ["approve", "reject"]}), ("id", {}), ("--note", {"default": ""}))
    add("escalate", ("category", {}), ("text", {}), ("--task", {}))
    add("decide", ("title", {}), ("--context", {"required": True}), ("--decision", {"required": True}), ("--task", {}))
    add("reply", ("msg_id", {}), ("text", {}))
    add("status")
    add("summary")
    hk = s.add_parser("hook")
    hk.add_argument("which", choices=["pretool"])
    return p


def need_ctx(a):
    if not a.run:
        raise HarnessError("no run selected: pass --run <dir> (agents get HARNESS_RUN automatically)")
    ctx = Ctx(a.run)
    ctx.check_actor(a.who)
    return ctx


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        return dispatch(a)
    except (HarnessError, config.ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def dispatch(a):
    c = a.cmd
    if c == "validate":
        cfg = config.load(find_org(a.org))
        print(f"ok: {cfg['org']['name']} ({len(cfg['roles'])} roles, {len(cfg['workflow']['states'])} states)")
        return 0
    if c == "hook":
        return hooks.pretool()
    if c == "run":
        org_dir = find_org(a.org)
        cfg = config.load(org_dir)
        auto = a.autonomy or cfg["org"].get("autonomy", "balanced")
        if auto not in config.AUTONOMY:
            raise HarnessError(f"--autonomy must be one of {config.AUTONOMY}")
        run = store.create(a.root, cfg["org"]["name"], org_dir.resolve(), a.goal, auto, a.run_id)
        if a.workdir:
            meta = run.read("run.json")
            meta["workdir"] = str(pathlib.Path(a.workdir).resolve())
            run.write("run.json", meta)
        ctx = Ctx(run.path)
        sched.start(ctx)
        print(f"run: {run.path}")
        if a.no_agents:
            return 0
        return drive(ctx, a.max_ticks, a.parallel or cfg.get("limits", {}).get("parallel", 3))
    if c == "resume":
        ctx = Ctx(a.path)
        return drive(ctx, a.max_ticks, a.parallel or ctx.cfg.get("limits", {}).get("parallel", 3))

    ctx = need_ctx(a)
    who = a.who
    if c == "msg":
        for m in bus.send(ctx, who, a.target, a.text, a.task):
            print(f"sent {m['id']} to {m['to']}")
    elif c == "inbox":
        msgs = bus.inbox(ctx, who, mark_read=not a.peek)
        for m in msgs:
            print(f"{m['id']} from {m['from']}{' re ' + m['task'] if m['task'] else ''} [{m['kind']}]: {m['text']}")
        if not msgs:
            print("(no new messages)")
    elif c == "task":
        task_cmd(ctx, who, a)
    elif c == "review":
        t = board.review(ctx, who, a.id, a.verdict, a.note)
        print(f"{t['id']} is now '{t['status']}'")
    elif c == "escalate":
        m, to = bus.escalate(ctx, who, a.category, a.text, a.task)
        print(f"escalated {m['id']} to {ctx.title(to) if to == HUMAN else to}")
    elif c == "decide":
        print(board.decide(ctx, who, a.title, a.context, a.decision, a.task))
    elif c == "reply":
        print("sent", bus.reply(ctx, who, a.msg_id, a.text)["id"])
    elif c == "status":
        print(board.render(ctx))
        for m in bus.open_escalations(ctx):
            print(f"! needs you: {m['id']} from {m['from']} [{m['category']}]: {m['text']}")
    elif c == "summary":
        print(sched.summary(ctx, "snapshot"))
    return 0


def task_cmd(ctx, who, a):
    s = a.sub
    if s == "new":
        t = board.create(ctx, who, a.title, a.owner, a.priority, a.dep, a.accept, a.kind, a.parent)
        print(t["id"])
    elif s == "list":
        print(board.render(ctx))
    elif s == "show":
        import json
        print(json.dumps(board.get(ctx, a.id), indent=2))
    elif s == "claim":
        board.claim(ctx, who, a.id)
    elif s == "handoff":
        board.handoff(ctx, who, a.id, a.to, a.note)
    elif s == "block":
        board.block(ctx, who, a.id, a.reason)
    elif s == "unblock":
        board.unblock(ctx, who, a.id)
    elif s == "move":
        t = board.move(ctx, who, a.id, a.state)
        print(f"{t['id']} is now '{t['status']}'")


def drive(ctx, max_ticks, parallel):
    status = sched.loop(ctx, max_ticks, parallel)
    text = sched.summary(ctx, status)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
