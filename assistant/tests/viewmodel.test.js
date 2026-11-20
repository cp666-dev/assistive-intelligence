// ui/viewmodel.js: engine state → the view-model render.js draws. No LUNA, no DOM.
import { describe, expect, it } from 'vitest';
import { Luna } from '@uaudio/luna-builder-sdk';

globalThis.Luna = Luna; // logic.js and viewmodel.js are classic scripts that set globals
await import('../logic.js');
await import('../ui/viewmodel.js');
const logic = globalThis.AddOnLogic;
const VM = globalThis.AIPVM;

const entry = (id, plan, extra = {}) => [id, { plan: { id, steps: [], ...plan }, state: 'pending', alt: 0, stepStates: [], errors: [], ...extra }];
const step = (action, args) => ({ action, args });

describe('slotsVM', () => {
  it('lists newest first and numbers only pending slots, from the top', () => {
    const entries = [
      entry('a', { title: 'Oldest', steps: [step('setTempo', { bpm: 90 })] }),
      entry('b', { title: 'Notice' }, { state: 'notice' }),
      entry('c', { title: 'Middle', steps: [step('setTempo', { bpm: 100 })] }),
      entry('d', { title: 'Newest', steps: [step('setTempo', { bpm: 120 })] }),
    ];
    const out = VM.slotsVM(entries, logic, new Set());
    expect(out.map((s) => s.id)).toEqual(['d', 'c', 'b', 'a']);
    expect(out.map((s) => s.n)).toEqual([1, 2, undefined, 3]);
    expect(out[2]).toEqual({ id: 'b', kind: 'notice', title: 'Notice' });
  });

  it('skips held ids, so numbers stay put under the pointer', () => {
    const entries = [entry('a', { title: 'A' }), entry('b', { title: 'B' })];
    const out = VM.slotsVM(entries, logic, new Set(['b']));
    expect(out.map((s) => [s.id, s.n])).toEqual([['a', 1]]);
  });

  it('sets risk: destructive beats check beats ok', () => {
    const del = step('deleteTracks', { tracks: ['Scratch'] });
    const safe = step('setTempo', { bpm: 120 });
    const [d, c, o] = VM.slotsVM(
      [
        entry('ok', { steps: [safe], confidence: 0.9, notes: 'Bus created stereo.' }),
        entry('check', { steps: [safe], confidence: 0.3 }),
        entry('del', { steps: [safe, del], confidence: 0.2 }),
      ],
      logic,
      new Set(),
    );
    expect([d.risk, c.risk, o.risk]).toEqual(['destructive', 'check', 'ok']);
    expect(c.check).toBe('Not sure I heard that right');
    expect(c.note).toBeNull();
    expect(o.check).toBeNull();
    expect(o.note).toBe('Bus created stereo.');
    expect(d.steps[1]).toEqual({ text: 'Scratch', destructive: true }); // no "Delete" after the DELETES tag
  });

  it('uses the plan notes as the check text when unsure', () => {
    const [s] = VM.slotsVM([entry('a', { confidence: 0.4, notes: 'Toms isn’t in this session' })], logic, new Set());
    expect(s.check).toBe('Toms isn’t in this session');
  });

  it('builds variants and follows the chosen one', () => {
    const plan = {
      title: 'Cut the click',
      choice: 'Cues only',
      steps: [step('cutClick', { at: 'Outro', where: 'cues' })],
      alternatives: [{ title: 'Everywhere', steps: [step('cutClick', { at: 'Outro', where: 'everywhere' })] }],
    };
    const [s] = VM.slotsVM([entry('a', plan, { alt: 1 })], logic, new Set());
    expect(s.variants).toEqual({ labels: ['Cues only', 'Everywhere'], selected: 1 });
    expect(s.steps[0].text).toBe('Cut the click from “Outro” (everywhere)');
    const [t] = VM.slotsVM([entry('a', { ...plan, choice: undefined })], logic, new Set());
    expect(t.variants.labels[0]).toBe('As said');
  });

  it('cleans step text: curly quotes, no ★', () => {
    const [s] = VM.slotsVM(
      [entry('a', { steps: [step('favouriteTake', { tracks: 'armed' }), step('createBus', { name: 'Drums' })] })],
      logic,
      new Set(),
    );
    expect(s.steps.map((x) => x.text)).toEqual(['Favourite the current take on armed tracks', 'Create stereo bus “Drums”']);
  });

  it('shows running progress and where a failure stopped', () => {
    const steps = [step('setTempo', { bpm: 1 }), step('setTempo', { bpm: 2 }), step('addSend', { from: ['Lead Vox'], to: 'Plate' }), step('setTempo', { bpm: 4 })];
    const running = entry('r', { title: 'R', steps }, { state: 'running', stepStates: [{ state: 'done' }, { state: 'done' }, { state: 'running' }] });
    const failed = entry('f', { title: 'F', steps: steps.slice(0, 3) }, {
      state: 'failed',
      stepStates: [{ state: 'done' }, { state: 'done' }, { state: 'failed', detail: 'No bus called "Plate"' }],
    });
    const [f, r] = VM.slotsVM([running, failed], logic, new Set());
    expect(r.kind).toBe('running');
    expect(r.progress).toEqual({ at: 3, total: 4 });
    expect(r.steps.map((x) => x.s)).toEqual(['done', 'done', 'running', 'todo']);
    expect(r.n).toBeUndefined();
    expect(f.kind).toBe('failed');
    expect(f.failedAt).toBe(3);
    expect(f.reason).toBe('No bus called “Plate”. Steps 1–2 stay in place.');
  });

  it('collapses done, carries the fade flag, and explains invalid plans', () => {
    const out = VM.slotsVM(
      [
        entry('d', { title: 'Kick + Snare → Comp Bus' }, { state: 'done', fade: true }),
        entry('i', { title: '', heard: 'make it feel like the demo' }, { state: 'invalid', errors: ['step 1: unknown action "vibe"'] }),
      ],
      logic,
      new Set(),
    );
    expect(out[0]).toEqual({ id: 'i', kind: 'invalid', title: 'Couldn’t turn that into actions', heard: 'make it feel like the demo', reason: 'step 1: unknown action "vibe"' });
    expect(out[1]).toEqual({ id: 'd', kind: 'done', title: 'Kick + Snare → Comp Bus', fading: true });
  });
});

