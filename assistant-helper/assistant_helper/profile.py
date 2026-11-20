"""The user's long-term profile: saved setups (recipes), learned preferences, feedback log.

Stored as plain JSON in ``profile.json`` so it can be read and edited by hand. Learning is
incremental: each /feedback event nudges ``preferences``; hand edits are never recomputed away.

Preferences (all injected into the planning prompt):

- ``naming``   {"Kick": "Kick In"}       operator renamed something a plan created
- ``plugins``  {"comp bus": "dbx 160"}   plugin the operator kept on a role (track/bus name)
- ``formats``  {"overheads": "stereo"}   mono/stereo per track role
- ``avoid``    ["setRecordArm"]          kinds of suggestion the operator keeps dismissing
- ``notes``    ["free text you write"]   anything else; never touched by the helper
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

EMPTY_PROFILE: dict[str, Any] = {
    "version": 1,
    "recipes": [],
    "preferences": {"naming": {}, "plugins": {}, "formats": {}, "avoid": [], "notes": []},
    "stats": {"go": {}, "dismissed": {}, "failed": {}},
    "feedback": [],
}

FEEDBACK_LOG_MAX = 300
TARGET_KEYS = ("on", "bus", "track", "target", "to", "destination", "name", "tracks", "targets")


def role_of(name: str) -> str:
    """'Kick 2' -> 'kick', 'Comp Bus' -> 'comp bus'."""
    s = re.sub(r"[\s_\-]*\d+$", "", (name or "").strip().lower())
    return re.sub(r"\s+", " ", s)


def plan_kind(steps: list[dict]) -> str:
    return "+".join(sorted({s.get("action", "?") for s in steps or []}))


class Profile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = copy.deepcopy(EMPTY_PROFILE)
        self.load()

    # ---- persistence -------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    disk = json.loads(self.path.read_text())
                    merged = copy.deepcopy(EMPTY_PROFILE)
                    merged.update(disk)
                    for k, v in EMPTY_PROFILE["preferences"].items():
                        merged["preferences"].setdefault(k, copy.deepcopy(v))
                    for k in EMPTY_PROFILE["stats"]:
                        merged["stats"].setdefault(k, {})
                    self.data = merged
                    self._mtime = self.path.stat().st_mtime
                    return
                except Exception as e:
                    log.error("profile.json unreadable (%s); starting empty in memory", e)
            self._mtime = None

    def reload_if_changed(self) -> None:
        if self.path.exists() and self.path.stat().st_mtime != getattr(self, "_mtime", None):
            self.load()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(self.path)
            self._mtime = self.path.stat().st_mtime

    def snapshot(self) -> dict:
        with self._lock:
            self.reload_if_changed()
            return copy.deepcopy(self.data)

    # ---- recipes -----------------------------------------------------------------------
    @property
    def recipes(self) -> list[dict]:
        self.reload_if_changed()
        return self.data.get("recipes", [])

    def put_recipe(self, name: str, steps: list, aliases: list | None = None, description: str | None = None) -> dict:
        with self._lock:
            self.reload_if_changed()
            rec = {"name": name, "aliases": list(aliases or []), "steps": steps}
            if description:
                rec["description"] = description
            rec["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            recipes = [r for r in self.data["recipes"] if r.get("name", "").lower() != name.lower()]
            recipes.append(rec)
            self.data["recipes"] = recipes
            self.save()
            return rec

    def delete_recipe(self, name: str) -> bool:
        with self._lock:
            self.reload_if_changed()
            before = len(self.data["recipes"])
            self.data["recipes"] = [r for r in self.data["recipes"] if r.get("name", "").lower() != name.lower()]
            changed = len(self.data["recipes"]) != before
            if changed:
                self.save()
            return changed

    # ---- learning ----------------------------------------------------------------------
    @property
    def preferences(self) -> dict:
        self.reload_if_changed()
        return self.data["preferences"]

    def record_feedback(self, fb: dict, plan: dict | None = None) -> None:
        with self._lock:
            self.reload_if_changed()
            entry = {"t": time.time(), **fb}
            if plan:
                entry["plan"] = {"title": plan.get("title"), "heard": plan.get("heard"), "steps": plan.get("steps")}
            self.data["feedback"].append(entry)
            del self.data["feedback"][:-FEEDBACK_LOG_MAX]
            self._learn(fb, plan)
            self.save()

    def _learn(self, fb: dict, plan: dict | None) -> None:
        prefs = self.data["preferences"]
        stats = self.data["stats"]
        outcome = fb.get("outcome")
        steps = fb.get("steps") or (plan or {}).get("steps") or []
        kind = plan_kind((plan or {}).get("steps") or steps)
        if outcome in ("go", "dismissed", "failed") and kind:
            stats[outcome][kind] = stats[outcome].get(kind, 0) + 1

        # Repeatedly dismissed, never accepted -> stop proposing it unprompted.
        dismissed = stats["dismissed"].get(kind, 0)
        accepted = stats["go"].get(kind, 0)
        if outcome == "dismissed" and dismissed >= 2 and dismissed > 2 * accepted and kind not in prefs["avoid"]:
            prefs["avoid"].append(kind)
        if outcome == "go" and kind in prefs["avoid"] and accepted >= dismissed:
            prefs["avoid"].remove(kind)

        if outcome == "go":
            for step in steps:
                self._learn_step(step, prefs)

        for edit in fb.get("edits") or []:
            if edit.get("kind") == "rename" and edit.get("from") and edit.get("to"):
                prefs["naming"][edit["from"]] = edit["to"]
                # a renamed thing keeps its learned format/plugin under the new role too
                old, new = role_of(edit["from"]), role_of(edit["to"])
                if old in prefs["formats"]:
                    prefs["formats"].setdefault(new, prefs["formats"][old])
            elif edit.get("kind") in ("plugin", "replacePlugin", "insertPlugin") and edit.get("to"):
                target = edit.get("target") or edit.get("track") or edit.get("bus")
                if target:
                    prefs["plugins"][role_of(target)] = edit["to"]
            elif edit.get("kind") == "format" and edit.get("target") and edit.get("to"):
                prefs["formats"][role_of(edit["target"])] = edit["to"]

    @staticmethod
    def _learn_step(step: dict, prefs: dict) -> None:
        action = (step.get("action") or "").lower()
        args = step.get("args") or {}

        # formats: any dict in the args with a name and a mono/stereo format
        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("name"), str) and o.get("format") in ("mono", "stereo"):
                    prefs["formats"][role_of(o["name"])] = o["format"]
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(args)

        # plugin choice per role
        if "plugin" in action or any("plugin" in k.lower() for k in args):
            plugin = next((v for k, v in args.items() if "plugin" in k.lower() and isinstance(v, str)), None)
            target = None
            for k in TARGET_KEYS:
                v = args.get(k)
                if isinstance(v, str):
                    target = v
                    break
                if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
                    target = v[0]
                    break
            if plugin and target:
                prefs["plugins"][role_of(target)] = plugin

    def prompt_summary(self) -> str:
        """Compact preferences block for the planning prompt ('' if nothing learned)."""
        p = self.preferences
        lines = []
        if p.get("naming"):
            lines.append("Naming (use the right-hand name instead of the left): " + json.dumps(p["naming"]))
        if p.get("plugins"):
            lines.append("Plugin choices by track/bus role: " + json.dumps(p["plugins"]))
        if p.get("formats"):
            lines.append("Usual formats by track role: " + json.dumps(p["formats"]))
        if p.get("avoid"):
            lines.append("Do not suggest these kinds of plans unless explicitly asked: " + ", ".join(p["avoid"]))
        for n in p.get("notes") or []:
            lines.append("Note: " + str(n))
        return "\n".join(lines)
