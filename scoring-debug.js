// Standalone scoring debug/tuning page. Loads events.json + scoring-weights.json,
// recomputes each event's score from its published signals, and re-ranks live as
// the weights change. New signals found in the data are added automatically.
import { eventDateLabel } from "./event-time.mjs?v=20260908-1";
import {
  computeScore,
  computeRaw,
  contributions,
  discoverSignals,
  mergeSignals,
  hasSignals,
} from "./scoring.mjs?v=20261009-2";

import { compareTaste, fitOrder, restoreWeights, saveWeights } from './weight-tuning.mjs';
import { selectBestBets } from './ranking.mjs?v=20261009-2';
const TIME_ZONE = "America/Detroit";
const DEFAULT_BASE = 0.5;
const DEFAULT_SCALE = 100;

const state = {
  events: [],
  preferences: {},
  referenceTime: Date.now(),
  weights: null,
  defaults: null,
  order: [],
  added: [],
  expanded: new Set(),
  day: "",
  sort: "rating",
  hasWeightsFile: true,
  requestedOrder: null,
  lastMovedId: null,
  suggestion: null,
  undoWeights: null,
};

const $ = (selector) => document.querySelector(selector);

function el(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (key === "className") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (value !== undefined && value !== null) element.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) if (child) element.append(child);
  return element;
}

function todayKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

const startDate = (event) => (event.start || "").slice(0, 10);
const endDate = (event) => (event.end || event.start || "").slice(0, 10);
const isMultiDay = (event) => startDate(event) !== endDate(event);
const isActiveOn = (event, day) => startDate(event) <= day && endDate(event) >= day;

function prettySignal(name) {
  return name.replaceAll("_", " ");
}

function formatScore(value) {
  return value == null || Number.isNaN(value) ? "—" : String(value);
}

