"""The helper's brain: audio -> VAD -> STT -> gate -> plan, plus conversation state.

Threads:
- PortAudio callback -> per-input worker thread (resample, VAD, segmenting)
- one "mlx" worker thread for every STT and LLM call (MLX is not used concurrently)
- the asyncio loop (HTTP, SSE, the planner queue)
"""

from __future__ import annotations

import asyncio
import subprocess
import copy
import itertools
import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from . import __version__, prompts
from .config import Config
from .events import EventBus
from .llm import BaseLLM
from .profile import Profile
from .recipes import match_recipe, wants_recipe, wants_save
from .stt import BaseSTT
from .vad import Segmenter, SileroVAD, VadParams
from .validate import Catalog, extract_json, normalize_step, plan_errors
from .vocab import VocabCorrector

log = logging.getLogger(__name__)

_FILLER_ONLY = re.compile(r"^(yeah|yes|no|ok|okay|cool|nice|great|right|sure|uh+|um+|hm+|mm+|ha+|thanks|thank you|wow)[.!?]*$", re.I)


class Assistant:
    def __init__(self, config: Config, stt: BaseSTT, llm: BaseLLM, events: EventBus | None = None):
        self.config = config
        self.stt = stt
        self.llm = llm
        self.events = events or EventBus()
        self.profile = Profile(config.profile_path)
        self.vocab = VocabCorrector(config.vocabulary_path)
        self.catalog = Catalog()
        self.snapshot: dict | None = None
        self.history: deque[dict] = deque(maxlen=int(config.get("historySize", 10)))
        self.plans: dict[str, dict] = {}  # id -> plan (+ internal _status/_t)
        self.capture = None  # set by start_capture()
        self.mlx = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
        self._ids = itertools.count(1)
        self._plan_ids = itertools.count(1)
        self._id_lock = threading.Lock()
        self._segmenters: dict[str, Segmenter] = {}
        self._partial_busy: set[str] = set()
        self._finalized: set[str] = set()
        self._utt_input: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self.llm_ready: asyncio.Event | None = None  # created in start()
        self.vad_params = VadParams.from_config(config.get("vad", {}), float(config.get("partialIntervalS", 1.0)))
        self.timings: list[dict] = []

    # ---- lifecycle ---------------------------------------------------------------------
    async def start(self, load_models: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self.events.bind_loop(self._loop)
        self._queue = asyncio.Queue()
        self.llm_ready = asyncio.Event()
        self._worker = asyncio.create_task(self._plan_worker())
        self._levels = asyncio.create_task(self._publish_levels())
        if load_models and self.config.get("memoryGuard", True):
            self._guard = asyncio.create_task(self._memory_guard())
        if load_models:
            self.mlx.submit(self._load_models)
        else:
            if self.llm.status == "ready":
                self.llm_ready.set()

    async def stop(self) -> None:
        if self.capture:
            self.capture.shutdown()
        if self._worker:
            self._worker.cancel()
        if getattr(self, "_levels", None):
            self._levels.cancel()
        if getattr(self, "_guard", None):
            self._guard.cancel()
        self.mlx.shutdown(wait=False, cancel_futures=True)

    def _load_models(self) -> None:
        for name, comp in (("STT", self.stt), ("LLM", self.llm)):
            try:
                comp.load()
            except Exception as e:
                log.exception("%s failed to load", name)
                self.events.publish({"type": "error", "message": f"{name} failed to load: {e}"})
            self.publish_status()
        if self.llm.status == "ready":
            if hasattr(self.llm, "warm"):
                self.llm.warm(prompts.GATE_SYSTEM)
            if self.catalog:
                self._warm_plan_prompt()
            if self._loop:
                self._loop.call_soon_threadsafe(self.llm_ready.set)

    # ---- memory guard: LUNA always wins ----------------------------------------------------
    @staticmethod
    def memory_pressure_level() -> int:
        """macOS memory pressure: 1 normal, 2 warning, 4 critical (0 if unknown)."""
        try:
            out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            return int(out or 0)
        except Exception:
            return 0

    async def _memory_guard(self, interval: float = 3.0, calm_s: float = 60.0) -> None:
        """Unload the language model as soon as macOS reports memory pressure, so a recording
        session never stalls for the assistant; reload it after `calm_s` of normal pressure."""
        calm_since = None
        while True:
            await asyncio.sleep(interval)
            level = self.memory_pressure_level()
            if level >= 2 and self.llm.status == "ready" and hasattr(self.llm, "unload"):
                calm_since = None
                self.llm_ready.clear()
                await self._run_mlx(self.llm.unload)
                log.warning("memory pressure %d: language model paused", level)
                self.events.publish({"type": "error", "message": "Memory is tight, so the assistant's brain is paused to keep LUNA running. It comes back by itself."})
                self.publish_status()
            elif level == 1 and self.llm.status == "paused":
                calm_since = calm_since or time.time()
                if time.time() - calm_since >= calm_s:
                    calm_since = None
                    log.info("memory calm: reloading language model")
                    self.llm.status = "loading"
                    self.publish_status()
                    await self._run_mlx(self._reload_llm)
            elif level != 1:
                calm_since = None

    def _reload_llm(self) -> None:
        try:
            self.llm.load()
        except Exception as e:
            log.exception("LLM reload failed")
            self.events.publish({"type": "error", "message": f"language model failed to reload: {e}"})
        self.publish_status()
        if self.llm.status == "ready":
            if hasattr(self.llm, "warm"):
                self.llm.warm(prompts.GATE_SYSTEM)
            if self.catalog:
                self._warm_plan_prompt()
            if self._loop:
                self._loop.call_soon_threadsafe(self.llm_ready.set)

    # ---- input levels for the panel's meters --------------------------------------------
    async def _publish_levels(self, interval: float = 0.1) -> None:
        """10 Hz: per-input peak levels while listening (only when one moved by >= 1 dB), and one
        all-null event when listening stops. Hold and decay live in LevelMeter; the panel only draws."""
        from .audio import LevelMeter

        meter = LevelMeter()
        last = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            dt, last = now - last, now
            cap = self.capture
            levels = meter.update(cap.take_peaks(), dt) if cap and cap.listening else meter.silence()
            if levels is not None:
                self.events.publish({"type": "levels", "levels": levels})

    def start_capture(self) -> None:
        from .audio import CaptureManager

        self.capture = CaptureManager(
            on_audio=self.feed_audio, on_flush=self.flush_input, on_status=self.publish_status,
            device_override=self.config.get("device"), channel_override=self.config.channel_override,
        )

    # ---- status ------------------------------------------------------------------------
    def status(self) -> dict:
        cap = self.capture
        return {
            "stt": self.stt.status,
            "llm": self.llm.status,
            "listening": bool(cap and cap.listening),
            "inputs": cap.open_inputs if cap else [],
            "audioErrors": list(cap.errors) if cap else [],
        }

    def health(self) -> dict:
        return {"ok": True, "version": __version__, **self.status(),
                "models": {"stt": getattr(self.stt, "model_id", None), "llm": getattr(self.llm, "model_id", None)}}

    def publish_status(self) -> None:
        self.events.publish({"type": "status", **self.status()})

    # ---- ids ---------------------------------------------------------------------------
    def new_utterance_id(self) -> str:
        with self._id_lock:
            return f"u{next(self._ids)}"

    def new_plan_id(self) -> str:
        with self._id_lock:
            return f"p{next(self._plan_ids)}"

    # ---- audio path (any thread) -------------------------------------------------------
    def _segmenter(self, input_name: str) -> Segmenter:
        seg = self._segmenters.get(input_name)
        if seg is None:
            seg = Segmenter(
                vad=SileroVAD(), params=self.vad_params, new_id=self.new_utterance_id,
                on_start=lambda uid, n=input_name: self._utt_input.__setitem__(uid, n),
                on_partial=lambda uid, a, n=input_name: self._on_partial(n, uid, a),
                on_end=lambda uid, a, n=input_name: self._on_end(n, uid, a),
            )
            self._segmenters[input_name] = seg
        return seg

    def feed_audio(self, input_name: str, samples16k: np.ndarray) -> None:
        """16 kHz mono float32 from one input. Same path for live capture and files."""
        self._segmenter(input_name).feed(samples16k)

    def flush_input(self, input_name: str) -> None:
        seg = self._segmenters.get(input_name)
        if seg:
            seg.flush()

    def _on_partial(self, input_name: str, uid: str, audio: np.ndarray) -> None:
        if uid in self._partial_busy or self.stt.status != "ready":
            return  # one partial in flight per utterance; skip if the MLX thread is busy
        self._partial_busy.add(uid)

        def job():
            try:
                if uid in self._finalized:
                    return
                text = self.vocab.correct(self.stt.transcribe(audio))
                if text and uid not in self._finalized:
                    self.events.publish({"type": "transcript", "id": uid, "text": text, "final": False,
                                         "input": input_name, "t": time.time()})
            except Exception:
                log.exception("partial STT failed")
            finally:
                self._partial_busy.discard(uid)

        self.mlx.submit(job)

    def _on_end(self, input_name: str, uid: str, audio: np.ndarray) -> None:
        t_end = time.time()

        def job():
            text = ""
            try:
                if audio.size and self.stt.status == "ready":
                    t0 = time.time()
                    text = self.vocab.correct(self.stt.transcribe(audio))
                    log.info("STT %s %.2fs audio -> %.2fs: %r", uid, audio.size / 16000, time.time() - t0, text)
            except Exception as e:
                log.exception("final STT failed")
                self.events.publish({"type": "error", "message": f"speech-to-text failed: {e}"})
            finally:
                self._finalized.add(uid)
            # always close the utterance on the panel side if a partial was shown
            self.events.publish({"type": "transcript", "id": uid, "text": text, "final": True,
                                 "input": input_name, "t": time.time()})
            if text and self._loop:
                item = {"kind": "speech", "text": text, "uid": uid, "input": input_name, "t_end": t_end}
                self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

        self.mlx.submit(job)

    # ---- debug helpers (only exposed with --debug) -------------------------------------
    def inject_utterance(self, text: str, input_name: str = "debug") -> str:
        """Pretend `text` was just spoken and transcribed (exercises the gate)."""
        uid = self.new_utterance_id()
        text = self.vocab.correct(text)
        self.events.publish({"type": "transcript", "id": uid, "text": text, "final": True,
                             "input": input_name, "t": time.time()})
        self._queue.put_nowait({"kind": "speech", "text": text, "uid": uid, "input": input_name, "t_end": time.time()})
        return uid

    def feed_file(self, path: str, input_name: str = "file", realtime: bool = False) -> float:
        """Feed an audio file through the exact live path (device-rate chunks -> soxr -> VAD -> STT)."""
        import soundfile as sf
        import soxr

        data, sr = sf.read(path, dtype="float32", always_2d=True)
        x = data.mean(axis=1)
        x = np.concatenate([np.zeros(int(sr * 0.5), np.float32), x, np.zeros(int(sr * 2.0), np.float32)])
        rs = soxr.ResampleStream(sr, 16000, 1, dtype="float32")
        block = int(sr * 0.032)
        for i in range(0, len(x), block):
            y = rs.resample_chunk(x[i : i + block])
            if len(y):
                self.feed_audio(input_name, y)
            if realtime:
                time.sleep(block / sr)
        return len(x) / sr

    # ---- typed prompts / panel input ---------------------------------------------------
    def submit_prompt(self, text: str) -> str:
        pid = self.new_plan_id()
        self._queue.put_nowait({"kind": "text", "text": self.vocab.correct(text.strip()), "uid": None, "input": "typed",
                                "plan_id": pid, "t_end": time.time()})
        return pid

    def set_catalog(self, actions: list[dict]) -> None:
        self.catalog.set(actions)
        if self.catalog:
            self.mlx.submit(self._warm_plan_prompt)

    def _warm_plan_prompt(self) -> None:
        """Pre-compute the KV cache of the planning system prompt so the first plan is fast."""
        if self.llm.status != "ready" or not hasattr(self.llm, "warm"):
            return
        try:
            self.llm.warm(prompts.plan_system(self.catalog.for_prompt()))
        except Exception:
            log.exception("plan prompt warm-up failed")

    def set_snapshot(self, snap: dict) -> None:
        self.snapshot = snap

    def record_feedback(self, fb: dict) -> None:
        pid = fb.get("planId")
        plan = self.plans.get(pid) if pid else None
        if plan:
            plan["_status"] = {"go": "ran", "dismissed": "dismissed", "failed": "failed"}.get(fb.get("outcome"), "pending")
            for h in self.history:
                if h.get("plan") and h["plan"]["id"] == pid:
                    h["plan"]["status"] = plan["_status"]
        self.profile.record_feedback(fb, self._public(plan) if plan else None)
        # A saveRecipe step that carries its own steps is stored here too.
        if fb.get("outcome") == "go":
            for st in fb.get("steps") or []:
                a = st.get("args") or {}
                if st.get("action", "").lower() == "saverecipe" and a.get("name") and isinstance(a.get("steps"), list):
                    self.profile.put_recipe(a["name"], a["steps"], a.get("aliases"), a.get("description"))

    def reset_conversation(self) -> None:
        """Forget the conversation and pending plans (debug: each test utterance starts fresh)."""
        self.history.clear()
        for p in self.plans.values():
            if p.get("_status") == "pending":
                p["_status"] = "dismissed"

    # ---- planning ----------------------------------------------------------------------
    def pending_plans(self) -> list[dict]:
        ttl = float(self.config.get("pendingPlanTtlS", 300))
        now = time.time()
        return [self._public(p) for p in self.plans.values()
                if p.get("_status") == "pending" and now - p.get("_t", 0) < ttl]

    @staticmethod
    def _public(plan: dict | None) -> dict | None:
        if plan is None:
            return None
        return {k: v for k, v in plan.items() if not k.startswith("_")}

    async def _plan_worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._handle(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("planning failed")
                self.events.publish({"type": "error", "message": f"planning failed: {e}",
                                     **({"planId": item["plan_id"]} if item.get("plan_id") else {})})

    async def _run_mlx(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.mlx, fn, *args)

    async def _handle(self, item: dict) -> None:
        text, uid, typed = item["text"], item["uid"], item["kind"] == "text"
        timing: dict[str, Any] = {"heard": text}
        t0 = time.time()
        entry = {"id": uid or item.get("plan_id"), "who": item.get("input"), "text": text, "t": t0, "plan": None}
        history = list(self.history)
        self.history.append(entry)
        pending = self.pending_plans()

        if not self.catalog:
            msg = "No action catalog yet (the panel sends it on connect)."
            if typed:
                self.events.publish({"type": "error", "message": msg, "planId": item.get("plan_id")})
            log.info("skip %r: %s", text, msg)
            return

        if self.llm.status == "paused":
            msg = "The assistant's brain is paused while memory is tight (LUNA comes first)."
            if typed:
                self.events.publish({"type": "error", "message": msg, "planId": item.get("plan_id")})
            log.info("skip %r: llm paused", text)
            return

        if not self.llm_ready.is_set():
            try:
                await asyncio.wait_for(self.llm_ready.wait(), timeout=180)
            except asyncio.TimeoutError:
                self.events.publish({"type": "error", "message": "language model not ready", "planId": item.get("plan_id")})
                return

        # (a) gate - spoken only
        if not typed and self.config.get("gate", True):
            if _FILLER_ONLY.match(text.strip()) or len(text.split()) < 2:
                log.info("gate: filler %r", text)
                return
            g0 = time.time()
            res = await self._run_mlx(self.llm.chat, prompts.GATE_SYSTEM, prompts.gate_user(text, history, pending), 3, False)
            timing["gateS"] = round(time.time() - g0, 2)
            verdict = res.text.strip().upper()
            log.info("gate %r -> %s (%.2fs)", text, verdict, timing["gateS"])
            if not verdict.startswith("Y"):
                entry["gate"] = "no"
                return
        self.events.publish({"type": "thinking", "utteranceId": uid,
                             **({"planId": item["plan_id"]} if item.get("plan_id") else {})})

        # (b) plan
        recipe = None
        if wants_recipe(text) and not wants_save(text):
            recipe, score = match_recipe(text, self.profile.recipes)
            timing["recipe"] = recipe["name"] if recipe else None
        p0 = time.time()
        plan = await self._plan(text, history, pending, recipe, timing)
        timing["planS"] = round(time.time() - p0, 2)
        timing["totalS"] = round(time.time() - item["t_end"], 2)

        if plan is None or not plan.get("steps"):
            note = (plan or {}).get("notes") or "nothing in the catalog matches that"
            log.info("no plan for %r: %s", text, note)
            if typed:
                self.events.publish({"type": "error", "message": f"Couldn't turn that into actions: {note}",
                                     "planId": item.get("plan_id")})
            self.timings.append(timing)
            return

        plan_id = item.get("plan_id") or self.new_plan_id()
        supersedes = plan.pop("supersedes", None)
        plan = {
            "id": plan_id,
            "utteranceId": uid,
            "heard": text,
            "title": (plan.get("title") or "Plan")[:60],
            "confidence": plan["confidence"],
            "steps": plan["steps"],
            **({"choice": plan["choice"]} if plan.get("alternatives") and plan.get("choice") else {}),
            **({"alternatives": plan["alternatives"]} if plan.get("alternatives") else {}),
            **({"notes": plan["notes"]} if plan.get("notes") else {}),
        }
        if supersedes and supersedes in self.plans and self.plans[supersedes].get("_status") == "pending":
            self.plans[supersedes]["_status"] = "superseded"
            self.events.publish({"type": "retract", "planId": supersedes, "reason": "superseded"})
            for h in self.history:
                if h.get("plan") and h["plan"]["id"] == supersedes:
                    h["plan"]["status"] = "superseded"
        self.plans[plan_id] = {**plan, "_status": "pending", "_t": time.time()}
        entry["plan"] = {"id": plan_id, "title": plan["title"], "steps": plan["steps"], "status": "pending"}
        timing["planId"] = plan_id
        self.timings.append(timing)
        log.info("plan %s %.2fs after end of speech: %s", plan_id, timing["totalS"], plan["title"])
        self.events.publish({"type": "plan", "plan": plan, "timing": timing})

    async def _plan(self, text: str, history: list[dict], pending: list[dict], recipe: dict | None,
                    timing: dict) -> dict | None:
        system = prompts.plan_system(self.catalog.for_prompt())
        user = prompts.plan_user(text, self.snapshot, history, pending, recipe,
                                 prompts.profile_text(self.profile.prompt_summary(), self.profile.recipes))
        max_tok = int(self.config.get("maxPlanTokens", 900))
        res = await self._run_mlx(self.llm.chat, system, user, max_tok, True)
        timing["llm"] = [{"prompt": res.prompt_tokens, "cached": res.cached_tokens, "gen": res.gen_tokens,
                          "s": round(res.seconds, 2)}]
        plan, errors = self._parse_plan(res.text)
        if errors:
            log.info("plan invalid, repairing: %s\nraw: %s", errors[:4], res.text[:2000])
            res2 = await self._run_mlx(self.llm.chat, system, prompts.repair_user(user, res.text, errors), max_tok, True)
            timing["llm"].append({"prompt": res2.prompt_tokens, "cached": res2.cached_tokens, "gen": res2.gen_tokens,
                                  "s": round(res2.seconds, 2)})
            plan2, errors2 = self._parse_plan(res2.text)
            if plan2 is not None and not errors2:
                plan2["confidence"] = max(0.0, plan2["confidence"] - 0.1)
                plan, errors = plan2, []
            else:
                plan = self._salvage(plan2 or plan)
                errors = errors2
        if plan is None and recipe:
            plan = self._recipe_fallback(recipe)
        if plan is None:
            raise ValueError("model output could not be turned into a valid plan: " + "; ".join(errors[:3]))
        self._sends_not_outputs(text, plan)
        self._ensure_click_alternatives(plan)
        self._flag_unknown_names(plan)
        return plan

    @staticmethod
    def _sends_not_outputs(text: str, plan: dict) -> None:
        """"Send X to a bus" keeps X's main output and adds a send (prompt rule 18). The model
        still reroutes outputs now and then when the bus sounds like processing ("a compression
        bus"), which silently takes X out of the mix, so asked for sends, a plan with no addSend
        gets its setOutput steps turned into sends."""
        if not re.search(r"\bsend(s|ing)?\b", text, re.I) or re.search(r"\b(route|output|outputs|sum)\b", text, re.I):
            return
        steps = plan.get("steps") or []
        if any(st.get("action") == "addSend" for st in steps):
            return
        for st in steps:
            if st.get("action") == "setOutput" and str((st.get("args") or {}).get("to", "")).lower() != "main":
                a = st["args"]
                st["action"], st["args"] = "addSend", {"from": a.get("channels"), "to": a.get("to")}

    # Args naming things that must already exist or be made by an earlier step, and args that
    # name something the step itself creates when missing (a bus for a send or an output).
    _REFS = {"channels", "from", "members", "on", "tracks"}
    _CREATES = {("createBus", "name"), ("addSend", "to"), ("setOutput", "to")}

    @staticmethod
    def _norm(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(name).lower())

    def _known(self, name: str, known: set[str]) -> bool:
        n = self._norm(name)
        if not n or n in {"main", "mainoutput", "armed", "all", "selected"}:
            return True
        stem = n.rstrip("s")
        # Same loose matching as the panel: exact, singular/plural, or prefix either way.
        return any(k == n or k.rstrip("s") == stem or k.startswith(n) or (len(k) >= 3 and n.startswith(k)) for k in known)

    def _flag_unknown_names(self, plan: dict) -> None:
        """Models invent tracks ("the toms" in a session without toms). Check every name a
        plan refers to against the session and what the plan itself creates; anything unknown
        lowers confidence and is named in the notes, so the operator sees it before Go."""
        snap = self.snapshot or {}
        known = {self._norm(x.get("name", "")) for x in (snap.get("tracks") or []) + (snap.get("buses") or [])}
        missing: list[str] = []
        for st in plan.get("steps", []):
            a, args = st.get("action"), st.get("args") or {}
            for key, val in args.items():
                if key not in self._REFS:
                    continue
                if a == "createTracks" and key == "tracks":
                    continue
                for v in val if isinstance(val, list) else [val]:
                    if isinstance(v, str) and not self._known(v, known) and v not in missing:
                        missing.append(v)
            if a == "createTracks":
                known |= {self._norm(t.get("name", "")) for t in args.get("tracks", []) if isinstance(t, dict)}
            for act, key in self._CREATES:
                if a == act and isinstance(args.get(key), str):
                    known.add(self._norm(args[key]))
            if a == "createAssistantInputs":
                known.add("assistant")
        if missing:
            plan["confidence"] = min(float(plan.get("confidence") or 0.5), 0.4)
            note = "Not in this session: " + ", ".join(missing)
            plan["notes"] = f"{note}. {plan['notes']}" if plan.get("notes") else note

    def _parse_plan(self, text: str) -> tuple[dict | None, list[str]]:
        try:
            raw = extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            return None, [f"not valid JSON: {e}"]
        if not isinstance(raw, dict):
            return None, ["top level must be an object"]
        plan = {
            "title": str(raw.get("title") or "").strip(),
            "confidence": raw.get("confidence"),
            "steps": [normalize_step(s, self.catalog) for s in (raw.get("steps") or [])],
            "notes": str(raw.get("notes") or "").strip()[:200],
            "supersedes": raw.get("supersedes") or None,
            "choice": str(raw.get("choice") or "").strip()[:40],
        }
        try:
            plan["confidence"] = min(1.0, max(0.0, float(plan["confidence"])))
        except (TypeError, ValueError):
            plan["confidence"] = 0.6
        alts = []
        for a in raw.get("alternatives") or []:
            if isinstance(a, dict) and isinstance(a.get("steps"), list):
                alts.append({"title": str(a.get("title") or "Alternative")[:60],
                             "steps": [normalize_step(s, self.catalog) for s in a["steps"]]})
        plan["alternatives"] = alts
        return plan, plan_errors(plan, self.catalog)

    def _salvage(self, plan: dict | None) -> dict | None:
        """Second attempt still invalid: keep only the valid steps (low confidence) if any."""
        if not plan:
            return None
        good = [s for i, s in enumerate(plan["steps"]) if not self.catalog.step_errors(s, f"steps[{i}]")]
        if not good:
            return None
        alts = []
        for a in plan.get("alternatives") or []:
            if not any(self.catalog.step_errors(s, "alt") for s in a["steps"]):
                alts.append(a)
        dropped = len(plan["steps"]) - len(good)
        return {**plan, "steps": good, "alternatives": alts, "confidence": min(plan["confidence"], 0.45),
                "notes": (plan.get("notes", "") + f" ({dropped} invalid step(s) dropped)").strip()}

    def _recipe_fallback(self, recipe: dict) -> dict | None:
        apply = self.catalog.resolve_name("applyRecipe")
        if apply:
            schema = self.catalog.by_name[apply].get("params") or {}
            props = schema.get("properties") or {}
            key = next((k for k in ("name", "recipe", "recipeName") if k in props), None) or next(iter(props), "name")
            steps = [{"action": apply, "args": {key: recipe["name"]}}]
        else:
            steps = [normalize_step(s, self.catalog) for s in recipe.get("steps", [])]
        plan = {"title": f"Load “{recipe['name']}”", "confidence": 0.7, "steps": steps, "alternatives": [],
                "notes": "Saved setup applied as stored.", "supersedes": None}
        return plan if not plan_errors(plan, self.catalog) else None

    # Click cuts must offer "cues only" and "everywhere".
    def _ensure_click_alternatives(self, plan: dict) -> None:
        click_steps = [s for s in plan["steps"] if "click" in s["action"].lower()]
        if not click_steps:
            plan["alternatives"] = [a for a in plan.get("alternatives") or []
                                    if not any("click" in s["action"].lower() for s in a["steps"])]
            return
        variants = self._click_scope_variants(click_steps[0]["action"])
        if not variants:
            plan["notes"] = (plan.get("notes", "") + " Click scope can't be chosen with this action.").strip()
            return
        (key, cues_val), (_, all_val) = variants

        def with_scope(val):
            out = copy.deepcopy(plan["steps"])
            for s in out:
                if "click" in s["action"].lower():
                    s["args"][key] = val
            return out

        cues, everywhere = with_scope(cues_val), with_scope(all_val)
        others = [a for a in plan.get("alternatives") or []
                  if not any("click" in s["action"].lower() for s in a["steps"])]
        alts = [{"title": "Everywhere", "steps": everywhere}] + others
        if not plan_errors({"steps": cues, "alternatives": alts}, self.catalog):
            plan["steps"] = cues
            plan["choice"] = "Cues only"
            plan["alternatives"] = alts

    def _click_scope_variants(self, action: str):
        props = ((self.catalog.by_name.get(action) or {}).get("params") or {}).get("properties") or {}
        outputs = (self.snapshot or {}).get("clickOutputs") or []
        for key, sch in props.items():
            enum = sch.get("enum") or []
            if enum:
                cue = next((v for v in enum if isinstance(v, str) and "cue" in v.lower()), None)
                every = next((v for v in enum if isinstance(v, str) and re.search(r"all|every", v, re.I)), None)
                if cue and every:
                    return (key, cue), (key, every)
            if sch.get("type") == "array" and outputs and re.search(r"output|dest|where", key, re.I):
                cues = [o for o in outputs if "cue" in o.lower()]
                if cues:
                    return (key, cues), (key, list(outputs))
        return None