describe('statusVM', () => {
  const up = { helper: true, stt: 'ready', llm: 'ready', listening: true, inputs: [], since: 0 };
  const x = { lunaUp: true, hasSession: true, thinking: 0, hearingUntil: 0, now: 100000, micCount: 2, speaker: 'Talkback' };
  const word = (s, more = {}) => VM.statusVM({ ...up, ...s }, { ...x, ...more }).word;

  it('follows the precedence: LUNA, service, paused, loading, error, session, thinking, hearing, listening', () => {
    expect(word({ helper: false }, { lunaUp: false, thinking: 1 })).toBe('WAKING UP');
    expect(word({ helper: false, since: 90000 }, { thinking: 1 })).toBe('CONNECTING');
    expect(word({ helper: false, since: 0 })).toBe('OFFLINE');
    expect(word({ llm: 'paused', stt: 'loading' })).toBe('PAUSED');
    expect(word({ stt: 'loading', llm: 'error' })).toBe('WAKING UP');
    expect(word({ llm: 'error' })).toBe('OFFLINE');
    expect(word({}, { hasSession: false, thinking: 1 })).toBe('READY');
    expect(word({}, { thinking: 1, hearingUntil: 200000 })).toBe('THINKING');
    expect(word({}, { hearingUntil: 200000 })).toBe('HEARING');
    expect(word({})).toBe('LISTENING');
    expect(word({ listening: false })).toBe('READY');
  });

  it('writes the detail line and the mic limit', () => {
    expect(VM.statusVM(up, { ...x, hearingUntil: 200000 }).detail).toBe('Talkback is speaking');
    expect(VM.statusVM(up, { ...x, micCount: 1 }).detail).toBe('1 mic · room is quiet');
    expect(VM.statusVM(up, x).micsFull).toBe(false);
    expect(VM.statusVM(up, { ...x, micCount: 4 }).micsFull).toBe(true);
    expect(VM.statusVM({ ...up, helper: false }, x).detailTitle).toMatch(/Install\.command/);
  });

  it('says when an armed mic can’t be heard, and why', () => {
    const errs = ['TALKBACK 1-2: no hardware channel known; set "channels": {"TALKBACK 1-2": <0-based channel>} in config.json', 'MIC/LINE 3: audio device \'X\' not found'];
    const vm = VM.statusVM({ ...up, listening: false, audioErrors: errs }, x);
    expect(vm.word).toBe('CAN’T HEAR');
    expect(vm.detail).toBe('TALKBACK 1-2: no hardware channel known · +1 more');
    expect(vm.detailTitle).toContain('config.json');
    expect(word({ audioErrors: errs }, { hearingUntil: 200000 })).toBe('HEARING');
  });
});

describe('shortInput', () => {
  it('shortens LUNA input names for the meter bridge', () => {
    expect(VM.shortInput('TALKBACK 1')).toBe('TB 1');
    expect(VM.shortInput('MIC/LINE 3')).toBe('IN 3');
    expect(VM.shortInput('MIC/LINE HIZ 1/2')).toBe('IN 1-2');
    expect(VM.shortInput('LINE 5/6')).toBe('IN 5-6');
    expect(VM.shortInput('S/PDIF L')).toBe('S/PDIF');
    expect(VM.shortInput(null)).toBe('—');
  });
});

