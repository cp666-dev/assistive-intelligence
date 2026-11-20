// UA Assistive Intelligence (CP Assistant): engine wiring. Connects to LUNA and to the local helper,
// shows the helper's plans as numbered slots, runs one when the operator presses GO, keeps the
// helper's picture of the session current, and cuts the click at the positions the session stores.
//
// The DOM is built by ui/render.js (window.AIP) from view-models made by ui/viewmodel.js
// (window.AIPVM). This file only keeps engine state and calls the panel's setters.

const logic = AddOnLogic;
const helper = new HelperClient();
let luna;
let session;
let releases = [];
const keep = (u) => releases.push(u);
const release = () => {
  for (const u of releases) u();
  releases = [];
};

const panel = AIP.createPanel(document.getElementById('app'), {
  onGo: (id) => go(id),
  onDismiss: (id) => dismiss(id),
  onVariant: (id, i) => {
    const p = plans.get(id);
    if (p?.state === 'pending') {
      p.alt = i;
      renderSlots();
    }
  },
  onAddMic: () => togglePopover('mics'),
  // Inside LUNA the page can't navigate away; LUNA opens the user's browser. Outside it, a new tab.
  onOpenUrl: async (url) => {
    if (luna && (await luna.gui.available().catch(() => false))) await luna.gui.openUrl(url).catch(() => window.open(url, '_blank'));
    else window.open(url, '_blank');
  },
  onDisarm: (trackId) => setArmed(trackById(trackId), false),
  onPickMic: (id) => {
    closePopover();
    if (armedTracks.length >= AIPVM.MAX_MICS) return;
    if (id.startsWith('input:')) newMicOn(id.slice('input:'.length));
    else setArmed(trackById(id), true);
  },
  onRemoveCut: (cutId) => removeCut(cutId),
  onToggleSetups: () => togglePopover('setups'),
  onUseSetup: (name) => {
    closePopover();
    addPlan({ id: `recipe-${Date.now()}`, title: `Your “${name}” setup`, steps: [{ action: 'applyRecipe', args: { name } }] });
  },
  onDeleteSetup: (name) => {
    setupConfirm = name; // inline confirm, no confirm()
    renderPopover();
  },
  onConfirmDeleteSetup: async (name) => {
    setupConfirm = null;
    await helper.deleteRecipe(name);
    await loadRecipes();
    renderPopover();
  },
  onCancelDeleteSetup: () => {
    setupConfirm = null;
    renderPopover();
  },
  onClosePopover: () => closePopover(),
  onWellHover: (inside) => setWellHover(inside),
  onReleaseHeld: () => releaseHeld(),
  onPrompt: (text) => submitPrompt(text), // false keeps the text in the field
});

// ---------- status: the mark's animation, one word and one line of detail ----------

const status = { helper: false, stt: null, llm: null, listening: false, inputs: [], audioErrors: [], since: Date.now() };
let hearingUntil = 0;
let thinking = 0;
let lunaUp = false;
let statusNow = AIPVM.statusVM(status, { lunaUp, hasSession: false, thinking, hearingUntil, now: Date.now(), micCount: 0 });
let speakerInput = null; // input of the last non-final transcript
let idleKey = '';

const connecting = () => statusNow.word === 'CONNECTING';
const longOffline = () => statusNow.word === 'OFFLINE' && !status.helper;

function renderStatus() {
  statusNow = AIPVM.statusVM(status, {
    lunaUp,
    hasSession: !!session,
    thinking,
    hearingUntil,
    now: Date.now(),
    micCount: armedTracks.length,
    speaker: speakerInput ? speakerFor(speakerInput) : null,
  });
  panel.setStatus(statusNow);
  // The meters, the caption's resting label and the empty slot lines follow the state.
  const key = `${statusNow.state}|${statusNow.word}`;
  if (key !== idleKey) {
    idleKey = key;
    paintMics();
    renderSlots();
  }
  renderCaption();
}
setInterval(renderStatus, 1000); // so "hearing" settles back to "listening" and offline wording ages

// ---------- suggestions ----------

const plans = new Map(); // id → { plan, state, alt, stepStates, errors }

