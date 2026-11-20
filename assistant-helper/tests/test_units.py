import json

import pytest

from assistant_helper.config import PACKAGE_DATA
from assistant_helper.profile import Profile, role_of
from assistant_helper.recipes import match_recipe, wants_recipe, wants_save
from assistant_helper.validate import Catalog, extract_json, normalize_step, plan_errors
from assistant_helper.vocab import VocabCorrector

from conftest import fixture

# ---- vocabulary ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocab():
    return VocabCorrector(PACKAGE_DATA / "vocabulary.json")


@pytest.mark.parametrize("heard,want", [
    ("put a dee bee ex one sixty on the bus", "put a dbx 160 on the bus"),
    ("load a dbx one sixty on it", "load a dbx 160 on it"),
    ("load a dbx160 on it", "load a dbx 160 on it"),
    ("throw an la two a on the vocal", "throw an LA-2A on the vocal"),
    ("LA 2A on bass", "LA-2A on bass"),
    ("an eleven seventy six on the snare", "an 1176 on the snare"),
    ("a stereo track for the over heads", "a stereo track for the overheads"),
    ("high hat and ride", "hi-hat and ride"),
    ("the high hats are loud", "the hi-hats are loud"),
    ("kick, snare, hats, ride", "kick, snare, hats, ride"),  # untouched
    ("a new play list on the buss", "a new playlist on the bus"),
])
def test_vocab(vocab, heard, want):
    assert vocab.correct(heard) == want


def test_vocab_hot_reload(tmp_path):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"replacements": [{"to": "Pultec", "from": ["pull tech"]}]}))
    v = VocabCorrector(p)
    assert v.correct("a pull tech on bass") == "a Pultec on bass"
    import os, time
    p.write_text(json.dumps({"replacements": [{"to": "Fairchild", "from": ["fair child"]}]}))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert v.correct("fair child and pull tech") == "Fairchild and pull tech"


# ---- recipes ---------------------------------------------------------------------------

RECIPES = [
    {"name": "String Quartet", "aliases": ["strings", "quartet"], "steps": []},
    {"name": "Mix Setup", "aliases": ["typical mix setup", "mix template"], "steps": []},
    {"name": "Drum Tracking", "aliases": ["drums"], "steps": []},
]


@pytest.mark.parametrize("text,name", [
    ("set me up my usual string quartet setup", "String Quartet"),
    ("load my typical mix setup", "Mix Setup"),
    ("can you bring up my usual strings session", "String Quartet"),
    ("load our standard drum tracking setup", "Drum Tracking"),
    ("my usual string quartett set up please", "String Quartet"),  # STT misspelling
])
def test_recipe_match(text, name):
    assert wants_recipe(text)
    r, score = match_recipe(text, RECIPES)
    assert r and r["name"] == name, (text, score)


def test_recipe_no_match():
    r, _ = match_recipe("load my usual brass section setup", RECIPES)
    assert r is None
    assert not wants_recipe("create a new send off kick and snare")


def test_save_intent():
    assert wants_save("save this as my string quartet setup")
    assert wants_save("remember that as my usual mix template")
    assert not wants_recipe("save this as my string quartet setup")


# ---- validation ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog():
    return Catalog(fixture("catalog.json")["actions"])


def test_plan_valid(catalog):
    plan = {"steps": [
        {"action": "createTracks", "args": {"tracks": [{"name": "Kick", "format": "mono", "input": "1"},
                                                       {"name": "OH", "format": "stereo", "input": "5-6"}]}},
        {"action": "createGroup", "args": {"name": "Drums", "members": ["Kick", "OH"]}},
        {"action": "newPlaylist", "args": {"tracks": "armed"}},
        {"action": "cutClick", "args": {"at": "playhead", "where": "cues"}},
    ]}
    assert plan_errors(plan, catalog) == []


@pytest.mark.parametrize("step,frag", [
    ({"action": "explode", "args": {}}, "unknown action"),
    ({"action": "createTracks", "args": {"tracks": [{"name": "Kick", "format": "quad"}]}}, "is not one of"),
    ({"action": "addSend", "args": {"from": ["Kick"]}}, "'to' is a required property"),
    ({"action": "setStartEarlier", "args": {"bars": "two"}}, "is not of type 'integer'"),
    ({"action": "addMarker", "args": {"name": "x", "at": 3, "colour": "red"}}, "Additional properties"),
    ({"action": "newPlaylist", "args": {"tracks": "everyone"}}, "is not valid under any of the given schemas"),
])
def test_plan_invalid(catalog, step, frag):
    errs = plan_errors({"steps": [step]}, catalog)
    assert errs and frag in errs[0]


def test_alternatives_validated(catalog):
    plan = {"steps": [], "alternatives": [{"title": "x", "steps": [{"action": "cutClick", "args": {"at": 9, "where": "monitors"}}]}]}
    assert "alternatives[0]" in plan_errors(plan, catalog)[0]


def test_normalize_step(catalog):
    assert normalize_step({"name": "addmarker", "args": {"name": "A", "at": 1}}, catalog) == {"action": "addMarker", "args": {"name": "A", "at": 1}}
    assert normalize_step({"action": "setStartEarlier", "bars": 2}, catalog) == {"action": "setStartEarlier", "args": {"bars": 2}}


