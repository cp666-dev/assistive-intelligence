import json
import threading
import time

import httpx
import numpy as np

from conftest import fixture

ORIGIN = {"Origin": "ua-content://com.cp.assistant"}

PLAN_SEND = json.dumps({
    "title": "Kick + Snare → Comp Bus (dbx 160)", "confidence": 0.86,
    "steps": [
        {"action": "createBus", "args": {"name": "Comp Bus", "format": "stereo"}},
        {"action": "addSend", "args": {"from": ["Kick", "Snare"], "to": "Comp Bus"}},
        {"action": "insertPlugin", "args": {"on": "Comp Bus", "plugin": "dbx 160"}},
    ],
    "notes": "", "supersedes": None,
})


def setup_session(url, snap="snapshot1.json"):
    assert httpx.post(url + "/catalog", json=fixture("catalog.json")).json() == {"ok": True}
    assert httpx.post(url + "/context", json=fixture(snap)).json() == {"ok": True}


def test_options_preflight_204_any_path(live):
    srv, _, _ = live
    for path in ("/catalog", "/prompt", "/profile/recipe/whatever", "/nope"):
        r = httpx.options(srv.url + path, headers={**ORIGIN, "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": "content-type, x-foo"})
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "*"
        assert "x-foo" in r.headers["access-control-allow-headers"]
        assert "POST" in r.headers["access-control-allow-methods"]


def test_cors_on_responses_and_health(live):
    srv, _, _ = live
    r = httpx.get(srv.url + "/health", headers=ORIGIN)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    h = r.json()
    assert h["ok"] is True and h["stt"] == "ready" and h["llm"] == "ready"
    assert h["listening"] is False and h["inputs"] == [] and "version" in h


def test_sse_status_on_connect_then_events(live):
    srv, llm, _ = live
    setup_session(srv.url)
    got = []

    def reader():
        with httpx.stream("GET", srv.url + "/events", headers=ORIGIN, timeout=10) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["access-control-allow-origin"] == "*"
            for line in r.iter_lines():
                if line.startswith("data: "):
                    got.append(json.loads(line[6:]))
                    if got[-1]["type"] == "plan":
                        return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.3)
    llm.plan_outputs.append(PLAN_SEND)
    r = httpx.post(srv.url + "/prompt", json={"text": "create a new send off of kick and snare to a compression bus, load a dbx160 on it", "source": "text"})
    body = r.json()
    assert body["ok"] and len(body["planIds"]) == 1
    t.join(timeout=5)
    assert got[0]["type"] == "status" and {"stt", "llm", "listening", "inputs"} <= set(got[0])
    assert any(e["type"] == "thinking" for e in got)
    plan = got[-1]["plan"]
    assert plan["id"] == body["planIds"][0]
    assert plan["utteranceId"] is None
    assert plan["heard"].startswith("create a new send")
    assert [s["action"] for s in plan["steps"]] == ["createBus", "addSend", "insertPlugin"]
    assert 0 <= plan["confidence"] <= 1


def test_sse_heartbeat(live, monkeypatch):
    import assistant_helper.server as server

    monkeypatch.setattr(server, "HEARTBEAT_S", 0.2)
    srv, _, _ = live
    n = 0
    with httpx.stream("GET", srv.url + "/events", timeout=5) as r:
        for line in r.iter_lines():
            if line.startswith("data: ") and json.loads(line[6:])["type"] == "status":
                n += 1
                if n == 3:
                    break
    assert n == 3


def test_prompt_uses_catalog_and_snapshot_in_prompt(live):
    srv, llm, _ = live
    setup_session(srv.url)
    llm.plan_outputs.append(PLAN_SEND)
    httpx.post(srv.url + "/prompt", json={"text": "send kick and snare to a comp bus", "source": "text"})
    srv.wait_event(lambda e: e["type"] == "plan")
    system, user = llm.calls[-1]
    assert "- cutClick:" in system and "- createTracks:" in system
    assert "- Kick: audio mono" in user and "CLICK OUTPUTS: MON, CUE 1, CUE 2" in user


def test_typed_prompt_skips_gate(live):
    srv, llm, _ = live
    setup_session(srv.url)
    llm.chatter.add("who wants coffee")
    llm.plan_outputs.append('{"title":"","confidence":0,"steps":[],"notes":"not a session request"}')
    pid = httpx.post(srv.url + "/prompt", json={"text": "who wants coffee", "source": "text"}).json()["planIds"][0]
    ev = srv.wait_event(lambda e: e["type"] == "error" and e.get("planId") == pid)
    assert "not a session request" in ev["message"]
    from assistant_helper.prompts import GATE_SYSTEM
    assert all(c[0] != GATE_SYSTEM for c in llm.calls)


