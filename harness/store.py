"""Plain-file state for one run: JSON/JSONL under runs/<org>/<run-id>/. Locking is a lockfile
(O_EXCL) so it behaves the same on Windows and POSIX."""
import contextlib
import json
import os
import pathlib
import time
import datetime


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Run:
    def __init__(self, path):
        self.path = pathlib.Path(path)

    # --- files -----------------------------------------------------------
    def p(self, *parts):
        return self.path.joinpath(*parts)

    @contextlib.contextmanager
    def lock(self, timeout=15):
        lf = self.p(".lock")
        end = time.time() + timeout
        while True:
            try:
                fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.time() > end:
                    raise RuntimeError(f"could not lock {lf}; if no harness is running, delete it")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.unlink(lf)

    def read(self, rel, default=None):
        f = self.p(rel)
        if not f.exists():
            return default
        return json.loads(f.read_text(encoding="utf-8"))

    def write(self, rel, obj):
        f = self.p(rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        os.replace(tmp, f)

    def append(self, rel, line):
        f = self.p(rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_jsonl(self, rel):
        f = self.p(rel)
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    def log(self, who, text):
        self.append("events.log", f"{now()} [{who}] {text}")

    def next_id(self, kind, prefix):
        meta = self.read("run.json")
        n = meta.setdefault("counters", {}).get(kind, 0) + 1
        meta["counters"][kind] = n
        self.write("run.json", meta)
        return f"{prefix}{n:03d}"


def create(root, org_name, org_dir, goal, autonomy, run_id=None):
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    run = Run(pathlib.Path(root) / org_name / run_id)
    if run.path.exists():
        raise FileExistsError(f"run already exists: {run.path}")
    run.path.mkdir(parents=True)
    run.write("run.json", {"id": run_id, "org": org_name, "org_dir": str(org_dir), "goal": goal,
                           "autonomy": autonomy, "created": now(), "counters": {}})
    return run
