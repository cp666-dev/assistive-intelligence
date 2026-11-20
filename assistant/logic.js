// CP Assistant: logic that does not touch the DOM, so tests can exercise it against Luna.MockEngine.
//
// The helper (assistant-helper/) turns speech and typed prompts into plans: lists of steps from
// the CATALOG below. This file validates those plans, describes them for the suggestion cards
// and executes them against LUNA. Tracks, buses and plugins are named as people say them and
// resolved here against the live session. See HELPER-PROTOCOL.md for the wire format.

const ARM_CONTEXT = 'com.cp.assistant.arm';
const CLICK_CONTEXT = 'com.cp.assistant.click';
const NAMED_COLORS = {
  red: 0xffd94b4b, orange: 0xffe8873a, yellow: 0xffe8c93a, green: 0xff4bb543, teal: 0xff2fa39b,
  blue: 0xff3a7fe8, purple: 0xff8a5ad9, pink: 0xffe05aa8, brown: 0xff8d6a4a, grey: 0xff8c8c8c, gray: 0xff8c8c8c,
};
// Inputs that are not numbered like a front panel ("input 3" never means MON R).
const UNNUMBERED_INPUT = /^(MON|VIRTUAL|S\/PDIF|SPDIF|ADAT|AUX|TALKBACK|CUE|HP)/i;
// Spoken shorthand for common track names.
const ALIASES = { oh: 'overheads', ohs: 'overheads', hh: 'hats', hihat: 'hats', 'hi-hat': 'hats', bd: 'kick', kik: 'kick', sn: 'snare', vox: 'vocal', bv: 'backing vocal', bvs: 'backing vocals', gtr: 'guitar', keys: 'keyboard' };

const S = (props, required = []) => ({ type: 'object', properties: props, required, additionalProperties: false });
const NAMES = { type: 'array', items: { type: 'string' }, description: 'Track or bus names as the user says them' };
const TRACK_SET = { description: 'Track names, or "armed" (record-armed tracks), "all", "selected"', anyOf: [NAMES, { type: 'string', enum: ['armed', 'all', 'selected'] }] };
const POSITION = { description: 'A bar number (1-based), a marker name, or "playhead"', anyOf: [{ type: 'integer', minimum: 1 }, { type: 'string' }] };

const CATALOG = [
  { name: 'createTracks', description: 'Create audio or instrument tracks, optionally on hardware inputs ("1", "5-6" for a stereo pair, or an input name). Tracks are created in the order given.',
    params: S({ tracks: { type: 'array', items: S({ name: { type: 'string' }, kind: { type: 'string', enum: ['audio', 'instrument'] }, format: { type: 'string', enum: ['mono', 'stereo'] }, input: { type: 'string' }, color: { type: 'string' } }, ['name']) }, recordArm: { type: 'boolean' } }, ['tracks']) },
  { name: 'createBus', description: 'Create an aux bus.', params: S({ name: { type: 'string' }, format: { type: 'string', enum: ['mono', 'stereo'] } }, ['name']) },
  { name: 'setOutput', description: 'Route the main output of tracks/buses to a bus (created if missing) or "main" (the main output).', params: S({ channels: NAMES, to: { type: 'string' } }, ['channels', 'to']) },
  { name: 'createGroup', description: 'Create a group linking tracks/buses so one fader/edit controls all of them.', params: S({ name: { type: 'string' }, members: NAMES, links: { type: 'array', items: { type: 'string', enum: ['mix', 'edit', 'plugin'] } } }, ['name', 'members']) },
  { name: 'addSend', description: 'Add a send from tracks to a bus (created stereo if missing).', params: S({ from: NAMES, to: { type: 'string' }, levelDb: { type: 'number' }, preFader: { type: 'boolean' } }, ['from', 'to']) },
  { name: 'insertPlugin', description: 'Load a plugin (search text such as "dbx 160", "1176", "LA-2A") on a track or bus.', params: S({ on: { type: 'string' }, plugin: { type: 'string' } }, ['on', 'plugin']) },
  { name: 'setRecordArm', description: 'Record-arm or disarm tracks.', params: S({ tracks: TRACK_SET, on: { type: 'boolean' } }, ['tracks', 'on']) },
  { name: 'setInputMonitor', description: 'Turn input monitoring on or off for tracks.', params: S({ tracks: TRACK_SET, on: { type: 'boolean' } }, ['tracks', 'on']) },
  { name: 'setMute', description: 'Mute or unmute tracks/buses.', params: S({ tracks: TRACK_SET, on: { type: 'boolean' } }, ['tracks', 'on']) },
  { name: 'setSolo', description: 'Solo or unsolo tracks.', params: S({ tracks: TRACK_SET, on: { type: 'boolean' } }, ['tracks', 'on']) },
  { name: 'setGain', description: 'Set the fader of tracks/buses in dB.', params: S({ tracks: TRACK_SET, db: { type: 'number' } }, ['tracks', 'db']) },
  { name: 'renameTrack', description: 'Rename a track or bus.', params: S({ from: { type: 'string' }, to: { type: 'string' } }, ['from', 'to']) },
  { name: 'setTrackColor', description: 'Colour tracks: red, orange, yellow, green, teal, blue, purple, pink, brown, grey.', params: S({ tracks: TRACK_SET, color: { type: 'string' } }, ['tracks', 'color']) },
  { name: 'deleteTracks', description: 'Delete tracks or buses. Destructive: only when explicitly asked.', params: S({ tracks: NAMES }, ['tracks']) },
  { name: 'newPlaylist', description: 'Start a new empty playlist (take lane) on tracks, ready to record a new take.', params: S({ tracks: TRACK_SET, duplicate: { type: 'boolean' } }, ['tracks']) },
  { name: 'favouriteTake', description: 'Mark the current take on tracks as a favourite (★ on the playlist name and a marker).', params: S({ tracks: TRACK_SET, label: { type: 'string' } }, ['tracks']) },
  { name: 'setStartEarlier', description: 'Move the play/record start N bars earlier, giving more intro before the performers come in.', params: S({ bars: { type: 'integer', minimum: 1 } }, ['bars']) },
  { name: 'setPlayStart', description: 'Set the play/record start position.', params: S({ at: POSITION }, ['at']) },
  { name: 'addMarker', description: 'Add a named marker.', params: S({ name: { type: 'string' }, at: POSITION }, ['name', 'at']) },
  { name: 'cutClick', description: 'Silence the click from a position onwards during playback/recording ("cues": headphones only; "everywhere": cues and control room).', params: S({ at: POSITION, where: { type: 'string', enum: ['cues', 'everywhere'] } }, ['at', 'where']) },
  { name: 'clearClickCuts', description: 'Remove all click cuts.', params: S({}) },
  { name: 'clickSettings', description: 'Turn the click on/off during play, record or count-in, and count-in itself.', params: S({ duringPlay: { type: 'boolean' }, duringRecord: { type: 'boolean' }, duringCountIn: { type: 'boolean' }, countIn: { type: 'boolean' } }) },
  { name: 'setTempo', description: 'Set the session tempo (BPM).', params: S({ bpm: { type: 'number', minimum: 20, maximum: 400 } }, ['bpm']) },
  { name: 'setMeter', description: 'Set the time signature.', params: S({ numerator: { type: 'integer' }, denominator: { type: 'integer' } }, ['numerator', 'denominator']) },
  { name: 'setLoop', description: 'Set the loop range in bars (stop bar exclusive) and turn looping on.', params: S({ fromBar: POSITION, toBar: POSITION }, ['fromBar', 'toBar']) },
  { name: 'transport', description: 'Play, stop, record or return to zero.', params: S({ command: { type: 'string', enum: ['play', 'stop', 'record', 'returnToZero'] } }, ['command']) },
  { name: 'armAssistant', description: 'Arm or disarm tracks for the assistant: it listens to their inputs.', params: S({ tracks: TRACK_SET, on: { type: 'boolean' } }, ['tracks', 'on']) },
  { name: 'createAssistantInputs', description: 'Create input-only "Assistant" tracks (no output) on the given inputs and arm them for the assistant, e.g. a control-room and a live-room mic.', params: S({ inputs: { type: 'array', items: { type: 'string' } } }, ['inputs']) },
  { name: 'applyRecipe', description: 'Apply a saved setup from the user\'s profile ("my usual string quartet setup").', params: S({ name: { type: 'string' } }, ['name']) },
  { name: 'saveRecipe', description: 'Save the current session setup (tracks, inputs, routing, sends, plugins, groups) as a named setup.', params: S({ name: { type: 'string' }, aliases: { type: 'array', items: { type: 'string' } }, tracks: NAMES }, ['name']) },
];
const CATALOG_BY_NAME = Object.fromEntries(CATALOG.map((a) => [a.name, a]));

