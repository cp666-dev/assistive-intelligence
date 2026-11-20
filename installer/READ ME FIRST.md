# UA Assistive Intelligence

A side panel for LUNA that listens to the room (or what you type) and turns requests into session
actions — new tracks, buses, sends, routing, markers, takes — that you run with one click.
Speech recognition and the language model run on your Mac; after setup, nothing leaves it.

## What you need

- A Mac with Apple Silicon (M1 or later) and 16 GB of memory or more
- LUNA 3.1.1 or later
- About 10 GB of free disk space and internet access the first time (for the AI models)
- Any microphone: your Mac's built-in mic, a USB mic or interface, or an Apollo (talkback works)

## Install (one double-click)

1. Unzip this folder anywhere, for example your Desktop.
2. Double-click **Install.command**. A Terminal window walks you through it:
   it checks your Mac, installs the helper and the models (10–30 minutes the first time),
   installs the LUNA Add On and checks your microphone.
   - If macOS says it can't open Install.command: open **Terminal**, type `bash ` (with a
     space), drag Install.command into the window and press Return.
3. When macOS asks to let **UA Assistive Intelligence Helper** use the microphone, click **Allow**.

After that the folder isn't needed: the installer copies what it needs. The helper starts by
itself whenever you log in.

## Use it

1. Open a session in LUNA. On the Timeline or Mixer page choose
   **Add Ons > Browsers > UA Assistive Intelligence**.
2. Click **+ MIC** and choose the input you'll speak into, under *Listen to an input* (your Mac's
   microphone, TALKBACK on an Apollo, or a MIC/LINE input). The panel adds a silent "Assistant"
   track for it and its meter moves when you talk. Status reads **LISTENING**.
3. Say a request — or type it in the box. A card appears within a few seconds showing exactly
   what will happen. Press **GO** to run it, or dismiss it.

## Phrases to start with

Each of these was tested spoken aloud in three different voices, twice each, through the same
speech recognition and planner you're running: every time, the right card appeared, 2–5 seconds
after the speaker stopped. Speak normally, then pause for a moment.

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

The assistant only makes cards for requests, and nothing changes in your session until you press GO.

## If something isn't right

| The panel says | Do this |
|---|---|
| **OFFLINE** | The helper isn't running. Run Install.command again. |
| **CAN'T HEAR** | Hover it for the reason. "No sound at all": allow *UA Assistive Intelligence Helper* in System Settings > Privacy & Security > Microphone, or check that the input you picked is on and has signal. |
| **PAUSED** | Your Mac was short of memory, so the helper gave LUNA priority. It comes back by itself. |
| No meter movement | Click + MIC and pick the input you're actually speaking into. |

Logs: `~/Library/Logs/CP Assistant/`. To remove everything, double-click **Uninstall.command**.

---
Built by Connor Panayi · https://panayi.me
