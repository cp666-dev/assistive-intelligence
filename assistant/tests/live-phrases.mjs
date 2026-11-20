// Live check, against a running LUNA with an EMPTY session open (it adds tracks and buses):
//   node tests/live-phrases.mjs ../assistant-helper/smoke/out/phrases.json
// Runs the plan the helper made for each spoken phrase (smoke/phrases.py) through the same
// execute() as the panel's GO, in phrase order, and reports each step. Sets the session up like the
// phrase's snapshot first (Vocal, Kick, Snare, Hats, Ride, Overheads, a Drums bus).
import { readFileSync } from 'node:fs';
import { Luna } from '@uaudio/luna-builder-sdk';

globalThis.Luna = Luna;
await import('../logic.js');
const logic = globalThis.AddOnLogic;

const results = JSON.parse(readFileSync(process.argv[2] ?? '../assistant-helper/smoke/out/phrases.json', 'utf8'));
const luna = await Luna.connect();
const session = await luna.session();
await session.ready();
const ctx = { luna, session };
const names = () => logic.userTracks(session).map((t) => t.name.value);

if (logic.userTracks(session).length) {
  console.log(`Session "${session.name?.value ?? ''}" has tracks: open an empty session for this check.`);
  process.exit(1);
}

async function ensureKit() {
  const have = new Set(names());
  const want = [['Vocal', 'mono'], ['Kick', 'mono'], ['Snare', 'mono'], ['Hats', 'mono'], ['Ride', 'mono'], ['Overheads', 'stereo']];
  const missing = want.filter(([n]) => !have.has(n));
  if (missing.length) {
    const r = await logic.execute(ctx, { steps: [{ action: 'createTracks', args: { tracks: missing.map(([name, format]) => ({ name, format })) } }] });
    if (!r.every((x) => x.ok)) throw new Error(`kit: ${JSON.stringify(r)}`);
  }
  if (!session.buses.items.some((b) => b.name.value === 'Drums')) {
    const r = await logic.execute(ctx, { steps: [{ action: 'createBus', args: { name: 'Drums', format: 'stereo' } }] });
    if (!r.every((x) => x.ok)) throw new Error(`bus: ${JSON.stringify(r)}`);
  }
}

let failed = 0;
for (const r of results) {
  const trial = r.trials.find((t) => t.ok);
  if (!trial) {
    console.log(`SKIP  ${r.phrase} (no passing plan)`);
    continue;
  }
  if (!/^Set up mono tracks|^Add a stereo drum bus|tempo|marker|Loop bars/i.test(r.phrase)) await ensureKit();
  const t0 = Date.now();
  const out = await logic.execute(ctx, { steps: trial.steps });
  const ok = out.length === trial.steps.length && out.every((x) => x.ok);
  if (!ok) failed++;
  console.log(`${ok ? 'OK  ' : 'FAIL'}  ${String(Date.now() - t0).padStart(5)} ms  ${r.phrase}`);
  for (const x of out) console.log(`        ${x.ok ? '✓' : '✗'} ${logic.describeStep(x.step)}${x.ok ? '' : `: ${x.error}`}`);
}
console.log(`\n${failed ? `${failed} failed` : 'All ran'} in LUNA. Tracks now: ${names().join(', ')}`);
await luna.close();
process.exit(failed ? 1 : 0);
