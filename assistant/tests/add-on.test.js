// Runs against Luna.MockEngine: no LUNA needed. `npm test`.
import { describe, expect, it } from 'vitest';
import { Luna } from '@uaudio/luna-builder-sdk';

globalThis.Luna = Luna; // logic.js is a classic script that reads the Luna global
await import('../logic.js');
const logic = globalThis.AddOnLogic;

const APOLLO = ['MIC/LINE 1', 'MIC/LINE 2', 'MIC/LINE 3', 'MIC/LINE 4', 'LINE 5', 'LINE 6', 'MON L', 'MON R', 'VIRTUAL 1'];

async function connected({ tracks = ['Vocal', 'Bass'], inputs = APOLLO } = {}) {
  const engine = new Luna.MockEngine();
  engine.addSession('abc', { name: 'Demo', tracks });
  engine.addDevice('Apollo', { inputs });
  engine.addDevice('Apollo pairs', { inputs: ['LINE 5/6'], stemFormat: 'stereo' });
  engine.addPlugin('uaudio_dbx_160', { name: 'dbx 160 Compressor' });
  engine.addPlugin('com.universal_audio_inc.vst.37AU', { name: 'UAD dbx 160 Legacy' });
  await engine.settled();
  const luna = await Luna.connect({ port_: engine.port() });
  const session = await luna.session();
  await Promise.all(session.tracks.items.map((t) => t.loadStrip()));
  return { engine, luna, session, ctx: { luna, session } };
}

/** Answer engine commands the mock does not have (new_group, set_playlist_name), recording them. */
function intercept(luna, verbs) {
  const calls = [];
  const post = luna.raw.post.bind(luna.raw);
  luna.raw.post = async (path, data) => {
    const verb = path.split('/').pop();
    if (verbs[verb]) {
      calls.push({ verb, data });
      return verbs[verb](data);
    }
    return post(path, data);
  };
  return calls;
}

const named = (session, n) => logic.userTracks(session).find((t) => t.name.value === n);

describe('names and inputs', () => {
  it('matches spoken names loosely', () => {
    const items = ['Kick In', 'Snare Top', 'Hats', 'Overheads', 'Room'].map((name) => ({ name }));
    expect(logic.matchName(items, 'kick')?.name).toBe('Kick In');
    expect(logic.matchName(items, 'snare')?.name).toBe('Snare Top');
    expect(logic.matchName(items, 'hi-hat')?.name).toBe('Hats');
    expect(logic.matchName(items, 'OH')?.name).toBe('Overheads');
    expect(logic.matchName(items, 'overhead')?.name).toBe('Overheads');
    expect(logic.matchName(items, 'rom')?.name).toBe('Room');
    expect(logic.matchName(items, 'bass')).toBeUndefined();
  });

  it('numbers front-panel inputs and pairs, never monitor or virtual ones', () => {
    const n = logic.numberInputs([
      { uid: 'a', name: 'MIC/LINE/HIZ 1', stemFormat: 'mono' },
      { uid: 'b', name: 'MIC/LINE 2', stemFormat: 'mono' },
      { uid: 'c', name: 'MIC/LINE HIZ 1/2', stemFormat: 'stereo' },
      { uid: 'd', name: 'MON L', stemFormat: 'mono' },
      { uid: 'e', name: 'VIRTUAL 1', stemFormat: 'mono' },
    ]);
    expect(n.map((i) => i.number)).toEqual(['1', '2', '1-2', null, null]);
    expect(n.map((i) => i.channel)).toEqual([0, 1, 0, null, null]);
    expect(logic.resolveInput(n, '2')?.uid).toBe('b');
    expect(logic.resolveInput(n, 'input 1')?.uid).toBe('a');
    expect(logic.resolveInput(n, '1-2')?.uid).toBe('c');
    expect(logic.resolveInput(n, '1/2')?.uid).toBe('c');
    expect(logic.resolveInput(n, '1', 'stereo')?.uid).toBe('c');
    expect(logic.resolveInput(n, 'virtual 1')?.uid).toBe('e');
    // Exact names win over "input 3" prefixes and numbers.
    const mac = logic.numberInputs([{ uid: 'm', name: 'Internal Microphone', stemFormat: 'mono' }]);
    expect(logic.resolveInput(mac, 'Internal Microphone')?.uid).toBe('m');
    expect(logic.resolveInput(n, 'MIC/LINE HIZ 1/2')?.uid).toBe('c');
  });
});