function addPlan(plan) {
  // An alternative identical to the main steps is the main option under another name.
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const dup = (plan.alternatives ?? []).find((a) => same(a.steps, plan.steps));
  if (dup) plan = { ...plan, choice: plan.choice ?? dup.title, alternatives: plan.alternatives.filter((a) => a !== dup) };
  if (!plan.alternatives?.length) delete plan.alternatives;
  const all = [plan.steps, ...(plan.alternatives ?? []).map((a) => a.steps)];
  const errors = plan.notice ? [] : all.flatMap((steps) => logic.validateSteps(steps));
  plans.set(plan.id, { plan, state: plan.notice ? 'notice' : errors.length ? 'invalid' : 'pending', alt: 0, stepStates: [], errors });
  if (wellInside) hold(plan.id);
  if (!plan.notice) panel.arrive();
  renderSlots();
  if (!heldIds.has(plan.id)) panel.root.querySelector('.aip-well').scrollTop = 0;
}

function addNotice(id, message) {
  addPlan({ id, notice: true, title: message, steps: [] });
}

function retractPlan(id) {
  const p = plans.get(id);
  if (p && p.state === 'pending') {
    plans.delete(id);
    heldIds.delete(id);
  }
  renderSlots();
}

function chosenSteps(p) {
  return p.alt === 0 ? p.plan.steps : p.plan.alternatives[p.alt - 1].steps;
}

async function go(id) {
  const p = plans.get(id);
  if (!p || p.state !== 'pending' || !session) return;
  p.state = 'running';
  const steps = chosenSteps(p);
  p.stepStates = steps.map(() => ({ state: 'todo' }));
  renderSlots();
  const before = new Map(logic.userTracks(session).map((t) => [t.id, t.name.value]));
  const results = await logic.execute({ luna, session, helper }, { steps }, (i, state, detail) => {
    p.stepStates[i] = { state, detail };
    renderSlots();
  });
  const failed = results.find((r) => !r.ok);
  p.state = failed ? 'failed' : 'done';
  renderSlots();
  helper.feedback({ planId: id, outcome: failed ? 'failed' : 'go', steps, error: failed?.error }).catch(() => {});
  if (!failed) watchEdits(id, before);
  pushListen(); // a plan may have armed or disarmed mics (armAssistant, createAssistantInputs)
  pushContext();
  renderMics();
  renderContext();
  if (!failed) {
    // A done slot is one line, then fades and goes.
    setTimeout(() => {
      if (plans.get(id)?.state !== 'done') return;
      p.fade = true;
      renderSlots();
      setTimeout(() => {
        if (plans.get(id)?.state === 'done') {
          plans.delete(id);
          renderSlots();
        }
      }, 1000);
    }, 6000);
  }
}

function dismiss(id) {
  const p = plans.get(id);
  if (!p) return;
  plans.delete(id);
  if (p.state === 'pending' || p.state === 'invalid') helper.feedback({ planId: id, outcome: 'dismissed', steps: p.plan.steps }).catch(() => {});
  renderSlots();
}

// Held arrivals: while the pointer is in the slot well, new plans wait behind "+N NEW" so nothing
// moves under the operator's hand. Released on leave (after 300 ms), on click, or after 10 s.
const heldIds = new Set();
let wellInside = false;
let leaveTimer;
let heldTimer;

function hold(id) {
  heldIds.add(id);
  if (heldIds.size === 1) heldTimer = setTimeout(releaseHeld, 10000);
}

function releaseHeld() {
  clearTimeout(leaveTimer);
  clearTimeout(heldTimer);
  if (!heldIds.size) return;
  heldIds.clear();
  renderSlots();
}

function setWellHover(inside) {
  wellInside = inside;
  clearTimeout(leaveTimer);
  if (!inside) leaveTimer = setTimeout(releaseHeld, 300);
}

// Pending slot numbers are the ⌥1..⌥9 keys; a key is ignored for 600 ms after they change.
let slotIdByNumber = new Map();
let numbersSig = '';
let numbersChangedAt = 0;

function renderSlots() {
  let list = AIPVM.slotsVM([...plans.entries()], logic, heldIds);
  if (!plans.size && longOffline()) list = [AIPVM.OFFLINE_NOTICE];
  panel.setSlots(list, { held: heldIds.size, empty: AIPVM.emptyLines(statusNow.state, connecting()) });
  const pending = list.filter((s) => s.kind === 'pending');
  const sig = pending.map((s) => s.id).join('\n');
  if (sig !== numbersSig) {
    numbersSig = sig;
    numbersChangedAt = Date.now();
    slotIdByNumber = new Map(pending.map((s) => [s.n, s.id]));
  }
}

document.addEventListener('keydown', (e) => {
  if (document.activeElement === panel.input) return;
  if (!(e.altKey && !e.metaKey && !e.ctrlKey && /^Digit[1-9]$/.test(e.code))) return;
  if (Date.now() - numbersChangedAt < 600) return;
  const id = slotIdByNumber.get(Number(e.code.slice(5)));
  if (!id) return;
  e.preventDefault();
  go(id);
});

