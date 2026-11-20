"""Parse model output into JSON and validate plans against the panel's action catalog."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class Catalog:
    def __init__(self, actions: list[dict] | None = None):
        self.actions: list[dict] = []
        self.by_name: dict[str, dict] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self.set(actions or [])

    def set(self, actions: list[dict]) -> None:
        self.actions = [a for a in actions if isinstance(a, dict) and a.get("name")]
        self.by_name = {a["name"]: a for a in self.actions}
        self._validators = {}
        for a in self.actions:
            schema = a.get("params") or {"type": "object"}
            try:
                Draft202012Validator.check_schema(schema)
                self._validators[a["name"]] = Draft202012Validator(schema)
            except SchemaError:
                self._validators[a["name"]] = Draft202012Validator({"type": "object"})

    def __contains__(self, name: str) -> bool:
        return name in self.by_name

    def __bool__(self) -> bool:
        return bool(self.actions)

    def resolve_name(self, name: str) -> str | None:
        if name in self.by_name:
            return name
        low = (name or "").lower()
        for n in self.by_name:
            if n.lower() == low:
                return n
        return None

    def step_errors(self, step: Any, where: str) -> list[str]:
        if not isinstance(step, dict):
            return [f"{where}: step must be an object"]
        action = step.get("action")
        if not isinstance(action, str) or action not in self.by_name:
            return [f"{where}: unknown action {action!r}; allowed: {', '.join(self.by_name)}"]
        args = step.get("args", {})
        if not isinstance(args, dict):
            return [f"{where} ({action}): args must be an object"]
        errs = []
        for e in self._validators[action].iter_errors(args):
            path = "/".join(str(p) for p in e.absolute_path)
            errs.append(f"{where} ({action}) args{('/' + path) if path else ''}: {e.message}")
        return errs

    def for_prompt(self) -> str:
        """Compact catalog listing: one action per line, schema as minified JSON."""
        lines = []
        for a in self.actions:
            lines.append(
                f"- {a['name']}: {a.get('description', '').strip()}\n  args schema: "
                + json.dumps(a.get("params") or {"type": "object"}, separators=(",", ":"))
            )
        return "\n".join(lines)


def normalize_step(step: Any, catalog: Catalog) -> Any:
    """Fix trivially wrong shapes the model sometimes emits (case of action names,
    {"name":..} instead of {"action":..}, args missing)."""
    if not isinstance(step, dict):
        return step
    s = dict(step)
    if "action" not in s:
        for k in ("name", "tool", "type"):
            if isinstance(s.get(k), str) and catalog.resolve_name(s[k]):
                s["action"] = s.pop(k)
                break
    if isinstance(s.get("action"), str):
        s["action"] = catalog.resolve_name(s["action"]) or s["action"]
    if "args" not in s:
        extra = {k: v for k, v in s.items() if k != "action"}
        s = {"action": s.get("action"), "args": extra}
    if "arguments" in s.get("args", {}) and len(s["args"]) == 1 and isinstance(s["args"]["arguments"], dict):
        s["args"] = s["args"]["arguments"]
    return {"action": s.get("action"), "args": s.get("args") if s.get("args") is not None else {}}


def plan_errors(plan: dict, catalog: Catalog) -> list[str]:
    errs: list[str] = []
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return ["steps must be an array"]
    for i, st in enumerate(steps):
        errs += catalog.step_errors(st, f"steps[{i}]")
    for j, alt in enumerate(plan.get("alternatives") or []):
        if not isinstance(alt, dict) or not isinstance(alt.get("steps"), list):
            errs.append(f"alternatives[{j}] must be {{title, steps}}")
            continue
        for i, st in enumerate(alt["steps"]):
            errs += catalog.step_errors(st, f"alternatives[{j}].steps[{i}]")
    return errs


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


# Small models sometimes drop the "{" that opens the next step: `}},"action":"createGroup",...`.
_MISSING_STEP_BRACE = re.compile(r'\}\s*,\s*"action"\s*:')


def extract_json(text: str) -> Any:
    """First complete JSON object in model output (tolerates fences, prose, <think> blocks,
    trailing commas, and a step object missing its opening brace). Raises ValueError."""
    if text is None:
        raise ValueError("empty output")
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return _first_object(t)
    except ValueError:
        if not _MISSING_STEP_BRACE.search(t):
            raise
        return _first_object(_MISSING_STEP_BRACE.sub('},{"action":', t), close_open=True)


def _first_object(t: str, close_open: bool = False) -> Any:
    """The first balanced {...} in t. close_open: if the text ends inside it, close what is still
    open (only used after the missing-brace repair, whose output ends one ] and one } short)."""
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in output")
    stack, in_str, esc = [], False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack:
            stack.pop()
            if not stack:
                return _loads(t[start : i + 1])
    if close_open and stack and not in_str:
        return _loads(t[start:].rstrip().rstrip(",") + "".join(reversed(stack)))
    raise ValueError("unterminated JSON object (output truncated?)")


def _loads(chunk: str) -> Any:
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))
