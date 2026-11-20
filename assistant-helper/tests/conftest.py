from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import uvicorn

from assistant_helper.assistant import Assistant
from assistant_helper.config import load_config
from assistant_helper.llm import BaseLLM, LLMResult
from assistant_helper.prompts import GATE_SYSTEM
from assistant_helper.server import create_app
from assistant_helper.stt import BaseSTT

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeLLM(BaseLLM):
    """Gate: YES unless the utterance is in `chatter`. Plan: pops scripted outputs."""

    model_id = "fake"

    def __init__(self):
        self.status = "ready"
        self.plan_outputs: list[str] = []
        self.chatter: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def load(self):
        self.status = "ready"

    def chat(self, system, user, max_tokens=512, stop_on_json=True, assistant_prefix=""):
        self.calls.append((system, user))
        if system == GATE_SYSTEM:
            last = user.rsplit('LAST utterance: "', 1)[1].split('"', 1)[0]
            return LLMResult("NO" if last in self.chatter else "YES")
        out = self.plan_outputs.pop(0) if self.plan_outputs else '{"title":"","confidence":0,"steps":[]}'
        return LLMResult(out, prompt_tokens=100, gen_tokens=20, seconds=0.01)


class FakeSTT(BaseSTT):
    model_id = "fake"

    def __init__(self, text="add a marker called bridge"):
        self.status = "ready"
        self.text = text
        self.calls = 0

    def load(self):
        self.status = "ready"

    def transcribe(self, audio: np.ndarray) -> str:
        self.calls += 1
        return self.text


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_ASSISTANT_HOME", str(tmp_path))
    return tmp_path


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        app = create_app(assistant, load_models=False, capture=False)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            try:
                httpx.get(self.url + "/health", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def wait_event(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            for ev in list(self.assistant.events.history):
                if pred(ev):
                    return ev
            time.sleep(0.02)
        raise AssertionError("event not seen; history=" + json.dumps(self.assistant.events.history)[-2000:])


@pytest.fixture
def fakes(home):
    cfg = load_config()
    llm, stt = FakeLLM(), FakeSTT()
    a = Assistant(cfg, stt, llm)
    return a, llm, stt


@pytest.fixture
def live(fakes):
    a, llm, stt = fakes
    with LiveServer(a) as srv:
        yield srv, llm, stt