/** Learning signal: names the operator changes on the tracks a plan touched, for 2 minutes. */
function watchEdits(planId, before) {
  const until = Date.now() + 120000;
  const seen = new Map(logic.userTracks(session).map((t) => [t.id, t.name.value]));
  const offs = logic.userTracks(session).map((t) =>
    t.name.onChange(() => {
      if (Date.now() > until) return;
      const from = seen.get(t.id);
      const to = t.name.value;
      if (from && to && from !== to && !before.has(t.id)) {
        helper.feedback({ planId, outcome: 'go', steps: [], edits: [{ kind: 'rename', from, to }] }).catch(() => {});
      }
      seen.set(t.id, to);
    }),
  );
  setTimeout(() => offs.forEach((o) => o()), 120000);
}

// ---------- live caption: the line being heard, resting a few seconds after ----------

let cap = null; // last transcript event + { request, expired }
let captionTimer;

function showCaption(e) {
  cap = { ...e, request: cap?.id === e.id ? cap.request : false };
  if (!e.final) speakerInput = e.input ?? null;
  hearingUntil = e.final ? 0 : Date.now() + 1500;
  clearTimeout(captionTimer);
  captionTimer = setTimeout(() => {
    if (cap) cap.expired = true;
    renderCaption();
  }, e.final ? 5000 : 8000);
  renderStatus();
}

let captionKey = '';
function renderCaption() {
  const vm = AIPVM.captionVM(cap, statusNow.state, speakerFor, connecting());
  const key = JSON.stringify(vm);
  if (key === captionKey) return;
  captionKey = key;
  panel.setCaption(vm);
}

helper.on((e) => {
  if (e.type === 'connection') {
    status.helper = e.connected;
    if (!e.connected) status.since = Date.now();
    renderStatus();
    paintContext();
    if (e.connected) onHelperUp();
  } else if (e.type === 'status') {
    Object.assign(status, { helper: true, stt: e.stt, llm: e.llm, listening: !!e.listening, inputs: e.inputs ?? [], audioErrors: e.audioErrors ?? [] });
    renderStatus();
  } else if (e.type === 'levels') onLevels(e.levels ?? {});
  else if (e.type === 'transcript') showCaption(e);
  else if (e.type === 'thinking') {
    thinking++;
    setTimeout(() => (thinking = Math.max(0, thinking - 1)), 30000); // never stuck "thinking"
    if (cap && e.utteranceId === cap.id) cap.request = true;
    renderStatus();
  } else if (e.type === 'plan') {
    thinking = Math.max(0, thinking - 1);
    addPlan(e.plan);
    renderStatus();
  } else if (e.type === 'retract') retractPlan(e.planId);
  else if (e.type === 'error') {
    console.warn('helper:', e.message);
    if (e.planId) {
      thinking = Math.max(0, thinking - 1);
      // A typed prompt the helper could not plan: say so in a slot instead of leaving silence.
      addNotice(e.planId, e.message);
    }
    renderStatus();
  }
});

async function onHelperUp() {
  try {
    await helper.catalog(logic.CATALOG.map(({ name, description, params }) => ({ name, description, params })));
    await pushContextNow();
    await pushListen();
    await loadRecipes();
  } catch (e) {
    console.warn('helper setup failed', e);
  }
}

// ---------- typed prompt ----------

function submitPrompt(text) {
  if (!status.helper) {
    addNotice(`local-${Date.now()}`, 'The helper app isn’t running, so nothing can be planned yet. If you haven’t installed it, see the panel’s GET THE HELPER link.');
    return false;
  }
  helper.prompt(text).catch((e) => addNotice(`local-${Date.now()}`, `Could not reach the helper app: ${e.message}`));
  return true;
}

// ---------- keeping the helper's picture of the session current ----------

let contextTimer;
function pushContext() {
  clearTimeout(contextTimer);
  contextTimer = setTimeout(pushContextNow, 500);
}
async function pushContextNow() {
  if (!session || !status.helper) return;
  try {
    await helper.context(await logic.snapshot(luna, session));
  } catch (e) {
    console.warn('context push failed', e);
  }
}
async function pushListen() {
  if (!session || !status.helper) return;
  const inputs = await logic.listenInputs(luna, session);
  try {
    const r = await helper.listen(inputs, inputs.length > 0);
    status.audioErrors = r?.errors ?? [];
    if (status.audioErrors.length) console.warn('listen errors', status.audioErrors);
    renderStatus();
  } catch (e) {
    console.warn('listen failed', e);
  }
}