// ---------- small utilities ----------

const norm = (s) => String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '');
const words = (s) => String(s ?? '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
const singular = (s) => s.replace(/(es|s)$/, '');

function editDistance(a, b) {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return d[a.length][b.length];
}

/** Best match for a spoken name among items with names, or undefined. */
function matchName(items, spoken, nameOf = (x) => x.name) {
  const raw = String(spoken ?? '').trim();
  const target = norm(ALIASES[raw.toLowerCase()] ?? raw);
  if (!target) return undefined;
  const named = items.map((x) => ({ x, n: norm(nameOf(x)) })).filter((e) => e.n);
  const pick = (pred) => named.find(pred)?.x;
  return (
    pick((e) => e.n === target) ??
    pick((e) => singular(e.n) === singular(target)) ??
    pick((e) => e.n.startsWith(target)) ??
    pick((e) => e.n.includes(target)) ??
    pick((e) => target.includes(e.n) && e.n.length >= 3) ??
    named
      .map((e) => ({ ...e, d: editDistance(e.n, target) }))
      .filter((e) => e.d <= Math.max(1, Math.floor(target.length / 4)))
      .sort((a, b) => a.d - b.d)[0]?.x
  );
}

const uidOf = (node) => node?.id;
const nameOf = (node) => (typeof node?.name === 'string' ? node.name : node?.name?.value ?? '');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Wait until `fn()` is truthy (LUNA answers before its pushes arrive). */
async function until(fn, what, ms = 4000) {
  const t0 = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`);
    await sleep(25);
  }
}

// ---------- inputs ----------

/**
 * LUNA's device inputs with the number people use for them: "3" for MIC/LINE 3, "1-2" for a
 * stereo pair. `channel` is the 0-based hardware channel the helper captures (front-panel
 * inputs come first on UA interfaces); null when unknown.
 */
function numberInputs(inputs) {
  return inputs.map((i) => {
    let number = null;
    if (!UNNUMBERED_INPUT.test(i.name)) {
      const pair = /(\d+)\s*[\/-]\s*(\d+)\s*$/.exec(i.name);
      const one = /(\d+)\s*$/.exec(i.name);
      if (i.stemFormat === 'stereo' && pair) number = `${pair[1]}-${pair[2]}`;
      else if (i.stemFormat !== 'stereo' && one) number = one[1];
    }
    const channel = number && !number.includes('-') ? Number(number) - 1 : number ? Number(number.split('-')[0]) - 1 : null;
    return { uid: i.uid, name: i.name, format: i.stemFormat, device: i.deviceId, number, channel };
  });
}

/** An input for "3", "5-6", "5/6", "input 3", "virtual 1" or an exact name. */
function resolveInput(numbered, ref, format) {
  // An exact name first, before "input 3" style prefixes are stripped ("Internal Microphone").
  const named = numbered.find((i) => i.name?.toLowerCase() === String(ref ?? '').trim().toLowerCase());
  if (named && (!format || named.format === format)) return named;
  const r = String(ref ?? '').trim().replace(/^(input|in|channel|ch)\b\s*/i, '');
  const pair = /^(\d+)\s*(?:[\/-]|and|&)\s*(\d+)$/i.exec(r);
  const want = pair ? `${pair[1]}-${pair[2]}` : r;
  const wantStereo = pair || format === 'stereo';
  if (/^\d+(-\d+)?$/.test(want)) {
    const exact = numbered.find((i) => i.number === want && (wantStereo ? i.format === 'stereo' : i.format !== 'stereo'));
    if (exact) return exact;
    if (wantStereo && !pair) {
      // "stereo track on input 7" → the pair starting at 7.
      return numbered.find((i) => i.format === 'stereo' && i.number?.startsWith(`${want}-`));
    }
    return numbered.find((i) => i.number === want);
  }
  const byFormat = numbered.filter((i) => !format || i.format === format);
  return matchName(byFormat, r) ?? matchName(numbered, r);
}

// ---------- session model ----------

function userTracks(session) {
  return session.visibleTracks;
}
function auxBuses(session) {
  return session.buses.items.filter((b) => !b.isMaster && !/^CUE \d/i.test(nameOf(b)));
}
function channels(session) {
  return [...userTracks(session), ...auxBuses(session)];
}

function findChannel(session, spoken) {
  return matchName(channels(session), spoken, nameOf);
}
function needChannel(session, spoken) {
  const c = findChannel(session, spoken);
  if (!c) throw new Error(`no track or bus called "${spoken}"`);
  return c;
}

/** Tracks for a TRACK_SET argument. */
function trackSet(session, set) {
  const tracks = userTracks(session);
  if (set === 'all') return tracks;
  if (set === 'armed') return tracks.filter((t) => t.recordEnabled.value === true);
  if (set === 'selected') {
    const sel = new Set(session.context?.selectionTracks?.value ?? []);
    return tracks.filter((t) => sel.has(uidOf(t)));
  }
  const names = Array.isArray(set) ? set : [set];
  return names.flatMap((n) => {
    // "the drums" can mean several tracks: the group-ish word matches everything containing it.
    const exact = findChannel(session, n);
    if (exact) return [exact];
    throw new Error(`no track or bus called "${n}"`);
  });
}

/** Tick for a POSITION argument. */
function positionTick(session, at) {
  const tl = session.musical.timeline;
  if (typeof at === 'number' || /^\d+$/.test(String(at))) return tl.tickAtBar(Number(at));
  if (String(at).toLowerCase() === 'playhead') return session.transport.location.value?.tick ?? 0;
  const m = matchName(session.markers.items, at, (mk) => mk.name.value);
  if (!m) throw new Error(`no marker called "${at}"`);
  return m.start?.value ?? m.tick?.value ?? m.location?.value;
}

function barOf(session, tick) {
  return session.musical.timeline.barBeat(tick ?? 0).bar;
}

// ---------- assistant arming (stored with the session) ----------

/** The session's Add On storage: `storage` since LUNA 3.1.1 (was `preferences`; same methods). */
function sessionStore(session) {
  return session.storage ?? session.preferences;
}

async function armContext(session) {
  const c = sessionStore(session).context(ARM_CONTEXT);
  await c.load?.();
  return c;
}

async function isAssistantArmed(session, track) {
  const c = await armContext(session);
  return c.value(uidOf(track), 'armed')?.value === true;
}

async function setAssistantArmed(session, track, on) {
  const c = await armContext(session);
  await c.add(uidOf(track), 'armed', false);
  await c.set(uidOf(track), 'armed', !!on);
}

async function assistantArmedIds(session) {
  const c = await armContext(session);
  return new Set(userTracks(session).filter((t) => c.value(uidOf(t), 'armed')?.value === true).map(uidOf));
}

/** What the helper should capture: the inputs of assistant-armed audio tracks. */
async function listenInputs(luna, session) {
  const armed = await assistantArmedIds(session);
  const numbered = numberInputs(await luna.audioInputs());
  const out = [];
  for (const t of userTracks(session)) {
    if (!armed.has(uidOf(t)) || t.trackType.value !== 'audio') continue;
    const inp = numbered.find((i) => i.uid === t.inputNodeUid.value);
    if (inp) out.push({ name: inp.name, device: inp.device, channel: inp.channel, format: inp.format, track: nameOf(t) });
  }
  return out;
}

// ---------- click cuts (stored with the session) ----------

async function clickContext(session) {
  const c = sessionStore(session).context(CLICK_CONTEXT);
  await c.load?.();
  return c;
}
async function clickCuts(session) {
  return (await clickContext(session)).value('cuts', 'list')?.value ?? [];
}
async function setClickCuts(session, list) {
  const c = await clickContext(session);
  await c.add('cuts', 'list', []);
  await c.set('cuts', 'list', list);
}

/**
 * Which click outputs should be muted at `tick`: the cut with the latest start at or before
 * `tick` wins. Returns 'none', 'cues' or 'everywhere'. Cuts apply only while rolling.
 */
function clickCutAt(cuts, tick, rolling) {
  if (!rolling) return 'none';
  let best;
  for (const c of cuts) if (c.tick <= tick && (!best || c.tick >= best.tick)) best = c;
  return best?.where ?? 'none';
}

/** The next cut start after `tick`, for scheduling it precisely. */
function nextClickCut(cuts, tick) {
  return cuts.filter((c) => c.tick > tick).sort((a, b) => a.tick - b.tick)[0];
}

function clickTrack(session) {
  return session.tracks.items.find((t) => t.trackType.value === 'click');
}

/** The click's outputs for a mode: cue sends only, or the main output too. */
function clickOutputsFor(click, where) {
  if (!click || where === 'none') return [];
  const ids = ['cue_0', 'cue_1', 'cue_2', 'cue_3'];
  if (where === 'everywhere') ids.unshift('main');
  return ids.map((id) => click.outputs.byId(id)).filter((o) => o && (o.id === 'main' || (o.destinations?.value ?? []).length));
}

// ---------- snapshot for the helper ----------

async function snapshot(luna, session) {
  const numbered = numberInputs(await luna.audioInputs());
  const armed = await assistantArmedIds(session);
  const tl = session.musical.timeline;
  const inputName = (uid) => numbered.find((i) => i.uid === uid)?.name ?? null;
  const busName = (uid) => nameOf(session.buses.byId(uid)) || null;
  const outputDests = (ch, id) => (ch.outputs?.byId(id)?.destinations?.value ?? []).map((d) => d.name ?? busName(d.busUid));
  const plugins = (ch) => (ch.loadedEffects ?? []).map((e) => e.pluginName?.value).filter(Boolean);
  const sends = (ch) =>
    [0, 1, 2, 3, 4, 5, 6, 7].flatMap((n) => outputDests(ch, `send_${n}`).map((bus) => ({ slot: n, bus })));
  const selected = new Set(session.context?.selectionTracks?.value ?? []);
  const t = session.transport;
  const click = clickTrack(session);
  return {
    session: {
      name: session.name.value ?? null,
      tempo: t.tempo?.value ?? null,
      meter: t.timeSignature?.value ? `${t.timeSignature.value.numerator ?? t.timeSignature.value[0]}/${t.timeSignature.value.denominator ?? t.timeSignature.value[1]}` : null,
      playStartBar: barOf(session, t.playStart?.value),
      locationBar: barOf(session, t.location.value?.tick),
      playing: t.playing.value === true,
      recording: t.recordEnabled?.value === true,
    },
    tracks: userTracks(session).map((tr) => ({
      name: nameOf(tr),
      type: tr.trackType.value,
      format: tr.stemFormat?.value ?? null,
      input: inputName(tr.inputNodeUid?.value),
      output: outputDests(tr, 'main'),
      sends: sends(tr),
      plugins: plugins(tr),
      recordArmed: tr.recordEnabled.value === true,
      assistantArmed: armed.has(uidOf(tr)),
      playlist: session.playlistFor?.(tr)?.name?.value ?? null,
      selected: selected.has(uidOf(tr)),
    })),
    buses: auxBuses(session).map((b) => ({ name: nameOf(b), format: b.stemFormat?.value ?? null, output: outputDests(b, 'main'), plugins: plugins(b) })),
    inputs: numbered.map(({ number, name, format }) => ({ number, name, format })),
    markers: [...session.markers.items].map((m) => ({ name: m.name.value, bar: tl.barBeat(m.start?.value ?? m.tick?.value ?? 0).bar })),
    clickOutputs: click ? clickOutputsFor(click, 'everywhere').flatMap((o) => (o.id === 'main' ? outputDests(click, 'main') : outputDests(click, o.id))) : [],
  };
}

// ---------- validating and describing plans ----------

function checkValue(schema, v, path, errors) {
  if (schema.anyOf) {
    const ok = schema.anyOf.some((s) => {
      const e = [];
      checkValue(s, v, path, e);
      return !e.length;
    });
    if (!ok) errors.push(`${path}: not one of the allowed forms`);
    return;
  }
  const type = schema.type;
  if (type === 'object') {
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return errors.push(`${path}: expected an object`);
    for (const r of schema.required ?? []) if (v[r] === undefined) errors.push(`${path}.${r}: missing`);
    for (const [k, val] of Object.entries(v)) {
      const s = schema.properties?.[k];
      if (!s) {
        if (schema.additionalProperties === false) errors.push(`${path}.${k}: unknown`);
        continue;
      }
      checkValue(s, val, `${path}.${k}`, errors);
    }
  } else if (type === 'array') {
    if (!Array.isArray(v)) return errors.push(`${path}: expected a list`);
    v.forEach((x, i) => checkValue(schema.items, x, `${path}[${i}]`, errors));
  } else if (type === 'string') {
    if (typeof v !== 'string') return errors.push(`${path}: expected text`);
    if (schema.enum && !schema.enum.includes(v)) errors.push(`${path}: must be one of ${schema.enum.join(', ')}`);
  } else if (type === 'integer' || type === 'number') {
    if (typeof v !== 'number' || (type === 'integer' && !Number.isInteger(v))) return errors.push(`${path}: expected a ${type}`);
    if (schema.minimum !== undefined && v < schema.minimum) errors.push(`${path}: below ${schema.minimum}`);
    if (schema.maximum !== undefined && v > schema.maximum) errors.push(`${path}: above ${schema.maximum}`);
  } else if (type === 'boolean') {
    if (typeof v !== 'boolean') errors.push(`${path}: expected true/false`);
  }
}

/** Problems with a plan's steps; empty when every step is a known action with valid args. */
function validateSteps(steps) {
  const errors = [];
  if (!Array.isArray(steps) || !steps.length) return ['plan has no steps'];
  steps.forEach((s, i) => {
    const spec = CATALOG_BY_NAME[s?.action];
    if (!spec) return errors.push(`step ${i + 1}: unknown action "${s?.action}"`);
    checkValue(spec.params, s.args ?? {}, `step ${i + 1} ${s.action}`, errors);
  });
  return errors;
}

const list = (xs) => (Array.isArray(xs) ? (xs.length > 2 ? `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}` : xs.join(' and ')) : xs === 'armed' ? 'armed tracks' : `${xs} tracks`);
const at = (p) => (typeof p === 'number' || /^\d+$/.test(String(p)) ? `bar ${p}` : String(p).toLowerCase() === 'playhead' ? 'the playhead' : `"${p}"`);

/** One line of plain English per step, for the suggestion card. */
function describeStep({ action, args = {} }) {
  switch (action) {
    case 'createTracks':
      return `Create ${args.tracks
        .map((t) => `${t.name} (${t.format ?? 'mono'}${t.kind === 'instrument' ? ' instrument' : ''}${t.input ? `, in ${t.input}` : ''})`)
        .join(', ')}${args.recordArm ? ', record-armed' : ''}`;
    case 'createBus': return `Create ${args.format ?? 'stereo'} bus "${args.name}"`;
    case 'setOutput': return `Route ${list(args.channels)} → ${args.to === 'main' ? 'Main Output' : args.to}`;
    case 'createGroup': return `Group ${list(args.members)} as "${args.name}"${args.links ? ` (${args.links.join('+')})` : ''}`;
    case 'addSend': return `Send ${list(args.from)} → ${args.to}${args.levelDb !== undefined ? ` at ${args.levelDb} dB` : ''}${args.preFader ? ' (pre-fader)' : ''}`;
    case 'insertPlugin': return `Load ${args.plugin} on ${args.on}`;
    case 'setRecordArm': return `${args.on ? 'Arm' : 'Disarm'} ${list(args.tracks)}`;
    case 'setInputMonitor': return `Input monitor ${args.on ? 'on' : 'off'}: ${list(args.tracks)}`;
    case 'setMute': return `${args.on ? 'Mute' : 'Unmute'} ${list(args.tracks)}`;
    case 'setSolo': return `${args.on ? 'Solo' : 'Unsolo'} ${list(args.tracks)}`;
    case 'setGain': return `Set ${list(args.tracks)} to ${args.db} dB`;
    case 'renameTrack': return `Rename ${args.from} → ${args.to}`;
    case 'setTrackColor': return `Colour ${list(args.tracks)} ${args.color}`;
    case 'deleteTracks': return `Delete ${list(args.tracks)}`;
    case 'newPlaylist': return `New ${args.duplicate ? 'duplicate' : 'empty'} playlist on ${list(args.tracks)}`;
    case 'favouriteTake': return `★ Favourite the current take on ${list(args.tracks)}`;
    case 'setStartEarlier': return `Start ${args.bars} bar${args.bars === 1 ? '' : 's'} earlier`;
    case 'setPlayStart': return `Start at ${at(args.at)}`;
    case 'addMarker': return `Marker "${args.name}" at ${at(args.at)}`;
    case 'cutClick': return `Cut the click from ${at(args.at)} (${args.where === 'cues' ? 'headphones only' : 'everywhere'})`;
    case 'clearClickCuts': return 'Remove click cuts';
    case 'clickSettings': return `Click: ${Object.entries(args).map(([k, v]) => `${k.replace(/([A-Z])/g, ' $1').toLowerCase()} ${v ? 'on' : 'off'}`).join(', ')}`;
    case 'setTempo': return `Tempo ${args.bpm} BPM`;
    case 'setMeter': return `Time signature ${args.numerator}/${args.denominator}`;
    case 'setLoop': return `Loop ${at(args.fromBar)} to ${at(args.toBar)}`;
    case 'transport': return { play: 'Play', stop: 'Stop', record: 'Record', returnToZero: 'Return to zero' }[args.command];
    case 'armAssistant': return `${args.on ? 'Arm' : 'Disarm'} ${list(args.tracks)} for the assistant`;
    case 'createAssistantInputs': return `Assistant input track${args.inputs.length > 1 ? 's' : ''} on ${list(args.inputs)}`;
    case 'applyRecipe': return `Apply your "${args.name}" setup`;
    case 'saveRecipe': return `Save this setup as "${args.name}"`;
    default: return action;
  }
}

/** Steps that change or remove what the operator already has; the card marks them. */
function isDestructive(step) {
  return step.action === 'deleteTracks';
}

// ---------- executing ----------

async function newTrack(session, opts) {
  const reply = await session.newTrack({ makeNameUnique: true, ...opts });
  // SDK 3.1.1 resolves to the new Luna.Track[]; older ones gave LUNA's raw reply.
  const first = Array.isArray(reply) ? reply[0] : reply;
  const uid = first?.id ?? first?.track_uid ?? first?.uid ?? (Array.isArray(first?.new_tracks) ? first.new_tracks[0] : undefined);
  return until(() => {
    const t = uid ? session.tracks.byId(uid) : session.tracks.items.find((x) => nameOf(x) === opts.name);
    return t && t.name.value != null ? t : undefined;
  }, `track "${opts.name}"`);
}

async function ensureBus(session, name, format = 'stereo') {
  const existing = matchName(auxBuses(session), name, nameOf);
  if (existing) return existing;
  return session.newBus({ name, stemFormat: format });
}

async function outputsOf(ch) {
  if (ch.outputs.load) await ch.outputs.load();
  return ch.outputs;
}

async function routeMain(luna, session, ch, to) {
  const outputs = await outputsOf(ch);
  const main = outputs.byId('main');
  if (String(to).toLowerCase() === 'main' || /^main( output)?$/i.test(to)) {
    await main.retarget(session.master?.id ?? 'main', 'Assistant: route to Main');
    return;
  }
  const bus = await ensureBus(session, to);
  await main.retarget(uidOf(bus), `Assistant: route to ${nameOf(bus)}`); // assign would append beside Main Output (fact 43)
}

async function setSwitch(control, on) {
  if (!control) return;
  if (control.load && control.on === undefined) await control.load();
  if (control.on === !!on) return;
  if (control.real?.set) await control.real.set(!!on);
  else await control.toggle();
}

/** The play/record start, read fresh: LUNA may have moved it since the last push. */
async function playStartTick(luna, session) {
  const r = await luna.raw.get(`/sessions/${session.id}?children=0&commands=0&flatvalue=1&propfilter=play_start`);
  return r?.properties?.play_start ?? session.transport.playStart?.value ?? 0;
}

async function setPlayStartTick(luna, session, tick) {
  if (session.transport.setPlayStart) await session.transport.setPlayStart(tick);
  else await luna.raw.set(`/sessions/${session.id}/play_start`, tick);
}

/** Every action: (ctx, args) → short result text. ctx = { luna, session, helper }. */
const ACTIONS = {
  async createTracks({ luna, session }, { tracks, recordArm }) {
    const numbered = numberInputs(await luna.audioInputs());
    const made = [];
    const unassigned = [];
    for (const spec of tracks) {
      const format = spec.format ?? 'mono';
      const t = await newTrack(session, { name: spec.name, trackType: spec.kind ?? 'audio', stemFormat: format });
      if (spec.input) {
        // The planner numbers inputs from the request or its own idea of a studio; a Mac with fewer
        // inputs still gets the track, without an input, rather than a card that stops halfway.
        const inp = resolveInput(numbered, spec.input, format);
        if (inp) await t.inputNodeUid.set(inp.uid);
        else unassigned.push(`${spec.name} (no input ${spec.input})`);
      }
      if (spec.color && NAMED_COLORS[spec.color.toLowerCase()]) await t.color.set(NAMED_COLORS[spec.color.toLowerCase()]);
      if (recordArm) await t.recordEnabled.set(true);
      made.push(nameOf(t));
    }
    return `created ${made.join(', ')}${unassigned.length ? `; no input set on ${unassigned.join(', ')}` : ''}`;
  },

  async createBus({ session }, { name, format }) {
    const b = await ensureBus(session, name, format ?? 'stereo');
    return `bus ${nameOf(b)}`;
  },

  async setOutput({ luna, session }, { channels: names, to }) {
    for (const n of names) await routeMain(luna, session, needChannel(session, n), to);
    return `routed ${names.length}`;
  },

  async createGroup({ luna, session }, { name, members, links }) {
    const uids = members.map((m) => uidOf(needChannel(session, m)));
    const reply = await luna.raw.post(`/sessions/${session.id}/new_group`, {
      session_uid: session.id,
      name,
      group_attributes: (links ?? ['mix', 'edit']).join(','),
      tracks: uids,
      make_name_unique: true,
    });
    return `group ${reply?.uid ?? name}`;
  },

  async addSend({ session }, { from, to, levelDb, preFader }) {
    const bus = await ensureBus(session, to);
    for (const n of from) {
      const ch = needChannel(session, n);
      const outputs = await outputsOf(ch);
      // First send slot already on this bus, else the first empty one.
      let send;
      for (let i = 0; i < 8; i++) {
        const o = outputs.byId(`send_${i}`);
        const dests = o?.destinations?.value ?? [];
        if (dests.some((d) => d.busUid === uidOf(bus))) { send = o; break; }
        if (!send && o && !dests.length) send = o;
      }
      if (!send) throw new Error(`${nameOf(ch)} has no free send`);
      if (!(send.destinations?.value ?? []).some((d) => d.busUid === uidOf(bus))) await send.retarget(uidOf(bus), `Assistant: send to ${nameOf(bus)}`);
      if (levelDb !== undefined && send.gain?.real?.set) await send.gain.real.set(levelDb);
      if (preFader !== undefined && send.preFader?.set) await send.preFader.set(preFader);
    }
    return `sent ${from.length} → ${nameOf(bus)}`;
  },

  async insertPlugin({ luna, session }, { on, plugin }) {
    const ch = needChannel(session, on);
    const p = await findPlugin(luna, plugin);
    await ch.insert(p.id);
    return `${nameOf(p) || p.id} on ${nameOf(ch)}`;
  },

  async setRecordArm({ session }, { tracks, on }) {
    for (const t of trackSet(session, tracks)) await t.recordEnabled.set(!!on);
    return 'ok';
  },

  async setInputMonitor({ session }, { tracks, on }) {
    for (const t of trackSet(session, tracks)) await t.inputMonitor.set(!!on);
    return 'ok';
  },

  async setMute({ session }, { tracks, on }) {
    for (const t of trackSet(session, tracks)) {
      await t.loadStrip?.();
      await setSwitch(t.mute, on);
    }
    return 'ok';
  },

  async setSolo({ session }, { tracks, on }) {
    for (const t of trackSet(session, tracks)) {
      await t.loadStrip?.();
      await setSwitch(t.solo, on);
    }
    return 'ok';
  },

  async setGain({ session }, { tracks, db }) {
    for (const t of trackSet(session, tracks)) {
      await t.loadStrip?.();
      await t.gain.real.set(db);
    }
    return 'ok';
  },

  async renameTrack({ session }, { from, to }) {
    await needChannel(session, from).name.set(to);
    return 'ok';
  },

  async setTrackColor({ session }, { tracks, color }) {
    const argb = NAMED_COLORS[color.toLowerCase()];
    if (!argb) throw new Error(`unknown colour "${color}"`);
    for (const t of trackSet(session, tracks)) await t.color.set(argb);
    return 'ok';
  },

  async deleteTracks({ session }, { tracks }) {
    await session.deleteTracks(tracks.map((n) => uidOf(needChannel(session, n))));
    return `deleted ${tracks.length}`;
  },

  async newPlaylist({ session }, { tracks, duplicate }) {
    const ts = trackSet(session, tracks);
    if (!ts.length) throw new Error('no tracks to make a playlist on');
    await session.newPlaylists(ts.map(uidOf), { duplicate: !!duplicate });
    return `new playlist on ${ts.length}`;
  },

  async favouriteTake({ luna, session }, { tracks, label }) {
    const ts = trackSet(session, tracks);
    if (!ts.length) throw new Error('no tracks to favourite');
    let firstStart;
    for (const t of ts) {
      const pl = session.playlistFor?.(t);
      if (!pl) continue;
      const name = pl.name?.value ?? `V${pl.version?.value ?? ''}`;
      if (!name.startsWith('★')) {
        await luna.raw.post(`/sessions/${session.id}/set_playlist_name`, {
          session_uid: session.id,
          track_uid: uidOf(t),
          version: pl.version?.value ?? 1,
          name: `★ ${label ?? name}`,
        });
      }
      if (firstStart === undefined && pl.clips) {
        await pl.clips.load?.();
        const starts = pl.clips.items.map((c) => c.start.value).filter((x) => typeof x === 'number');
        if (starts.length) firstStart = Math.min(...starts);
      }
    }
    // LUNA keeps one marker per position: where the session already has one (a "Top" marker at the
    // start of the take, say), the ★ playlist name is the record and no second marker is added.
    const at = firstStart ?? session.transport.playStart?.value ?? 0;
    await session.markers.load?.();
    if (!session.markers.items.some((m) => m.location?.value === at)) await session.addMarker(at, `★ ${label ?? 'Favourite take'}`);
    return `★ on ${ts.length}`;
  },

  async setStartEarlier({ luna, session }, { bars }) {
    const tl = session.musical.timeline;
    const now = await playStartTick(luna, session);
    const bar = tl.barBeat(now).bar;
    const target = tl.tickAtBar(bar - bars);
    if (target < 0) throw new Error(`only ${bar - 1} bar(s) before the start; move bar 1 later first`);
    await setPlayStartTick(luna, session, target);
    return `start at bar ${bar - bars}`;
  },

  async setPlayStart({ luna, session }, { at: pos }) {
    await setPlayStartTick(luna, session, positionTick(session, pos));
    return 'ok';
  },

  async addMarker({ session }, { name, at: pos }) {
    await session.addMarker(positionTick(session, pos), name);
    return 'ok';
  },

  async cutClick({ session }, { at: pos, where }) {
    const tick = positionTick(session, pos);
    const cuts = (await clickCuts(session)).filter((c) => c.tick !== tick);
    cuts.push({ tick, where, label: String(pos) });
    await setClickCuts(session, cuts);
    return `click cut at bar ${barOf(session, tick)}`;
  },

  async clearClickCuts({ session }) {
    await setClickCuts(session, []);
    return 'ok';
  },

  async clickSettings({ luna, session }, args) {
    const base = `/sessions/${session.id}`;
    const map = { duringPlay: 'click_during_play', duringRecord: 'click_during_record', duringCountIn: 'click_during_count_in', countIn: 'count_in_enabled' };
    for (const [k, prop] of Object.entries(map)) if (args[k] !== undefined) await luna.raw.set(`${base}/${prop}`, args[k]);
    return 'ok';
  },

  async setTempo({ session }, { bpm }) {
    await session.musical.setTempo(bpm);
    return 'ok';
  },

  async setMeter({ session }, { numerator, denominator }) {
    await session.musical.setTimeSignature(numerator, denominator);
    return 'ok';
  },

  async setLoop({ session }, { fromBar, toBar }) {
    await session.transport.setLoop(positionTick(session, fromBar), positionTick(session, toBar));
    if (session.transport.looping.value !== true) await session.transport.looping.set?.(true);
    return 'ok';
  },

  async transport({ session }, { command }) {
    const t = session.transport;
    if (command === 'play') await t.play();
    else if (command === 'stop') await t.stop();
    else if (command === 'record') await t.record();
    else await t.returnToZero();
    return 'ok';
  },

  async armAssistant({ session }, { tracks, on }) {
    const ts = trackSet(session, tracks);
    for (const t of ts) await setAssistantArmed(session, t, on);
    return `${ts.length} ${on ? 'armed' : 'disarmed'}`;
  },

  async createAssistantInputs({ luna, session }, { inputs }) {
    const numbered = numberInputs(await luna.audioInputs());
    for (const ref of inputs) {
      const inp = resolveInput(numbered, ref);
      if (!inp) throw new Error(`no input "${ref}"`);
      const t = await newTrack(session, { name: `Assistant ${inp.name}`, trackType: 'audio', stemFormat: inp.format === 'stereo' ? 'stereo' : 'mono' });
      await t.inputNodeUid.set(inp.uid);
      // Input only: drop the main output so nothing reaches the mix.
      const main = (await outputsOf(t)).byId('main');
      for (const d of main.destinations?.value ?? []) await main.unassign(d.uid);
      await setAssistantArmed(session, t, true);
    }
    return `${inputs.length} assistant input(s)`;
  },

  async applyRecipe(ctx, { name }) {
    if (!ctx.helper) throw new Error('the helper is not running');
    const profile = await ctx.helper.profile();
    const recipes = profile?.recipes ?? {};
    const all = Object.entries(recipes).map(([n, r]) => ({ name: n, ...r }));
    const r = matchName(all, name) ?? all.find((x) => (x.aliases ?? []).some((a) => norm(a) === norm(name)));
    if (!r) throw new Error(`no saved setup called "${name}"`);
    const errors = validateSteps(r.steps);
    if (errors.length) throw new Error(`saved setup "${r.name}" is invalid: ${errors[0]}`);
    const results = await execute(ctx, { steps: r.steps.filter((s) => s.action !== 'applyRecipe') });
    const failed = results.filter((x) => !x.ok);
    if (failed.length) throw new Error(`${failed.length} step(s) of "${r.name}" failed: ${failed[0].error}`);
    return `applied ${r.name}`;
  },

  async saveRecipe(ctx, { name, aliases, tracks }) {
    if (!ctx.helper) throw new Error('the helper is not running');
    const steps = await captureRecipe(ctx.luna, ctx.session, tracks);
    await ctx.helper.saveRecipe({ name, aliases, steps });
    return `saved ${steps.length} steps as "${name}"`;
  },
};

/** The best plugin for spoken text: "dbx160", "dbx one sixty" (after the helper's vocabulary pass), "LA2A". */
async function findPlugin(luna, text) {
  await luna.plugins.load();
  const spaced = text.replace(/([a-z])(\d)/gi, '$1 $2').replace(/(\d)([a-z])/gi, '$1 $2').replace(/\s+/g, ' ').trim();
  const queries = [text.trim(), spaced, spaced.replace(/ /g, '-'), ...spaced.split(' ').filter((w) => w.length >= 3).sort((a, b) => b.length - a.length)];
  for (const q of queries) {
    const found = luna.plugins.find(q).filter((p) => {
      const n = norm(`${nameOf(p)} ${p.id}`);
      return words(spaced).every((w) => n.includes(w));
    });
    if (found.length) return rankPlugins(found, spaced)[0];
  }
  throw new Error(`no plugin matching "${text}"`);
}

/** Prefer UA native plugins (uaudio_*) over legacy and third-party versions with the same name. */
function rankPlugins(found, query) {
  const q = norm(query);
  const score = (p) => {
    const n = norm(nameOf(p) || p.id);
    return (n.includes(q) ? 0 : 2) + (String(p.id).startsWith('uaudio_') ? 0 : 1) + (/legacy/i.test(nameOf(p)) ? 1 : 0);
  };
  return [...found].sort((a, b) => score(a) - score(b));
}

/** Steps that rebuild the current setup (or the named tracks) in another session. */
async function captureRecipe(luna, session, only) {
  const numbered = numberInputs(await luna.audioInputs());
  const armed = await assistantArmedIds(session);
  const keep = only?.length ? new Set(only.map((n) => uidOf(needChannel(session, n)))) : null;
  const tracks = userTracks(session).filter((t) => !keep || keep.has(uidOf(t)));
  const steps = [];
  const inputRef = (t) => {
    const i = numbered.find((x) => x.uid === t.inputNodeUid.value);
    return i ? i.number ?? i.name : undefined;
  };
  const assistantTracks = tracks.filter((t) => armed.has(uidOf(t)));
  const normal = tracks.filter((t) => !armed.has(uidOf(t)));
  if (normal.length)
    steps.push({ action: 'createTracks', args: { tracks: normal.map((t) => ({ name: nameOf(t), kind: t.trackType.value === 'instrument' ? 'instrument' : 'audio', format: t.stemFormat?.value === 'stereo' ? 'stereo' : 'mono', ...(inputRef(t) ? { input: inputRef(t) } : {}) })) } });
  if (assistantTracks.length) steps.push({ action: 'createAssistantInputs', args: { inputs: assistantTracks.map(inputRef).filter(Boolean) } });
  const busName = (uid) => nameOf(session.buses.byId(uid));
  const usedBuses = new Set();
  const byOutput = new Map();
  for (const t of normal) {
    const outputs = await outputsOf(t);
    for (const d of outputs.byId('main')?.destinations?.value ?? []) {
      const n = busName(d.busUid);
      if (!n || session.buses.byId(d.busUid)?.isMaster) continue;
      usedBuses.add(n);
      byOutput.set(n, [...(byOutput.get(n) ?? []), nameOf(t)]);
    }
  }
  const sendsTo = new Map();
  for (const t of normal) {
    const outputs = await outputsOf(t);
    for (let i = 0; i < 8; i++)
      for (const d of outputs.byId(`send_${i}`)?.destinations?.value ?? []) {
        const n = busName(d.busUid);
        if (!n) continue;
        usedBuses.add(n);
        sendsTo.set(n, [...(sendsTo.get(n) ?? []), nameOf(t)]);
      }
  }
  for (const b of auxBuses(session).filter((b) => usedBuses.has(nameOf(b))))
    steps.push({ action: 'createBus', args: { name: nameOf(b), format: b.stemFormat?.value === 'mono' ? 'mono' : 'stereo' } });
  for (const [bus, from] of byOutput) steps.push({ action: 'setOutput', args: { channels: from, to: bus } });
  for (const [bus, from] of sendsTo) steps.push({ action: 'addSend', args: { from, to: bus } });
  for (const ch of [...normal, ...auxBuses(session).filter((b) => usedBuses.has(nameOf(b)))])
    for (const e of ch.loadedEffects ?? []) {
      const p = e.pluginName?.value;
      if (p) steps.push({ action: 'insertPlugin', args: { on: nameOf(ch), plugin: p } });
    }
  const armedRec = normal.filter((t) => t.recordEnabled.value === true).map(nameOf);
  if (armedRec.length) steps.push({ action: 'setRecordArm', args: { tracks: armedRec, on: true } });
  return steps;
}

/**
 * Run a plan's steps in order. Each step is its own undo entry: LUNA holds pushes inside a
 * transaction, so a step could not see the tracks an earlier step made. Stops at the first
 * failure; returns one result per step that ran.
 */
async function execute(ctx, plan, onStep = () => {}) {
  const results = [];
  for (const [i, step] of plan.steps.entries()) {
    onStep(i, 'running');
    try {
      const fn = ACTIONS[step.action];
      if (!fn) throw new Error(`unknown action ${step.action}`);
      const detail = await fn(ctx, step.args ?? {});
      results.push({ step, ok: true, detail });
      onStep(i, 'done', detail);
    } catch (e) {
      results.push({ step, ok: false, error: e.message ?? String(e) });
      onStep(i, 'failed', e.message ?? String(e));
      break;
    }
  }
  return results;
}

const AddOnLogic = {
  CATALOG,
  ACTIONS,
  ARM_CONTEXT,
  CLICK_CONTEXT,
  matchName,
  numberInputs,
  resolveInput,
  trackSet,
  positionTick,
  validateSteps,
  describeStep,
  isDestructive,
  execute,
  snapshot,
  listenInputs,
  sessionStore,
  isAssistantArmed,
  setAssistantArmed,
  assistantArmedIds,
  clickCuts,
  setClickCuts,
  clickCutAt,
  nextClickCut,
  clickTrack,
  clickOutputsFor,
  captureRecipe,
  rankPlugins,
  userTracks,

  /** Text for the transport counter. */
  locationText(transport) {
    return Luna.formatBarBeat(transport.location.value?.bar_beat);
  },
};

globalThis.AddOnLogic = AddOnLogic;
