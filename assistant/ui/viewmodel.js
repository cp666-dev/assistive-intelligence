// UA Assistive Intelligence: engine state → view-model for render.js.
//
// Pure functions, no DOM, no LUNA calls. They take the shapes main.js already keeps today
// (the `status` object, the `plans` Map entries, transcript events, click cuts, recipes) and
// return exactly what AIP.createPanel's setters expect. Every user-visible string the panel
// shows lives here or in render.js; do not reword them elsewhere.

(function (global) {
  'use strict';

  const MAX_MICS = 4;

  // ---------------------------------------------------------------- status (§4.1)

  /**
   * s: main.js `status` { helper, stt, llm, listening, inputs, audioErrors, since }
   * x: { lunaUp, hasSession, thinking (count), hearingUntil (ms), now (ms), micCount, speaker }
   */
  function statusVM(s, x) {
    const n = x.micCount;
    const full = n >= MAX_MICS;
    if (!x.lunaUp) return { state: 'waking', word: 'WAKING UP', detail: 'Waiting for LUNA', micsFull: full };
    if (!s.helper) {
      const long = x.now - s.since > 20000;
      return long
        ? { state: 'offline', word: 'OFFLINE', detail: 'Assistant service isn’t running', detailTitle: 'The helper app does the listening and planning on this Mac. Not installed yet? Get it from the link in the panel. Installed? It starts at login; run Install.command again to repair it.', micsFull: full }
        : { state: 'offline', word: 'CONNECTING', detail: 'Reaching the assistant service', micsFull: full };
    }
    if (s.llm === 'paused') return { state: 'paused', word: 'PAUSED', detail: 'Giving LUNA memory · back by itself', micsFull: full };
    if (s.stt === 'loading' || s.llm === 'loading') return { state: 'waking', word: 'WAKING UP', detail: 'Loading speech and planner · about 10 s', micsFull: full };
    if (s.stt === 'error' || s.llm === 'error') return { state: 'offline', word: 'OFFLINE', detail: 'Model failed to load · see helper log', micsFull: full };
    if (!x.hasSession) return { state: 'idle', word: 'READY', detail: 'No session open', micsFull: full };
    if (x.thinking > 0) return { state: 'thinking', word: 'THINKING', detail: 'Planning the last request', micsFull: full };
    if (s.listening && x.now < x.hearingUntil) return { state: 'hearing', word: 'HEARING', detail: `${x.speaker || 'Someone'} is speaking`, micsFull: full };
    if (s.audioErrors?.length) {
      // e.g. "TALKBACK 1-2: no hardware channel known; set …" → the part before the advice
      const first = s.audioErrors[0].split(';')[0];
      const more = s.audioErrors.length > 1 ? ` · +${s.audioErrors.length - 1} more` : '';
      return { state: s.listening ? 'listening' : 'idle', word: 'CAN’T HEAR', detail: first + more, detailTitle: s.audioErrors.join('\n'), micsFull: full };
    }
    if (s.listening) return { state: 'listening', word: 'LISTENING', detail: `${n} mic${n === 1 ? '' : 's'} · room is quiet`, micsFull: full };
    return { state: 'idle', word: 'READY', detail: 'Arm a mic, or type below', micsFull: full };
  }

  /** Meters dim (and stay unlit) when the assistant isn't actually listening. */
  const micsIdle = (state) => state === 'offline' || state === 'waking' || state === 'paused';

  // ---------------------------------------------------------------- mics (§4.2)

  /** "TALKBACK 1" → "TB 1", "MIC/LINE 3" → "IN 3", "MIC/LINE HIZ 1/2" → "IN 1-2", else first 6 chars. */
  function shortInput(name) {
    if (!name) return '—';
    const tb = /TALKBACK\s*(\d+)/i.exec(name);
    if (tb) return `TB ${tb[1]}`;
    const num = /(\d+)(?:\s*[-/]\s*(\d+))?\s*$/.exec(name);
    if (num) return `IN ${num[1]}${num[2] ? `-${num[2]}` : ''}`;
    return name.slice(0, 6).toUpperCase();
  }

  /** tracks: armed LUNA tracks in session order; inputFor(track) → LUNA input name or null. */
  function micsVM(tracks, inputFor) {
    return tracks.map((t) => {
      const full = inputFor(t);
      return { id: t.id, name: t.name.value, input: shortInput(full), inputFull: full || 'No input' };
    });
  }

  /** helper `levels` event ({ inputName: dBFS|null }) → { trackId: dBFS|null } for setLevels. */
  function levelsVM(levels, tracks, inputFor) {
    const out = {};
    for (const t of tracks) {
      const v = levels[inputFor(t)];
      out[t.id] = typeof v === 'number' && isFinite(v) ? v : null;
    }
    return out;
  }

  /** Loudest armed input, 0..1, for panel.setMarkLevel while hearing. */
  function markLevel(levelsByTrack) {
    const vals = Object.values(levelsByTrack).filter((v) => v != null);
    if (!vals.length) return 0;
    return Math.max(0, Math.min(1, (Math.max(...vals) + 60) / 60));
  }

  // Device inputs nobody speaks into (returns, digital ins, monitor buses): not offered as mics.
  const NOT_A_MIC = /^(MON|VIRTUAL|S\/PDIF|SPDIF|ADAT|AUX|CUE|HP|RETURN|LOOPBACK)/i;
  /** Talkback first, then a computer's own mic, then interface inputs, in LUNA's order. */
  function micRank(name) {
    if (/talkback/i.test(name)) return 0;
    if (/microphone/i.test(name)) return 1;
    if (/^(MIC|LINE|INPUT|IN\b)/i.test(name)) return 2;
    return 3;
  }

  /**
   * Mic list popover groups. tracks: all user tracks; armedIds: Set; inputFor as above;
   * inputs: luna.audioInputs() (each { uid, name }) for "a new mic on an input", which makes an
   * input-only Assistant track in one click, so an empty session needs no typing to start.
   */
  function micListVM(tracks, armedIds, inputFor, inputs = []) {
    const audio = tracks.filter((t) => t.trackType.value === 'audio' && !armedIds.has(t.id));
    const withIn = audio.filter((t) => inputFor(t));
    const noIn = audio.filter((t) => !inputFor(t));
    const full = armedIds.size >= MAX_MICS;
    const heard = new Set(tracks.filter((t) => armedIds.has(t.id)).map(inputFor).filter(Boolean));
    const fresh = inputs
      .filter((i) => i.name && !NOT_A_MIC.test(i.name) && !heard.has(i.name))
      .map((i, n) => ({ i, n, r: micRank(i.name) }))
      .sort((a, b) => a.r - b.r || a.n - b.n)
      .map(({ i }) => ({ id: `input:${i.uid}`, name: i.name, input: shortInput(i.name), disabled: full,
        title: `Listen to ${i.name}: adds an input-only track “Assistant ${i.name}” (no output)` }));
    const groups = [];
    groups.push({
      title: `Listen to track · ${armedIds.size} of ${MAX_MICS}`,
      items: withIn.map((t) => ({ id: t.id, name: t.name.value, input: inputFor(t), title: `${t.name.value} · ${inputFor(t)}` })),
    });
    if (fresh.length) groups.push({ title: 'Listen to an input', items: fresh });
    if (noIn.length) groups.push({ title: 'No input', items: noIn.map((t) => ({ id: t.id, name: t.name.value, disabled: true, title: 'Give this track an input first' })) });
    const note = withIn.length || fresh.length ? '' : 'LUNA lists no audio inputs. Check its audio device in Settings.';
    return { type: 'mics', groups: groups.filter((g) => g.items.length || g === groups[0]), note };
  }

  // ---------------------------------------------------------------- click cuts + setups (§4.6)

  /** cut: { tick, where: 'cues'|'everywhere', label }, bar: number from tl.barBeat(cut.tick).bar */
  function cutVM(cut, bar, index) {
    const where = cut.where === 'cues' ? 'cues' : 'all';
    const named = cut.label && !/^\d+$/.test(cut.label);
    return {
      id: `${cut.tick}:${index}`,
      cut,
      label: named ? `Off from “${cut.label}” (bar ${bar}) · ${where}` : `Off from bar ${bar} · ${where}`,
      title: cut.where === 'cues' ? `Headphones only, from bar ${bar}` : `Headphones and control room, from bar ${bar}`,
    };
  }

  function setupsVM(recipes, confirmName) {
    return { type: 'setups', confirm: confirmName || null, items: recipes.map(([name, r]) => ({ name, steps: r.steps?.length ?? 0 })) };
  }

  // ---------------------------------------------------------------- caption (§4.5)

  const REST = { offline: 'Offline', waking: 'Starting', paused: 'Off while paused', idle: 'No mics', listening: 'Quiet', hearing: 'Quiet', thinking: 'Quiet' };

  /**
   * cap: the last transcript event kept by main.js ({ id, text, final, input, request, expired })
   * or null. speakerFor(input) → armed track name for that input (fallback: the input name).
   */
  function captionVM(cap, state, speakerFor, connecting) {
    if (!cap || cap.expired) return { mode: 'rest', source: connecting ? 'Connecting' : REST[state] || 'Quiet' };
    const mode = cap.request ? 'request' : cap.final ? 'final' : 'live';
    return { mode, source: speakerFor(cap.input) || cap.input || '', text: cap.text };
  }

  // ---------------------------------------------------------------- slots (§4.4)

  /** describeStep() text → panel text: curly quotes, no ★ glyph, no leading "Delete" after a DELETES tag. */
  function stepText(text, destructive) {
    let t = String(text).replace(/^★\s*/, '').replace(/"([^"]*)"/g, '“$1”');
    if (destructive) t = t.replace(/^Delete\s+/, '');
    return t;
  }

  /**
   * entries: [...plans.entries()] from main.js, oldest first (Map insertion order).
   * Returns render.js slots, newest first. Pending slots are numbered 1..n from the top; those
   * numbers are the ⌥1..⌥9 keys. heldIds: Set of plan ids held back while the pointer is in the well.
   */
  function slotsVM(entries, logic, heldIds) {
    const out = [];
    let n = 0;
    for (const [id, p] of [...entries].reverse()) {
      if (heldIds && heldIds.has(id)) continue;
      const plan = p.plan;
      if (p.state === 'notice') { out.push({ id, kind: 'notice', title: plan.title }); continue; }
      if (p.state === 'done') { out.push({ id, kind: 'done', title: plan.title ?? 'Done', fading: !!p.fade }); continue; }
      if (p.state === 'invalid') {
        out.push({ id, kind: 'invalid', title: plan.title || 'Couldn’t turn that into actions', heard: plan.heard || null, reason: p.errors?.[0] || 'No LUNA action matches. Name the tracks or the effect.' });
        continue;
      }
      const steps = p.alt === 0 || !plan.alternatives ? plan.steps : plan.alternatives[p.alt - 1].steps;
      if (p.state === 'pending') {
        n++;
        const low = (plan.confidence ?? 1) < 0.5;
        const destructive = steps.some((s) => logic.isDestructive(s));
        const variants = plan.alternatives?.length ? { labels: [plan.choice ?? 'As said', ...plan.alternatives.map((a) => a.title)], selected: p.alt } : null;
        out.push({
          id, n, kind: 'pending',
          risk: destructive ? 'destructive' : low ? 'check' : 'ok',
          title: plan.title ?? 'Suggestion',
          check: low ? plan.notes || 'Not sure I heard that right' : null,
          note: !low && plan.notes ? plan.notes : null,
          heard: plan.heard || null,
          variants,
          steps: steps.map((s) => {
            const d = logic.isDestructive(s);
            return { text: stepText(logic.describeStep(s), d), destructive: d };
          }),
        });
        continue;
      }
      // running / failed: per-step progress from p.stepStates ({ state: 'todo'|'running'|'done'|'failed', detail })
      const states = steps.map((_, i) => p.stepStates[i]?.state ?? 'todo');
      const vmSteps = steps.map((s, i) => ({ s: states[i], text: stepText(logic.describeStep(s), false) }));
      if (p.state === 'running') {
        const running = states.indexOf('running');
        const done = states.filter((x) => x === 'done').length;
        out.push({ id, kind: 'running', title: plan.title ?? 'Suggestion', steps: vmSteps, progress: { at: running >= 0 ? running + 1 : Math.min(done + 1, steps.length), total: steps.length } });
      } else if (p.state === 'failed') {
        const at = states.indexOf('failed');
        const kept = states.filter((x) => x === 'done').length;
        const why = (p.stepStates[at]?.detail || 'It stopped').replace(/"([^"]*)"/g, '“$1”').replace(/\.?\s*$/, '.');
        const keep = kept === 0 ? '' : kept === 1 ? ' Step 1 stays in place.' : ` Steps 1–${kept} stay in place.`;
        out.push({ id, kind: 'failed', title: plan.title ?? 'Suggestion', steps: vmSteps, failedAt: at + 1, reason: why + keep });
      }
    }
    return out;
  }

  /** Slot list when there are no slots, per state (§4.4). */
  function emptyLines(state, connecting) {
    if (connecting) return ['Connecting to the assistant service. This takes a few seconds after login.'];
    if (state === 'waking') return ['Typed requests are kept and planned as soon as it’s ready.'];
    return ['Requests heard in the room become numbered slots here, newest first.', 'Nothing runs until you press GO, or ⌥ and the slot number.'];
  }

  /** Long-offline replaces the empty lines with this boxed notice (only when nothing else is listed). */
  const HELPER_URL = 'https://github.com/cp666-dev/assistive-intelligence/releases/latest';
  const OFFLINE_NOTICE = {
    id: 'offline', kind: 'notice-boxed', title: 'The helper app isn’t running',
    reason: 'This panel needs its free helper app, which does the listening and planning on your Mac. Not installed yet? Download the installer. Already installed? It starts by itself at login; run Install.command again to repair it.',
    action: { label: 'GET THE HELPER', url: HELPER_URL },
  };

  global.AIPVM = { MAX_MICS, HELPER_URL, statusVM, micsIdle, shortInput, micsVM, levelsVM, markLevel, micListVM, cutVM, setupsVM, captionVM, stepText, slotsVM, emptyLines, OFFLINE_NOTICE };
})(typeof window !== 'undefined' ? window : globalThis);