// ---------- meter bridge: armed mics and their levels ----------

let armedIds = new Set();
let armedTracks = []; // user tracks armed for the assistant, in session order
let inputNames = new Map(); // track id → LUNA input name, or null
let deviceInputs = []; // luna.audioInputs(), numbered, for "Listen to an input"
let levelsByTrack = {}; // last helper levels, by track id

const trackById = (id) => logic.userTracks(session).find((t) => t.id === id);
const inputFor = (t) => inputNames.get(t.id) ?? null;
const speakerFor = (input) => armedTracks.find((t) => inputFor(t) === input)?.name.value ?? null;

async function setArmed(track, on) {
  if (!track) return;
  await logic.setAssistantArmed(session, track, on);
  await renderMics();
  await pushListen();
  pushContext();
}

/** "+ MIC" > an input: an input-only Assistant track on it, armed, as createAssistantInputs makes. */
async function newMicOn(inputUid) {
  const inp = deviceInputs.find((i) => i.uid === inputUid);
  if (!inp || !session) return;
  try {
    const [r] = await logic.execute({ luna, session, helper }, { steps: [{ action: 'createAssistantInputs', args: { inputs: [inp.name] } }] });
    if (r && !r.ok) console.warn('new mic', r.error ?? r);
  } catch (e) {
    console.warn('new mic', e);
  }
  await renderMics();
  await pushListen();
  pushContext();
}

async function renderMics() {
  if (session) {
    armedIds = await logic.assistantArmedIds(session);
    const inputs = logic.numberInputs(await luna.audioInputs());
    deviceInputs = inputs;
    const tracks = logic.userTracks(session);
    inputNames = new Map(tracks.map((t) => [t.id, inputs.find((i) => i.uid === t.inputNodeUid.value)?.name ?? null]));
    armedTracks = tracks.filter((t) => armedIds.has(t.id));
  } else {
    armedIds = new Set();
    armedTracks = [];
    inputNames = new Map();
  }
  paintMics();
  renderStatus();
  if (popover === 'mics') renderPopover();
}

function paintMics() {
  const idle = AIPVM.micsIdle(statusNow.state);
  panel.setMics(AIPVM.micsVM(armedTracks, inputFor), { idle });
  panel.setLevels(idle ? {} : levelsByTrack);
}

/** Helper `levels` event: real input peaks only. Without it (older helper) the meters stay unlit. */
function onLevels(levels) {
  levelsByTrack = AIPVM.levelsVM(levels, armedTracks, inputFor);
  if (AIPVM.micsIdle(statusNow.state)) {
    panel.setLevels({});
    return;
  }
  panel.setLevels(levelsByTrack);
  if (statusNow.state === 'hearing') panel.setMarkLevel(AIPVM.markLevel(levelsByTrack));
}

// ---------- context cells: click cuts and saved setups ----------

let cuts = [];
let cutCells = [];
let recipes = [];

async function loadRecipes() {
  const profile = status.helper ? await helper.profile().catch(() => null) : null;
  // The profile keeps recipes as a list of { name, steps, ... }; the panel wants [name, recipe].
  const list = profile?.recipes ?? [];
  recipes = Array.isArray(list) ? list.map((r) => [r.name, r]) : Object.entries(list);
  paintContext();
}

async function renderContext() {
  cuts = session ? await logic.clickCuts(session) : [];
  const tl = session?.musical.timeline;
  cutCells = [...cuts].sort((a, b) => a.tick - b.tick).map((c, i) => AIPVM.cutVM(c, tl.barBeat(c.tick).bar, i));
  paintContext();
}

function paintContext() {
  panel.setContext({ cuts: cutCells, setups: recipes.length, setupsOpen: popover === 'setups' });
  // The setups popover is anchored to the cell setContext just rebuilt.
  if (popover === 'setups') renderPopover();
}

async function removeCut(cutId) {
  const vm = cutCells.find((c) => c.id === cutId);
  if (!vm || !session) return;
  await logic.setClickCuts(session, cuts.filter((k) => k !== vm.cut));
  renderContext();
}

// ---------- popovers: one at a time ----------

let popover = null; // null | 'mics' | 'setups'
let setupConfirm = null;

async function togglePopover(kind) {
  if (kind === 'mics' && !session) return;
  popover = popover === kind ? null : kind;
  setupConfirm = null;
  if (popover === 'mics') await renderMics();
  paintContext();
  if (popover !== 'setups') renderPopover();
}

function closePopover() {
  if (!popover) return;
  popover = null;
  setupConfirm = null;
  panel.setPopover(null);
  paintContext();
}

