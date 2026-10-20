"""PreToolUse hook: deny file edits outside the role's `owns` globs (exit 2 blocks the call)."""
import json
import os
import pathlib
import re
import sys


def glob_re(g):
    g = g.replace("\\", "/")
    out, i = "", 0
    while i < len(g):
        if g[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return re.compile("^" + out + "$")


def owns_path(globs, path, base):
    try:
        rel = pathlib.Path(path).resolve().relative_to(pathlib.Path(base).resolve()).as_posix()
    except ValueError:
        return False
    return any(glob_re(g).match(rel) for g in globs)


def pretool():
    from .org import Ctx
    ev = json.load(sys.stdin)
    role = os.environ.get("HARNESS_ROLE")
    if not role or "HARNESS_RUN" not in os.environ:
        return 0
    ctx = Ctx(os.environ["HARNESS_RUN"])
    globs = ctx.roles[role].get("owns")
    path = (ev.get("tool_input") or {}).get("file_path") or (ev.get("tool_input") or {}).get("notebook_path")
    if globs is None or not path:
        return 0
    if owns_path(globs, path, ev.get("cwd") or os.getcwd()):
        return 0
    print(f"'{role}' may only edit: {', '.join(globs)}. Ask the owner of '{path}' via msg or task instead.", file=sys.stderr)
    return 2
