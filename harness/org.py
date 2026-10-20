"""Context object: a validated org config plus the run it is executing."""
from . import config, store

HUMAN = "human"


class HarnessError(Exception):
    """User/agent-facing error with an actionable message."""


class Ctx:
    def __init__(self, run_path):
        self.run = store.Run(run_path)
        meta = self.run.read("run.json")
        if meta is None:
            raise HarnessError(f"no run at {run_path}")
        self.meta = meta
        self.cfg = config.load(meta["org_dir"])
        self.roles = self.cfg["roles"]
        self.states = self.cfg["workflow"]["states"]
        self.autonomy = meta["autonomy"]

    def check_actor(self, who):
        if who != HUMAN and who not in self.roles:
            raise HarnessError(f"unknown role '{who}'; roles: {', '.join(self.roles)}")

    def title(self, who):
        return self.cfg["human"]["title"] if who == HUMAN else self.roles[who]["title"]

    def reports_of(self, rid):
        if rid == HUMAN:
            return list(self.cfg["human"].get("reports", []))
        return [r for r, v in self.roles.items() if v["reports_to"] == rid]

    def channel_members(self, c):
        m = self.cfg.get("channels", {}).get(c)
        return list(self.roles) if m == "*" else list(m or [])

    def expand(self, sel, me):
        """Resolve a selector (role id, @reports, @lead, @all, @channel:x) to role ids."""
        if sel in self.roles:
            return [sel]
        if sel == "@reports":
            return self.reports_of(me)
        if sel == "@lead":
            up = self.roles[me]["reports_to"] if me != HUMAN else None
            return [up] if up else []
        if sel == "@all":
            return [r for r in self.roles if r != me]
        if sel.startswith("@channel:"):
            return [r for r in self.channel_members(sel[9:]) if r != me]
        return []

    def may_message(self, me, to):
        if me == HUMAN:
            return True
        if to == HUMAN:
            return self.roles[me]["reports_to"] == HUMAN
        if self.roles[me]["reports_to"] == to:  # up the chain is always open
            return True
        return any(to in self.expand(s, me) for s in self.roles[me].get("can_message", []))

    def may_assign(self, me, to):
        if me == HUMAN or me == to:
            return True
        return any(to in self.expand(s, me) for s in self.roles[me].get("can_assign", []))

    def is_terminal(self, state):
        return bool(self.states[state].get("terminal"))