function renderPopover() {
  if (popover === 'mics' && session) panel.setPopover(AIPVM.micListVM(logic.userTracks(session), armedIds, inputFor, deviceInputs));
  else if (popover === 'setups') panel.setPopover(AIPVM.setupsVM(recipes, setupConfirm));
  else panel.setPopover(null);
}

// ---------- click cuts: muting the click at the stored positions ----------

let clickMode = 'none';
let mutedByUs = []; // click outputs this page muted, to restore
let cutTimer;

async function muteOn(output) {
  const r = await luna.raw.get(`${output.path}/controls/mute?flatvalue=1&children=0&commands=0`);
  return r?.properties?.real_value === true;
}

async function applyClickMode(mode) {
  if (mode === clickMode) return;
  clickMode = mode;
  const click = logic.clickTrack(session);
  if (!click) return;
  await click.outputs.load?.();
  const want = logic.clickOutputsFor(click, mode);
  const wantIds = new Set(want.map((o) => o.id));
  for (const o of mutedByUs.filter((o) => !wantIds.has(o.id))) if (await muteOn(o)) await o.mute.toggle();
  mutedByUs = mutedByUs.filter((o) => wantIds.has(o.id));
  for (const o of want) {
    if (mutedByUs.some((m) => m.id === o.id)) continue;
    if (!(await muteOn(o))) {
      await o.mute.toggle();
      mutedByUs.push(o);
    }
  }
}

/** Decide the click state for the playhead, and time the next cut to the millisecond. */
function evaluateClick(tickOverride) {
  if (!session) return;
  const t = session.transport;
  const rolling = t.playing.value === true;
  const tick = tickOverride ?? t.location.value?.tick ?? 0;
  applyClickMode(logic.clickCutAt(cuts, tick, rolling)).catch((e) => console.warn('click cut', e));
  clearTimeout(cutTimer);
  const next = rolling && logic.nextClickCut(cuts, tick);
  if (next) {
    const tl = session.musical.timeline;
    // Throws until LUNA has sent the tempo map (SDK 3.1.1); the next tick event retries.
    let ms = Infinity;
    try { ms = (tl.seconds(next.tick) - tl.seconds(tick)) * 1000; } catch { /* tempo not loaded */ }
    // Muting takes a few ms; aim just before the downbeat so its click is the first one cut.
    if (ms < 4000) cutTimer = setTimeout(() => evaluateClick(next.tick), Math.max(0, ms - 40));
  }
}

// ---------- session binding ----------

async function bindSession() {
  release();
  session = luna.sessions.current();
  plans.clear();
  heldIds.clear();
  closePopover();
  renderSlots();
  if (!session) {
    await renderMics();
    await renderContext();
    return;
  }
  await session.ready();
  await Promise.all(session.tracks.items.map((t) => t.loadStrip?.()));
  await renderMics();
  await renderContext();

  const onStructure = () => {
    pushContext();
    renderMics();
    rebindTracks();
  };
  keep(session.tracks.onChange(onStructure));
  keep(session.buses.onChange(pushContext));
  keep(session.markers.onChange(pushContext));
  const t = session.transport;
  keep(t.playing.onChange(() => {
    evaluateClick();
    pushContext();
  }));
  keep(t.location.onChange(() => evaluateClick()));
  const clickPrefs = logic.sessionStore(session).context(logic.CLICK_CONTEXT);
  if (clickPrefs.onChange) keep(clickPrefs.onChange(renderContext));

  let trackOffs = [];
  function rebindTracks() {
    trackOffs.forEach((o) => o());
    trackOffs = logic.userTracks(session).flatMap((tr) => [
      tr.name.onChange(() => {
        pushContext();
        renderMics();
      }),
      tr.recordEnabled.onChange(pushContext),
      tr.inputNodeUid.onChange(() => {
        renderMics();
        pushListen();
        pushContext();
      }),
    ]);
  }
  rebindTracks();
  keep(() => trackOffs.forEach((o) => o()));
  keep(() => applyClickMode('none'));

  await pushContextNow();
  await pushListen();
}

async function connect() {
  renderStatus();
  paintContext();
  helper.start();
  for (;;) {
    try {
      // Inside LUNA the page URL carries ?port=; in a browser this falls back to LUNA on localhost.
      luna = await Luna.connect();
      break;
    } catch (e) {
      renderStatus();
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
  lunaUp = true;
  renderStatus();
  Luna.bindShortcuts(luna);
  await luna.sessions.ready();
  luna.sessions.focused.onChange(bindSession);
  luna.sessions.items.onChange(bindSession);
  await bindSession();
}

connect();
