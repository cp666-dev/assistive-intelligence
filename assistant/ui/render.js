// UA Assistive Intelligence: V2 "Strip" panel renderer.
//
// This file IS the markup spec. It builds the exact DOM that panel.css styles, from a plain
// view-model, and knows nothing about LUNA or the helper. main.js maps engine state to the
// view-model (IMPLEMENTATION.md §4) and calls the setters below. Use it as-is.
//
//   const panel = AIP.createPanel(document.getElementById('app'), handlers);
//   panel.setStatus({ state: 'hearing', word: 'HEARING', detail: 'Talkback is speaking' });
//   panel.setMics([{ id, name, input, inputFull }]);  panel.setLevels({ [id]: -18.5 });
//   panel.setContext({ cuts: [{ id, label, title }], setups: 3, setupsOpen: false });
//   panel.setCaption({ mode: 'live', source: 'TALKBACK', text: '…' });
//   panel.setSlots([...], { held: 0 });
//   panel.setPopover(null | { type: 'mics', ... } | { type: 'setups', ... });
//   panel.arrive();   // one-shot mark flash when a new suggestion lands
//
// Every setter is idempotent and only touches its own region, so the prompt field keeps focus
// and the meters can update at 10 Hz without re-rendering anything else.

(function (global) {
  'use strict';

  const SVGNS = 'http://www.w3.org/2000/svg';

  // Icons: Lucide (ISC licence) geometry, 24-unit grid, stroked with currentColor.
  const ICONS = {
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    chevronUp: '<path d="m18 15-6-6-6 6"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  };

  // The mark: UA's diamond, verbatim from LUNA.app/.../images/misc/UniversalAudioIcon.svg.
  const MARK =
    '<svg class="aip-mark" viewBox="0 0 181.25 110.25" aria-hidden="true" data-state="offline">' +
    '<path class="core" d="M11.896,56.266 L89.983,101.538 L168.074,56.266 L89.987,8.737 Z"/>' +
    '<path class="ring" fill-rule="evenodd" d="M2.099,56.41L89.987,2.914l87.887,53.496l-86.633,50.22l-1.254,0.729L2.099,56.41z M11.915,56.303l78.068,45.261l78.073-45.261L89.987,8.784L11.915,56.303z"/>' +
    '<path class="letters" d="M107.065,76.147v-0.775V62.56l39.026-1.875l-0.21-2.718h-38.816V34.19c0-6.594-5.342-11.936-11.936-11.939c-6.594,0.004-11.934,5.346-11.934,11.939v11.588l-0.012,0.104v12.086h-2.286l-4.387,6.061l6.673-0.32v10.752c-0.007,3.82-3.1,6.914-6.923,6.92c-3.819-0.006-6.913-3.1-6.919-6.92l0.055-34.095v-0.519c0,0,0.162-4.57-2.501-6.965c-3.526-3.082-10.864-1.287-10.864-1.287c9.088,0.051,8.309,7.834,8.309,7.834l-0.014,0.937v34.095c0.002,6.594,5.34,11.937,11.935,11.937c6.594,0,11.937-5.343,11.937-11.937V63.465l13.892-0.666l0.017,12.572v0.359c0,0-0.261,4.568,2.404,6.964c3.526,3.082,10.864,1.289,10.864,1.289C106.67,84.086,107.065,76.147,107.065,76.147 M88.198,57.967V46.163l0.013-0.109V34.19c0.007-3.824,3.096-6.916,6.919-6.923c3.823,0.007,6.916,3.099,6.92,6.923l0.032,23.777H88.198z"/>' +
    '</svg>';

  const METER_SEGMENTS = 24; // -60 dBFS .. 0 dBFS, 2.5 dB per segment
  const METER_HOT_FROM = 16; // segments 17-24 (-20 dBFS and up) are pumpkin

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function btn(cls, text, title) {
    const b = el('button', cls, text);
    b.type = 'button';
    if (title) b.title = title;
    return b;
  }
  function icon(name, size) {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('class', 'aip-ico aip-ico-' + size);
    s.setAttribute('aria-hidden', 'true');
    s.innerHTML = ICONS[name];
    return s;
  }

  function createPanel(root, h) {
    h = h || {};
    const call = (name, ...args) => (h[name] ? h[name](...args) : undefined);

    root.classList.add('aip');
    root.innerHTML = '';

    // ---- meter bridge
    const bridge = el('div', 'aip-bridge');
    const stateRow = el('div', 'aip-state');
    stateRow.insertAdjacentHTML('afterbegin', MARK);
    const mark = stateRow.firstChild;
    const stateText = el('div', 'aip-state-text');
    const word = el('div', 'aip-state-word');
    const detail = el('div', 'aip-state-detail');
    stateText.append(word, detail);
    const addMic = btn('aip-btn aip-add-mic', null, 'Add a mic for the assistant to listen to');
    addMic.append(icon('plus', 10), el('span', null, 'MIC'));
    addMic.onclick = (ev) => { ev.stopPropagation(); call('onAddMic'); };
    stateRow.append(stateText, addMic);
    const mics = el('div', 'aip-mics');
    bridge.append(stateRow, mics);

    // ---- context cells
    const ctx = el('div', 'aip-ctx');

    // ---- caption
    const cap = el('div', 'aip-cap');
    cap.dataset.mode = 'rest';
    const capField = el('div', 'aip-cap-field');
    const capSrc = el('span', 'aip-cap-src');
    const capText = el('div', 'aip-cap-text');
    const capSpan = el('span');
    capText.append(capSpan);
    const capTag = el('span', 'aip-tag aip-tag--request', 'REQUEST');
    capTag.hidden = true;
    capField.append(capSrc, capText, capTag);
    cap.append(capField);

    // ---- section header
    const sec = el('div', 'aip-sec');
    const secL = el('div', 'aip-sec-side');
    const secTitle = el('div', 'aip-sec-title', 'READY TO RUN');
    const secR = el('div', 'aip-sec-side aip-sec-side--end');
    const held = btn('aip-tag aip-tag--new', '', 'Show new suggestions');
    held.hidden = true;
    held.onclick = () => call('onReleaseHeld');
    const count = el('span', 'aip-sec-count is-zero', '0');
    secR.append(held, count);
    sec.append(secL, secTitle, secR);

    // ---- slots
    const slotsWrap = el('div', 'aip-slots');
    const well = el('div', 'aip-well');
    slotsWrap.append(well);
    well.addEventListener('mouseenter', () => call('onWellHover', true));
    well.addEventListener('mouseleave', () => call('onWellHover', false));

    // ---- prompt
    const form = el('form', 'aip-prompt');
    form.autocomplete = 'off';
    const input = el('input', 'aip-input');
    input.placeholder = 'Type a request, press Return';
    input.spellcheck = false;
    form.append(input);
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const t = input.value.trim();
      if (!t) return;
      if (call('onPrompt', t) !== false) input.value = '';
    };

    root.append(bridge, ctx, cap, sec, slotsWrap, form);

    // ---- popover layer
    let pop = null;
    let popAnchor = null;
    document.addEventListener('mousedown', (ev) => {
      if (!pop) return;
      if (pop.contains(ev.target) || (popAnchor && popAnchor.contains(ev.target))) return;
      call('onClosePopover');
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && pop) { ev.preventDefault(); call('onClosePopover'); }
    });

    // ================================================================ setters

    function setStatus(s) {
      root.dataset.state = s.state;
      mark.setAttribute('data-state', s.state);
      word.textContent = s.word;
      detail.textContent = s.detail || '';
      detail.title = s.detailTitle || '';
      addMic.disabled = !!s.micsFull;
    }

    /** 0..1, drives the hearing core opacity (.5..1). Call with the loudest armed input. */
    function setMarkLevel(v) {
      const o = 0.5 + 0.5 * Math.max(0, Math.min(1, v));
      mark.style.setProperty('--aip-level', o.toFixed(2));
    }

    function arrive() {
      mark.classList.remove('is-arrive');
      void mark.getBoundingClientRect();
      mark.classList.add('is-arrive');
    }
    mark.addEventListener('animationend', (ev) => {
      if (ev.animationName === 'aip-arrive') mark.classList.remove('is-arrive');
    });

    let micRows = new Map();
    /** mics: [{ id, name, input, inputFull }]. idle=true dims the meters (paused, waking, offline). */
    function setMics(list, opt) {
      opt = opt || {};
      mics.replaceChildren();
      micRows = new Map();
      mics.classList.toggle('is-idle', !!opt.idle);
      if (!list.length) {
        mics.append(el('div', 'aip-mics-none', 'No mics armed · the assistant hears nothing'));
        return;
      }
      for (const m of list) {
        const row = el('div', 'aip-mic');
        const name = el('div', 'aip-mic-name', m.name);
        name.title = m.name;
        const meter = el('div', 'aip-meter');
        const segs = [];
        for (let i = 0; i < METER_SEGMENTS; i++) {
          const s = el('i');
          if (i >= METER_HOT_FROM) s.classList.add('is-hot');
          meter.append(s);
          segs.push(s);
        }
        const inp = el('div', 'aip-mic-input', m.input);
        inp.title = m.inputFull || m.input;
        const x = btn('aip-mic-x', null, 'Stop listening to ' + m.name);
        x.append(icon('x', 10));
        x.onclick = () => call('onDisarm', m.id);
        row.append(name, meter, inp, x);
        mics.append(row);
        micRows.set(m.id, { segs, lit: 0 });
      }
    }

    /** levels: { micId: dBFS | null }. Cheap: only flips classes that changed. */
    function setLevels(levels) {
      for (const [id, r] of micRows) {
        const db = levels[id];
        const n = db == null ? 0 : Math.max(0, Math.min(METER_SEGMENTS, Math.round((db + 60) / 2.5)));
        if (n === r.lit) continue;
        for (let i = 0; i < METER_SEGMENTS; i++) r.segs[i].classList.toggle('is-lit', i < n);
        r.lit = n;
      }
    }

    let setupsCell = null;
    /** cuts: [{ id, label, title }] (0-3), setups: count, setupsOpen: bool */
    function setContext(c) {
      ctx.replaceChildren();
      const cuts = c.cuts || [];
      const cutCell = (cut, withKey) => {
        const cell = el('div', 'aip-cell aip-cell--click');
        cell.append(el('span', 'aip-cell-k', withKey ? 'CLICK' : 'CLICK'));
        const v = el('span', 'aip-cell-v' + (cut ? '' : ' is-empty'), cut ? cut.label : 'No cuts');
        if (cut && cut.title) v.title = cut.title;
        cell.append(v);
        if (cut) {
          const x = btn('aip-cell-ico', null, 'Remove this click cut');
          x.append(icon('x', 10));
          x.onclick = () => call('onRemoveCut', cut.id);
          cell.append(x);
        }
        return cell;
      };
      const first = el('div', 'aip-ctx-row');
      first.append(cutCell(cuts[0], true));
      setupsCell = btn('aip-cell aip-cell--setups' + (c.setupsOpen ? ' is-open' : ''), null, 'Saved setups');
      setupsCell.append(el('span', 'aip-cell-k', 'SETUPS'), el('span', 'aip-cell-v' + (c.setups ? '' : ' is-empty'), String(c.setups || 0)));
      const ico = el('span', 'aip-cell-ico');
      ico.append(icon(c.setupsOpen ? 'chevronUp' : 'chevronDown', 10));
      setupsCell.append(ico);
      setupsCell.onclick = (ev) => { ev.stopPropagation(); call('onToggleSetups'); };
      first.append(setupsCell);
      ctx.append(first);
      for (const cut of cuts.slice(1)) {
        const row = el('div', 'aip-ctx-row');
        row.append(cutCell(cut, true));
        ctx.append(row);
      }
    }

    /** mode: 'live' | 'final' | 'request' | 'rest'. In 'rest', source is the resting label. */
    function setCaption(c) {
      cap.dataset.mode = c.mode;
      capSrc.textContent = c.source || '';
      capSpan.textContent = c.mode === 'rest' ? '' : (c.text || '');
      capTag.hidden = c.mode !== 'request';
      requestAnimationFrame(() => capText.classList.toggle('is-overflow', capText.scrollWidth > capText.clientWidth + 1));
    }

    // ---- slots
    const slotEls = new Map();

    function stepsList(steps) {
      const ul = el('ul', 'aip-steps');
      for (const s of steps) {
        const li = el('li', 'aip-step' + (s.destructive && (s.s || 'pending') === 'pending' ? ' is-destructive' : ''));
        li.dataset.s = s.s || 'pending';
        const mk = el('span', 'aip-step-mk');
        if (s.s === 'done') mk.append(icon('check', 11));
        if (s.s === 'failed') mk.append(icon('x', 11));
        li.append(mk);
        if (s.destructive && (s.s || 'pending') === 'pending') li.append(el('span', 'aip-tag aip-tag--deletes', 'DELETES'));
        li.append(el('span', 'aip-step-t', s.text));
        ul.append(li);
      }
      return ul;
    }
    function kv(k, v) {
      const r = el('div', 'aip-kv');
      r.append(el('span', 'aip-kv-k', k), el('span', 'aip-kv-v', v));
      return r;
    }
    function dismissBtn(s) {
      const x = btn('aip-dismiss', null, 'Dismiss');
      x.append(icon('x', 11));
      x.onclick = () => call('onDismiss', s.id);
      return x;
    }

    function buildSlot(s) {
      if (s.kind === 'done') {
        const d = el('div', 'aip-slot aip-slot--done' + (s.fading ? ' is-fading' : ''));
        const i = el('span', 'aip-done-ico');
        i.append(icon('check', 12));
        d.append(i, el('span', 'aip-done-title', s.title), el('span', 'aip-done-word', 'DONE'));
        return d;
      }
      if (s.kind === 'notice') {
        const n = el('div', 'aip-notice');
        n.append(el('span', 'aip-notice-k', 'NOTE'), el('span', 'aip-notice-t', s.title));
        return n;
      }
      if (s.kind === 'notice-boxed') {
        const n = el('div', 'aip-notice aip-notice--boxed');
        const body = el('div', 'aip-notice-body');
        body.append(el('div', 'aip-notice-title', s.title), el('div', 'aip-reason', s.reason || ''));
        if (s.action) {
          const a = btn('aip-btn aip-notice-action', s.action.label, s.action.url);
          a.onclick = () => call('onOpenUrl', s.action.url);
          body.append(a);
        }
        n.append(el('span', 'aip-notice-led'), body);
        return n;
      }

      const slot = el('div', 'aip-slot' + (s.kind === 'pending' ? '' : ' aip-slot--status'));
      slot.dataset.kind = s.kind;
      slot.dataset.risk = s.risk || 'ok';
      const body = el('div', 'aip-slot-body');
      const head = el('div', 'aip-slot-head');
      if (s.kind === 'pending') head.append(el('span', 'aip-slot-n', String(s.n)));
      head.append(el('div', 'aip-slot-title', s.title));
      if (s.kind !== 'running') head.append(dismissBtn(s));
      body.append(head);

      const det = el('div', 'aip-slot-detail');
      if (s.kind === 'pending') {
        if (s.check) {
          const r = el('div', 'aip-check');
          r.append(el('span', 'aip-tag aip-tag--check', 'CHECK'), el('span', 'aip-check-why', s.check));
          det.append(r);
        }
        if (s.heard) det.append(kv('Heard', s.heard));
        if (s.variants && s.variants.labels.length > 1) {
          const seg = el('div', 'aip-seg');
          s.variants.labels.forEach((label, i) => {
            const b = btn('aip-seg-btn' + (i === s.variants.selected ? ' is-on' : ''), label);
            b.onclick = () => call('onVariant', s.id, i);
            seg.append(b);
          });
          det.append(seg);
        }
        det.append(stepsList(s.steps));
        if (s.note) det.append(kv('Note', s.note));
      } else if (s.kind === 'running' || s.kind === 'failed') {
        det.append(stepsList(s.steps));
        if (s.reason) det.append(el('div', 'aip-reason', s.reason));
      } else if (s.kind === 'invalid') {
        if (s.heard) det.append(el('div', 'aip-reason', 'Heard: ' + s.heard));
        det.append(el('div', 'aip-reason', s.reason));
      }
      body.append(det);
      slot.append(body);

      if (s.kind === 'pending') {
        const go = btn('aip-go', null, 'Run these steps');
        go.append(el('span', 'aip-go-label', 'GO'));
        if (s.n && s.n <= 9) go.append(el('span', 'aip-go-key', '⌥' + s.n));
        go.onclick = () => call('onGo', s.id);
        slot.append(go);
      } else {
        const st = el('div', 'aip-stat');
        if (s.kind === 'running') {
          st.append(el('span', 'aip-stat-big', s.progress.at + '/' + s.progress.total), el('span', 'aip-stat-small', 'RUNNING'));
        } else if (s.kind === 'failed') {
          st.append(el('span', 'aip-stat-label is-failed', 'STOPPED'), el('span', 'aip-stat-label', 'AT ' + s.failedAt));
        } else {
          st.append(el('span', 'aip-stat-label is-dim', 'CAN’T'), el('span', 'aip-stat-label is-dim', 'RUN'));
        }
        slot.append(st);
      }
      return slot;
    }

    /**
     * slots: newest first (see IMPLEMENTATION.md §4.4). opt.held: number of arrivals waiting while
     * the pointer is in the well. New ids animate in; ids that disappear animate out.
     */
    function setSlots(list, opt) {
      opt = opt || {};
      const pending = list.filter((s) => s.kind === 'pending').length;
      count.textContent = String(pending);
      count.classList.toggle('is-zero', pending === 0);
      held.hidden = !opt.held;
      held.textContent = opt.held ? '+' + opt.held + ' NEW' : '';

      const keep = new Set(list.map((s) => s.id));
      for (const [id, node] of slotEls) {
        if (!keep.has(id)) {
          slotEls.delete(id);
          node.classList.add('is-leaving');
          node.addEventListener('animationend', () => node.remove(), { once: true });
          setTimeout(() => node.remove(), 200);
        }
      }
      const frag = [];
      for (const s of list) {
        const fresh = buildSlot(s);
        const old = slotEls.get(s.id);
        if (!old && opt.animate !== false) fresh.classList.add('is-entering');
        slotEls.set(s.id, fresh);
        if (old) old.replaceWith(fresh);
        frag.push(fresh);
      }
      if (!list.length) {
        well.replaceChildren(...[...well.children].filter((c) => c.classList.contains('is-leaving')));
        const e = el('div', 'aip-empty');
        for (const line of opt.empty || []) e.append(el('div', null, line));
        well.append(e);
        return;
      }
      const leaving = [...well.children].filter((c) => c.classList.contains('is-leaving'));
      well.replaceChildren(...frag, ...leaving);
    }

    // ---- popovers
    function setPopover(p) {
      if (pop) pop.remove();
      pop = null;
      popAnchor = null;
      addMic.classList.toggle('is-open', !!p && p.type === 'mics');
      if (!p) return;

      const rootBox = root.getBoundingClientRect();
      if (p.type === 'mics') {
        pop = el('div', 'aip-pop-mics');
        popAnchor = addMic;
        const a = addMic.getBoundingClientRect();
        pop.style.top = Math.round(a.bottom - rootBox.top + 8) + 'px';
        for (const g of p.groups) {
          pop.append(el('div', 'aip-menu-head', g.title));
          for (const it of g.items) {
            const b = btn('aip-menu-item');
            b.disabled = !!it.disabled;
            b.append(el('span', 'aip-menu-name', it.name), el('span', 'aip-menu-sub', it.input || '—'));
            b.title = it.title || '';
            b.onclick = () => call('onPickMic', it.id);
            pop.append(b);
          }
        }
        if (p.note) pop.append(el('div', 'aip-menu-note', p.note));
      } else {
        pop = el('div', 'aip-pop-setups');
        popAnchor = setupsCell;
        const capBox = capField.getBoundingClientRect();
        pop.style.top = Math.round(capBox.bottom - rootBox.top + 4) + 'px';
        const head = el('div', 'aip-pop-head');
        head.append(el('span', 'aip-pop-title', 'SAVED SETUPS'), el('span', 'aip-pop-esc', 'ESC'));
        pop.append(head);
        if (!p.items.length) {
          pop.append(el('div', 'aip-pop-empty', 'No saved setups yet.'));
        } else {
          const list = el('div', 'aip-pop-list');
          for (const it of p.items) {
            const confirming = p.confirm === it.name;
            const r = el('div', 'aip-setup' + (confirming ? ' is-confirm' : ''));
            r.append(el('span', 'aip-setup-name', confirming ? 'Delete “' + it.name + '”?' : it.name));
            if (!confirming) r.append(el('span', 'aip-setup-steps', it.steps + (it.steps === 1 ? ' STEP' : ' STEPS')));
            const use = btn('aip-btn aip-setup-use' + (confirming ? '' : ' is-on'), confirming ? 'DELETE' : 'USE');
            use.onclick = () => call(confirming ? 'onConfirmDeleteSetup' : 'onUseSetup', it.name);
            const del = btn('aip-setup-del', null, confirming ? 'Keep it' : 'Delete this setup');
            del.append(icon(confirming ? 'x' : 'trash', 11));
            del.onclick = () => call(confirming ? 'onCancelDeleteSetup' : 'onDeleteSetup', it.name);
            r.append(use, del);
            list.append(r);
          }
          pop.append(list);
        }
        pop.append(el('div', 'aip-pop-hint', 'Say “save this as my drum setup” after a run to add one.'));
      }
      root.append(pop);
    }

    return {
      root, input,
      setStatus, setMarkLevel, arrive, setMics, setLevels, setContext, setCaption, setSlots, setPopover,
      focusPrompt: () => input.focus(),
    };
  }

  global.AIP = { createPanel, ICONS, METER_SEGMENTS };
})(typeof window !== 'undefined' ? window : globalThis);