describe('plans', () => {
  it('accepts valid steps and explains invalid ones', () => {
    expect(logic.validateSteps([{ action: 'createBus', args: { name: 'Comp' } }])).toEqual([]);
    expect(logic.validateSteps([{ action: 'setRecordArm', args: { tracks: 'armed', on: true } }])).toEqual([]);
    expect(logic.validateSteps([{ action: 'launchRocket', args: {} }])[0]).toMatch(/unknown action/);
    expect(logic.validateSteps([{ action: 'createBus', args: {} }])[0]).toMatch(/name: missing/);
    expect(logic.validateSteps([{ action: 'setStartEarlier', args: { bars: 0 } }])[0]).toMatch(/below 1/);
    expect(logic.validateSteps([{ action: 'cutClick', args: { at: 33, where: 'nowhere' } }])[0]).toMatch(/one of cues, everywhere/);
    expect(logic.validateSteps([])).toEqual(['plan has no steps']);
  });

  it('describes steps in plain English', () => {
    expect(logic.describeStep({ action: 'addSend', args: { from: ['Kick', 'Snare'], to: 'Comp Bus' } })).toBe('Send Kick and Snare → Comp Bus');
    expect(logic.describeStep({ action: 'setStartEarlier', args: { bars: 2 } })).toBe('Start 2 bars earlier');
    expect(logic.describeStep({ action: 'cutClick', args: { at: 'Outro', where: 'cues' } })).toBe('Cut the click from "Outro" (headphones only)');
    expect(logic.isDestructive({ action: 'deleteTracks', args: { tracks: ['x'] } })).toBe(true);
  });

  it('describes every catalog action', () => {
    for (const a of logic.CATALOG) expect(typeof logic.ACTIONS[a.name]).toBe('function');
  });
});