describe('captionVM', () => {
  const speakerFor = (input) => ({ 'TALKBACK 1': 'Talkback' })[input] ?? null;

  it('runs live, then final, then request, then rests', () => {
    const cap = { id: 'u1', text: 'send the toms', input: 'TALKBACK 1', final: false, request: false };
    expect(VM.captionVM(cap, 'hearing', speakerFor)).toEqual({ mode: 'live', source: 'Talkback', text: 'send the toms' });
    expect(VM.captionVM({ ...cap, final: true }, 'listening', speakerFor).mode).toBe('final');
    expect(VM.captionVM({ ...cap, final: true, request: true }, 'thinking', speakerFor).mode).toBe('request');
    expect(VM.captionVM({ ...cap, expired: true }, 'listening', speakerFor)).toEqual({ mode: 'rest', source: 'Quiet' });
  });

  it('falls back to the input name, and rests with the state’s label', () => {
    expect(VM.captionVM({ id: 'u2', text: 'hi', input: 'MIC/LINE 4' }, 'hearing', speakerFor).source).toBe('MIC/LINE 4');
    expect(VM.captionVM(null, 'idle', speakerFor).source).toBe('No mics');
    expect(VM.captionVM(null, 'paused', speakerFor).source).toBe('Off while paused');
    expect(VM.captionVM(null, 'waking', speakerFor).source).toBe('Starting');
    expect(VM.captionVM(null, 'offline', speakerFor).source).toBe('Offline');
    expect(VM.captionVM(null, 'offline', speakerFor, true).source).toBe('Connecting');
  });
});

describe('levels', () => {
  const tracks = [{ id: 't1' }, { id: 't2' }, { id: 't3' }];
  const inputFor = (t) => ({ t1: 'TALKBACK 1', t2: 'MIC/LINE 3' })[t.id] ?? null;

  it('maps helper levels by input name to tracks, null when silent or unknown', () => {
    const out = VM.levelsVM({ 'TALKBACK 1': -22, 'MIC/LINE 3': null }, tracks, inputFor);
    expect(out).toEqual({ t1: -22, t2: null, t3: null });
    expect(VM.markLevel(out)).toBeCloseTo(38 / 60);
    expect(VM.markLevel({ t1: null })).toBe(0);
  });
});

describe('micListVM', () => {
  const track = (id, name, type = 'audio') => ({ id, name: { value: name }, trackType: { value: type } });
  const inputs = [
    { uid: 'i1', name: 'MIC/LINE 1' }, { uid: 'i2', name: 'VIRTUAL 1' }, { uid: 'i3', name: 'TALKBACK 1-2' },
    { uid: 'i4', name: 'MON L' }, { uid: 'i5', name: 'MacBook Pro Microphone' }, { uid: 'i6', name: 'MIC/LINE 2' },
  ];

  it('offers inputs as one-click mics: talkback, then the computer mic, then interface inputs', () => {
    const vm = VM.micListVM([], new Set(), () => null, inputs);
    expect(vm.groups.map((g) => g.title)).toEqual(['Listen to track · 0 of 4', 'Listen to an input']);
    expect(vm.groups[1].items.map((i) => [i.id, i.name])).toEqual([
      ['input:i3', 'TALKBACK 1-2'], ['input:i5', 'MacBook Pro Microphone'], ['input:i1', 'MIC/LINE 1'], ['input:i6', 'MIC/LINE 2'],
    ]);
    expect(vm.note).toBe('');
  });

  it('leaves out inputs already heard, and disables new ones when full', () => {
    const armed = [1, 2, 3, 4].map((n) => track(`t${n}`, `Mic ${n}`));
    const inputFor = (t) => (t.id === 't1' ? 'TALKBACK 1-2' : null);
    const vm = VM.micListVM(armed, new Set(['t1', 't2', 't3', 't4']), inputFor, inputs);
    const fresh = vm.groups.find((g) => g.title === 'Listen to an input').items;
    expect(fresh.map((i) => i.name)).not.toContain('TALKBACK 1-2');
    expect(fresh.every((i) => i.disabled)).toBe(true);
  });

  it('says so when LUNA lists no inputs at all', () => {
    expect(VM.micListVM([], new Set(), () => null, []).note).toMatch(/no audio inputs/);
  });
});


describe('offline', () => {
  it('points someone without the helper at its installer', () => {
    expect(VM.OFFLINE_NOTICE.action).toEqual({ label: 'GET THE HELPER', url: VM.HELPER_URL });
    expect(VM.HELPER_URL).toBe('https://github.com/cp666-dev/assistive-intelligence/releases/latest');
  });

  it('says OFFLINE after 20 s without the helper, and never names a developer path', () => {
    const s = { helper: false, since: 0 };
    const x = { lunaUp: true, hasSession: true, thinking: 0, hearingUntil: 0, now: 30000, micCount: 0 };
    const vm = VM.statusVM(s, x);
    expect(vm.word).toBe('OFFLINE');
    expect(vm.detailTitle).not.toMatch(/assistant-helper\//);
  });
});
