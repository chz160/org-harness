"""Load and validate an org definition. Pure data: no domain knowledge lives here."""
import pathlib
import yaml

TOP_KEYS = {"org", "human", "roles", "channels", "kinds", "workflow", "gates",
            "escalation", "limits", "hooks"}
ROLE_KEYS = {"title", "prompt", "responsibilities", "reports_to", "can_message", "can_assign",
             "model", "tools", "owns", "workspace", "decides", "limits"}
AUTONOMY = ("ask-often", "balanced", "autonomous")


class ConfigError(Exception):
    pass


def load(org_dir):
    org_dir = pathlib.Path(org_dir)
    path = org_dir / "org.yaml"
    if not path.is_file():
        raise ConfigError(f"{path}: not found (an org is a folder containing org.yaml)")
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    errs = []
    validate(cfg, org_dir, errs)
    if errs:
        raise ConfigError(f"{path}: {len(errs)} problem(s)\n  " + "\n  ".join(errs))
    cfg["_dir"] = str(org_dir)
    return cfg


def selector_ok(sel, roles, channels):
    if sel in roles or sel in ("@reports", "@lead", "@all"):
        return True
    return sel.startswith("@channel:") and sel[9:] in channels


def validate(cfg, org_dir, errs):
    if not isinstance(cfg, dict):
        errs.append("top level must be a mapping")
        return
    for k in cfg:
        if k not in TOP_KEYS:
            errs.append(f"{k}: unknown key (allowed: {', '.join(sorted(TOP_KEYS))})")
    org = cfg.get("org") or {}
    for req in ("name",):
        if req not in org:
            errs.append(f"org.{req}: required")
    if org.get("autonomy", "balanced") not in AUTONOMY:
        errs.append(f"org.autonomy: must be one of {AUTONOMY}")
    for f in org.get("context", []):
        if not (org_dir / f).is_file():
            errs.append(f"org.context: file '{f}' not found in {org_dir}")

    human = cfg.get("human") or {}
    if "title" not in human:
        errs.append("human.title: required (what the human is called in this org)")
    roles = cfg.get("roles") or {}
    if not roles:
        errs.append("roles: at least one role required")
    channels = cfg.get("channels") or {}
    for rid, r in roles.items():
        r = r or {}
        roles[rid] = r
        for k in r:
            if k not in ROLE_KEYS:
                errs.append(f"roles.{rid}.{k}: unknown key")
        for req in ("title", "reports_to"):
            if req not in r:
                errs.append(f"roles.{rid}.{req}: required")
        up = r.get("reports_to")
        if up and up != "human" and up not in roles:
            errs.append(f"roles.{rid}.reports_to: unknown role '{up}'")
        for field in ("can_message", "can_assign"):
            for sel in r.get(field, []):
                if not selector_ok(sel, roles, channels):
                    errs.append(f"roles.{rid}.{field}: unknown target '{sel}'")
        if r.get("prompt") and not (org_dir / r["prompt"]).is_file():
            errs.append(f"roles.{rid}.prompt: file '{r['prompt']}' not found")
        if r.get("workspace", "shared") not in ("shared", "worktree"):
            errs.append(f"roles.{rid}.workspace: must be shared or worktree")
    for rid in roles:  # reporting chain must reach the human without a cycle
        seen, cur = set(), rid
        while cur != "human" and cur in roles:
            if cur in seen:
                errs.append(f"roles.{rid}.reports_to: cycle in reporting chain")
                break
            seen.add(cur)
            cur = roles[cur].get("reports_to", "human")
    for rid in human.get("reports", []):
        if rid not in roles:
            errs.append(f"human.reports: unknown role '{rid}'")
    if not human.get("reports"):
        errs.append("human.reports: at least one role must report to the human (they receive the goal)")
    for c, members in channels.items():
        if members == "*":
            continue
        for m in members:
            if m not in roles:
                errs.append(f"channels.{c}: unknown role '{m}'")

    wf = cfg.get("workflow") or {}
    states = wf.get("states") or {}
    if not states:
        errs.append("workflow.states: required")
    if wf.get("initial") not in states:
        errs.append(f"workflow.initial: '{wf.get('initial')}' is not a state")
    if not any((s or {}).get("terminal") for s in states.values()):
        errs.append("workflow.states: at least one state needs terminal: true")
    trans = wf.get("transitions") or []
    edges = set()
    for i, t in enumerate(trans):
        for end in ("from", "to"):
            if t.get(end) not in states:
                errs.append(f"workflow.transitions[{i}].{end}: '{t.get(end)}' is not a state")
        for who in t.get("by", ["any"]):
            if who not in roles and who not in ("any", "owner"):
                errs.append(f"workflow.transitions[{i}].by: unknown role '{who}'")
        edges.add((t.get("from"), t.get("to")))
    reach, frontier = {wf.get("initial")}, [wf.get("initial")]
    while frontier:
        cur = frontier.pop()
        for a, b in edges:
            if a == cur and b not in reach:
                reach.add(b)
                frontier.append(b)
    for s in states:
        if s not in reach:
            errs.append(f"workflow.states.{s}: unreachable from initial state '{wf.get('initial')}'")

    for i, g in enumerate(cfg.get("gates") or []):
        tr = g.get("transition", "")
        a, _, b = tr.partition("->")
        if (a, b) not in edges:
            errs.append(f"gates[{i}].transition: '{tr}' is not a workflow transition")
        for r in g.get("signoff", []):
            if r not in roles:
                errs.append(f"gates[{i}].signoff: unknown role '{r}'")
        if not g.get("signoff"):
            errs.append(f"gates[{i}].signoff: at least one role required")
        if g.get("on_reject") not in states:
            errs.append(f"gates[{i}].on_reject: '{g.get('on_reject')}' is not a state")
        for k in g.get("kinds", []):
            if k not in (cfg.get("kinds") or []):
                errs.append(f"gates[{i}].kinds: '{k}' not in top-level kinds")

    esc = cfg.get("escalation") or {}
    cats = esc.get("categories") or {}
    for c, v in cats.items():
        to = (v or {}).get("to")
        if to != "human" and to not in roles:
            errs.append(f"escalation.categories.{c}.to: unknown role '{to}'")
    for c in esc.get("always_human", []):
        if c not in cats:
            errs.append(f"escalation.always_human: unknown category '{c}'")
    for lvl, v in (esc.get("autonomy") or {}).items():
        if lvl not in AUTONOMY:
            errs.append(f"escalation.autonomy.{lvl}: not a level {AUTONOMY}")
        if v != "all" and any(c not in cats for c in v):
            errs.append(f"escalation.autonomy.{lvl}: lists unknown category")
    for rid, r in roles.items():
        for c in r.get("decides", []):
            if c not in cats:
                errs.append(f"roles.{rid}.decides: unknown category '{c}'")
