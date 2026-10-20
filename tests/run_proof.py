#!/usr/bin/env python3
"""Proof that one engine runs different orgs with zero code changes: drive each org's scenario
through the real scheduler with a scripted stand-in for claude, then assert on the files."""
import json, os, pathlib, re, subprocess, sys, tempfile, yaml

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
    t = json.loads((run / "tasks" / "T001.json").read_text())
    check(ctx_terminal(org, t["status"]), f"T001 reached terminal state '{t['status']}'")
    check(any("REJECTED" in h["note"] for h in t["history"]) or org == "x", "history shows a rejection before approval")
    check(any((run / "decisions").glob("ADR-*.md")), "decision recorded as ADR")
    return run


def ctx_terminal(org, state):
    cfg = yaml.safe_load((REPO / "orgs" / org / "org.yaml").read_text(encoding="utf-8"))
    return bool(cfg["workflow"]["states"][state].get("terminal"))


def domain_free():
    """The engine source must not mention any role id or title from any shipped org."""
    words = set()
    for f in (REPO / "orgs").glob("*/org.yaml"):
        cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
        for rid, r in cfg["roles"].items():
            words |= {rid, r["title"]}
        words.add(cfg["human"]["title"])
    code = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "harness").glob("*.py"))
    hits = sorted(w for w in words if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", code, re.I))
    check(not hits, f"engine code contains no role names or titles from any org {hits or ''}")


if __name__ == "__main__":
    root = pathlib.Path(tempfile.mkdtemp(prefix="orgh-proof-"))
    domain_free()
    for org in sys.argv[1:] or ["dev-team", "security-audit"]:
        run_org(org, root)
    print("\nPASS" if not fails else f"\nFAILED: {len(fails)}")
    sys.exit(1 if fails else 0)