def test_extract_json():
    assert extract_json('<think>\n</think>\nSure!\n```json\n{"a": [1, 2,], "b": "}"}\n```') == {"a": [1, 2], "b": "}"}
    assert extract_json('{"x": {"y": 1}} trailing') == {"x": {"y": 1}}
    with pytest.raises(ValueError):
        extract_json('{"x": 1')


# ---- learning --------------------------------------------------------------------------


def test_learning(tmp_path):
    p = Profile(tmp_path / "profile.json")
    steps = [
        {"action": "createTracks", "args": {"tracks": [{"name": "Overheads", "format": "stereo", "input": "5-6"}]}},
        {"action": "insertPlugin", "args": {"on": "Comp Bus", "plugin": "dbx 160"}},
    ]
    p.record_feedback({"planId": "p1", "outcome": "go", "steps": steps,
                       "edits": [{"kind": "rename", "from": "Kick", "to": "Kick In"}]})
    prefs = Profile(tmp_path / "profile.json").preferences  # persisted
    assert prefs["formats"]["overheads"] == "stereo"
    assert prefs["plugins"]["comp bus"] == "dbx 160"
    assert prefs["naming"] == {"Kick": "Kick In"}
    arm = {"steps": [{"action": "setRecordArm", "args": {"tracks": ["Kick"], "on": True}}]}
    p.record_feedback({"planId": "p2", "outcome": "dismissed"}, arm)
    assert p.preferences["avoid"] == []
    p.record_feedback({"planId": "p3", "outcome": "dismissed"}, arm)
    assert p.preferences["avoid"] == ["setRecordArm"]
    s = p.prompt_summary()
    assert "Kick In" in s and "dbx 160" in s and "setRecordArm" in s
    # hand edits survive
    data = json.loads((tmp_path / "profile.json").read_text())
    data["preferences"]["notes"] = ["Always name the vocal 'Lead Vox'"]
    (tmp_path / "profile.json").write_text(json.dumps(data))
    import os, time
    os.utime(tmp_path / "profile.json", (time.time() + 5, time.time() + 5))
    assert "Lead Vox" in p.prompt_summary()


def test_role_of():
    assert role_of("Kick 2") == "kick" and role_of("Comp Bus") == "comp bus" and role_of("Tom-1") == "tom"


def test_extract_json_repairs_a_step_missing_its_opening_brace():
    # Qwen3-8B output for "set me up mono audio tracks for bass amp and bass DI ...", verbatim.
    raw = ('{"title":"Create Bass Tracks & Group","confidence":0.9,"steps":[{"action":"createTracks","args":{"tracks":'
           '[{"name":"Bass AMP","kind":"audio","format":"mono","input":"1"},{"name":"Bass DI","kind":"audio","format":"mono","input":"2"}]}},'
           '"action":"createGroup","args":{"name":"Bass Group","members":["Bass AMP","Bass DI"],"links":["mix"]}}')
    plan = extract_json(raw)
    assert [s["action"] for s in plan["steps"]] == ["createTracks", "createGroup"]
    assert plan["steps"][1]["args"]["members"] == ["Bass AMP", "Bass DI"]
    assert plan["confidence"] == 0.9


def test_extract_json_still_rejects_truncated_output():
    with pytest.raises(ValueError):
        extract_json('{"title":"x","steps":[{"action":"setTempo","args":{"bpm":')
    with pytest.raises(ValueError):
        extract_json('{"steps":[{"action":"a","args":{}},"action":"b","args":{"x":"unterminated')


def test_stereo_pair_uses_its_first_channel_mapping(tmp_path):
    from assistant_helper.config import load_config

    cfg = load_config(tmp_path)
    cfg.data["channels"] = {"TALKBACK 1": 14, "MIC/LINE 8": 7}
    assert cfg.channel_override("TALKBACK 1-2") == 14
    assert cfg.channel_override("talkback 1") == 14
    assert cfg.channel_override("MIC/LINE 3-4") is None
    assert cfg.channel_override("TALKBACK 2") is None


def test_send_requests_never_reroute_outputs():
    from assistant_helper.assistant import Assistant

    plan = {"steps": [{"action": "createBus", "args": {"name": "Comp Bus", "format": "stereo"}},
                      {"action": "setOutput", "args": {"channels": ["Kick", "Snare"], "to": "Comp Bus"}}]}
    Assistant._sends_not_outputs("Send the kick and snare to a new compression bus.", plan)
    assert plan["steps"][1] == {"action": "addSend", "args": {"from": ["Kick", "Snare"], "to": "Comp Bus"}}
    routed = {"steps": [{"action": "setOutput", "args": {"channels": ["Kick"], "to": "Drums"}}]}
    Assistant._sends_not_outputs("Route the kick to the drum bus.", routed)
    assert routed["steps"][0]["action"] == "setOutput"
    both = {"steps": [{"action": "setOutput", "args": {"channels": ["Kick"], "to": "Drums"}},
                      {"action": "addSend", "args": {"from": ["Kick"], "to": "Verb"}}]}
    Assistant._sends_not_outputs("Bus the kick to drums and send it to the verb.", both)
    assert both["steps"][0]["action"] == "setOutput"
