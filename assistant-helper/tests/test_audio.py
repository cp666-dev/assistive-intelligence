"""CaptureManager error paths (no device is opened, so no microphone prompt)."""

from assistant_helper.audio import CaptureManager


def make(**kw):
    return CaptureManager(on_audio=lambda n, x: None, on_flush=lambda n: None, on_status=lambda: None, **kw)


def test_missing_device_reported():
    cm = make()
    opened, errors = cm.configure([{"name": "MIC/LINE 1", "device": "No Such Interface 9000", "channel": 0}], True)
    assert opened == [] and errors == ["MIC/LINE 1: audio device 'No Such Interface 9000' not found"]
    assert cm.listening is False
    cm.configure([], False)
    assert cm.errors == []
    cm.shutdown()


def test_channel_out_of_range_and_override():
    import sounddevice as sd

    dev = next(d for d in sd.query_devices() if d["max_input_channels"] > 0)
    cm = make(channel_override=lambda name: 999 if name == "Talkback" else None)
    opened, errors = cm.configure([{"name": "Talkback", "device": dev["name"], "channel": 0}], True)
    assert opened == [] and "channel 999 out of range" in errors[0]
    cm.shutdown()


def test_channel_comes_from_config_then_driver_name_then_guess(monkeypatch):
    from assistant_helper import coreaudio
    from assistant_helper.audio import ListenInput

    apollo = coreaudio.Device(uid="com_uaudio_driver_UAD2AudioEngine:0", name="Universal Audio Thunderbolt",
                              inputs=16, channel_names=["MIC/LINE 1", "MIC/LINE 2"] + [""] * 12 + ["TALKBACK 1", "TALKBACK 2"])
    monkeypatch.setattr(coreaudio, "input_devices", lambda: [apollo])
    specs = [
        ListenInput("MIC/LINE 2", "x", channel=9, guess=1),            # config.json wins
        ListenInput("TALKBACK 1-2", "x", channel=None, format="stereo"),  # driver's name
        ListenInput("MIC/LINE 3", "x", channel=None, guess=2),         # unnamed: the panel's guess
        ListenInput("VIRTUAL 1", "x", channel=None),                   # nothing known: reported
    ]
    out, errors = CaptureManager._resolve_channels(specs, "Universal Audio Thunderbolt")
    assert [(s.name, s.channel) for s in out] == [("MIC/LINE 2", 9), ("TALKBACK 1-2", 14), ("MIC/LINE 3", 2)]
    assert out[1].channels == [14, 15]
    assert len(errors) == 1 and errors[0].startswith('VIRTUAL 1: no hardware channel known; set "channels"')


def test_a_one_channel_device_needs_no_number(monkeypatch):
    from assistant_helper import coreaudio
    from assistant_helper.audio import ListenInput

    monkeypatch.setattr(coreaudio, "input_devices", lambda: [])
    mac = [ListenInput("MacBook Pro Microphone", "BuiltInMicrophoneDevice", channel=None)]
    out, errors = CaptureManager._resolve_channels(mac, "MacBook Pro Microphone", 1)
    assert [(s.name, s.channel) for s in out] == [("MacBook Pro Microphone", 0)] and errors == []
    out, errors = CaptureManager._resolve_channels(mac, "Some Interface", 8)  # 8 channels: no guessing
    assert out == [] and errors[0].startswith("MacBook Pro Microphone: no hardware channel known")


def test_missing_device_reported_for_inputs_without_a_channel():
    cm = make(channel_override=lambda name: 14 if name == "TALKBACK 2" else None)
    opened, errors = cm.configure([
        {"name": "TALKBACK 1", "device": "No Such Interface 9000", "channel": None},
        {"name": "TALKBACK 2", "device": "No Such Interface 9000", "channel": None},
    ], True)
    assert [(w.name, w.channel) for w in cm._wanted] == [("TALKBACK 1", None), ("TALKBACK 2", 14)]
    assert errors == ["TALKBACK 1: audio device 'No Such Interface 9000' not found",
                      "TALKBACK 2: audio device 'No Such Interface 9000' not found"]
    cm.configure([], False)
    assert cm.errors == []
    cm.shutdown()


def test_luna_device_uid_finds_the_device_by_name(monkeypatch):
    import sounddevice as sd
    from assistant_helper import coreaudio

    monkeypatch.setattr(coreaudio, "input_devices", lambda: [
        coreaudio.Device(uid="BuiltInMicrophoneDevice", name="MacBook Pro Microphone", inputs=1)])
    monkeypatch.setattr(sd, "query_devices", lambda *a: [
        {"name": "BlackHole 2ch", "max_input_channels": 2},
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    ])
    idx, info = CaptureManager._find_device("BuiltInMicrophoneDevice")
    assert idx == 1 and info["name"] == "MacBook Pro Microphone"


def test_channel_for_matches_driver_names():
    from assistant_helper.coreaudio import channel_for

    names = ["MIC/LINE 1", "MIC/LINE 2", "TALKBACK 1", "TALKBACK 2"]
    assert channel_for("TALKBACK 1-2", names) == 2
    assert channel_for("talkback  2", names) == 3
    assert channel_for("MIC/LINE 1/2", names) == 0
    assert channel_for("MIC/LINE 9", names) is None
    assert channel_for("Input 1", ["", ""]) is None


def test_silent_input_is_reported_until_sound_arrives(monkeypatch):
    import time as _time

    from assistant_helper import audio
    from assistant_helper.audio import ListenInput, _InputTap

    cm = make()
    tap = _InputTap(spec=ListenInput("MIC/LINE 1", "x", channel=0), resampler=None)

    class FakeStream:
        device_name = "Interface"
        opened_at = _time.monotonic() - audio.SILENT_AFTER_S - 1
        taps = [tap]
        healthy = True

        def close(self):
            pass

    with cm._lock:
        cm.enabled = True
        cm._wanted = [tap.spec]
        cm._streams = {"x": FakeStream()}
    statuses = []
    cm.on_status = lambda: statuses.append(list(cm.errors))
    cm._stop.wait(2.5)  # one watchdog pass
    assert any("no sound at all from 'Interface'" in e for e in cm.errors)
    tap.heard = True
    cm._stop.wait(2.5)
    assert cm.errors == []
    assert statuses and statuses[-1] == []
    with cm._lock:
        cm._streams = {}
    cm.shutdown()


def test_ua_driver_id_finds_the_ua_interface(monkeypatch):
    import sounddevice as sd

    devices = [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        {"name": "Universal Audio Thunderbolt", "max_input_channels": 16},
    ]
    monkeypatch.setattr(sd, "query_devices", lambda *a: devices)
    idx, info = CaptureManager._find_device("com_uaudio_driver_UAD2AudioEngine:0")
    assert idx == 1 and info["name"] == "Universal Audio Thunderbolt"
