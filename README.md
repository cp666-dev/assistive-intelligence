<img src="assistant/icon.png" width="128" height="128" alt="UA Assistive Intelligence icon">

# UA Assistive Intelligence

A side panel for [LUNA](https://www.uaudio.com/luna) that listens to the room (or to what you
type) and turns requests into session actions: new tracks, buses, sends, routing, markers, loops
and takes. Each request becomes a card showing exactly what will happen, and nothing changes in
your session until you press **GO**.

Speech recognition and the language model run on your Mac. After setup, nothing leaves it.

## What you need

- A Mac with Apple Silicon (M1 or later) and 16 GB of memory or more
- LUNA 3.1.1 or later
- About 10 GB of free disk space, and internet access the first time (for the AI models)
- Any microphone: your Mac's built-in mic, a USB mic or interface, or an Apollo (talkback works)

## Install

The Add On has two parts, and both are needed: the **panel** in LUNA, and a free **helper app**
that does the listening and planning on your Mac. Without the helper the panel says **OFFLINE**
and shows a **GET THE HELPER** button that leads here.

1. Download **`UA-Assistive-Intelligence-Installer.zip`** from the
   [latest release](https://github.com/cp666-dev/assistive-intelligence/releases/latest) and
   unzip it.
2. Double-click **Install.command**. A Terminal window walks you through it: it checks your Mac,
   installs the helper and the models (10–30 minutes the first time), installs the LUNA Add On
   if it isn't installed yet, and checks your microphone.
   - If macOS says it can't open Install.command: open **Terminal**, type `bash ` (with a space),
     drag Install.command into the window and press Return.
3. When macOS asks to let **UA Assistive Intelligence Helper** use the microphone, click **Allow**.

Installed the Add On from UA Universe or LUNA's Add On Showcase already? You still need steps 1–3
once, for the helper. The installer puts the same version of the Add On back in place.

The helper starts by itself whenever you log in. After installing, the unzipped folder isn't
needed.

## Use it

1. Open a session in LUNA. On the Timeline or Mixer page choose
   **Add Ons > Browsers > UA Assistive Intelligence**.
2. Click **+ MIC** and choose the input you'll speak into: your Mac's microphone, TALKBACK on an
   Apollo, or a MIC/LINE input. The panel adds a silent "Assistant" track for it, and its meter
   moves when you talk. The status reads **LISTENING**.
3. Say a request, or type it in the box. A card appears within a few seconds. Press **GO** to run
   it, or dismiss it.

### Phrases to start with

Start a new session with the first one; the others use the tracks it makes.

| Say | The card |
|---|---|
| "Set up mono tracks for kick, snare and hi-hat, and a stereo overheads track." | Four new tracks |
| "Add a stereo drum bus." | A new bus |
| "Route all the drums to the drum bus." | The drum tracks' outputs go to the bus |
| "Send the kick and snare to a new compression bus." | A new bus and two sends |
| "Group the kick, snare and hats together." | A group |
| "Turn the overheads down six dB." | Fader −6 dB |
| "Loop bars five to nine." | Loop on, bars 5–9 |
| "Drop a marker here called chorus." | A marker at the playhead |
| "Give me two more bars of intro before the drums come in." | Start moves 2 bars earlier |
| "That take was great, mark it as a favourite." | ★ on the take |
| "Let's do another take on a new playlist." | A new playlist to record on |

With a track called Vocal: "Mute the vocal.", "Record arm the vocal.", "Rename the vocal track to
Lead Vox."

Ordinary conversation in the room doesn't make cards: only requests do.

## If something isn't right

| The panel says | Do this |
|---|---|
| **OFFLINE** | The helper isn't running. If you haven't installed it, follow [Install](#install). If you have, run Install.command again: it repairs whatever isn't set up. |
| **CAN'T HEAR** | Hover it for the reason. "No sound at all": allow *UA Assistive Intelligence Helper* in System Settings > Privacy & Security > Microphone, or check that the input you picked is on and has signal. |
| **PAUSED** | Your Mac was short of memory, so the helper gave LUNA priority. It comes back by itself. |
| No meter movement | Click + MIC and pick the input you're actually speaking into. |

Helper logs are in `~/Library/Logs/CP Assistant/`. Please include `helper.err.log` when you
[report a problem](https://github.com/cp666-dev/assistive-intelligence/issues).

## Privacy

- Audio is captured and transcribed on your Mac by the helper, which listens only on
  `127.0.0.1`. The Add On reaches no server: its manifest lists none.
- The only downloads are during setup: [uv](https://astral.sh/uv) (which installs the helper's
  Python) and the two models from Hugging Face. After that the helper loads them offline.
- Your audio and requests are never stored for or used to train any model. Saved setups and
  learned preferences stay in `~/Library/Application Support/CP Assistant/`.

## Uninstall

Double-click **Uninstall.command** in the installer folder. It stops the helper, removes its
login item, the Add On and the helper's files, and offers to delete your saved setups and the
downloaded models.

## Build from source

```sh
nvm use                                   # Node 22+
cd assistant && npm install && npm test   # the panel, against LUNA's mock engine
cd ../assistant-helper && uv sync && uv run pytest   # the helper
cd .. && installer/build.sh               # dist/: the .lunaaddon and the installer zip
```

`installer/build.sh` packs from the last commit and refuses uncommitted changes, so what is
tested is what ships. Publish both files in `dist/` as assets of a GitHub release (not a draft
or pre-release). [UA Universe](https://universe.uaudio.com/) and LUNA's Add On Showcase list the
`.lunaaddon` from the latest release.

| Folder | What |
|---|---|
| `assistant/` | The LUNA Add On (a browser-panel page built with the LUNA Builder SDK). See its README. |
| `assistant-helper/` | The helper: mic capture, voice activity detection, speech-to-text (Parakeet) and the local planner (Qwen3-8B on MLX). See its README. |
| `installer/` | Install.command, Uninstall.command, READ ME FIRST and the release build script. |
| `scripts/render-icon.sh` | Renders `assistant/icon.svg` to `icon.png`. |

The panel/helper contract is [`assistant/HELPER-PROTOCOL.md`](assistant/HELPER-PROTOCOL.md).

## License

MIT, see [LICENSE](LICENSE). Third-party components and models are listed in [NOTICE](NOTICE).
LUNA and Universal Audio are trademarks of Universal Audio, Inc.