describe('executing', () => {
  it('sets up a drum kit on inputs 1-6 (example 1)', async () => {
    const { engine, session, ctx, luna } = await connected({ tracks: [] });
    const calls = intercept(luna, { new_group: () => ({ uid: 'g1' }) });
    const results = await logic.execute(ctx, {
      steps: [
        { action: 'createTracks', args: { tracks: [
          { name: 'Kick', input: '1' }, { name: 'Snare', input: '2' }, { name: 'Hats', input: '3' },
          { name: 'Ride', input: '4' }, { name: 'Overheads', format: 'stereo', input: '5-6' },
        ] } },
        { action: 'createGroup', args: { name: 'Drums', members: ['Kick', 'Snare', 'Hats', 'Ride', 'Overheads'] } },
      ],
    });
    expect(results.map((r) => r.error ?? 'ok')).toEqual(['ok', 'ok']);
    await engine.settled();
    const ins = await ctx.luna.audioInputs();
    // The mock numbers each device's inputs from in_1, so compare uids, not names, for the pair.
    const uidFor = (name) => ins.find((i) => i.name === name).uid;
    expect(logic.userTracks(session).map((t) => [t.name.value, t.stemFormat.value, t.inputNodeUid.value])).toEqual([
      ['Kick', 'mono', uidFor('MIC/LINE 1')], ['Snare', 'mono', uidFor('MIC/LINE 2')], ['Hats', 'mono', uidFor('MIC/LINE 3')],
      ['Ride', 'mono', uidFor('MIC/LINE 4')], ['Overheads', 'stereo', uidFor('LINE 5/6')],
    ]);
    expect(calls[0].data).toMatchObject({ name: 'Drums', group_attributes: 'mix,edit' });
    expect(calls[0].data.tracks).toHaveLength(5);
  });

  it('makes tracks on inputs this Mac lacks, without an input, and carries on', async () => {
    const { ctx, session, engine } = await connected({ tracks: [], inputs: ['INPUT 1'] });
    const results = await logic.execute(ctx, { steps: [
      { action: 'createTracks', args: { tracks: [{ name: 'Kick', input: '1' }, { name: 'Snare', input: '2' }, { name: 'Overheads', format: 'stereo', input: '7-8' }] } },
      { action: 'createBus', args: { name: 'Drum Bus' } },
    ] });
    await engine.settled();
    expect(results.map((r) => r.ok)).toEqual([true, true]);
    expect(results[0].detail).toBe('created Kick, Snare, Overheads; no input set on Snare (no input 2), Overheads (no input 7-8)');
    expect(named(session, 'Kick').inputNodeUid.value).toBeTruthy();
    expect(named(session, 'Snare')).toBeDefined();
  });

  it('stops at the first failing step and says why', async () => {
    const { ctx } = await connected();
    const results = await logic.execute(ctx, { steps: [
      { action: 'renameTrack', args: { from: 'Vocal', to: 'Lead Vox' } },
      { action: 'renameTrack', args: { from: 'Tuba', to: 'x' } },
      { action: 'renameTrack', args: { from: 'Bass', to: 'never' } },
    ] });
    expect(results.map((r) => r.ok)).toEqual([true, false]);
    expect(results[1].error).toMatch(/no track or bus called "Tuba"/);
  });

  it('sends kick and snare to a new comp bus with a dbx 160 (example 2)', async () => {
    const { engine, session, ctx } = await connected({ tracks: ['Kick', 'Snare', 'Bass'] });
    const results = await logic.execute(ctx, { steps: [
      { action: 'addSend', args: { from: ['kick', 'snare'], to: 'Comp Bus' } },
      { action: 'insertPlugin', args: { on: 'Comp Bus', plugin: 'dbx160' } },
    ] });
    expect(results.map((r) => r.error ?? 'ok')).toEqual(['ok', 'ok']);
    await engine.settled();
    const bus = session.buses.items.find((b) => b.name.value === 'Comp Bus');
    expect(bus).toBeTruthy();
    for (const n of ['Kick', 'Snare']) {
      const t = named(session, n);
      await t.outputs.load();
      expect(t.outputs.byId('send_0').destinations.value.map((d) => d.busUid)).toEqual([bus.id]);
    }
    expect(results[1].detail).toMatch(/dbx 160 Compressor/); // the UADx version, not the legacy one
  });

  it('routes main outputs to a bus by replacing, not appending', async () => {
    const { engine, session, ctx } = await connected({ tracks: ['Kick', 'Snare'] });
    const [r] = await logic.execute(ctx, { steps: [{ action: 'setOutput', args: { channels: ['Kick', 'Snare'], to: 'Drum Bus' } }] });
    expect(r.ok).toBe(true);
    await engine.settled();
    const bus = session.buses.items.find((b) => b.name.value === 'Drum Bus');
    // Retarget unassigns then assigns (live LUNA routes to the bus; the mock maps a main
    // output's assignment to a hardware output), so check what was asked for.
    const assigns = engine.requests.filter((r) => r.path.endsWith('/new_output_assignment') || r.path.endsWith('/assign'));
    expect(assigns.length).toBe(2);
    expect(assigns.every((r) => JSON.stringify(r).includes(bus.id))).toBe(true);
    const kick = named(session, 'Kick');
    await kick.outputs.load();
    expect(kick.outputs.byId('main').destinations.value).toHaveLength(1);
  });

  it('favourites a take, starts a new playlist and adds intro bars (example 3)', async () => {
    const { engine, luna, session, ctx } = await connected({ tracks: ['Kick', 'Snare'] });
    const renames = intercept(luna, { set_playlist_name: () => null });
    await luna.raw.set('/sessions/abc/play_start', session.musical.timeline.tickAtBar(5));
    await engine.settled();
    for (const n of ['Kick', 'Snare']) await named(session, n).recordEnabled.set(true);
    await engine.settled();
    const results = await logic.execute(ctx, { steps: [
      { action: 'favouriteTake', args: { tracks: 'armed' } },
      { action: 'newPlaylist', args: { tracks: 'armed' } },
      { action: 'setStartEarlier', args: { bars: 2 } },
      { action: 'cutClick', args: { at: 9, where: 'cues' } },
    ] });
    expect(results.map((r) => r.error ?? 'ok')).toEqual(['ok', 'ok', 'ok', 'ok']);
    await engine.settled();
    expect(renames.map((r) => r.data.name.startsWith('★'))).toEqual([true, true]);
    expect(session.markers.items.map((m) => m.name.value)).toContain('★ Favourite take');
    expect(session.musical.timeline.barBeat(engine.value('/sessions/abc', 'play_start')).bar).toBe(3);
    expect(await logic.clickCuts(session)).toEqual([{ tick: session.musical.timeline.tickAtBar(9), where: 'cues', label: '9' }]);
  });

  it('favourites a take where a marker already sits at its start', async () => {
    const { engine, luna, session, ctx } = await connected({ tracks: ['Drums'] });
    const renames = intercept(luna, { set_playlist_name: () => null });
    await session.addMarker(session.musical.timeline.tickAtBar(1), 'Top'); // the take starts at bar 1 too
    await engine.settled();
    const [r] = await logic.execute(ctx, { steps: [{ action: 'favouriteTake', args: { tracks: ['Drums'] } }] });
    expect(r.error ?? 'ok').toBe('ok');
    await engine.settled();
    expect(renames).toHaveLength(1);
    expect(session.markers.items.map((m) => m.name.value)).toEqual(['Top']);
  });

  it('refuses to start before the beginning of the session', async () => {
    const { ctx } = await connected();
    const [r] = await logic.execute(ctx, { steps: [{ action: 'setStartEarlier', args: { bars: 4 } }] });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/move bar 1 later/);
  });
});

