"""Optional git isolation: a role with `workspace: worktree` works on its own branch in its own
checkout under the run folder. Merging back is opt-in (`run --auto-merge`) and only happens when a
task enters a workflow state flagged `merge: true`."""
import os
import subprocess
import threading

_lock = threading.Lock()


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def base_dir(ctx):
    return ctx.meta.get("workdir") or os.getcwd()


def branch_of(ctx, role):
    return f"org/{ctx.meta['id']}/{role}"


def ensure(ctx, role):
    """Return the directory this role should run in (created on first use)."""
    base = base_dir(ctx)
    if ctx.roles[role].get("workspace") != "worktree":
        return base
    wt = ctx.run.p("wt", role)
    with _lock:
        if wt.exists():
            return str(wt)
        if _git(base, "rev-parse", "--git-dir").returncode != 0:
            ctx.run.log("engine", f"{role}: workdir {base} is not a git repo; using it directly")
            return base
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = _git(base, "worktree", "add", "-b", branch_of(ctx, role), str(wt))
        if r.returncode != 0:
            ctx.run.log("engine", f"{role}: worktree failed ({r.stderr.strip()}); using {base} directly")
            return base
        ctx.run.log("engine", f"{role}: worktree {wt} on {branch_of(ctx, role)}")
        return str(wt)


def merge(ctx, role):
    """Merge the role's branch into whatever is checked out in the workdir. Returns an error or None."""
    base = base_dir(ctx)
    if not ctx.run.p("wt", role).exists():
        return None
    with _lock:
        r = _git(base, "merge", "--no-ff", "--no-edit", branch_of(ctx, role))
        if r.returncode != 0:
            _git(base, "merge", "--abort")
            return (r.stdout + r.stderr).strip()[-300:]
    ctx.run.log("engine", f"merged {branch_of(ctx, role)} into {base}")
    return None
