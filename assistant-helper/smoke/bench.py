"""Model benchmark: start a scratch helper per model and score its plans with pass/fail checks.

    uv run python smoke/bench.py mlx-community/Qwen3-8B-4bit mlx-community/Qwen3-14B-4bit

Each model runs in its own helper (port 8793, scratch home, no audio capture) one at a time, so
only one model is ever in memory. Prints a scorecard, latencies and peak memory per model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
PORT = 8793
URL = f"http://127.0.0.1:{PORT}"


def fx(name):
    return json.loads((FIX / name).read_text())


def acts(plan):
    return [s["action"] for s in plan.get("steps", [])]


def step(plan, action):
    return next((s["args"] for s in plan.get("steps", []) if s["action"] == action), None)


def names_in(plan):
    out = []
    for s in plan.get("steps", []):
        a = s["args"]
        for k in ("channels", "from", "members", "tracks"):
            v = a.get(k)
            if isinstance(v, list):
                out += [x if isinstance(x, str) else x.get("name", "") for x in v]
        for k in ("on", "to"):
            if isinstance(a.get(k), str):
                out.append(a[k])
    return [n.lower() for n in out]


def check_drums(p):
    ct = step(p, "createTracks") or {}
    got = [(t.get("name", "").lower(), t.get("format", "mono"), t.get("input")) for t in ct.get("tracks", [])]
    want = [("kick", "mono", "1"), ("snare", "mono", "2"), ("hats", "mono", "3"), ("ride", "mono", "4"), ("overheads", "stereo", "5-6")]
    ok_tracks = [(n.replace("hi-hat", "hats").replace("hihat", "hats"), f, i) for n, f, i in got] == want
    g = step(p, "createGroup") or {}
    return ok_tracks and "setOutput" in acts(p) and len(g.get("members", [])) == 5


def check_comp(p):
    s = step(p, "addSend") or {}
    ins = step(p, "insertPlugin") or {}
    return (sorted(x.lower() for x in s.get("from", [])) == ["kick", "snare"] and "setOutput" not in acts(p)
            and "160" in ins.get("plugin", "") and ins.get("on", "").lower() == s.get("to", "").lower())


def check_take(p):
    a = acts(p)
    sse = step(p, "setStartEarlier") or {}
    return all(x in a for x in ("favouriteTake", "newPlaylist", "setStartEarlier", "cutClick")) and sse.get("bars", 0) >= 1


def check_send_verb(p):
    s = step(p, "addSend") or {}
    ins = step(p, "insertPlugin") or {}
    return (sorted(x.lower() for x in s.get("from", [])) == ["kick", "snare"] and "setOutput" not in acts(p)
            and "plate" in ins.get("plugin", "").lower())


def check_route(p):
    o = step(p, "setOutput") or {}
    return sorted(x.lower() for x in o.get("channels", [])) == ["kick", "snare"] and "addSend" not in acts(p)


def check_missing(p):
    # No toms in the session: it must not invent them (or must flag low confidence).
    return not any("tom" in n for n in names_in(p)) or p.get("confidence", 1) < 0.5


CASES = [
    ("drum setup (example 1)", "snapshot0.json", "Set me up mono audio tracks for kick, snare, hats, ride, a stereo track for overheads. inputs are 1-6 in that order and a collective stereo output is fine and then create a group that controls all of them", check_drums),
    ("comp send + dbx (example 2)", "snapshot1.json", "create a new send off of kick and snare to a compression bus, load a dbx160 on it", check_comp),
    ("take/playlist/intro/click (example 3)", "snapshot2.json", "ok that drum take was great, mark it as a favourite and then create a new playlist to record, give me extra bars of intro before the drummer comes in then cut the click at the end of the part", check_take),
    ("'send' stays a send", "snapshot1.json", "send kick and snare to a new verb bus with a plate on it", check_send_verb),
    ("'route' is an output", "snapshot1.json", "route kick and snare into a drum bus instead of the main output", check_route),
    ("doesn't invent tracks", "snapshot1.json", "add a send from the toms to the comp bus", check_missing),
]
CHATTER = ["man that was a long drive", "who wants coffee", "that snare sounds amazing in here",
           "I think the second verse was dragging a little", "did you guys catch the game last night"]
REQUESTS = ["can we get more intro bars before the drums come in", "stick an eleven seventy six on the vocal",
            "let's go again on a new playlist"]


def rss_mb(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0) / 1024
    except Exception:
        return 0


def wait_event(stream_lines, pred, timeout=60):
    t0 = time.time()
    for line in stream_lines:
        if line.startswith("data:"):
            e = json.loads(line[5:])
            if pred(e):
                return e
        if time.time() - t0 > timeout:
            break
    return None


def bench(model: str) -> dict:
    home = tempfile.mkdtemp(prefix="cp-bench-")
    (Path(home) / "config.json").write_text(json.dumps({"port": PORT, "llmModel": model}))
    env = {**os.environ, "CP_ASSISTANT_HOME": home, "CP_ASSISTANT_PORT": str(PORT)}
    proc = subprocess.Popen([sys.executable, "-m", "assistant_helper", "--no-capture", "--debug"], cwd=ROOT, env=env,
                            stdout=open(Path(home) / "log.txt", "w"), stderr=subprocess.STDOUT)
    peak = 0.0
    res = {"model": model, "cases": [], "gate": []}
    try:
        t0 = time.time()
        while True:
            peak = max(peak, rss_mb(proc.pid))
            try:
                if httpx.get(f"{URL}/health", timeout=2).json().get("llm") == "ready":
                    break
            except Exception:
                pass
            if time.time() - t0 > 400 or proc.poll() is not None:
                raise RuntimeError(f"helper did not get ready; see {home}/log.txt")
            time.sleep(1)
        res["load_s"] = round(time.time() - t0, 1)
        httpx.post(f"{URL}/catalog", json=fx("catalog.json"), timeout=30)
        with httpx.stream("GET", f"{URL}/events", timeout=None) as r:
            lines = r.iter_lines()
            for label, snap, text, check in CASES:
                httpx.post(f"{URL}/context", json=fx(snap), timeout=10)
                time.sleep(0.2)
                pid = httpx.post(f"{URL}/prompt", json={"text": text, "source": "text"}, timeout=10).json()["planIds"][0]
                t1 = time.time()
                e = wait_event(lines, lambda e: (e.get("type") == "plan" and e["plan"]["id"] == pid) or (e.get("type") == "error" and e.get("planId") == pid))
                peak = max(peak, rss_mb(proc.pid))
                plan = (e or {}).get("plan") or {}
                res["cases"].append({"case": label, "ok": bool(plan) and check(plan), "s": round(time.time() - t1, 1),
                                     "steps": [(s["action"], s["args"]) for s in plan.get("steps", [])],
                                     "confidence": plan.get("confidence"), "error": (e or {}).get("message")})
            for text, want in [(c, False) for c in CHATTER] + [(q, True) for q in REQUESTS]:
                uid = httpx.post(f"{URL}/debug/utterance", json={"text": text}, timeout=10).json().get("utteranceId")
                # The gate says yes by announcing "thinking" for the utterance; a no is silence.
                e = wait_event(lines, lambda e: e.get("type") == "thinking" and e.get("utteranceId") == uid, timeout=15)
                got = e is not None
                res["gate"].append({"text": text, "want": want, "got": got})
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    res["peak_gb"] = round(peak / 1024, 1)
    return res


def main():
    models = sys.argv[1:] or ["mlx-community/Qwen3-8B-4bit", "mlx-community/Qwen3-14B-4bit"]
    all_res = []
    for m in models:
        print(f"\n=== {m}", flush=True)
        r = bench(m)
        all_res.append(r)
        for c in r["cases"]:
            print(f"  {'PASS' if c['ok'] else 'FAIL'} {c['case']:<40} {c['s']:>5}s conf={c['confidence']}")
            if not c["ok"]:
                print("       ", json.dumps(c["steps"])[:600], c["error"] or "")
        g_ok = sum(1 for g in r["gate"] if g["want"] == g["got"])
        print(f"  gate {g_ok}/{len(r['gate'])}", [g["text"] for g in r["gate"] if g["want"] != g["got"]])
        print(f"  load {r['load_s']}s, peak memory {r['peak_gb']} GB")
    out = ROOT / "smoke" / "out"
    out.mkdir(exist_ok=True)
    (out / "bench.json").write_text(json.dumps(all_res, indent=1))


if __name__ == "__main__":
    main()