describe('assistant arming', () => {
  it('stores arming with the session and lists the inputs to listen to', async () => {
    const { engine, session, luna, ctx } = await connected({ tracks: ['Vocal', 'Talkback'] });
    const ins = await luna.audioInputs();
    await named(session, 'Talkback').inputNodeUid.set(ins.find((i) => i.name === 'MIC/LINE 3').uid);
    await logic.execute(ctx, { steps: [{ action: 'armAssistant', args: { tracks: ['talkback'], on: true } }] });
    await engine.settled();
    expect(await logic.isAssistantArmed(session, named(session, 'Talkback'))).toBe(true);
    expect(await logic.isAssistantArmed(session, named(session, 'Vocal'))).toBe(false);
    expect(await logic.listenInputs(luna, session)).toEqual([
      { name: 'MIC/LINE 3', device: 'Apollo', channel: 2, format: 'mono', track: 'Talkback' },
    ]);
  });
});

describe('one-click mics', () => {
  it('makes an input-only Assistant track on a computer mic and listens to it', async () => {
    const engine = new Luna.MockEngine();
    engine.addSession('abc', { name: 'Empty', tracks: [] });
    engine.addDevice('BuiltInMicrophoneDevice', { inputs: ['MacBook Pro Microphone'] });
    await engine.settled();
    const luna = await Luna.connect({ port_: engine.port() });
    const session = await luna.session();
    const [r] = await logic.execute({ luna, session }, { steps: [{ action: 'createAssistantInputs', args: { inputs: ['MacBook Pro Microphone'] } }] });
    expect(r.ok).toBe(true);
    await engine.settled();
    const t = named(session, 'Assistant MacBook Pro Microphone');
    expect(t).toBeDefined();
    expect(await logic.isAssistantArmed(session, t)).toBe(true);
    // No number in the name: the helper picks the channel (a one-channel device needs none).
    expect(await logic.listenInputs(luna, session)).toEqual([
      { name: 'MacBook Pro Microphone', device: 'BuiltInMicrophoneDevice', channel: null, format: 'mono', track: 'Assistant MacBook Pro Microphone' },
    ]);
  });
});