async function fetchJson(path) {
  const response = await fetch(`${path}?v=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

// ---------------------------------------------------------------------------
// Scoring for the current weights

function evaluate(event) {
  const signals = event?.scoring?.signals;
  const has = hasSignals(event);
  const published = Number.isFinite(event.score) ? event.score : null;
  if (!has) {
    return { event, has: false, published, recomputed: published, raw: null, rows: [], clamped: false };
  }
  const raw = computeRaw(signals, state.weights);
  return {
    event,
    has: true,
    published,
    recomputed: computeScore(signals, state.weights),
    raw,
    rows: contributions(signals, state.weights),
    clamped: raw < 0 || raw > 1,
  };
}

function sortEvaluated(items) {
  const byStart = (a, b) => (a.event.start || "").localeCompare(b.event.start || "");
  if (state.sort === "requested" && state.requestedOrder) {
    const ranks = new Map(state.requestedOrder.map((id, index) => [id, index]));
    return [...items].sort((a, b) => ranks.get(a.event.id) - ranks.get(b.event.id));
  }
  if (state.sort === "start") return [...items].sort(byStart);
  if (state.sort === "published") return [...items].sort((a, b) => (b.published ?? -1) - (a.published ?? -1) || byStart(a, b));
  return [...items].sort((a, b) => compareTaste(a.event, b.event, state.weights));
}

function visibleItems() {
  return sortEvaluated(state.events.filter(event => isActiveOn(event, state.day)).map(evaluate));
}

function clearRanking() {
  state.requestedOrder = null;
  state.lastMovedId = null;
  state.suggestion = null;
  if (state.sort === "requested") state.sort = "rating";
  $("#sort-select").value = state.sort;
  $("#ranking-status").textContent = "";
}

function invalidateSuggestion() {
  state.suggestion = null;
  state.undoWeights = null;
  $("#ranking-status").textContent = state.requestedOrder ? "Weights changed. Suggest again for your requested order." : "";
}

function renderRankingControls(evaluated) {
  const noSignals = evaluated.filter(item => !item.has).length;
  $("#sort-select option[value=requested]").disabled = !state.requestedOrder;
  $("#suggest-button").disabled = !state.requestedOrder || evaluated.length < 2 || noSignals > 0;
  $("#clear-ranking-button").disabled = !state.requestedOrder;
  $("#undo-suggestion-button").disabled = !state.undoWeights;
  if (noSignals) $("#ranking-status").textContent = `Cannot suggest weights for this day: ${noSignals} event${noSignals === 1 ? " has" : "s have"} no scoring signals. Choose another day or adjust weights manually.`;
  $("#weight-suggestion").hidden = !state.suggestion;
}

function moveEvent(id, offset) {
  const items = visibleItems();
  const from = items.findIndex(item => item.event.id === id);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= items.length) return;
  const order = items.map(item => item.event.id);
  order.splice(to, 0, order.splice(from, 1)[0]);
  state.requestedOrder = order;
  state.lastMovedId = id;
  state.suggestion = null;
  state.sort = "requested";
  $("#sort-select").value = state.sort;
  $("#ranking-status").textContent = `${items[from].event.title} moved to #${to + 1}. Scores still use the current weights. Suggest weights when your order is ready.`;
  renderEvents();
  const card = [...$("#event-list").children].find(node => node.dataset.id === String(id));
  const button = card?.querySelector(`[data-move-offset="${offset}"]:not(:disabled)`) || card?.querySelector(".score-event__moves button:not(:disabled)");
  button?.focus({ preventScroll: true });
}

const signedWeight = spec => spec.weight * (spec.polarity === "negative" ? -1 : 1);
const weightLabel = value => `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}`;

function suggestWeights() {
  if (!state.requestedOrder) return;
  const events = visibleItems().map(item => item.event);
  const byId = new Map(events.map(event => [event.id, event]));
  const desired = state.requestedOrder.map(id => byId.get(id));
  const result = fitOrder(desired, state.weights, { movedId: state.lastMovedId });
  if (result.unsupported) {
    $("#ranking-status").textContent = "At least two events with scoring signals are needed to suggest weights.";
    return;
  }
  state.suggestion = result;
  const changes = state.order.map(name => ({ name, before: signedWeight(state.weights.signals[name]), after: signedWeight(result.config.signals[name]) }))
    .filter(row => Math.abs(row.after - row.before) > 1e-7)
    .sort((a, b) => Math.abs(b.after - b.before) - Math.abs(a.after - a.before));
  $("#suggestion-changes").replaceChildren(...changes.map(row => el("tr", {}, [
    el("th", { scope: "row", text: prettySignal(row.name) }),
    el("td", { text: weightLabel(row.before) }),
    el("td", { text: weightLabel(row.after) }),
  ])));
  const fit = result.matched === result.total ? "Requested order matched." : "Best attempt; the full requested order could not be matched.";
  $("#suggestion-summary").textContent = `${fit} ${result.matched} of ${result.total} requested event pairs matched; ${result.ties} tied. ${changes.length} weight${changes.length === 1 ? "" : "s"} changed.`;
  $("#apply-suggestion-button").disabled = changes.length === 0;
  $("#ranking-status").textContent = "Suggestion ready. Review the changes below; current weights are unchanged.";
  renderRankingControls(visibleItems());
}

function applySuggestion() {
  if (!state.suggestion) return;
  const { config, matched, total, ties } = state.suggestion;
  state.undoWeights = structuredClone(state.weights);
  state.weights = config;
  clearRanking();
  state.sort = "rating";
  $("#sort-select").value = state.sort;
  renderWeightPanel();
  renderEvents();
  $("#ranking-status").textContent = `Suggestion applied to the preview: ${matched} of ${total} requested pairs matched; ${ties} tied. Showing the fitted rating order. Save or copy weights to keep them.`;
}

// ---------------------------------------------------------------------------
// Rendering

function renderSummary(evaluated) {
  const target = $("#summary");
  const multi = evaluated.filter((item) => isMultiDay(item.event)).length;
  const drift = evaluated.filter((item) => item.has && item.published != null && item.recomputed !== item.published).length;
  const noBreakdown = evaluated.filter((item) => !item.has).length;
  const parts = [
    el("span", {}, [el("strong", { text: String(evaluated.length) }), document.createTextNode(" events")]),
    el("span", { text: `${multi} multi-day` }),
    el("span", { className: drift ? "is-drift" : "", text: drift ? `${drift} differ from published` : "matches published" }),
  ];
  if (noBreakdown) parts.push(el("span", { text: `${noBreakdown} without signals` }));
  if (state.added.length) {
    parts.push(el("span", { className: "is-new", text: `${state.added.length} new signal${state.added.length === 1 ? "" : "s"}: ${state.added.map(prettySignal).join(", ")}` }));
  }
  if (!state.hasWeightsFile) parts.push(el("span", { className: "is-drift", text: "scoring-weights.json missing — run scripts/export_scoring_weights.py" }));
  target.replaceChildren(...parts);
}

function scoreBlock(item) {
  const children = [el("strong", { className: "score-big", text: formatScore(item.recomputed) })];
  if (item.has && item.published != null && item.recomputed !== item.published) {
    children.push(el("span", { className: "score-published is-drift", title: "Published score differs from the recomputed rating", text: `published ${item.published}` }));
  } else if (item.published != null) {
    children.push(el("span", { className: "score-published", text: `published ${item.published}` }));
  }
  if (!item.has) children.push(el("span", { className: "score-note", text: "no breakdown" }));
  return el("div", { className: "score-event__scores" }, children);
}

function breakdownTable(item) {
  if (!item.rows.length) return el("p", { className: "breakdown-empty", text: "No scoring signals on this event." });
  const table = el("table", { className: "breakdown" });
  const head = el("thead", {}, [
    el("tr", {}, [
      el("th", { text: "Signal" }),
      el("th", { text: "P(yes)" }),
      el("th", { text: "Weight" }),
      el("th", { text: "Contribution" }),
    ]),
  ]);
  const body = el("tbody");
  for (const row of item.rows) {
    const pct = Math.round(row.probability * 100);
    const magnitude = Math.min(1, Math.abs(row.delta) / 0.25);
    const bar = el("span", { className: `bar bar--${row.polarity}` }, [
      el("span", { className: "bar__fill", style: `width:${(magnitude * 100).toFixed(0)}%` }),
    ]);
    body.append(el("tr", {}, [
      el("td", {}, [el("span", { className: "sig-name", text: prettySignal(row.name) + (row.appliedProbability !== row.probability ? ' (retail overlap adjusted)' : '') })]),
      el("td", {}, [el("span", { className: "p-viz" }, [bar, el("span", { text: `${pct}%` })])]),
      el("td", { text: `${row.polarity === "negative" ? "−" : "+"}${row.weight.toFixed(2)}` }),
      el("td", { className: row.delta < 0 ? "is-neg" : "is-pos", text: `${row.delta >= 0 ? "+" : "−"}${Math.abs(row.delta).toFixed(3)}` }),
    ]));
  }
  table.append(head, body);
  const foot = el("p", { className: "breakdown-foot", text: `base ${state.weights.base.toFixed(2)} → raw ${item.raw.toFixed(3)}${item.clamped ? " (clamped)" : ""} → ${item.recomputed}` });
  return el("div", { className: "breakdown-wrap" }, [table, foot]);
}

function topContributors(item) {
  return item.rows.slice(0, 3)
    .map((row) => `${prettySignal(row.name)} ${row.delta >= 0 ? "+" : "−"}${Math.abs(row.delta).toFixed(2)}`)
    .join(" · ");
}

function eventCard(item, index, items) {
  const title = el("h3");
  try {
    const url = new URL(item.event.url);
    title.append(el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer", text: item.event.title }));
  } catch {
    title.textContent = item.event.title;
  }
  const multi = isMultiDay(item.event);
  const metaBits = [eventDateLabel(item.event), item.event.venue, item.event.city, item.event.category].filter(Boolean);
  const meta = el("p", { className: "score-event__meta" }, [
    multi ? el("span", { className: "multi-badge", text: "multi-day" }) : null,
    document.createTextNode(metaBits.join(" · ")),
  ]);
  const head = el("header", { className: "score-event__head" }, [
    el("span", { className: "score-event__rank", text: String(index + 1) }),
    el("div", { className: "score-event__title" }, [title, meta]),
    scoreBlock(item),
  ]);
  const moves = el("div", { className: "score-event__moves", "aria-label": `Change rank for ${item.event.title}` });
  for (const [offset, symbol, direction] of [[-1, "↑", "up"], [1, "↓", "down"]]) {
    const button = el("button", { type: "button", text: symbol, "aria-label": `Move ${item.event.title} ${direction}`, dataset: { moveOffset: offset } });
    button.disabled = !items[index + offset];
    button.addEventListener("click", () => moveEvent(item.event.id, offset));
    moves.append(button);
  }
  head.append(moves);

  const details = el("details", { className: "score-event__breakdown" });
  details.open = state.expanded.has(item.event.id);
  details.addEventListener("toggle", () => {
    if (details.open) state.expanded.add(item.event.id);
    else state.expanded.delete(item.event.id);
  });
  details.append(
    el("summary", {}, [
      el("span", { className: "summary-label", text: `Breakdown${item.has ? "" : " (unavailable)"}` }),
      el("span", { className: "summary-top", text: item.has ? topContributors(item) : "" }),
    ]),
    breakdownTable(item),
  );
  return el("article", { className: `score-event${multi ? " is-multi" : ""}`, dataset: { id: item.event.id } }, [head, details]);
}

function renderBestBets() {
  const base = selectBestBets(state.events, { day: state.day, now: state.referenceTime, preferences: state.preferences, weights: state.defaults });
  const tuned = selectBestBets(state.events, { day: state.day, now: state.referenceTime, preferences: state.preferences, weights: state.weights });
  const titleList = rows => rows.map((r, i) => `${i + 1}. ${r.event.title} (${r.score})`).join(' · ') || 'No eligible events';
  $('#shortlist-comparison').replaceChildren(el('p', { text: `Exported defaults: ${titleList(base)}` }), el('p', { text: `Preview weights: ${titleList(tuned)}` }));
}

function renderEvents() {
  renderBestBets();
  const scrollTop = document.scrollingElement.scrollTop;
  const evaluated = visibleItems();
  renderRankingControls(evaluated);
  renderSummary(evaluated);
  const list = $("#event-list");
  if (!evaluated.length) {
    list.replaceChildren(el("p", { className: "empty-state", text: "No events active on this day." }));
  } else {
    list.replaceChildren(...evaluated.map((item, index) => eventCard(item, index, evaluated)));
  }
  document.scrollingElement.scrollTop = scrollTop;
}

// Weight panel is built once; edits mutate state and re-render only the list.

function weightRow(name) {
  const spec = state.weights.signals[name];
  const polarity = el("select", { className: "weight-polarity", "aria-label": `${prettySignal(name)} direction` }, [
    el("option", { value: "positive", text: "+ add" }),
    el("option", { value: "negative", text: "− penalty" }),
  ]);
  polarity.value = spec.polarity;
  const weight = el("input", { type: "number", className: "weight-value", step: "0.01", min: "-1", max: "1", value: String(spec.weight), "aria-label": `${prettySignal(name)} weight` });
  const update = () => {
    spec.polarity = polarity.value;
    spec.weight = Number(weight.value);
    invalidateSuggestion();
    scheduleRender();
  };
  polarity.addEventListener("change", update);
  weight.addEventListener("input", update);
  return el("div", { className: "weight-row" }, [
    el("span", { className: "weight-name" }, [
      el("span", { text: prettySignal(name) }),
      spec.isNew ? el("span", { className: "weight-badge", text: "new" }) : null,
    ]),
    polarity,
    weight,
  ]);
}

function renderWeightPanel() {
  const list = $("#weight-list");
  list.replaceChildren(...state.order.map(weightRow));
  $("#base-input").value = state.weights.base.toFixed(2);
}

// ---------------------------------------------------------------------------
// Wiring

let pending = false;
function scheduleRender() {
  if (pending) return;
  pending = true;
  window.requestAnimationFrame(() => {
    pending = false;
    renderEvents();
  });
}

function copyWeights() {
  const payload = {
    rulesVersion: state.weights.rulesVersion,
    base: state.weights.base,
    scale: state.weights.scale,
    signals: Object.fromEntries(state.order.map((name) => [name, { weight: state.weights.signals[name].weight, polarity: state.weights.signals[name].polarity }])),
  };
  const text = JSON.stringify(payload, null, 2);
  navigator.clipboard?.writeText(text).then(
    () => { $("#status").textContent = "Weights copied to clipboard"; },
    () => { $("#status").textContent = "Copy failed — see console"; console.log(text); },
  );
}

function resetWeights() {
  state.weights = structuredClone(state.defaults);
  state.order = Object.keys(state.weights.signals);
  state.undoWeights = null;
  clearRanking();
  renderWeightPanel();
  renderEvents();
  $("#status").textContent = "Weights reset to the exported defaults";
}

function bindControls() {
  $("#suggest-button").addEventListener("click", suggestWeights);
  $("#apply-suggestion-button").addEventListener("click", applySuggestion);
  $("#discard-suggestion-button").addEventListener("click", () => {
    state.suggestion = null;
    $("#ranking-status").textContent = "Suggestion discarded. Your requested order is still available to edit.";
    renderRankingControls(visibleItems());
  });
  $("#clear-ranking-button").addEventListener("click", () => { clearRanking(); renderEvents(); });
  $("#undo-suggestion-button").addEventListener("click", () => {
    if (!state.undoWeights) return;
    state.weights = state.undoWeights;
    state.undoWeights = null;
    clearRanking();
    renderWeightPanel();
    renderEvents();
    $("#ranking-status").textContent = "Previous preview weights restored. Save again if you want to replace saved browser weights.";
  });
  $('#save-button').addEventListener('click', () => {
    try { $('#status').textContent = saveWeights(state.weights, localStorage) ? 'Saved on this browser. Reload the brief to apply.' : 'Browser storage unavailable; weights were not saved.'; }
    catch { $('#status').textContent = 'Browser storage unavailable; weights were not saved.'; }
  });
  $('#clear-saved-button').addEventListener('click', () => {
    try { localStorage.removeItem('daily-brief:weights:v2'); resetWeights(); $('#status').textContent = 'Saved weights cleared. Reload the brief to use defaults.'; }
    catch { $('#status').textContent = 'Browser storage unavailable.'; }
  });
  $('#edition-file').addEventListener('change', async e => {
    try {
      const doc = JSON.parse(await e.target.files[0].text());
      if (!Array.isArray(doc.events) || !doc.editionDate || !Number.isFinite(Date.parse(doc.generatedAt))) throw new Error('Choose a saved events.json document');
      state.events = doc.events; state.day = doc.editionDate; state.referenceTime = Date.parse(doc.generatedAt);
      const discovered = discoverSignals(state.events);
      state.defaults = { ...state.defaults, signals: mergeSignals(state.defaults, discovered).signals };
      const merged = mergeSignals(state.weights, discovered);
      state.weights = { ...state.weights, signals: merged.signals };
      state.order = Object.keys(state.weights.signals);
      state.added = state.order.filter(name => state.weights.signals[name].isNew);
      state.undoWeights = null; clearRanking(); renderWeightPanel();
      $('#day-input').value = state.day; renderEvents(); $('#status').textContent = 'Saved edition loaded locally; no upload.';
    } catch (error) { $('#status').textContent = error.message; }
  });
  $("#day-input").addEventListener("change", (changeEvent) => {
    if (changeEvent.target.value) {
      state.day = changeEvent.target.value;
      clearRanking();
      renderEvents();
    }
  });
  $("#today-button").addEventListener("click", () => {
    state.day = todayKey();
    clearRanking();
    $("#day-input").value = state.day;
    renderEvents();
  });
  $("#sort-select").addEventListener("change", (changeEvent) => {
    state.sort = changeEvent.target.value;
    if (state.sort !== "requested") clearRanking();
    renderEvents();
  });
  $("#base-input").addEventListener("input", (inputEvent) => {
    const value = Number(inputEvent.target.value);
    if (Number.isFinite(value)) {
      state.weights.base = value;
      invalidateSuggestion();
      scheduleRender();
    }
  });
  $("#reset-button").addEventListener("click", resetWeights);
  $("#copy-button").addEventListener("click", copyWeights);
}

async function init() {
  bindControls();
  let eventsDoc = null;
  let weightsDoc = null;
  try {
    [eventsDoc, weightsDoc] = await Promise.all([fetchJson("events.json"), fetchJson("scoring-weights.json")]);
  } catch (error) {
    eventsDoc = eventsDoc || await fetchJson("events.json").catch(() => null);
    state.hasWeightsFile = false;
    $("#status").textContent = `Could not load weights (${error.message}); using zeros`;
  }
  state.events = Array.isArray(eventsDoc?.events) ? eventsDoc.events : [];
  const discovered = discoverSignals(state.events);
  const baseConfig = weightsDoc || { base: DEFAULT_BASE, scale: DEFAULT_SCALE, signals: {} };
  const { signals, added } = mergeSignals(baseConfig, discovered);
  state.added = added;
  state.weights = {
    rulesVersion: baseConfig.rulesVersion || 1,
    base: Number.isFinite(baseConfig.base) ? baseConfig.base : DEFAULT_BASE,
    scale: Number.isFinite(baseConfig.scale) ? baseConfig.scale : DEFAULT_SCALE,
    signals,
  };
  state.defaults = structuredClone(state.weights);
  state.weights = state.defaults;
  try { state.weights = restoreWeights(state.defaults, window.localStorage); } catch {}
  const restored = state.weights !== state.defaults;
  state.weights = structuredClone(state.weights);
  state.order = Object.keys(signals);

  state.preferences = await fetchJson('brief-preferences.json').catch(() => ({}));
  state.referenceTime = Date.parse(eventsDoc?.generatedAt) || Date.now();
  state.day = eventsDoc?.editionDate || todayKey();
  $("#day-input").value = state.day;
  state.sort = $("#sort-select").value;
  renderWeightPanel();
  renderEvents();
  $("#status").textContent = `${state.events.length} events loaded · ${restored ? 'saved browser weights' : 'default weights'}`;
}

init();
