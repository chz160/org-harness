#!/usr/bin/env python3
"""Stand-in for `claude -p` used by the proof run: replays a scripted list of harness commands per
role, one list per wake. Lets the scheduler, permissions and gates be tested with zero tokens.
A step starting with '!' must be rejected by the engine; any other step must succeed."""
import json, os, shlex, subprocess, sys, pathlib
import yaml

role, run = os.environ["HARNESS_ROLE"], pathlib.Path(os.environ["HARNESS_RUN"])
scenario = yaml.safe_load(open(os.environ["HARNESS_SCENARIO"], encoding="utf-8"))
counter = run / "state" / f"fake_{role}.n"
counter.parent.mkdir(exist_ok=True)
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
steps = (scenario["wakes"].get(role) or [])
steps = steps[n] if n < len(steps) else (scenario.get("repeat") or {}).get(role, [])
for step in steps:
    must_fail = step.startswith("!")
    if step.startswith("sh:"):  # run a shell command in the agent's working directory (worktree)
        p = subprocess.run(step[3:], shell=True, capture_output=True, text=True)
        if p.returncode:
            print(f"SCENARIO MISMATCH: {step!r}: {p.stderr}", file=sys.stderr)
            sys.exit(3)
        continue
    argv = [sys.executable, "-m", "harness"] + shlex.split(step.lstrip("!"))
    p = subprocess.run(argv, capture_output=True, text=True)
    if (p.returncode != 0) != must_fail:
        print(f"SCENARIO MISMATCH for {role} wake {n}: {step!r} rc={p.returncode} {p.stderr.strip()}", file=sys.stderr)
        sys.exit(3)
    print(("rejected: " if must_fail else "ok: ") + (p.stderr if must_fail else p.stdout).strip())
print(json.dumps({"result": "scripted", "session_id": f"fake-{role}"}))
