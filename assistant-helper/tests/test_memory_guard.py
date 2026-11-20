"""The memory guard unloads the language model under macOS memory pressure and reloads it after calm."""
from __future__ import annotations

import asyncio

from assistant_helper.assistant import Assistant
from assistant_helper.config import load_config
from assistant_helper.events import EventBus

from conftest import FakeLLM, FakeSTT


class UnloadableLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.unloads = 0
        self.loads = 0

    def unload(self):
        self.unloads += 1
        self.status = "paused"

    def load(self):
        self.loads += 1
        self.status = "ready"


def test_guard_pauses_under_pressure_and_reloads_after_calm(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_ASSISTANT_HOME", str(tmp_path))
    llm = UnloadableLLM()
    a = Assistant(load_config(), FakeSTT(), llm, EventBus())
    levels = iter([1, 2, 2, 1, 1, 1, 1])
    monkeypatch.setattr(Assistant, "memory_pressure_level", staticmethod(lambda: next(levels, 1)))
    seen = []

    async def run():
        await a.start(load_models=False)
        a.llm_ready.set()
        guard = asyncio.create_task(a._memory_guard(interval=0.01, calm_s=0.03))
        for _ in range(200):
            await asyncio.sleep(0.01)
            seen.append(llm.status)
            if llm.loads:
                break
        guard.cancel()
        await a.stop()

    asyncio.run(run())
    assert llm.unloads == 1
    assert "paused" in seen
    assert llm.loads == 1 and llm.status == "ready"
    assert a.llm_ready.is_set()


def test_typed_prompt_while_paused_answers_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_ASSISTANT_HOME", str(tmp_path))
    llm = UnloadableLLM()
    bus = EventBus()
    a = Assistant(load_config(), FakeSTT(), llm, bus)
    events = []
    monkeypatch.setattr(bus, "publish", lambda e: events.append(e))

    async def run():
        await a.start(load_models=False)
        a.set_catalog([{"name": "createBus", "description": "x", "params": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}])
        llm.status = "paused"
        pid = a.submit_prompt("make a verb bus")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(e.get("type") == "error" and e.get("planId") == pid for e in events):
                break
        await a.stop()
        return pid

    pid = asyncio.run(run())
    errs = [e for e in events if e.get("type") == "error" and e.get("planId") == pid]
    assert errs and "paused" in errs[0]["message"]


def test_unknown_names_are_flagged_not_invented(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_ASSISTANT_HOME", str(tmp_path))
    a = Assistant(load_config(), FakeSTT(), FakeLLM(), EventBus())
    a.snapshot = {"tracks": [{"name": "Kick"}, {"name": "Snare"}], "buses": [{"name": "Comp Bus"}]}
    toms = {"title": "x", "confidence": 0.9, "steps": [{"action": "addSend", "args": {"from": ["Toms"], "to": "Comp Bus"}}]}
    a._flag_unknown_names(toms)
    assert toms["confidence"] <= 0.4 and "Toms" in toms["notes"]
    fine = {"title": "x", "confidence": 0.9, "steps": [
        {"action": "createTracks", "args": {"tracks": [{"name": "Overheads"}]}},
        {"action": "addSend", "args": {"from": ["kick", "snare", "overheads"], "to": "Verb"}},
        {"action": "insertPlugin", "args": {"on": "Verb", "plugin": "plate"}},
        {"action": "setRecordArm", "args": {"tracks": "armed", "on": True}},
    ]}
    a._flag_unknown_names(fine)
    assert fine["confidence"] == 0.9 and "notes" not in fine
