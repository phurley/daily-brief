#!/usr/bin/env node
// Pure offline selection: no model calls, no private browser profile.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { selectBestBets, localDay } from '../ranking.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const eventsPath = option('--events', path.join(root, 'events.json'));
const out = option('--out', path.join(path.dirname(eventsPath), 'recommendations.json'));
const events = JSON.parse(fs.readFileSync(eventsPath));
const preferences = JSON.parse(fs.readFileSync(path.join(root, 'brief-preferences.json')));
const weights = JSON.parse(fs.readFileSync(path.join(root, 'scoring-weights.json')));
const now = Date.parse(option('--at', events.generatedAt));
if (!Number.isFinite(now)) throw new Error('A valid --at timestamp or events.generatedAt is required');
const day = localDay(now);
const tomorrow = new Date(Date.parse(`${day}T12:00:00Z`) + 86400000).toISOString().slice(0, 10);
const document = { schemaVersion: '1.0.0', rankingVersion: 1, generatedAt: new Date(now).toISOString(), days: [day, tomorrow].map(date => ({ date, events: selectBestBets(events.events, { day: date, now, preferences, weights }).map(row => ({ ...row.event, recommendation: { occurrenceId: row.occurrenceId, score: row.score, components: row.components, reasons: row.reasons, unknowns: row.unknowns } })) })) };
const temp = `${out}.tmp-${process.pid}`;
fs.writeFileSync(temp, JSON.stringify(document, null, 2) + '\n');
fs.renameSync(temp, out);
console.log(`Wrote ${out}: ${document.days.map(d => `${d.date}: ${d.events.length}`).join(', ')}`);
