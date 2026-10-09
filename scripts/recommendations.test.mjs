import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { selectBestBets } from '../ranking.mjs';
const read = file => JSON.parse(fs.readFileSync(new URL(`../${file}`, import.meta.url)));
test('exported widget feed has exactly the shared selector order for its reference time', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'brief-selection-'));
  try {
    const out = path.join(dir, 'recommendations.json');
    execFileSync(process.execPath, [new URL('./select-best-bets.mjs', import.meta.url).pathname, '--out', out]);
    const feed = JSON.parse(fs.readFileSync(out));
    for (const day of feed.days) {
      const selection = selectBestBets(read('events.json').events, { day: day.date, now: Date.parse(feed.generatedAt), weights: read('scoring-weights.json'), preferences: read('brief-preferences.json') });
      assert.deepEqual(day.events.map(e => e.recommendation.occurrenceId), selection.map(r => r.occurrenceId));
      assert.ok(day.events.length <= 7);
    }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
