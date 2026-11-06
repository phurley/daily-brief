// vibe-check retries, model fallback and usage logging, with a fake fetch.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const log = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "vibe-usage-")), "usage.jsonl");
process.env.LLM_USAGE_LOG = log;
const { generateCopy, vibeModels, SECTIONS } = await import("../vibe-check/generate.mjs");

const copy = { messages: SECTIONS.map(({ section, role }) => ({ section, role, text: `Plain copy for ${section}.` })) };
const ok = (content = copy, extra = {}) => ({ ok: true, status: 200, text: async () => JSON.stringify({
  choices: [{ finish_reason: "stop", message: { content: JSON.stringify(content) } }],
  usage: { prompt_tokens: 100, completion_tokens: 50, cost: 0.0004 }, ...extra }) });
const status = (code) => ({ ok: false, status: code, text: async () => `{"error":{"code":${code}}}` });
const cutOff = { ok: true, status: 200, text: async () => JSON.stringify({
  choices: [{ finish_reason: "length", message: { content: '{"messages": [{"text": "Unterminated' } }] }) };

function fakeFetch(answers) {
  const calls = [];
  return { calls, fetchImpl: async (url, init) => { calls.push(JSON.parse(init.body)); return answers.shift(); } };
}
const quiet = { sleep: async () => {}, log: () => {} };

test("a rate limit is retried on the same model", async () => {
  const { calls, fetchImpl } = fakeFetch([status(429), ok()]);
  const result = await generateCopy([], "k", ["a/one", "b/two"], { fetchImpl, ...quiet });
  assert.equal(result.model, "a/one");
  assert.deepEqual(calls.map((c) => c.model), ["a/one", "a/one"]);
  assert.equal(calls[0].max_tokens, 4000);
  assert.deepEqual(calls[0].usage, { include: true });
});

test("three cut-off answers move to the next model", async () => {
  const { calls, fetchImpl } = fakeFetch([cutOff, cutOff, ok({ messages: [] }), ok()]);
  const result = await generateCopy([], "k", ["a/one", "b/two"], { fetchImpl, ...quiet });
  assert.equal(result.model, "b/two");
  assert.deepEqual(calls.map((c) => c.model), ["a/one", "a/one", "a/one", "b/two"]);
  assert.equal(result.generated.size, SECTIONS.length);
});

test("out of credits fails at once", async () => {
  const { calls, fetchImpl } = fakeFetch([status(402), ok()]);
  await assert.rejects(generateCopy([], "k", ["a/one", "b/two"], { fetchImpl, ...quiet }), /OpenRouter 402/);
  assert.equal(calls.length, 1);
});

test("the last error is raised when every model fails", async () => {
  const { fetchImpl } = fakeFetch(Array(6).fill(0).map(() => status(503)));
  await assert.rejects(generateCopy([], "k", ["a/one", "b/two"], { fetchImpl, ...quiet }), /OpenRouter 503/);
});

test("calls and failures land in the shared usage log", () => {
  const rows = fs.readFileSync(log, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.ok(rows.every((row) => row.caller === "vibe"));
  assert.ok(rows.some((row) => row.kind === "chat" && row.cost === 0.0004));
  assert.ok(rows.some((row) => row.kind === "error" && row.model === "a/one"));
});

test("the chain comes from models.json; env replaces its first model", () => {
  const role = { chain: [{ model: "a/one" }, { model: "b/two" }] };
  assert.deepEqual(vibeModels(role, {}), ["a/one", "b/two"]);
  assert.deepEqual(vibeModels(role, { OPENROUTER_VIBE_MODEL: "b/two" }), ["b/two"]);
  assert.deepEqual(vibeModels(), ["google/gemini-3.1-flash-lite", "google/gemini-2.5-flash-lite"]);
});
