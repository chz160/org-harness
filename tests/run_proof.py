#!/usr/bin/env python3
"""Proof that one engine runs different orgs with zero code changes: drive each org's scenario
through the real scheduler with a scripted stand-in for claude, then assert on the files."""
import json, os, pathlib, re, shutil, subprocess, sys, tempfile, yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake_claude.py"
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run_org(org, root):
    sc = REPO / "tests" / "scenarios" / f"{org}.yaml"
    spec = yaml.safe_load(sc.read_text(encoding="utf-8"))
    env = dict(os.environ, HARNESS_CLAUDE_BIN=str(FAKE), HARNESS_SCENARIO=str(sc), PYTHONPATH=str(REPO))
    p = subprocess.run([sys.executable, "-m", "harness", "run", "--org", org, "--goal", spec["goal"],
                        "--root", str(root), "--run-id", "proof"], env=env, capture_output=True, text=True, cwd=REPO)
    print(f"\n== {org} ==")
    print(p.stdout.strip().splitlines()[0] if p.stdout else p.stderr)
    run = root / org / "proof"
    check(p.returncode == 0, f"run exited cleanly {p.stderr.strip()[-200:]}")
    summary = (run / "summary.md").read_text(encoding="utf-8") if (run / "summary.md").exists() else ""
    check(f"End state: **{spec['expect_end']}**" in summary, f"run ended '{spec['expect_end']}'")
    msgs = [json.loads(x) for x in (run / "messages.jsonl").read_text().splitlines()]
    check(all({"id", "ts", "from", "to", "kind", "task"} <= set(m) for m in msgs), f"{len(msgs)} messages logged with sender/recipient/ts/task")
    check(any(m["kind"] == "feedback" for m in msgs), "gate rejection delivered feedback to the owner")
    tasks = [json.loads(f.read_text()) for f in sorted((run / "tasks").glob("T*.json"))]
    check(all(ctx_terminal(org, t["status"]) for t in tasks), "all tasks reached a terminal state: " + ", ".join(f"{t['id']}={t['status']}" for t in tasks))
    check(any("REJECTED" in h["note"] for t in tasks for h in t["history"]), "history shows a gate rejection before approval")
    check(any((run / "decisions").glob("ADR-*.md")), "decision recorded as ADR")
    return run


def ctx_terminal(org, state):
    cfg = yaml.safe_load((REPO / "orgs" / org / "org.yaml").read_text(encoding="utf-8"))
    return bool(cfg["workflow"]["states"][state].get("terminal"))