def test_invalid_plan_is_repaired_once(live):
    srv, llm, _ = live
    setup_session(srv.url)
    llm.plan_outputs += [
        '{"title":"Marker","confidence":0.9,"steps":[{"action":"addMarker","args":{"label":"Bridge","at":9}}]}',
        '```json\n{"title":"Marker","confidence":0.9,"steps":[{"action":"addMarker","args":{"name":"Bridge","at":9}}]}\n```',
    ]
    httpx.post(srv.url + "/prompt", json={"text": "add a marker called bridge", "source": "text"})
    ev = srv.wait_event(lambda e: e["type"] == "plan")
    assert ev["plan"]["steps"] == [{"action": "addMarker", "args": {"name": "Bridge", "at": 9}}]
    assert "is invalid" in llm.calls[-1][1]


def test_unknown_action_never_reaches_panel(live):
    srv, llm, _ = live
    setup_session(srv.url)
    bad = '{"title":"x","confidence":0.9,"steps":[{"action":"deleteEverything","args":{}}]}'
    llm.plan_outputs += [bad, bad]
    pid = httpx.post(srv.url + "/prompt", json={"text": "nuke it", "source": "text"}).json()["planIds"][0]
    srv.wait_event(lambda e: e["type"] == "error" and e.get("planId") == pid)
    assert not any(e["type"] == "plan" for e in srv.assistant.events.history)


def test_click_cut_gets_cues_and_everywhere(live):
    srv, llm, _ = live
    setup_session(srv.url, "snapshot2.json")
    llm.plan_outputs.append('{"title":"Cut click","confidence":0.8,"steps":[{"action":"cutClick","args":{"at":"Outro","where":"everywhere"}}]}')
    httpx.post(srv.url + "/prompt", json={"text": "cut the click at the outro", "source": "text"})
    plan = srv.wait_event(lambda e: e["type"] == "plan")["plan"]
    assert plan["steps"] == [{"action": "cutClick", "args": {"at": "Outro", "where": "cues"}}]
    assert plan["choice"] == "Cues only"
    assert [a["title"] for a in plan["alternatives"]] == ["Everywhere"]
    assert plan["alternatives"][0]["steps"] == [{"action": "cutClick", "args": {"at": "Outro", "where": "everywhere"}}]


def test_revision_retracts_pending_plan(live):
    srv, llm, _ = live
    setup_session(srv.url, "snapshot0.json")
    llm.plan_outputs.append('{"title":"Bass track","confidence":0.9,"steps":[{"action":"createTracks","args":{"tracks":[{"name":"Bass","format":"mono","input":"3"}]}}]}')
    p1 = httpx.post(srv.url + "/prompt", json={"text": "make a bass track on input 3", "source": "text"}).json()["planIds"][0]
    srv.wait_event(lambda e: e["type"] == "plan" and e["plan"]["id"] == p1)
    llm.plan_outputs.append(json.dumps({"title": "Bass track (stereo)", "confidence": 0.9, "supersedes": p1,
        "steps": [{"action": "createTracks", "args": {"tracks": [{"name": "Bass", "format": "stereo", "input": "3-4"}]}}]}))
    p2 = httpx.post(srv.url + "/prompt", json={"text": "actually make it stereo", "source": "text"}).json()["planIds"][0]
    srv.wait_event(lambda e: e["type"] == "plan" and e["plan"]["id"] == p2)
    hist = srv.assistant.events.history
    ri = next(i for i, e in enumerate(hist) if e["type"] == "retract")
    pi = next(i for i, e in enumerate(hist) if e["type"] == "plan" and e["plan"]["id"] == p2)
    assert hist[ri] == {"type": "retract", "planId": p1, "reason": "superseded"} and ri < pi
    # the pending plan was shown to the model
    assert f"- {p1} \"Bass track\"" in llm.calls[-1][1]


def test_conversation_history_in_prompt(live):
    srv, llm, _ = live
    setup_session(srv.url)
    llm.plan_outputs.append('{"title":"Fav","confidence":0.9,"steps":[{"action":"favouriteTake","args":{"tracks":["Kick"]}}]}')
    httpx.post(srv.url + "/prompt", json={"text": "favourite the kick take", "source": "text"})
    srv.wait_event(lambda e: e["type"] == "plan")
    llm.plan_outputs.append('{"title":"Fav","confidence":0.9,"steps":[{"action":"favouriteTake","args":{"tracks":["Snare"]}}]}')
    httpx.post(srv.url + "/prompt", json={"text": "do the same for the snare", "source": "text"})
    srv.wait_event(lambda e: e["type"] == "plan" and e["plan"]["heard"].startswith("do the same"))
    user = llm.calls[-1][1]
    assert '"favourite the kick take" -> plan p1' in user and '"action":"favouriteTake"' in user


