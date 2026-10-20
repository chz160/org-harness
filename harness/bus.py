"""Messaging: direct, channel, broadcast, escalation. Every message is one JSONL line per
recipient: {id, ts, from, to, via, task, kind, text}."""
import json

from . import store
from .org import HUMAN, HarnessError


def _deliver(ctx, frm, recipients, text, kind, task=None, via=None, extra=None):
    out = []
    with ctx.run.lock():
        for to in recipients:
            m = {"id": ctx.run.next_id("msg", "M"), "ts": store.now(), "from": frm, "to": to,
                 "via": via, "task": task, "kind": kind, "text": text}
            m.update(extra or {})
            ctx.run.append("messages.jsonl", json.dumps(m))
            ctx.run.log(frm, f"{kind} -> {to}" + (f" ({via})" if via else "") + (f" [{task}]" if task else "") + f": {text[:80]}")
            out.append(m)
    return out


def send(ctx, frm, target, text, task=None, kind="dm"):
    ctx.check_actor(frm)
    if target == HUMAN:
        raise HarnessError("use `escalate <category>` to reach the " + ctx.cfg["human"]["title"])
    via = None
    if target.startswith("@"):
        recipients = ctx.expand(target, frm)
        via = target
        if not recipients:
            raise HarnessError(f"{target} resolves to nobody for '{frm}'")
        if target == "@all" and frm != HUMAN and "@all" not in ctx.roles[frm].get("can_message", []):
            raise HarnessError(f"role '{frm}' may not broadcast (@all not in can_message)")
        if target.startswith("@channel:"):
            ch = target[9:]
            if frm != HUMAN and frm not in ctx.channel_members(ch) and target not in ctx.roles[frm].get("can_message", []):
                raise HarnessError(f"role '{frm}' is not in channel '{ch}'")
        elif target == "@reports" and frm != HUMAN and "@reports" not in ctx.roles[frm].get("can_message", []):
            raise HarnessError(f"role '{frm}' may not message @reports")
    else:
        if target not in ctx.roles:
            raise HarnessError(f"unknown recipient '{target}'; roles: {', '.join(ctx.roles)}")
        if not ctx.may_message(frm, target):
            allowed = sorted({r for s in ctx.roles[frm].get("can_message", []) for r in ctx.expand(s, frm)}
                             | {ctx.roles[frm]["reports_to"]})
            raise HarnessError(f"role '{frm}' may not message '{target}'. Allowed: {', '.join(allowed)}. "
                               f"To reach someone else, message your lead or escalate.")
        recipients = [target]
    return _deliver(ctx, frm, recipients, text, kind, task, via)


def escalation_target(ctx, frm, category):
    esc = ctx.cfg.get("escalation", {})
    cats = esc.get("categories", {})
    if category not in cats:
        raise HarnessError(f"unknown escalation category '{category}'; known: {', '.join(cats)}")
    to = cats[category]["to"]
    lvl = esc.get("autonomy", {}).get(ctx.autonomy, "all")
    if category in esc.get("always_human", []) or lvl == "all" or category in lvl:
        to = HUMAN
    if frm != HUMAN and category in ctx.roles[frm].get("decides", []):
        raise HarnessError(f"role '{frm}' may decide '{category}' alone; record it with `decide` instead")
    if to == frm:
        to = ctx.roles[frm]["reports_to"]
    return to


def escalate(ctx, frm, category, text, task=None):
    to = escalation_target(ctx, frm, category)
    m = _deliver(ctx, frm, [to], text, "escalation", task, extra={"category": category})[0]
    if task and to == HUMAN:
        from . import board
        board.set_flag(ctx, task, awaiting_human=m["id"])
    return m, to


def reply(ctx, frm, msg_id, text):
    """Answer a message (typically a human decision on an escalation)."""
    orig = next((m for m in ctx.run.read_jsonl("messages.jsonl") if m["id"] == msg_id), None)
    if not orig:
        raise HarnessError(f"no message {msg_id}")
    if orig["to"] != frm:
        raise HarnessError(f"message {msg_id} was addressed to '{orig['to']}', not '{frm}'")
    out = _deliver(ctx, frm, [orig["from"]], text, "reply", orig.get("task"), extra={"in_reply_to": msg_id})
    if orig.get("task"):
        from . import board
        board.set_flag(ctx, orig["task"], awaiting_human=None)
    return out[0]


def inbox(ctx, who, mark_read=True):
    seen = set(ctx.run.read(f"state/read_{who}.json", []))
    new = [m for m in ctx.run.read_jsonl("messages.jsonl") if m["to"] == who and m["id"] not in seen]
    if mark_read and new:
        with ctx.run.lock():
            ctx.run.write(f"state/read_{who}.json", sorted(seen | {m["id"] for m in new}))
    return new


def open_escalations(ctx):
    msgs = ctx.run.read_jsonl("messages.jsonl")
    answered = {m.get("in_reply_to") for m in msgs if m["kind"] == "reply"}
    return [m for m in msgs if m["kind"] == "escalation" and m["to"] == HUMAN and m["id"] not in answered]
