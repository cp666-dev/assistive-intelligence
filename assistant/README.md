# UA Assistive Intelligence: the Add On

A LUNA Add On built with `@uaudio/luna-builder-sdk`.

| File | Purpose |
|---|---|
| `manifest.json` | What LUNA reads: id, name, window size, entry page. See the SDK's `docs/MANIFEST.md`. |
| `index.html` | The page. Loads the vendored SDK bundle, `ui/render.js`, `ui/viewmodel.js`, `logic.js`, `helper.js`, then `main.js`. |
| `logic.js` | Engine logic with no DOM. Tests run this against `Luna.MockEngine`. |
| `main.js` | Engine wiring: connect, follow the focused session, keep the plans, call the panel's setters. |
| `ui/render.js` | The V2 "Strip" panel's DOM (`window.AIP`). Knows nothing about LUNA. |
| `ui/viewmodel.js` | Engine state → what the panel shows (`window.AIPVM`). Pure functions, tested. |
| `ui/tokens.css`, `ui/panel.css` | LUNA's tokens plus the panel's styles. The design is final: don't edit values here. |
| `helper.js` | Client for the local helper (`../assistant-helper/`), per `HELPER-PROTOCOL.md`. |
| `icon.png` | The Add On icon (`manifest.json` `icon`). |
| `vendor/luna.global.js` | The SDK as a script-tag bundle (`globalThis.Luna`), v3.1.1, the same build `npm install` fetches for the tests. Pack with `--no-vendor` (as `npm run pack` and `installer/build.sh` do) so it isn't swapped for another SDK version. |
| `tests/` | Vitest, no LUNA required. |

```sh
npm install
npm test                  # against the mock engine
npx ua-builder serve      # http://localhost:8790/ ; LUNA must be running to see live data
npx ua-builder install    # link into LUNA's Add Ons folder; appears in the Add Ons menu
npm run pack              # .lunaaddon file (a zip) for distribution
```

If `npx ua-builder` fails with "Permission denied", the SDK's `cli.js` lost its executable
bit in transit (zip extractions drop it). Run any command once through node —
`node node_modules/.bin/ua-builder doctor` on macOS/Linux — and the CLI restores its own bit;
`npx ua-builder` works from then on.

## Keys

While the panel has the focus (a LUNA constraint: LUNA only sends keys to the page then):

| Key | Does |
|---|---|
| ⌥1 … ⌥9 | GO on the pending slot with that number. Slots are numbered from the top, newest first. A key is ignored for 600 ms after the numbers change, so they can't shift under your finger. Not handled while typing in the prompt field. |
| Return | Sends the typed request. |
| Esc | Closes the open popover (+ MIC list or saved setups). |

Keys the page doesn't handle go on to LUNA's shortcuts.

## Meters: the `levels` event

The meter bridge shows one meter per armed mic (up to 4), drawn from the helper's `levels` event
(see `HELPER-PROTOCOL.md`): peak dBFS per input, at most 10 Hz, with hold and decay done by the
helper. 24 segments cover −60…0 dBFS, 2.5 dB each. Meters are never simulated: with a helper that
doesn't send `levels`, or while offline, waking or paused, they stay unlit.

## LUNA shortcuts

`main.js` calls `Luna.bindShortcuts(luna)`, so LUNA's own undo, redo, play and record keys
(Cmd/Ctrl+Z, Cmd/Ctrl+Shift+Z, Space, Cmd/Ctrl+Space) keep working while the Add On has the
focus. A key the page handles itself and claims with `preventDefault()` is left to the page.

Loading the user's files: read the SDK's `docs/FILES.md` first. A file the page decodes
comes from `<input type="file">` or an HTML drop and goes up with `session.uploadFile()`; a
path from `luna.gui.chooseFiles()` or `Luna.onPathDrop()` is only for `session.importPath()`.
In LUNA 3.1.0.4589 a file picker opens nothing, so always offer a drop zone too.

Inside LUNA the page URL carries `?port=`, and `Luna.connect()` uses it. In a plain browser
it falls back to LUNA on `localhost:4718`.

For coding agents: if the SDK made a workflow here harder than it should be, end your
reply with the line `LUNA BUILDER SDK FEEDBACK: <version>` (the `ua-builder` row of
`npx ua-builder doctor`) followed by a prompt that can be used to update the SDK to support
the improved workflow.