def test_speech_path_gate_and_plan(live):
    """Audio -> VAD -> (fake) STT -> gate -> plan, through the same feed_audio used by live capture."""
    srv, llm, stt = live
    setup_session(srv.url)
    llm.plan_outputs.append('{"title":"Marker","confidence":0.9,"steps":[{"action":"addMarker","args":{"name":"Bridge","at":9}}]}')
    a = srv.assistant
    a.feed_audio("MIC/LINE 8", np.zeros(16000, dtype=np.float32))  # silence only: nothing
    assert stt.calls == 0
    # inject utterances directly at the transcript stage (VAD is covered by test_pipeline)
    a._on_end("MIC/LINE 8", "u90", np.ones(16000, dtype=np.float32) * 0.01)
    ev = srv.wait_event(lambda e: e["type"] == "plan")
    assert ev["plan"]["utteranceId"] == "u90"
    fin = srv.wait_event(lambda e: e["type"] == "transcript" and e["final"])
    assert fin["id"] == "u90" and fin["input"] == "MIC/LINE 8" and "t" in fin


def test_gate_blocks_chatter(live):
    srv, llm, stt = live
    setup_session(srv.url)
    stt.text = "who wants coffee"
    llm.chatter.add("who wants coffee")
    srv.assistant._on_end("MIC/LINE 8", "u91", np.ones(16000, dtype=np.float32) * 0.01)
    srv.wait_event(lambda e: e["type"] == "transcript" and e["final"])
    time.sleep(0.3)
    assert not any(e["type"] in ("plan", "thinking") for e in srv.assistant.events.history)


def test_feedback_and_profile_learning(live):
    srv, llm, _ = live
    setup_session(srv.url)
    llm.plan_outputs.append(PLAN_SEND)
    pid = httpx.post(srv.url + "/prompt", json={"text": "kick and snare to a comp bus with a dbx", "source": "text"}).json()["planIds"][0]
    srv.wait_event(lambda e: e["type"] == "plan")
    steps = json.loads(PLAN_SEND)["steps"]
    r = httpx.post(srv.url + "/feedback", json={"planId": pid, "outcome": "go", "steps": steps,
                                               "edits": [{"kind": "rename", "from": "Kick", "to": "Kick In"}]})
    assert r.json() == {"ok": True}
    prof = httpx.get(srv.url + "/profile").json()
    assert prof["preferences"]["naming"] == {"Kick": "Kick In"}
    assert prof["preferences"]["plugins"]["comp bus"] == "dbx 160"
    assert prof["feedback"][-1]["planId"] == pid
    # preferences reach the planning prompt
    llm.plan_outputs.append(PLAN_SEND)
    httpx.post(srv.url + "/prompt", json={"text": "again", "source": "text"})
    srv.wait_event(lambda e: e["type"] == "plan" and e["plan"]["heard"] == "again")
    assert '"Kick": "Kick In"' in llm.calls[-1][1]
    assert httpx.post(srv.url + "/feedback", json={"planId": pid, "outcome": "maybe"}).status_code == 400


def test_recipe_endpoints(live):
    srv, _, _ = live
    steps = [{"action": "createTracks", "args": {"tracks": [{"name": "Violin 1", "format": "mono", "input": "1"}]}}]
    assert httpx.post(srv.url + "/profile/recipe", json={"name": "String Quartet", "aliases": ["strings"], "steps": steps}).json() == {"ok": True}
    prof = httpx.get(srv.url + "/profile").json()
    assert prof["recipes"][0]["name"] == "String Quartet" and prof["recipes"][0]["aliases"] == ["strings"]
    assert httpx.post(srv.url + "/profile/recipe", json={"name": "x"}).status_code == 400
    assert httpx.delete(srv.url + "/profile/recipe/string%20quartet").json() == {"ok": True}
    assert httpx.get(srv.url + "/profile").json()["recipes"] == []
    assert httpx.delete(srv.url + "/profile/recipe/nope").status_code == 404


def test_recipe_request_injects_matched_setup(live):
    srv, llm, _ = live
    setup_session(srv.url, "snapshot0.json")
    steps = [{"action": "createTracks", "args": {"tracks": [{"name": "Violin 1", "format": "mono", "input": "1"}]}}]
    httpx.post(srv.url + "/profile/recipe", json={"name": "String Quartet", "aliases": ["strings"], "steps": steps})
    llm.plan_outputs.append('{"title":"String Quartet","confidence":0.9,"steps":[{"action":"applyRecipe","args":{"name":"String Quartet"}}]}')
    httpx.post(srv.url + "/prompt", json={"text": "load my usual string quartet setup", "source": "text"})
    plan = srv.wait_event(lambda e: e["type"] == "plan")["plan"]
    assert plan["steps"][0]["action"] == "applyRecipe"
    assert 'MATCHED SAVED SETUP "String Quartet"' in llm.calls[-1][1]
    assert "- String Quartet (aka strings)" in llm.calls[-1][1]


def test_listen_without_capture_and_bad_json(live):
    srv, _, _ = live
    r = httpx.post(srv.url + "/listen", json={"inputs": [{"name": "MIC/LINE 1", "device": "X", "channel": 0}], "enabled": True})
    assert r.json()["ok"] is False and r.json()["errors"]
    r = httpx.post(srv.url + "/catalog", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.headers["access-control-allow-origin"] == "*"