describe('click cuts', () => {
  const cuts = [{ tick: 100, where: 'cues' }, { tick: 300, where: 'everywhere' }];
  it('applies the latest cut at or before the playhead, only while rolling', () => {
    expect(logic.clickCutAt(cuts, 50, true)).toBe('none');
    expect(logic.clickCutAt(cuts, 100, true)).toBe('cues');
    expect(logic.clickCutAt(cuts, 299, true)).toBe('cues');
    expect(logic.clickCutAt(cuts, 400, true)).toBe('everywhere');
    expect(logic.clickCutAt(cuts, 400, false)).toBe('none');
    expect(logic.nextClickCut(cuts, 150)).toEqual(cuts[1]);
    expect(logic.nextClickCut(cuts, 400)).toBeUndefined();
  });
});

describe('snapshot and recipes', () => {
  it('summarises the session for the helper', async () => {
    const { engine, session, ctx, luna } = await connected({ tracks: ['Kick', 'Snare'] });
    await logic.execute(ctx, { steps: [{ action: 'addSend', args: { from: ['Kick'], to: 'Verb' } }] });
    await engine.settled();
    const snap = await logic.snapshot(luna, session);
    expect(snap.session.name).toBe('Demo');
    expect(snap.tracks.map((t) => t.name)).toEqual(['Kick', 'Snare']);
    expect(snap.tracks[0].sends).toEqual([{ slot: 0, bus: 'Verb' }]);
    expect(snap.buses.map((b) => b.name)).toEqual(['Verb']);
    expect(snap.inputs.slice(0, 2)).toEqual([{ number: '1', name: 'MIC/LINE 1', format: 'mono' }, { number: '2', name: 'MIC/LINE 2', format: 'mono' }]);
  });

  it('captures a setup as steps that validate and can be replayed', async () => {
    const { engine, session, ctx, luna } = await connected({ tracks: [] });
    await logic.execute(ctx, { steps: [
      { action: 'createTracks', args: { tracks: [{ name: 'Violin 1', input: '1' }, { name: 'Cello', input: '2' }] } },
      { action: 'addSend', args: { from: ['Violin 1', 'Cello'], to: 'Hall' } },
    ] });
    await engine.settled();
    const steps = await logic.captureRecipe(luna, session);
    expect(logic.validateSteps(steps)).toEqual([]);
    expect(steps[0]).toEqual({ action: 'createTracks', args: { tracks: [
      { name: 'Violin 1', kind: 'audio', format: 'mono', input: '1' }, { name: 'Cello', kind: 'audio', format: 'mono', input: '2' },
    ] } });
    expect(steps).toContainEqual({ action: 'addSend', args: { from: ['Violin 1', 'Cello'], to: 'Hall' } });

    let saved;
    const helper = { saveRecipe: async (r) => { saved = r; }, profile: async () => ({ recipes: { 'string quartet': { aliases: ['my usual quartet'], steps: saved.steps } } }) };
    const fresh = await connected({ tracks: [] });
    await logic.execute({ ...ctx, helper }, { steps: [{ action: 'saveRecipe', args: { name: 'string quartet' } }] });
    const results = await logic.execute({ ...fresh.ctx, helper }, { steps: [{ action: 'applyRecipe', args: { name: 'my usual quartet' } }] });
    expect(results[0].error ?? 'ok').toBe('ok');
    await fresh.engine.settled();
    expect(logic.userTracks(fresh.session).map((t) => t.name.value)).toEqual(['Violin 1', 'Cello']);
  });
});
