#!/usr/bin/env node
/**
 * Daily Brief vibe-check: write dated editorial copy for every section.
 *
 * Reads the published JSON documents plus the *programmatic* almanac/sky
 * values that the page computes (via the same almanac-calc.mjs / on-this-date.mjs
 * modules), asks a low-cost OpenRouter model for the section copy, prunes
 * messages older than the retention window, and rewrites repo-root vibe.json.
 *
 * Usage:
 *   node generate.mjs                 # generate + write vibe.json
 *   node generate.mjs --dry-run       # call the model, print, do not write
 *   node generate.mjs --context-only  # print the model input, no API call
 *   node generate.mjs --print         # also print each generated message
 *
 * Environment (read from ./​.env if present):
 *   OPENROUTER_API_KEY        required
 *   OPENROUTER_VIBE_MODEL     default google/gemini-2.5-flash-lite
 *   VIBE_RETENTION_HOURS      default 24
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { computeAlmanacDay } from "../almanac-calc.mjs";
import { getOnThisDate } from "../on-this-date.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const VIBE_PATH = path.join(ROOT, "vibe.json");
const CACHE_DIR = path.join(HERE, "cache");

const TIME_ZONE = "America/Detroit";
const FALLBACK_LOCATION = { name: "Canton", region: "Michigan", latitude: 42.3086, longitude: -83.4824, timeZone: TIME_ZONE };
const DEFAULT_MODEL = "google/gemini-2.5-flash-lite";
const ENDPOINT = "https://openrouter.ai/api/v1/chat/completions";
const MAX_TEXT = 420;
const SUMMARY_CHARS = 260;

// Every (section, role) the page renders. masthead/eyebrow is deterministic.
const SECTIONS = [
  { section: "masthead", role: "headline", guide: "A magazine-cover headline for the whole day (max ~70 chars). Lead with a concrete image or unlikely juxtaposition from the day. Never a weather label or a category word." },
  { section: "masthead", role: "summary", guide: "2 sentences framing the day: the weather feel plus the single most interesting local or science thread." },
  { section: "weather", role: "note", guide: "One sentence with the day's conditions and high/low, mentioning rain chance only if notable." },
  { section: "almanac", role: "note", guide: "One sentence with sunrise, sunset, and the moon phase/illumination from the computed almanac." },
  { section: "today", role: "section-heading", guide: "A short heading for today's nearby-and-notable events (max ~45 chars). Name the flavor of the day, not the category." },
  { section: "today", role: "recommendation", guide: "1-2 sentences recommending the strongest in-person option(s) today, naming the event and time." },
  { section: "calendar", role: "note", guide: "One sentence about the most human calendar item in the next few days (birthday, appointment, deadline)." },
  { section: "news", role: "section-heading", guide: "A short heading naming the specific local thread or decision (max ~55 chars). Never 'Local Stories' or 'In the News'." },
  { section: "news", role: "summary", guide: "2 sentences on the most consequential local story, with the concrete decision or date." },
  { section: "plan-ahead", role: "section-heading", guide: "A short heading for tomorrow / the days ahead (max ~50 chars)." },
  { section: "plan-ahead", role: "recommendation", guide: "1-2 sentences recommending specific upcoming options by name." },
  { section: "science-technology", role: "section-heading", guide: "A short heading for the science/technology roundup (max ~55 chars). Point at the finding, not the field." },
  { section: "science-technology", role: "summary", guide: "2 sentences on the two best science/tech stories, with the specific finding or milestone." },
  { section: "starship", role: "note", guide: "One sentence on the current Starship launch estimate, or say the schedule is unset if there is no estimate." },
  { section: "footer", role: "note", guide: "One warm, aphoristic closing line. No advice clichés, no emoji." },
];

const RESPONSE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["messages"],
  properties: {
    messages: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["section", "role", "text"],
        properties: {
          section: { type: "string", enum: [...new Set(SECTIONS.map((s) => s.section))] },
          role: { type: "string", enum: [...new Set(SECTIONS.map((s) => s.role))] },
          text: { type: "string" },
        },
      },
    },
  },
};

// --------------------------------------------------------------------------- //
// Small helpers
// --------------------------------------------------------------------------- //

function loadEnv(file = path.join(HERE, ".env")) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const index = line.indexOf("=");
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

// Required documents must parse: a truncated file means the collector is
// mid-publish, and we would rather skip an hour than write copy from nothing.
function readRequiredJson(file) {
  const data = readJson(file, null);
  if (data === null) throw new Error(`${path.basename(file)} is missing or not valid JSON`);
  return data;
}

function dateKeyInZone(date = new Date(), timeZone = TIME_ZONE) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function shiftDate(key, days) {
  const date = new Date(`${key}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function eyebrow(dateKey, location) {
  const label = new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, weekday: "long", month: "long", day: "numeric" }).format(new Date(`${dateKey}T12:00:00Z`));
  return `${label} · ${location.name}, ${location.region}`;
}

function truncate(text, max = SUMMARY_CHARS) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length <= max ? value : `${value.slice(0, max - 1).trimEnd()}…`;
}

function datePart(value) {
  return typeof value === "string" && value.includes("T") ? value.slice(0, 10) : value || "";
}

function idFor(dateKey, section, role) {
  const shortRole = role === "section-heading" ? "heading" : role;
  return `${section}-${shortRole}-${dateKey}`;
}

function atomicWrite(file, text) {
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, text);
  fs.renameSync(tmp, file);
}

// --------------------------------------------------------------------------- //
// Context: published data + programmatic almanac/sky
// --------------------------------------------------------------------------- //

function buildContext(dateKey) {
  const tomorrow = shiftDate(dateKey, 1);
  const weather = readRequiredJson(path.join(ROOT, "weather.json"));
  const calendar = readRequiredJson(path.join(ROOT, "calendar.json"));
  const events = readRequiredJson(path.join(ROOT, "events.json"));
  const news = readRequiredJson(path.join(ROOT, "news.json"));
  const geeknews = readJson(path.join(ROOT, "geeknews.json"), { stories: [] });

  const weatherLocation = weather?.location;
  const location = weatherLocation
    ? { name: weatherLocation.name, region: weatherLocation.region, latitude: weatherLocation.latitude, longitude: weatherLocation.longitude, timeZone: weatherLocation.timeZone || TIME_ZONE }
    : FALLBACK_LOCATION;

  const today = weather?.daily?.find((day) => day.date === dateKey) || weather?.daily?.[0] || null;
  const tomorrowForecast = weather?.daily?.find((day) => day.date === tomorrow) || null;

  // The same computations the page runs, so the copy matches the rendered UI.
  let almanac = null;
  try {
    almanac = computeAlmanacDay(dateKey, location);
  } catch (error) {
    console.warn(`almanac computation failed: ${error.message}`);
  }
  let onThisDate = null;
  try {
    onThisDate = getOnThisDate(dateKey, location);
  } catch (error) {
    console.warn(`on-this-date computation failed: ${error.message}`);
  }

  const horizon = shiftDate(dateKey, 4);
  const calendarItems = (calendar.items || [])
    .filter((item) => {
      const keys = [item.date, datePart(item.startTime), datePart(item.endTime)].filter(Boolean);
      if (keys.some((key) => key >= dateKey && key <= horizon)) return true;
      return Boolean(item.recurrence);
    })
    .slice(0, 12)
    .map((item) => ({
      title: item.title,
      when: [item.date || datePart(item.startTime), item.startTime, item.recurrence?.frequency].filter(Boolean).join(" "),
      person: item.person || undefined,
      location: item.location || undefined,
    }));

  const eventFor = (key) =>
    (events.events || [])
      .filter((event) => datePart(event.start) === key)
      .sort((a, b) => (b.score || 0) - (a.score || 0) || (a.distanceMiles ?? 99) - (b.distanceMiles ?? 99))
      .slice(0, 10)
      .map((event) => ({
        title: event.title,
        time: event.start,
        venue: event.venue,
        city: event.city,
        price: event.price,
        distanceMiles: event.distanceMiles,
        category: event.category,
        summary: truncate(event.summary),
      }));

  const stories = (news.stories || []).slice(0, 6).map((story) => ({
    title: story.title,
    source: story.source?.name,
    summary: truncate(story.summary),
  }));

  const geekStories = (geeknews.stories || []).slice(0, 4).map((story) => ({
    title: story.title,
    summary: truncate(story.summary),
    localConnection: story.localConnection || undefined,
  }));
  const starship = geeknews.starshipEstimatedLaunch || null;

  const showers = (onThisDate?.skyEvents?.activeMeteorShowers || []).map((s) => `${s.name} (peak ${s.peak.slice(0, 10)}, ZHR ${s.zhr})`);
  const eclipse = onThisDate?.skyEvents?.upcomingEclipses?.[0];

  return {
    date: dateKey,
    location: `${location.name}, ${location.region}`,
    weather: {
      current: weather?.current ? { temperatureF: weather.current.temperatureF, condition: weather.current.conditionText } : null,
      today: today ? { condition: today.conditionText, highF: today.highF, lowF: today.lowF, precipitationChancePercent: today.precipitationChancePercent, narrative: today.narrative } : null,
      tomorrow: tomorrowForecast ? { condition: tomorrowForecast.conditionText, highF: tomorrowForecast.highF, lowF: tomorrowForecast.lowF, precipitationChancePercent: tomorrowForecast.precipitationChancePercent } : null,
    },
    almanac: almanac
      ? { sunrise: almanac.sunrise, sunset: almanac.sunset, solarNoon: almanac.solarNoon, daylightMinutes: almanac.daylightMinutes, moon: almanac.moon }
      : null,
    sky: onThisDate
      ? {
          season: onThisDate.season,
          daylightChangeMinutes: onThisDate.daylight?.changeFromYesterdayMinutes,
          nextEquilux: onThisDate.equilux?.next,
          nextMoonPhases: onThisDate.moon?.nextPhases,
          activeMeteorShowers: showers,
          nextEclipse: eclipse ? { kind: eclipse.kind, type: eclipse.type, inDays: eclipse.inDays } : null,
        }
      : null,
    calendar: calendarItems,
    eventsToday: eventFor(dateKey),
    eventsTomorrow: eventFor(tomorrow),
    news: stories,
    geeknews: { starship: starship ? { estimateLabel: starship.estimateLabel, summary: truncate(starship.summary) } : null, stories: geekStories },
  };
}

// --------------------------------------------------------------------------- //
// Prompt + model call
// --------------------------------------------------------------------------- //

// House voice, in the model's own context: short, concrete, a little wry.
const VOICE_EXAMPLES = [
  "masthead/headline: Warm clouds, tagged monarchs, and Great Lakes jazz with a pulse",
  "masthead/summary: An overcast but warm Saturday holds a full menu: release monarchs, catch free Detroit street-festival energy, or stay close for food and music in Canton. The local civic thread is a set of Ypsilanti hearings; science has a fresh invasive-species early warning.",
  "today/section-heading: Make the weekend feel alive",
  "today/recommendation: The easy answer is Canton's Brews, Brats and Bands at Preservation Park from 6-9 PM, only five miles away. For a more unusual night, the H.O.M.E.S. Project turns Great Lakes depth data into jazz and poetry at 6:30 PM.",
  "news/section-heading: Ypsilanti's next set of public choices",
  "news/summary: Ypsilanti has scheduled September 15 hearings on data-center standards, walking and housing changes, and planned-unit-development reforms; written comments are due by 4 PM September 14.",
  "plan-ahead/section-heading: Sunday has good reasons to get out",
  "science-technology/section-heading: Small signals, big stakes",
  "footer/note: Find a pocket of wonder, then give it the afternoon.",
];

const ANTI_PATTERNS = [
  "weather-label headlines (\"Rainy Sunday\")",
  "category headings (\"Local and Nearby Happenings\", \"Science and Technology Updates\", \"Local Stories of Interest\", \"Looking Ahead\")",
  "\"This week's newsletter highlights\" or any meta-commentary about the brief itself",
  "exclamation points, emoji, hashtags, markdown, or second-person filler",
];

function buildMessages(context) {
  const system = [
    "You are the staff writer for a small, calm local daily brief for Canton, Michigan.",
    "Write like a thoughtful local editor: warm, specific, practical, quietly confident.",
    "Use only facts present in the context. Never invent names, times, or figures. When a fact is missing, stay general rather than guess.",
    "Recommendations must name real options from the context, with their time.",
    "Return every requested section/role exactly once.",
    `Avoid: ${ANTI_PATTERNS.join("; ")}.`,
  ].join(" ");

  const spec = SECTIONS.map(({ section, role, guide }) => `- ${section} / ${role}: ${guide}`).join("\n");

  const user = [
    `Write the editorial copy for ${context.date} (${context.location}).`,
    "",
    "Sections to write:",
    spec,
    "",
    "The masthead eyebrow is generated separately; do not write it.",
    "",
    "Voice reference (examples of the desired register, not facts to reuse):",
    ...VOICE_EXAMPLES.map((line) => `  ${line}`),
    "",
    "Context (the facts you may use):",
    "```json",
    JSON.stringify(context, null, 2),
    "```",
  ].join("\n");

  return [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
}

async function callModel(messages, apiKey, model) {
  const body = {
    model,
    messages,
    temperature: 0.6,
    response_format: { type: "json_schema", json_schema: { name: "vibe", strict: true, schema: RESPONSE_SCHEMA } },
  };
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "HTTP-Referer": "https://phurley.github.io/daily-brief/",
      "X-Title": "Daily Brief vibe-check",
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`OpenRouter ${response.status}: ${truncate(text, 300)}`);
  }
  const payload = JSON.parse(text);
  const content = payload?.choices?.[0]?.message?.content;
  if (!content) throw new Error(`model returned no content: ${truncate(text, 300)}`);
  return { data: parseJson(content), usage: payload.usage || null };
}

function parseJson(content) {
  let text = String(content).trim();
  if (text.startsWith("```")) {
    text = text.replace(/^```[a-zA-Z]*\n?/, "").replace(/\n?```$/, "");
  }
  return JSON.parse(text);
}

// --------------------------------------------------------------------------- //
// Validation + merge
// --------------------------------------------------------------------------- //

function validateGenerated(messages) {
  const byPair = new Map();
  for (const item of Array.isArray(messages) ? messages : []) {
    if (!item || typeof item !== "object") continue;
    const key = `${item.section}/${item.role}`;
    if (!byPair.has(key)) byPair.set(key, item);
  }
  const missing = [];
  const problems = [];
  for (const { section, role } of SECTIONS) {
    const item = byPair.get(`${section}/${role}`);
    if (!item) {
      missing.push(`${section}/${role}`);
      continue;
    }
    const text = String(item.text || "").replace(/\s+/g, " ").trim();
    if (!text) problems.push(`${section}/${role}: empty`);
    else if (text.length > MAX_TEXT) problems.push(`${section}/${role}: too long (${text.length})`);
    else if (/[*_#`]|\]\(/.test(text)) problems.push(`${section}/${role}: contains markdown`);
  }
  if (missing.length || problems.length) {
    throw new Error(`invalid model output: ${[...missing.map((m) => `missing ${m}`), ...problems].join("; ")}`);
  }
  return byPair;
}

function buildMessagesFile(dateKey, generated) {
  return SECTIONS.map(({ section, role }, index) => ({
    id: idFor(dateKey, section, role),
    date: dateKey,
    section,
    role,
    text: String(generated.get(`${section}/${role}`).text).replace(/\s+/g, " ").trim(),
    order: index,
  }));
}

function mergeMessages(existing, dateKey, fresh) {
  const retentionHours = Number(process.env.VIBE_RETENTION_HOURS || 24);
  const cutoffKey = dateKeyInZone(new Date(Date.now() - retentionHours * 3600_000));
  const sectionOrder = new Map([[idFor(dateKey, "masthead", "eyebrow"), -1], ...SECTIONS.map(({ section, role }, index) => [idFor(dateKey, section, role), index])]);
  const kept = (existing?.messages || []).filter((message) => message?.date >= cutoffKey && message?.date !== dateKey);
  const merged = [...kept, ...fresh];
  merged.sort((a, b) => (a.date === b.date ? (sectionOrder.get(a.id) ?? 999) - (sectionOrder.get(b.id) ?? 999) : a.date.localeCompare(b.date)));
  return merged;
}

// --------------------------------------------------------------------------- //
// Main
// --------------------------------------------------------------------------- //

async function main() {
  loadEnv();
  const args = new Set(process.argv.slice(2));
  const dateKey = dateKeyInZone(new Date());

  const context = buildContext(dateKey);
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  atomicWrite(path.join(CACHE_DIR, `context-${dateKey}.json`), JSON.stringify(context, null, 2) + "\n");

  if (args.has("--context-only")) {
    process.stdout.write(JSON.stringify(context, null, 2) + "\n");
    return 0;
  }

  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey) throw new Error("OPENROUTER_API_KEY is not set (see .env.example)");
  const model = process.env.OPENROUTER_VIBE_MODEL || DEFAULT_MODEL;

  const messages = buildMessages(context);
  const { data, usage } = await callModel(messages, apiKey, model);
  const generated = validateGenerated(data.messages);

  const eyebrowMessage = { id: idFor(dateKey, "masthead", "eyebrow"), date: dateKey, section: "masthead", role: "eyebrow", text: eyebrow(dateKey, FALLBACK_LOCATION), order: -1 };
  const fresh = [eyebrowMessage, ...buildMessagesFile(dateKey, generated)];

  const existing = readJson(VIBE_PATH, { schemaVersion: "1.0.0", generatedAt: null, messages: [] });
  const output = {
    schemaVersion: "1.0.0",
    generatedAt: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    messages: mergeMessages(existing, dateKey, fresh),
  };

  if (args.has("--print") || args.has("--dry-run")) {
    for (const message of fresh) console.log(`${message.section}/${message.role}: ${message.text}`);
    console.log(`\n${fresh.length} fresh, ${output.messages.length} total${usage ? `, tokens ${usage.total_tokens}` : ""}.`);
  }

  if (args.has("--dry-run")) return 0;

  atomicWrite(VIBE_PATH, JSON.stringify(output, null, 2) + "\n");
  console.log(`wrote ${VIBE_PATH}: ${fresh.length} fresh, ${output.messages.length} total (model ${model})`);
  return 0;
}

main()
  .then((code) => process.exit(code ?? 0))
  .catch((error) => {
    console.error(`vibe-check failed: ${error.message}`);
    process.exit(1);
  });