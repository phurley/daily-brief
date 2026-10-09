#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { collectEstimate } from "./estimate.mjs";
import { reconcile } from "../starship.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PYTHON = process.env.STARSHIP_PYTHON || path.join(ROOT, "data-collect/.venv/bin/python");
const args = new Set(process.argv.slice(2));
const config = JSON.parse(fs.readFileSync(path.join(HERE, "config.json")));
if (!["shadow", "live"].includes(config.mode) || !(config.ttlHours > 0 && config.ttlHours <= 24)) throw new Error("Invalid Starship mode/TTL");
const attemptedAt = new Date();
const read = (file, fallback) => fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : fallback;
const previous = read(path.join(ROOT, "starship.json"), null);
const reviewed = read(path.join(HERE, "reviewed-evidence.json"), { evidence: [] });
const collected = spawnSync(PYTHON, [path.join(HERE, "sources.py"), path.join(HERE, "config.json"), attemptedAt.toISOString()], { encoding: "utf8", timeout: 360000, maxBuffer: 8 * 1024 * 1024 });
if (collected.status !== 0) throw new Error(collected.stderr || collected.error?.message || "Source adapters failed");
const { evidence: observed, sourceHealth } = JSON.parse(collected.stdout);
// Keep prior claims and explicit retractions, including during an outage. A new
// observation cannot silently undo a reviewed retraction or supersession.
const merged = new Map((previous?.evidence || []).map((e) => [e.id, e]));
for (const e of observed) {
  const prior = merged.get(e.id);
  merged.set(e.id, { ...e, ...(["retracted", "superseded"].includes(prior?.verification) ? { verification: prior.verification } : {}) });
}
for (const e of reviewed.evidence) merged.set(e.id, e);
for (const health of sourceHealth) {
  const prior = previous?.sourceHealth.find((s) => s.id === health.id);
  health.lastSuccessAt ||= prior?.lastSuccessAt || null;
}
const now = new Date();
const record = reconcile({ evidence: [...merged.values()], previous, mode: config.mode, ttlHours: config.ttlHours, now, sourceHealth, activeMission: config.activeMission });
record.lastAttemptAt = attemptedAt.toISOString();
if (config.communityEstimate?.enabled) {
  const { estimate, health } = await collectEstimate(record, previous, config.communityEstimate);
  record.communityEstimate = estimate;
  if (health) {
    health.lastSuccessAt ||= previous?.sourceHealth.find(s => s.id === health.id)?.lastSuccessAt || null;
    record.sourceHealth.push(health);
  } else {
    const priorHealth = previous?.sourceHealth.find(s => s.id === "reddit-estimate");
    if (priorHealth) record.sourceHealth.push(priorHealth);
  }
  if (estimate?.summary !== previous?.communityEstimate?.summary && estimate) record.whatChanged += ` Community best guess: ${estimate.summary}`;
}
const filename = now.toISOString().replaceAll(":", "-") + ".json";
record.snapshot = `starship/history/${filename}`;
const text = JSON.stringify(record, null, 2) + "\n";
// Full schema + date/target semantics validation before either atomic publish.
const validated = spawnSync(PYTHON, [path.join(HERE, "validate.py")], { input: text, encoding: "utf8" });
if (validated.status !== 0) throw new Error(validated.stderr || "Starship validation failed");
if (args.has("--dry-run")) {
  console.log(text);
} else {
  fs.mkdirSync(path.join(HERE, "history"), { recursive: true });
  fs.writeFileSync(path.join(ROOT, record.snapshot), text, { flag: "wx" });
  const temp = path.join(ROOT, `starship.json.tmp-${process.pid}`);
  fs.writeFileSync(temp, text);
  fs.renameSync(temp, path.join(ROOT, "starship.json"));
  console.log(`${record.status}; mode=${record.mode}; ${record.evidence.length} claims; ${sourceHealth.map((s) => `${s.id}=${s.state}`).join(", ")}`);
}
