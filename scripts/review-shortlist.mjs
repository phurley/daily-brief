// A bounded, varied shortlist for organizer verification. No browser profile.
import fs from 'node:fs';
import { rankEvent, localDay } from '../ranking.mjs';
import { reconcileRecommendationEvents } from '../event-experiences.mjs';
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const root = option('--root', new URL('../', import.meta.url).pathname);
const doc = JSON.parse(fs.readFileSync(`${root}/events.json`));
const configRoot = new URL('../', import.meta.url).pathname;
const preferences = JSON.parse(fs.readFileSync(`${configRoot}/brief-preferences.json`));
const weights = JSON.parse(fs.readFileSync(`${configRoot}/scoring-weights.json`));
const now = Date.parse(option('--at', new Date().toISOString())), day = localDay(now);
const tomorrow = new Date(Date.parse(`${day}T12:00:00Z`) + 86400000).toISOString().slice(0, 10);
const candidates = reconcileRecommendationEvents(doc.events).filter(e =>
  (e.sources || []).some(s => ['organizer', 'ticket'].includes(s.sourceAuthority)));
const pools = [];
for (const date of [day, tomorrow]) {
  const rows = candidates.map(event => rankEvent(event, { day: date, now, preferences, weights }))
    .filter(r => !r.ineligible && r.score >= 35)
    .sort((a, b) => b.score - a.score || Date.parse(a.event.start) - Date.parse(b.event.start));
  pools.push(rows.filter(r => localDay(r.event.start) === date));
  if (date === day) pools.push(rows.filter(r => localDay(r.event.start) > tomorrow), rows.filter(r => localDay(r.event.start) < day));
}
const selected = [], seen = new Set(), seenExperience = new Set();
// Interleave lanes, so missing prices in one source cannot starve tomorrow.
for (let i = 0; i < Math.max(0, ...pools.map(p => p.length)); i++) {
  for (let lane = 0; lane < pools.length; lane++) {
    const e = pools[lane][i]?.event;
    if (!e) continue;
    const experience = `${lane}|${e.experienceId || e.seriesId || e.title + '|' + e.venue}`;
    if (seen.has(e.id) || seenExperience.has(experience)) continue;
    seen.add(e.id); seenExperience.add(experience); selected.push(e);
    if (selected.length >= Number(option('--limit', 32))) break;
  }
  if (selected.length >= Number(option('--limit', 32))) break;
}
process.stdout.write(JSON.stringify(selected));