def domain_free():
    """The engine source must not mention any role id or title from any shipped org."""
    words = set()
    for f in (REPO / "orgs").glob("[!_]*/org.yaml"):
        cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
        for rid, r in cfg["roles"].items():
            words |= {rid, r["title"]}
        words.add(cfg["human"]["title"])
    code = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "harness").glob("*.py"))
    hits = sorted(w for w in words if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", code, re.I))
    check(not hits, f"engine code contains no role names or titles from any org {hits or ''}")


def harness(*args, env=None, cwd=REPO, ok=True):
    e = dict(os.environ, PYTHONPATH=str(REPO), **(env or {}))
    p = subprocess.run([sys.executable, "-m", "harness", *args], env=e, capture_output=True, text=True, cwd=cwd)
    return p


def fake_env(scenario):
    return {"HARNESS_CLAUDE_BIN": str(FAKE), "HARNESS_SCENARIO": str(scenario)}


def tweak_org(root, name, **limits):
    """Copy an org to a temp folder with different run limits (config only, no code)."""
    dst = root / f"{name}-tweaked"
    shutil.copytree(REPO / "orgs" / name, dst, dirs_exist_ok=True)
    f = dst / "org.yaml"
    cfg = yaml.safe_load(f.read_text())
    cfg["limits"].update(limits)
    f.write_text(yaml.safe_dump(cfg))
    return dst


def end_state(p):
    m = re.search(r"End state: \*\*(.+?)\*\*", p.stdout)
    return m.group(1) if m else p.stderr[-200:]


def write_scenario(root, name, data):
    f = root / f"{name}.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


def test_loops(root):
    print("\n== loop and stall detection ==")
    kick = ["task new \"Work\" --owner backend-dev --kind code"]
    org = tweak_org(root, "dev-team", pingpong=4, stall_ticks=50)
    sc = write_scenario(root, "pingpong", {"wakes": {"tech-lead": [kick]}, "repeat": {
        "backend-dev": ['msg frontend-dev "your move"'], "frontend-dev": ['msg backend-dev "no, yours"']}})
    p = harness("run", "--org", str(org), "--goal", "x", "--root", str(root), "--run-id", "pp", env=fake_env(sc))
    check(end_state(p) == "loop-detected", f"ping-pong between two roles pauses the run ({end_state(p)})")
    check("exchanged 4+ messages" in p.stdout, "the human gets an escalation naming the pair")
    org = tweak_org(root, "dev-team", pingpong=100, stall_ticks=4)
    sc = write_scenario(root, "stall", {"wakes": {"tech-lead": [kick]}, "repeat": {
        "backend-dev": ['msg frontend-dev "fyi"'], "frontend-dev": ['msg mobile-dev "fyi"'], "mobile-dev": ['msg backend-dev "fyi"']}})
    p = harness("run", "--org", str(org), "--goal", "x", "--root", str(root), "--run-id", "st", env=fake_env(sc))
    check(end_state(p) == "stalled", f"no task progress for N ticks stalls the run ({end_state(p)})")


def test_human(root):
    print("\n== human in the loop ==")
    sc = write_scenario(root, "esc", {"wakes": {
        "producer": [["task new \"Dash spec\" --owner game-designer --kind design"]],
        "game-designer": [['escalate lead_conflict "Art and tech disagree on dash VFX budget" --task T001'],
                          ["task move T001 in-progress", "task move T001 review"]],
        "creative-director": [["review approve T001"]], "qa-lead": [["review approve T001"]]}})
    p = harness("run", "--org", "game-studio", "--goal", "x", "--root", str(root), "--run-id", "esc", env=fake_env(sc))
    run = root / "game-studio" / "esc"
    check(end_state(p) == "needs-human", f"a human-bound escalation pauses the run ({end_state(p)})")
    t = json.loads((run / "tasks" / "T001.json").read_text())
    check(t["awaiting_human"] is not None, "only the blocked task waits; its state records the pending decision")
    mid = re.search(r"-> `reply (M\d+)", p.stdout).group(1)
    r = harness("--run", str(run), "reply", mid, "Take the tech lead's budget.")
    check(r.returncode == 0, f"{r.stderr.strip()}human replies to the escalation")
    p = harness("resume", str(run), env=fake_env(sc))
    check(end_state(p) == "done", f"after the reply the run resumes and finishes ({end_state(p)})")
    # autonomy: same category auto-resolves to a role when the org says so, unless always_human
    for lvl, who in (("autonomous", "producer"), ("ask-often", "studio head")):
        h = harness("run", "--org", "game-studio", "--goal", "x", "--root", str(root), "--run-id", "a-" + lvl, "--autonomy", lvl, "--no-agents")
        run2 = root / "game-studio" / ("a-" + lvl)
        e = harness("--run", str(run2), "--as", "game-designer", "escalate", "scope_change", "Need a new VFX asset")
        check(who in e.stdout.lower(), f"scope_change goes to '{who}' at autonomy {lvl}: {e.stdout.strip()}")
    # veto, redirect, human gate approval, pause
    run3 = root / "game-studio" / "a-ask-often"
    harness("--run", str(run3), "task", "new", "Spike", "--owner", "game-designer", "--kind", "design")
    v = harness("--run", str(run3), "veto", "T001", "--note", "out of scope")
    t = json.loads((run3 / "tasks" / "T001.json").read_text())
    check(v.returncode == 0 and "vetoed" in (t["blocked"] or ""), "human veto blocks the task and tells the owner")
    rd = harness("--run", str(run3), "redirect", "Focus on the dodge, not the dash.")
    check(rd.returncode == 0 and "dodge" in (run3 / "messages.jsonl").read_text(), "redirect reaches the lead")
    bad = harness("--run", str(run3), "--as", "game-designer", "veto", "T001", "--note", "x")
    check(bad.returncode != 0, "a non-human cannot veto")
    harness("--run", str(run3), "pause")
    p = harness("resume", str(run3), "--max-ticks", "1", env=fake_env(sc))
    check(p.returncode == 0 and not (run3 / "state" / "paused").exists(), "pause flag is cleared by resume")


def test_hooks(root):
    print("\n== org hooks ==")
    org = tweak_org(root, "dev-team")
    f = org / "org.yaml"
    cfg = yaml.safe_load(f.read_text())
    cfg["hooks"] = {"on_gate": "echo tests failed >&2; exit 1"}
    f.write_text(yaml.safe_dump(cfg))
    harness("run", "--org", str(org), "--goal", "x", "--root", str(root), "--run-id", "hk", "--no-agents")
    run = str(root / "dev-team" / "hk")
    harness("--run", run, "--as", "tech-lead", "task", "new", "T", "--owner", "backend-dev", "--kind", "code")
    for st in ("in-progress", "review"):
        harness("--run", run, "--as", "backend-dev", "task", "move", "T001", st)
    r = harness("--run", run, "--as", "reviewer", "review", "approve", "T001")
    check(r.returncode != 0 and "tests failed" in r.stderr, "an org on_gate hook can refuse a sign-off with its own message")
    bad = org / "org.yaml"
    cfg["hooks"] = {"on_everything": "x"}
    bad.write_text(yaml.safe_dump(cfg))
    v = harness("validate", str(org))
    check(v.returncode != 0 and "hooks.on_everything" in v.stderr, "unknown hook events fail validation")


def test_worktree(root):
    print("\n== worktree isolation and merge ==")
    repo = root / "target"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    (repo / "README").write_text("target\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    commit = "sh: mkdir -p server && echo ok > server/health.txt && git add -A && git -c user.name=t -c user.email=t@t commit -qm health"
    sc = write_scenario(root, "wt", {"wakes": {
        "tech-lead": [["task new \"Add health file\" --owner backend-dev --kind code"]],
        "backend-dev": [["task move T001 in-progress", commit, "task move T001 review"]],
        "reviewer": [["review approve T001"]], "qa": [["review approve T001"]]}})
    p = harness("run", "--org", "dev-team", "--goal", "x", "--root", str(root), "--run-id", "wt", "--workdir", str(repo),
                "--auto-merge", env=fake_env(sc))
    run = root / "dev-team" / "wt"
    check(end_state(p) == "done", f"run finishes ({end_state(p)})")
    check((run / "wt" / "backend-dev").exists(), "backend-dev worked in its own worktree")
    check((repo / "server" / "health.txt").exists(), "its branch was merged into the target repo when the task reached done")
    check(not (repo / "tests").exists(), "no stray files in the target repo")


if __name__ == "__main__":
    root = pathlib.Path(tempfile.mkdtemp(prefix="orgh-proof-"))
    domain_free()
    only = [a for a in sys.argv[1:]]
    for org in only or ["dev-team", "security-audit", "game-studio"]:
        run_org(org, root)
    if not only:
        test_loops(root)
        test_human(root)
        test_hooks(root)
        test_worktree(root)
    print("\nPASS" if not fails else f"\nFAILED: {len(fails)}")
    sys.exit(1 if fails else 0)
