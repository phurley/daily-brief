import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  computeScore,
  computeRaw,
  contributions,
  discoverSignals,
  mergeSignals,
} from "../scoring.mjs";

const weights = JSON.parse(readFileSync(new URL("../scoring-weights.json", import.meta.url), "utf8"));

test("reproduces every stored score from the published signals", () => {
  const events = JSON.parse(readFileSync(new URL("../events.json", import.meta.url), "utf8")).events;
  let checked = 0;
  for (const event of events) {
    const signals = event?.scoring?.signals;
    const stored = event?.scoring?.score;
    if (!signals || stored == null) continue;
    assert.equal(computeScore(signals, weights), stored, event.id);
    checked += 1;
  }
  assert.ok(checked > 100, `expected many scored events, checked ${checked}`);
});

test("neutral event scores the base and clamping holds", () => {
  assert.equal(computeScore({}, weights), 50);
  assert.equal(computeScore({ distinctive: 1, eclectic: 1, technical_science: 1, live: 1, funny: 1, artsy: 1, progressive: 1 }, weights), 100);
  assert.equal(computeScore({ recurring: 1, sporting: 1, large_venue: 1, craft_fair_shopping: 1, religious: 1 }, weights), 0);
});

test("contributions are signed and ordered by magnitude", () => {
  const rows = contributions({ distinctive: 1, recurring: 0.5 }, weights);
  const distinctive = rows.find((row) => row.name === "distinctive");
  const recurring = rows.find((row) => row.name === "recurring");
  assert.equal(distinctive.delta, 0.24);
  assert.equal(recurring.delta, -0.075);
  assert.ok(rows.indexOf(distinctive) < rows.indexOf(recurring));
});

test("new signals are discovered and added at zero weight", () => {
  const events = [{ scoring: { signals: { distinctive: 0.5, brand_new: 0.9 } } }];
  assert.deepEqual(discoverSignals(events), ["brand_new", "distinctive"]);
  const merged = mergeSignals(weights, discoverSignals(events));
  assert.equal(merged.added.length, 1);
  assert.equal(merged.signals.brand_new.weight, 0);
  assert.equal(computeScore({ brand_new: 1 }, { ...weights, signals: merged.signals }), 50);
});

test("raw value reflects the weighted sum before clamping", () => {
  assert.equal(computeRaw({ distinctive: 1 }, weights), 0.74);
});