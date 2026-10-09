import { rankEvents, eventDay, validateManifest, validateSection } from "./brief-selection.mjs";
import { fetchJSON, readSavedEdition, saveEdition } from "./edition-client.mjs";
import { isEventRecommended, eventDisplayTitle } from "./event-status.mjs?v=20261009-1";
import { starshipView, targetLabel, usableLaunchCache, launchDateKey, launchDateTime } from "./starship.mjs?v=20261009-1";
import { computeAlmanacDay } from "./almanac-calc.mjs?v=20260920-1";
import { weatherAppearance } from "./weather-appearance.mjs?v=20260830-1";
import { eventDateLabel } from "./event-time.mjs?v=20260908-1";
import { orderNewsStories } from "./story-order.mjs?v=20261009-1";

import { scienceView, scienceRotationSlot, scienceFreshness, scienceDate, scienceContext, scienceEditorial } from "./science.mjs?v=20261009-rotation-1";

import { orderRankedNews } from './news-ranking.mjs?v=20261009-2';
import { selectBestBets, rankEvent, eventStatus } from './ranking.mjs?v=20261009-2';
import { preferenceStore, feedback, seriesKey } from './preferences.mjs?v=20261009-2';
let preferences;
let rankingWeights;

let feedbackNotice = '';

const TIME_ZONE = "America/Detroit";
const REFRESH_MS = 15 * 60 * 1000;
const LAUNCH_CACHE_KEY = "daily-brief-rocket-launches:v1";
const LAUNCH_API_URL = "https://fdo.rocketlaunch.live/json/launches/next/5";

// Each data document is paired with its repository schema. Adding a new
// schema-backed document is a one-line registry change, not a rendering rewrite.
const DOCUMENTS = [
  ["weather", "weather.json", "schemas/weather.schema.json", true],
  ["calendar", "calendar.json", "schemas/calendar.schema.json", true],
  ["events", "events.json", "schemas/events.schema.json", true],
  ["news", "news.json", "schemas/news.schema.json", true],
  ["starship", "starship.json", "schemas/starship.schema.json", true],
  ["geeknews", "geeknews.json", "schemas/geeknews.schema.json", true],
  ["scienceHealth", "science-health.json", "schemas/science-health.schema.json", false],
  ["vibe", "vibe.json", "schemas/vibe.schema.json", true],
  ["photos", "photos.json", "schemas/photos.schema.json", false],
];

const state = {
  data: {},
  errors: [],
  signatures: new Map(),
  selectedDate: "",
  today: dateKey(new Date()),
  photoIndex: 0,
  photoTimer: null,
  eventExpiryTimer: null,
  calendarMonth: "",
  calendarSelected: "",
  calendarEventId: "",
  calendarEntries: [],
  launches: [],
  launchCacheFallback: false,
  launchFetchedAt: null,
};

let claimHoverTimer = null;
let claimHideTimer = null;
let activeClaim = null;

const $ = (selector) => document.querySelector(selector);

function dateKey(date) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function shiftDate(key, days) {
  const date = new Date(`${key}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function clampDate(key) {
  const minimum = shiftDate(state.today, -1);
  const maximum = shiftDate(state.today, 1);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key || "")) return state.today;
  return key < minimum ? minimum : key > maximum ? maximum : key;
}

function displayDate(key, options = {}) {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    weekday: "long",
    month: "long",
    day: "numeric",
    year: options.year ? "numeric" : undefined,
  }).format(new Date(`${key}T12:00:00Z`));
}

function displayTime(value) {
  if (!value) return "";
  const date = value.length === 5 ? new Date(`2000-01-01T${value}:00`) : new Date(value);
  return new Intl.DateTimeFormat("en-US", {
    timeZone: value.length === 5 ? undefined : TIME_ZONE,
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (key === "className") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (value !== undefined && value !== null) element.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child) element.append(child);
  }
  return element;
}

function safeLink(label, url) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("unsupported protocol");
    return node("a", { text: label, href: parsed.href, target: "_blank", rel: "noopener noreferrer" });
  } catch {
    return document.createTextNode(label);
  }
}

function safeImageUrl(url) {
  try {
    const parsed = new URL(url);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
}

function replaceChildren(selector, children) {
  const target = $(selector);
  target.replaceChildren(...(Array.isArray(children) ? children : [children]));
}

function emptyState() {
  return $("#empty-template").content.firstElementChild.cloneNode(true);
}

function validateTopLevel(data, schema, name) {
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(`${name} must be an object`);
  const missing = (schema.required || []).filter((key) => !(key in data));
  if (missing.length) throw new Error(`${name} is missing: ${missing.join(", ")}`);
  const expectedVersion = schema.properties?.schemaVersion?.const;
  if (expectedVersion && data.schemaVersion !== expectedVersion) {
    throw new Error(`${name} uses schema ${data.schemaVersion || "unknown"}; expected ${expectedVersion}`);
  }
  if (schema.additionalProperties === false) {
    const allowed = new Set(Object.keys(schema.properties || {}));
    const unexpected = Object.keys(data).filter((key) => !allowed.has(key));
    if (unexpected.length) throw new Error(`${name} has unexpected fields: ${unexpected.join(", ")}`);
  }
}

const schemaCache = new Map();
let refreshing = false;
let lastCheck = 0;
let manifest = null;
let savedMode = false;
let fullEvents = null;
let archiveRequest = null;
let showAllEvents = false;
const fullStories = {};
const storyLimits = {news:10,geeknews:10};
const sectionStates = {};
const sectionSelectors = {weather: '#weather-section', events:'#events-section', news:'#news-section', geeknews:'#geek-section'};

async function fetchJson(path) { return fetchJSON(path); }
async function loadDocument([name, dataPath, schemaPath, required]) {
  try {
    if (!schemaCache.has(schemaPath)) schemaCache.set(schemaPath, fetchJSON(schemaPath, {cache:'force-cache'}).catch(error => {schemaCache.delete(schemaPath); throw error;}));
    const [data, schema] = await Promise.all([fetchJSON(dataPath), schemaCache.get(schemaPath)]);
    validateTopLevel(data, schema, dataPath);
    if (!['photos','calendar'].includes(name)) validateSection(name,data);
    return {name, data, required};
  } catch (error) { return {name, error:error.message, required}; }
}
function configureRanking(config) {
  let storage;try{storage=window.localStorage}catch{}
  preferences=preferenceStore(storage, config?.preferences || {});
  rankingWeights=config?.weights || null;
  try {
    const weights=JSON.parse(storage?.getItem('daily-brief:weights:v2') || 'null');
    if(weights?.rulesVersion===2 && weights.signals && Number.isFinite(weights.base) && Number.isFinite(weights.scale)) rankingWeights=weights;
  }catch{}
}
function updateFreshness() {
  const label = manifest ? `Edition ${manifest.editionDate} · published ${new Date(manifest.generatedAt).toLocaleString('en-US', {timeZone:TIME_ZONE, month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}` : 'Full-document compatibility view';
  $('#live-status').textContent = `${savedMode ? 'Saved brief · ' : ''}${label}${state.errors.length ? ' · some sources unavailable' : ''}`;
  for (const [name,selector] of Object.entries(sectionSelectors)) {
    const target = $(selector);
    target.setAttribute('aria-busy', String(sectionStates[name] === 'loading'));
    let note = target.querySelector('.section-freshness');
    if (!note) { note = node('p', {className:'section-freshness',role:'status'}); target.prepend(note); }
    const updated = state.data[name]?.generatedAt;
    note.textContent = sectionStates[name] === 'loading' ? 'Loading…' : sectionStates[name] === 'error' ? 'Unavailable. Refresh to retry.' : updated ? `${savedMode ? 'Saved · ' : ''}Source updated ${new Date(updated).toLocaleString('en-US', {timeZone:TIME_ZONE,month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}` : 'No data available.';
  }
}
async function refreshData({ initial = false } = {}) {
  if (refreshing || document.hidden) return;
  refreshing = true;
  const previousToday = state.today;
  state.today = dateKey(new Date());
  if (state.today !== previousToday && state.selectedDate === previousToday) {
    state.selectedDate = state.today;
    const url = new URL(window.location); url.searchParams.delete('date');
    window.history.replaceState({date:state.today}, '', url);
    loadJoke();
  }
  state.errors = [];
  try {
    const next = validateManifest(await fetchJSON('brief-manifest.json', {timeout:5000}));
    if (manifest?.editionId !== next.editionId) {
      manifest = next; fullEvents = null; archiveRequest = null;
      for(const name of Object.keys(fullStories)) delete fullStories[name];
      storyLimits.news=10;storyLimits.geeknews=10;
      // An edition is coherent: old sections are cleared before the new set renders.
      for (const name of Object.keys(next.sections)) {delete state.data[name]; sectionStates[name]='loading';}
      render();updateFreshness();
    }
    savedMode = false;
    await Promise.all(Object.entries(next.sections).map(async ([name,descriptor]) => {
      try {
        if (!state.data[name]) {
          const data = validateSection(name, await fetchJSON(descriptor.url,{sha256:descriptor.sha256,cache:'force-cache'}));
          state.data[name] = data;
          if(name==='rankingConfig') configureRanking(data);
          sectionStates[name] = 'ready'; render(); updateFreshness();
        }
      } catch(error) { sectionStates[name]='error';state.errors.push(`${name}: ${error.message}`);updateFreshness(); }
    }));
    if (!state.errors.length && !saveEdition(next, Object.fromEntries(Object.keys(next.sections).map(name=>[name,state.data[name]])))) {
      state.errors.push('Browser storage unavailable; this edition cannot be saved for offline reopening.');
    }
  } catch(error) {
    savedMode = Boolean(manifest);
    state.errors.push(`Edition: ${error.message}`);
    // Rollback path: only use full documents when no compatible saved edition exists.
    if (!manifest) await Promise.all(DOCUMENTS.filter(([name])=>!['photos','calendar'].includes(name)).map(async descriptor => {
      const result = await loadDocument(descriptor);
      sectionStates[result.name] = result.error ? 'error' : 'ready';
      if(result.error) state.errors.push(`${result.name}: ${result.error}`);
      else {state.data[result.name]=result.data;render();updateFreshness();}
    }));
  } finally {
    lastCheck=Date.now();refreshing=false;
    if (initial || state.today!==previousToday) render();
    updateFreshness();renderErrors();renderStarship();
    $('#refresh-brief').disabled=false;
  }
}
async function ensureFullEvents() {
  if(fullEvents) return fullEvents;
  if(archiveRequest) return archiveRequest;
  const editionId=manifest?.editionId;
  archiveRequest=(async()=>{
    const descriptor=manifest?.archives?.events;
    const data=validateSection('events',await fetchJSON('events.json', {sha256:descriptor?.sha256, timeout:12000}));
    if(manifest?.editionId!==editionId) throw new Error('Edition changed; reopen the calendar to retry.');
    fullEvents=data.events;return fullEvents;
  })();
  try{return await archiveRequest}finally{archiveRequest=null}
}
async function loadFamilyCalendar() {
  const result=await loadDocument(DOCUMENTS.find(([name])=>name==='calendar'));
  if(result.error) {$('#calendar-note').textContent='Calendar unavailable. Close and reopen to retry.';return;}
  state.data.calendar=result.data;renderCalendar();renderDayContext();
}

function messagesFor(section) {
  return (state.data.vibe?.messages || [])
    .filter((message) => message.date === state.selectedDate && message.section === section)
    .sort((a, b) => (a.order || 0) - (b.order || 0));
}

function message(section, role, fallback) {
  return messagesFor(section).find((item) => item.role === role)?.text || fallback;
}

function renderMasthead() {
  const relative = state.selectedDate === state.today ? "Today" : state.selectedDate < state.today ? "Yesterday" : "Tomorrow";
  const forecast = state.data.weather?.daily?.find((day) => day.date === state.selectedDate);
  $("#masthead-eyebrow").textContent = message("masthead", "eyebrow", `${relative} · Canton, Michigan`);
  $('#page-title').replaceChildren(node('span',{className:'desktop-title',text:message('masthead','headline',relative==='Today'?'A good day, well considered.':`${relative}, in view.`)}),node('span',{className:'mobile-title',text:`${relative}’s daily brief`}));
  $("#masthead-summary").textContent = message(
    "masthead",
    "summary",
    forecast ? `${forecast.conditionText}, ${Math.round(forecast.lowF)}°–${Math.round(forecast.highF)}°. Here is what else the day has in store.` : "The available signals for this date, gathered in one calm place.",
  );
  const date = $("#edition-date");
  date.textContent = displayDate(state.selectedDate, { year: true });
  date.dateTime = state.selectedDate;
  const location = state.data.weather?.location;
  $("#location-label").textContent = location ? `${location.name}, ${location.region}` : "Canton, Michigan";
  document.title = `Daily Brief — ${displayDate(state.selectedDate)}`;
  renderDayContext();
}

function weatherCard(label, title, big, body, facts = [], options = {}) {
  const card = node("article", { className: `weather-card${options.className ? ` ${options.className}` : ""}` }, [
    node("span", { className: "weather-card__label", text: label }),
    big ? node("strong", { className: "weather-card__big", text: big }) : null,
    node("h3", { text: title }),
    body ? node("p", { text: body }) : null,
    options.icon ? node("span", { className: "weather-card__icon", text: options.icon, "aria-hidden": "true" }) : null,
  ]);
  if (facts.length) {
    const list = node("dl", { className: "weather-facts" });
    for (const [term, description] of facts) {
      list.append(node("div", {}, [node("dt", { text: term }), node("dd", { text: description })]));
    }
    card.append(list);
  }
  return card;
}

function moonIlluminationPath(percent, waxing) {
  const illumination = Math.min(1, Math.max(0, percent / 100));
  const center = 50;
  const radius = 46;
  const steps = 64;
  const terminator = [];
  const limb = [];
  for (let index = 0; index <= steps; index += 1) {
    const y = -radius + (radius * 2 * index) / steps;
    const width = Math.sqrt(Math.max(0, radius ** 2 - y ** 2));
    const boundary = waxing
      ? center + (1 - 2 * illumination) * width
      : center + (2 * illumination - 1) * width;
    terminator.push([boundary, center + y]);
    limb.push([center + (waxing ? width : -width), center + y]);
  }
  const points = waxing
    ? [...terminator, ...limb.reverse()]
    : [...limb, ...terminator.reverse()];
  return `${points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ")} Z`;
}

function moonGraphic(moon) {
  const waxing = moon.phase?.startsWith("waxing") || moon.phase === "first-quarter";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "moon-disc");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", moon.imageAlt || `${moon.summary}, ${Math.round(moon.illuminationPercent)} percent illuminated`);
  const definitions = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const clip = document.createElementNS("http://www.w3.org/2000/svg", "clipPath");
  clip.setAttribute("id", "moon-illumination-clip");
  const lightShape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  lightShape.setAttribute("d", moonIlluminationPath(moon.illuminationPercent, waxing));
  clip.append(lightShape);
  definitions.append(clip);
  const dark = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dark.setAttribute("class", "moon-disc__dark");
  dark.setAttribute("cx", "50");
  dark.setAttribute("cy", "50");
  dark.setAttribute("r", "46");
  const texture = document.createElementNS("http://www.w3.org/2000/svg", "image");
  texture.setAttribute("class", "moon-disc__texture");
  texture.setAttribute("href", "assets/moon-waxing-gibbous.png");
  // The source photo includes transparent padding. Zoom it to the lunar limb
  // so the dark phase backing does not read as a ring around the photograph.
  texture.setAttribute("x", "-3");
  texture.setAttribute("y", "-3");
  texture.setAttribute("width", "106");
  texture.setAttribute("height", "106");
  texture.setAttribute("clip-path", "url(#moon-illumination-clip)");
  svg.append(definitions, dark, texture);
  return svg;
}

function forecastLabel(key) {
  if (key === state.today) return "Today";
  if (key === shiftDate(state.today, 1)) return "Tomorrow";
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short" }).format(new Date(`${key}T12:00:00Z`));
}

function forecastRangeCard(days) {
  const columns = days.filter(Boolean).map((forecast) => node("div", { className: "forecast-day" }, [
    node("span", { className: "weather-card__label", text: forecastLabel(forecast.date) }),
    node("div", { className: "forecast-day__headline" }, [
      node("span", { className: "forecast-day__icon", text: forecast.icon, "aria-hidden": "true" }),
      node("strong", { text: `${Math.round(forecast.highF)}° / ${Math.round(forecast.lowF)}°` }),
    ]),
    node("h3", { text: forecast.conditionText }),
    node("p", { className: "forecast-day__detail", text: `${forecast.precipitationChancePercent}% chance of rain`, title: forecast.narrative }),
  ]));
  return node("article", { className: "weather-card weather-card--forecast" }, columns);
}

function skyWeatherCard(day, forecast) {
  const sunrise = forecast?.sunrise || day?.sunrise;
  const sunset = forecast?.sunset || day?.sunset;
  const riseDate = sunrise ? new Date(sunrise) : null;
  const setDate = sunset ? new Date(sunset) : null;
  const now = new Date();
  const progress = state.selectedDate === state.today && riseDate && setDate
    ? Math.min(1, Math.max(0, (now - riseDate) / (setDate - riseDate)))
    : .5;
  const daylight = node("div", { className: "daylight-visual" }, [
    node("span", { className: "weather-card__label", text: "Daylight" }),
    node("div", { className: "daylight-track", style: `--sun-position: ${(progress * 100).toFixed(1)}%` }, [
      node("span", { className: "daylight-track__sun", "aria-hidden": "true" }),
    ]),
    node("div", { className: "daylight-times" }, [
      node("span", {}, [node("small", { text: "Rise" }), document.createTextNode(displayTime(sunrise) || "—")]),
      node("span", {}, [node("small", { text: "Set" }), document.createTextNode(displayTime(sunset) || "—")]),
    ]),
  ]);
  const moon = day?.moon;
  const night = moon ? node("div", { className: "moon-layout" }, [
    moonGraphic(moon),
    node("div", {}, [
      node("strong", { className: "moon-percent", text: `${Math.round(moon.illuminationPercent)}%` }),
      node("span", { className: "moon-phase", text: moon.phase.replaceAll("-", " ") }),
      moon.moonrise ? node("small", { text: `Rises ${displayTime(moon.moonrise)}` }) : null,
    ]),
  ]) : null;
  return node("article", { className: "weather-card weather-card--sky" }, [
    daylight,
    night,
  ]);
}

function renderWeather() {
  const weather = state.data.weather;
  const forecast = weather?.daily?.find((day) => day.date === state.selectedDate);
  const location = weather?.location;
  let day = null;
  if (location) {
    try {
      day = computeAlmanacDay(state.selectedDate, location);
    } catch (error) {
      console.warn(`Could not compute the almanac for ${state.selectedDate}:`, error);
    }
  }
  const current = state.selectedDate === state.today ? weather?.current : null;
  const appearance = weatherAppearance({ date: state.selectedDate, today: state.today, forecast, current });
  const nextForecast = weather?.daily?.find((item) => item.date === shiftDate(state.selectedDate, 1));
  const cards = [];

  if (forecast) {
    cards.push(weatherCard(
      current ? appearance.source === "current" ? "Right now" : `Observed ${displayTime(current.observedAt)}` : "Forecast",
      current?.conditionText || forecast.conditionText,
      `${Math.round(current?.temperatureF ?? forecast.highF)}°`,
      forecast.narrative,
      current ? [["Feels", `${Math.round(current.feelsLikeF)}°`], ["Wind", `${Math.round(current.wind.speedMph)} mph ${current.wind.direction}`]] : [["High", `${Math.round(forecast.highF)}°`], ["Low", `${Math.round(forecast.lowF)}°`]],
      { icon: current?.icon || forecast.icon, className: "weather-card--now" },
    ));
    cards.push(forecastRangeCard([forecast, nextForecast]));
  }
  if (forecast || day) {
    cards.push(skyWeatherCard(day, forecast));
  }

  $('#weather-summary').textContent = forecast ? `${forecast.conditionText} · ${Math.round(current?.temperatureF ?? forecast.highF)}° · High ${Math.round(forecast.highF)}° / low ${Math.round(forecast.lowF)}° · ${forecast.precipitationChancePercent}% rain` : 'Weather unavailable for this date.';
  replaceChildren("#weather-grid", cards.length ? cards : [emptyState()]);
  updateAtmosphere(forecast);
}

function occursOn(item, key) {
  if (item.date === key) return true;
  const recurrence = item.recurrence;
  if (!recurrence || key < item.date || (recurrence.until && key > recurrence.until)) return false;
  const start = new Date(`${item.date}T12:00:00Z`);
  const target = new Date(`${key}T12:00:00Z`);
  const interval = recurrence.interval || 1;
  const days = Math.round((target - start) / 86400000);
  if (recurrence.frequency === "daily") return days % interval === 0;
  if (recurrence.frequency === "weekly") {
    const weekday = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"][target.getUTCDay()];
    return Math.floor(days / 7) % interval === 0 && (!recurrence.daysOfWeek || recurrence.daysOfWeek.includes(weekday));
  }
  if (recurrence.frequency === "monthly") {
    const months = (target.getUTCFullYear() - start.getUTCFullYear()) * 12 + target.getUTCMonth() - start.getUTCMonth();
    return months % interval === 0 && target.getUTCDate() === start.getUTCDate();
  }
  if (recurrence.frequency === "yearly") {
    return (target.getUTCFullYear() - start.getUTCFullYear()) % interval === 0 && target.toISOString().slice(5, 10) === item.date.slice(5, 10);
  }
  return false;
}

const CALENDAR_ICONS = {
  anniversary: "♥",
  appointment: "▣",
  birthday: "●",
  event: "◇",
  holiday: "✦",
  other: "•",
  reminder: "◌",
  task: "✓",
};

function calendarItemsForDate(key) {
  const calendarItems = (state.data.calendar?.items || [])
    .filter((item) => occursOn(item, key) && item.status !== "cancelled")
    .map((item) => ({ ...item, occurrenceDate: key }));
  const seen = new Set();
  return calendarItems
    .filter((item) => {
      const identity = `${item.occurrenceDate}:${item.title.toLowerCase().replace(/[^a-z0-9]/g, "")}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    })
    .sort((a, b) => (a.startTime || "99:99").localeCompare(b.startTime || "99:99") || a.title.localeCompare(b.title));
}

function upcomingCalendarItems(fromKey, windowDays = 21, limit = 4) {
  const results = [];
  for (let offset = 1; offset <= windowDays && results.length < limit; offset += 1) {
    const key = shiftDate(fromKey, offset);
    results.push(...calendarItemsForDate(key));
  }
  return results.slice(0, limit);
}

function shortDateParts(key) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric" })
    .formatToParts(new Date(`${key}T12:00:00Z`));
  return {
    month: parts.find((part) => part.type === "month")?.value || "",
    day: parts.find((part) => part.type === "day")?.value || "",
  };
}

function renderDayContext() {
  const exact = calendarItemsForDate(state.selectedDate);
  const context = exact.length ? exact.slice(0, 3) : upcomingCalendarItems(state.selectedDate, 14, 1);
  const children = context.map((item) => {
    const isUpcoming = item.occurrenceDate !== state.selectedDate;
    const when = isUpcoming ? new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" }).format(new Date(`${item.occurrenceDate}T12:00:00Z`)) : item.startTime ? displayTime(item.startTime) : "";
    return node("span", { className: "day-context__item" }, [
      node("span", { className: "day-context__icon", text: CALENDAR_ICONS[item.type] || "•", "aria-hidden": "true" }),
      document.createTextNode(`${isUpcoming ? "Next: " : ""}${item.title}${when ? ` · ${when}` : ""}`),
    ]);
  });
  replaceChildren("#day-context", children);
}

function renderCalendar() {
  const items = calendarItemsForDate(state.selectedDate);
  const label = node("h3", { className: "calendar-entries__label", text: relativeDayWord(state.selectedDate) });
  const entries = items.map((item) => {
    const title = node("h3", { className: "calendar-entry__title" });
    title.append(item.url ? safeLink(item.title, item.url) : document.createTextNode(item.title));
    const meta = [
      `${CALENDAR_ICONS[item.type] || "•"} ${item.startTime ? displayTime(item.startTime) : item.type}`,
      item.location,
      item.person,
    ].filter(Boolean).join(" · ");
    const details = [
      item.status === "tentative" ? "Tentative" : "",
      item.description,
    ].filter(Boolean).join(" · ");
    return node("article", { className: "calendar-entry" }, [
      node("span", { className: "calendar-entry__meta", text: meta }),
      title,
      details ? node("p", { className: "calendar-entry__details", text: details }) : null,
    ]);
  });
  replaceChildren("#calendar-list", [
    label,
    ...(entries.length ? entries : [node("p", { className: "calendar-entries__empty", text: "Nothing scheduled." })]),
  ]);
  $("#calendar-note").textContent = message("calendar", "note", "The things worth remembering.");

  const upcoming = upcomingCalendarItems(state.selectedDate);
  const target = $("#calendar-upcoming");
  target.replaceChildren();
  if (upcoming.length) {
    target.append(node("h3", { text: "Coming up" }));
    const list = node("ul", { className: "calendar-upcoming__list" });
    for (const item of upcoming) {
      const date = shortDateParts(item.occurrenceDate);
      list.append(node("li", { className: "calendar-upcoming__item" }, [
        node("time", { className: "calendar-upcoming__date", datetime: item.occurrenceDate }, [
          node("small", { text: date.month }),
          document.createTextNode(date.day),
        ]),
        node("p", {}, [
          document.createTextNode(item.title),
          node("span", { text: ` · ${item.type}` }),
        ]),
      ]));
    }
    target.append(list);
  }
}

function shortDateLabel(key) {
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" })
    .format(new Date(`${key}T12:00:00Z`));
}

function monthLabel(monthKey) {
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "long", year: "numeric" })
    .format(new Date(`${monthKey}-01T12:00:00Z`));
}

function shiftMonth(monthKey, delta) {
  const [year, month] = monthKey.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1 + delta, 1));
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
}

function eventsOnDay(key, events = uniqueEvents(), comparator = compareEventsForDisplay) {
  return events.filter((event) => eventStartDate(event) <= key && eventEndDate(event) >= key).sort(comparator);
}

function agendaEntry(event, key, { withDate = false } = {}) {
  const when = withDate ? shortDateLabel(key) : eventDateLabel(event);
  const selected = event.id === state.calendarEventId;
  const meta = node("span", { className: "agenda-item__meta" }, [
    node("span", { className: "agenda-item__where", text: [when, event.venue, event.city].filter(Boolean).join(" · ") }),
    event.score != null ? node("span", {
      className: "agenda-item__score",
      text: String(event.score),
      title: `Rating ${event.score} of 100`,
      "aria-label": `Rating ${event.score} of 100`,
    }) : null,
  ]);
  return node("button", {
    className: `agenda-item${selected ? " is-selected" : ""}`,
    type: "button",
    dataset: { eventId: event.id, date: key },
    "aria-pressed": String(selected),
  }, [
    meta,
    node("strong", { text: eventDisplayTitle(event) }),
  ]);
}

function renderEventCalendarAgenda(events) {
  const list = $("#calendar-agenda-list");
  list.replaceChildren();
  const selected = state.calendarSelected;
  $("#calendar-agenda-title").textContent = displayDate(selected, { year: true });

  // The calendar is the complete view: every event active that day, sorted by
  // start time; preference ranking only affects best bets.
  const dayEvents = eventsOnDay(selected, events, compareEventsForDisplay);
  state.calendarEntries = dayEvents.map((event) => ({ event, key: selected }));
  if (!state.calendarEntries.some((entry) => entry.event.id === state.calendarEventId)) {
    state.calendarEventId = state.calendarEntries[0]?.event.id || "";
  }

  list.append(node("h4", { className: "calendar-agenda__heading", text: dayEvents.length ? `On this day · ${dayEvents.length}` : "Nothing scheduled" }));
  if (dayEvents.length) {
    for (const event of dayEvents) list.append(agendaEntry(event, selected));
  } else {
    list.append(node("p", { className: "calendar-agenda__empty", text: "Pick another day — days with events are dotted on the grid." }));
  }
}

// The detail panel below the grid answers "what do we know about this event?"
// and keeps a link back to the original listing.
function renderEventCalendarDetail() {
  const detail = $("#calendar-detail");
  const content = $("#calendar-detail-content");
  const entry = state.calendarEntries.find((candidate) => candidate.event.id === state.calendarEventId);
  content.replaceChildren();
  if (!entry) {
    detail.hidden = true;
    return;
  }
  detail.hidden = false;
  const { event } = entry;
  const index = state.calendarEntries.indexOf(entry);
  const title = node("h3", { className: "calendar-detail__title" });
  title.append(safeLink(eventDisplayTitle(event), event.url));
  const facts = node("dl", { className: "calendar-detail__facts" });
  for (const [term, value] of eventPreviewFacts(event)) {
    facts.append(node("div", {}, [node("dt", { text: term }), node("dd", { text: value })]));
  }
  const link = safeLink("View full event ↗", event.url);
  if (link.nodeType === 1) link.className = "calendar-detail__link";
  content.append(
    node("span", { className: "card-meta", text: [eventDateLabel(event), event.category].filter(Boolean).join(" · ") }),
    title,
    node("p", { className: "calendar-detail__summary", text: event.summary }),
    facts,
    link,
    node("p", { className: "calendar-detail__position", text: `Event ${index + 1} of ${state.calendarEntries.length} in view` }),
  );
}

function selectCalendarEvent(eventId) {
  state.calendarEventId = eventId;
  for (const item of document.querySelectorAll("#calendar-agenda-list .agenda-item")) {
    const selected = item.dataset.eventId === eventId;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-pressed", String(selected));
  }
  renderEventCalendarDetail();
}

function stepCalendarEvent(delta) {
  if (!state.calendarEntries.length) return;
  const index = state.calendarEntries.findIndex((entry) => entry.event.id === state.calendarEventId);
  const next = (index + delta + state.calendarEntries.length) % state.calendarEntries.length;
  selectCalendarEvent(state.calendarEntries[next].event.id);
}

function renderEventCalendarGrid(events) {
  const grid = $("#calendar-grid");
  grid.replaceChildren();
  const [year, month] = state.calendarMonth.split("-").map(Number);
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const leading = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const cellCount = Math.ceil((leading + daysInMonth) / 7) * 7;
  for (let index = 0; index < cellCount; index += 1) {
    const dayNumber = index - leading + 1;
    const inMonth = dayNumber >= 1 && dayNumber <= daysInMonth;
    const date = new Date(Date.UTC(year, month - 1, dayNumber));
    const key = date.toISOString().slice(0, 10);
    const classes = ["calendar-day"];
    if (!inMonth) classes.push("calendar-day--outside");
    if (key === state.today) classes.push("calendar-day--today");
    if (key === state.calendarSelected) classes.push("calendar-day--selected");

    if (!inMonth) {
      grid.append(node("span", { className: classes.join(" "), "aria-hidden": "true" }, [
        node("span", { className: "calendar-day__number", text: String(date.getUTCDate()) }),
      ]));
      continue;
    }

    const dayEvents = eventsOnDay(key, events);
    if (dayEvents.length) classes.push("calendar-day--has-events");
    const cell = node("button", {
      className: classes.join(" "),
      type: "button",
      dataset: { date: key },
      "aria-pressed": String(key === state.calendarSelected),
      "aria-label": `${displayDate(key, { year: true })}, ${dayEvents.length ? `${dayEvents.length} event${dayEvents.length === 1 ? "" : "s"}` : "no events"}`,
    });
    cell.append(node("span", { className: "calendar-day__number", text: String(dayNumber) }));
    if (dayEvents.length) {
      const dots = node("span", { className: "calendar-day__dots", "aria-hidden": "true" });
      for (const event of dayEvents.slice(0, 4)) dots.append(node("i"));
      cell.append(dots);
    }
    grid.append(cell);
  }
}

function renderEventCalendar() {
  const events = fullEvents || uniqueEvents();
  $("#calendar-dialog-title").textContent = monthLabel(state.calendarMonth);
  renderEventCalendarGrid(events);
  renderEventCalendarAgenda(events);
  renderEventCalendarDetail();
}

function selectCalendarDay(key) {
  state.calendarSelected = key;
  state.calendarMonth = key.slice(0, 7);
  renderEventCalendar();
}

function setCalendarOpenState(open) {
  document.querySelectorAll("[data-calendar-open]").forEach((button) => button.setAttribute("aria-expanded", String(open)));
}

let calendarOpener = null;
async function openEventCalendar(openEvent) {
  calendarOpener = openEvent?.currentTarget || document.activeElement;
  const dialog = $("#calendar-dialog");
  state.calendarSelected = state.selectedDate;
  state.calendarMonth = state.selectedDate.slice(0, 7);
  state.calendarEventId = "";
  renderEventCalendar();
  setCalendarOpenState(true);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  $('#calendar-load-status').textContent='Loading the complete calendar…';
  $('#calendar-close').focus({ preventScroll: true });
  try {await ensureFullEvents(); if(dialog.open) renderEventCalendar(); $('#calendar-load-status').textContent='Complete calendar loaded.';}
  catch(error) {$('#calendar-load-status').textContent=`Showing selected events only. ${error.message}`;}

}

function closeEventCalendar() {
  const dialog = $("#calendar-dialog");
  setCalendarOpenState(false);
  if (dialog.open) dialog.close();
  else dialog.removeAttribute("open");
}

function bindEventCalendar() {
  document.querySelectorAll("[data-calendar-open]").forEach((button) => button.addEventListener("click", openEventCalendar));
  $("#calendar-close").addEventListener("click", closeEventCalendar);
  $("#calendar-previous").addEventListener("click", () => {
    state.calendarMonth = shiftMonth(state.calendarMonth, -1);
    renderEventCalendar();
  });
  $("#calendar-next").addEventListener("click", () => {
    state.calendarMonth = shiftMonth(state.calendarMonth, 1);
    renderEventCalendar();
  });
  $("#calendar-today").addEventListener("click", () => selectCalendarDay(state.today));
  $("#calendar-grid").addEventListener("click", (clickEvent) => {
    const cell = clickEvent.target.closest("[data-date]");
    if (cell) selectCalendarDay(cell.dataset.date);
  });
  $("#calendar-agenda-list").addEventListener("click", (clickEvent) => {
    const item = clickEvent.target.closest("[data-event-id]");
    if (item) selectCalendarEvent(item.dataset.eventId);
  });
  $("#calendar-detail-previous").addEventListener("click", () => stepCalendarEvent(-1));
  $("#calendar-detail-next").addEventListener("click", () => stepCalendarEvent(1));
  $("#calendar-dialog").addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "ArrowRight") {
      keyEvent.preventDefault();
      stepCalendarEvent(1);
    } else if (keyEvent.key === "ArrowLeft") {
      keyEvent.preventDefault();
      stepCalendarEvent(-1);
    }
  });
  $("#calendar-dialog").addEventListener("close", () => {setCalendarOpenState(false); calendarOpener?.focus({preventScroll:true});});
  $("#calendar-dialog").addEventListener("click", (clickEvent) => {
    if (clickEvent.target === $("#calendar-dialog")) closeEventCalendar();
  });
}

function uniqueEvents() {
  const events = state.data.events;
  const map = new Map();
  for (const item of Array.isArray(events?.events) ? events.events : []) map.set(item.id, item);
  return [...map.values()];
}

function eventStartDate(event) {
  return eventDay(event.start);
}

function eventEndDate(event) {
  return eventDay(event.end || event.start);
}

// An event with a multi-day window stays listed after single-day happenings so a
// long-running exhibition never buries the one-night-only items happening today.
function eventSpansDays(event) {
  const start = eventStartDate(event);
  const end = eventEndDate(event);
  return Boolean(start && end && start !== end);
}

function compareEventsForDisplay(a, b) {
  return Number(eventSpansDays(a)) - Number(eventSpansDays(b))
    || Date.parse(a.start) - Date.parse(b.start)
    || (b.score || 0) - (a.score || 0);
}

// The calendar doubles as the rating-tuning surface: within a day the highest
// rated events lead, with long-running windows always kept at the bottom.
function compareEventsByRating(a, b) { return rankEvents(a,b); }

function eventIsActiveOn(event, key) {
  const start = eventStartDate(event);
  const end = eventEndDate(event);
  if (!(start && end && start <= key && end >= key)) return false;
  if (key !== state.today || !event.end) return true;
  const closingTime = Date.parse(event.end);
  return Number.isNaN(closingTime) || closingTime > Date.now();
}

function scheduleEventExpiry(events) {
  window.clearTimeout(state.eventExpiryTimer);
  state.eventExpiryTimer = null;
  if (state.selectedDate !== state.today) return;
  const now = Date.now();
  const nextClosingTime = events
    .filter((event) => eventStartDate(event) <= state.today && eventEndDate(event) >= state.today && event.end)
    .map((event) => Date.parse(event.end))
    .filter((time) => !Number.isNaN(time) && time > now)
    .sort((a, b) => a - b)[0];
  if (!nextClosingTime) return;
  const delay = Math.min(nextClosingTime - now + 250, 2147483647);
  state.eventExpiryTimer = window.setTimeout(() => renderEvents(), delay);
}

function relativeDayWord(key) {
  if (key === state.today) return "Today";
  return key < state.today ? "Yesterday" : "Tomorrow";
}

function feedbackControls(event) {
  const controls = node('div', { className: 'event-feedback', 'aria-label': `Feedback for ${event.title}` });
  for (const [action, label] of [['more', 'More like this'], ['less', 'Less like this'], ['hide', 'Hide this occurrence'], ['favorite', preferences.get().favorites.includes(seriesKey(event)) ? 'Unfavorite' : 'Favorite']]) {
    const button = node('button', { type: 'button', text: label });
    button.addEventListener('click', async () => {
      button.disabled=true;
      try { await ensureFullEvents(); } catch { /* Apply to saved candidates when offline. */ }
      const persisted = preferences.set(feedback(preferences.get(), event, action));
      feedbackNotice = persisted ? 'Feedback saved on this browser. Undo is available.' : 'Feedback applied for this session; browser storage is unavailable.';
      if(!fullEvents) feedbackNotice += ' Full candidates are unavailable; using the saved selection.';
      renderEvents();
      $('#recommendation-status').focus({ preventScroll: true });
    });
    controls.append(button);
  }
  return controls;
}

function recommendationDetails(event, ranking) {
  const details = node('details', { className: 'recommendation-details' });
  details.append(node('summary', { text: 'Why this?' }));
  const row = ranking || rankEvent(event, { day: state.selectedDate, now: Date.now(), preferences: preferences.get(), weights: rankingWeights });
  details.append(node('p', { text: row.ineligible ? `Not in best bets: ${row.ineligible}` : row.reasons.join(' · ') || 'Neutral fit; no recorded preferences.' }));
  details.append(node('p', { text: `Taste ${row.components.taste} · Practicality ${row.components.practicality} · Novelty ${row.components.novelty} · Data quality ${row.components.quality}. Ranking points, not a probability.` }));
  if (row.unknowns.length) details.append(node('p', { text: row.unknowns.join(' · ') }));
  details.append(feedbackControls(event));
  return details;
}

function renderPreferenceControls() {
  let panel = $('#recommendation-controls');
  if (!panel) {
    panel = node('div', { id: 'recommendation-controls', className: 'recommendation-controls' });
    $('#today-lane').before(panel);
  }
  const button = (label, action) => { const b = node('button', { type: 'button', text: label }); b.addEventListener('click', action); return b; };
  const toggle = button('See all events', () => { $('#events-all').open=true; $('#events-all').scrollIntoView({block:'start'}); });
  toggle.setAttribute('aria-pressed', String(showAllEvents));
  const undo = button('Undo feedback', () => { const saved = preferences.undo(); feedbackNotice = saved ? 'Last preference change undone.' : 'Undone for this session; browser storage is unavailable.'; renderEvents(); });
  undo.disabled = !preferences.canUndo();
  const settings = node('details');
  settings.append(node('summary', { text: 'Recommendation preferences' }), node('p', { text: 'Household profile on this browser. More/less adjusts this event category; favorites apply to its series. Feedback stays on this device.' }));
  const form = node('form', { className: 'recommendation-settings' });
  const p = preferences.get();
  const count = node('input', { type: 'number', min: 1, max: 20, value: p.ranking.limit, required: '' });
  const distance = node('input', { type: 'number', min: 0, step: 'any', value: p.constraints.maxDistanceMiles ?? '', placeholder: 'No limit' });
  const exact = node('input', { type: 'checkbox' }); exact.checked = p.constraints.selectedDayOnly;
  form.append(node('label', {}, ['Best bets: ', count].map(x => typeof x === 'string' ? document.createTextNode(x) : x)), node('label', {}, [document.createTextNode('Maximum miles (unknown distances excluded when set): '), distance]), node('label', {}, [exact, document.createTextNode('Selected day only')]), node('button', { type: 'submit', text: 'Save preferences' }));
  form.addEventListener('submit', e => { e.preventDefault(); const next = preferences.get(); next.ranking.limit = Number(count.value); next.constraints.maxDistanceMiles = distance.value === '' ? null : Number(distance.value); next.constraints.selectedDayOnly = exact.checked; const saved = preferences.set(next); feedbackNotice = saved ? 'Preferences saved on this browser.' : 'Applied for this session; browser storage is unavailable.'; renderEvents(); });
  settings.append(form, node('p', { text: `${p.hiddenOccurrences.length} hidden occurrences · ${p.favorites.length} favorites · Category adjustments: ${Object.entries(p.topicAffinities).map(([name, value]) => `${name} ${value > 0 ? '+' : ''}${value}`).join(', ') || 'none'}` }), button('Reset preferences and feedback', () => { const saved = preferences.reset(); feedbackNotice = saved ? 'Preferences reset. Undo is available.' : 'Reset for this session; browser storage is unavailable.'; renderEvents(); }));
  panel.replaceChildren(toggle, button('Open full calendar', openEventCalendar), undo, settings, node('p', { id: 'recommendation-status', role: 'status', tabindex: '-1', text: feedbackNotice }));
}

function eventCard(event, ranking) {
  const title = node("h3");
  title.append(safeLink(eventDisplayTitle(event), event.url));
  return node("article", { className: "card", dataset:{eventId:event.id} }, [
    node("span", { className: "card-meta", text: [eventDateLabel(event), event.category].filter(Boolean).join(" · ") }),
    title,
    node("p", { text: event.summary }),
    node("p", { className: "card__footer", text: [event.venue, event.city, event.price || 'Price unknown', event.distanceMiles != null ? `${event.distanceMiles} mi` : 'Distance unknown', eventStatus(event) === 'cancelled' ? 'CANCELED' : ''].filter(Boolean).join(" · ") }),
    recommendationDetails(event, ranking),
    detailButton(event),
  ]);
}

function claimCard(event) {
  const title = node("strong");
  title.append(safeLink(eventDisplayTitle(event), event.url));
  return node("article", {
    className: "claim",
    dataset: { eventId: event.id },
    "aria-haspopup": "dialog",
    "aria-expanded": "false",
  }, [
    node("time", { text: eventDateLabel(event), datetime: eventStartDate(event) }),
    title,
    detailButton(event),
  ]);
}

// Three lanes: single-day (and multi-day that ends on the selected date) first,
// then multi-day windows still running past it, then everything starting later.
function renderEvents() {
  hideEventPreview();
  const allEvents = fullEvents || uniqueEvents();
  const selected = state.selectedDate;
  renderPreferenceControls();
  const best = selectBestBets(allEvents, { day: selected, now: Date.now(), preferences: preferences.get(), weights: rankingWeights });
  const horizon = shiftDate(selected, preferences.get().ranking.horizonDays);
  const visible = best.map(row => row.event);
  const rows = new Map(best.map(row => [row.event, row]));
  replaceChildren('#events-list', visible.length ? visible.map(event => eventCard(event, rows.get(event))) : [node('p', { text: 'No matching best bets. Open the full calendar or adjust your preferences.' })]);
  $('#today-lane-title').textContent = `${visible.length} best bets`;
  $('#ongoing-lane').hidden = true;
  $('#plan-ahead').hidden = true;
  replaceChildren('#ongoing-list', []);
  replaceChildren('#claims-list', []);
  $('#events-title').textContent = 'Nearby & notable';
  $('#events-note').textContent = 'A varied shortlist for the selected day and weeks ahead. Use “Why this?” to tune it.';
  if(showAllEvents && fullEvents) {
    const all=eventsOnDay(selected,fullEvents,compareEventsByRating);
    replaceChildren('#events-all-list',all.length ? all.map(event=>eventCard(event)) : [emptyState()]);
    $('#events-all-summary').textContent=`See all · ${all.length} events on this date`;
  }
  scheduleEventExpiry(allEvents);
  refreshShelfControls();
}

function detailButton(event) {
  const button=node('button',{type:'button',className:'event-details-button',text:'Details','aria-label':`Details for ${event.title}`,'aria-haspopup':'dialog'});
  button.addEventListener('click',()=>openEventDetails(event,button));
  return button;
}
function openEventDetails(event,opener) {
  hideEventPreview();
  const dialog=$('#event-details-dialog');
  $('#event-details-title').textContent=event.title;
  const facts=node('dl',{className:'event-preview__facts'});
  for(const [term,value] of eventPreviewFacts(event)) facts.append(node('div',{},[node('dt',{text:term}),node('dd',{text:value})]));
  const signals=Object.entries(event.scoring?.signals || {}).sort((a,b)=>b[1]-a[1]).slice(0,6).map(([name,value])=>`${name.replaceAll('_',' ')}: ${Math.round(value*100)}%`).join(' · ');
  $('#event-details-content').replaceChildren(node('p',{text:event.summary}),facts,
    node('p',{text:signals ? `Score signals (estimated topic matches): ${signals}. The rating combines these signals with distance and preferences.` : 'Score explanation is not available for this event.'}),safeLink('Visit website ↗',event.url));
  dialog.onclose=()=>{if(opener.isConnected) opener.focus({preventScroll:true});else $('[data-calendar-open]').focus();};
  dialog.showModal();$('#event-details-close').focus();
}

function eventPreviewFacts(event) {
  return [
    ["Rating", event.score != null ? `${event.score} / 100` : ""],
    ["When", `${eventDateLabel(event)} · ${displayTime(event.start)}${event.end ? ` – ${displayTime(event.end)}` : " (end time not supplied)"}`],
    ["Booking deadline", event.deadline],
    ["Where", [event.venue, event.city, event.region].filter(Boolean).join(" · ")],
    ["Price", event.price],
    ["Registration", event.registration],
    ["Distance", event.distanceMiles != null ? `${event.distanceMiles} miles` : ""],
  ].filter(([, value]) => value);
}

function positionEventPreview(anchor) {
  const preview = $("#event-preview");
  const anchorRect = anchor.getBoundingClientRect();
  const previewRect = preview.getBoundingClientRect();
  const gutter = 12;
  const left = Math.min(
    window.innerWidth - previewRect.width - gutter,
    Math.max(gutter, anchorRect.left + anchorRect.width / 2 - previewRect.width / 2),
  );
  const above = anchorRect.top - previewRect.height - 10;
  const top = above >= gutter
    ? above
    : Math.min(window.innerHeight - previewRect.height - gutter, anchorRect.bottom + 10);
  preview.style.left = `${Math.max(gutter, left)}px`;
  preview.style.top = `${Math.max(gutter, top)}px`;
  preview.style.visibility = "visible";
}

function showEventPreview(claim, { focus = false } = {}) {
  window.clearTimeout(claimHoverTimer);
  window.clearTimeout(claimHideTimer);
  const event = uniqueEvents().find((item) => item.id === claim.dataset.eventId);
  if (!event) return;

  if (activeClaim && activeClaim !== claim) activeClaim.setAttribute("aria-expanded", "false");
  activeClaim = claim;
  claim.setAttribute("aria-expanded", "true");
  const preview = $("#event-preview");
  const title = node("h3");
  title.append(safeLink(eventDisplayTitle(event), event.url));
  const facts = node("dl", { className: "event-preview__facts" });
  for (const [term, value] of eventPreviewFacts(event)) {
    facts.append(node("div", {}, [node("dt", { text: term }), node("dd", { text: value })]));
  }
  const close = node("button", { className: "event-preview__close", type: "button", text: "×", "aria-label": "Close event details" });
  close.addEventListener("click", () => hideEventPreview({ restoreFocus: true }));
  preview.replaceChildren(node("article", { className: "card event-preview__card" }, [
    close,
    node("span", { className: "card-meta", text: [eventDateLabel(event), event.category].filter(Boolean).join(" · ") }),
    title,
    node("p", { text: event.summary }),
    facts,
  ]));
  preview.hidden = false;
  preview.style.visibility = "hidden";
  positionEventPreview(claim);
  if (focus) preview.focus({ preventScroll: true });
}

function hideEventPreview({ restoreFocus = false } = {}) {
  window.clearTimeout(claimHoverTimer);
  window.clearTimeout(claimHideTimer);
  const preview = $("#event-preview");
  if (!preview) return;
  const claim = activeClaim;
  if (claim) claim.setAttribute("aria-expanded", "false");
  activeClaim = null;
  preview.hidden = true;
  preview.style.visibility = "hidden";
  if (restoreFocus) claim?.querySelector("a")?.focus({ preventScroll: true });
}

function scheduleEventPreviewHide() {
  window.clearTimeout(claimHideTimer);
  claimHideTimer = window.setTimeout(() => hideEventPreview(), 180);
}

function bindClaimDetails() {
  const surface = $("#events-section");
  const preview = $("#event-preview");
  surface.addEventListener("pointerover", (pointerEvent) => {
    if (pointerEvent.pointerType && pointerEvent.pointerType !== "mouse") return;
    const claim = pointerEvent.target.closest(".claim");
    if (!claim || claim.contains(pointerEvent.relatedTarget)) return;
    window.clearTimeout(claimHideTimer);
    window.clearTimeout(claimHoverTimer);
    claimHoverTimer = window.setTimeout(() => showEventPreview(claim), 700);
  });
  surface.addEventListener("pointerout", (pointerEvent) => {
    const claim = pointerEvent.target.closest(".claim");
    if (!claim || claim.contains(pointerEvent.relatedTarget) || preview.contains(pointerEvent.relatedTarget)) return;
    window.clearTimeout(claimHoverTimer);
    scheduleEventPreviewHide();
  });
  surface.addEventListener("contextmenu", (contextEvent) => {
    const claim = contextEvent.target.closest(".claim");
    if (!claim) return;
    contextEvent.preventDefault();
    showEventPreview(claim);
  });
  surface.addEventListener("keydown", (keyEvent) => {
    if (!(keyEvent.key === "ContextMenu" || (keyEvent.shiftKey && keyEvent.key === "F10"))) return;
    const claim = keyEvent.target.closest(".claim");
    if (!claim) return;
    keyEvent.preventDefault();
    showEventPreview(claim, { focus: true });
  });
  preview.addEventListener("pointerenter", () => window.clearTimeout(claimHideTimer));
  preview.addEventListener("pointerleave", scheduleEventPreviewHide);
  for (const selector of ["#ongoing-list", "#claims-list"]) {
    $(selector).addEventListener("scroll", () => hideEventPreview(), { passive: true });
  }
  document.addEventListener("pointerdown", (pointerEvent) => {
    if (activeClaim && !activeClaim.contains(pointerEvent.target) && !preview.contains(pointerEvent.target)) hideEventPreview();
  });
  document.addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "Escape" && !preview.hidden) hideEventPreview({ restoreFocus: true });
  });
  window.addEventListener("scroll", () => hideEventPreview(), { passive: true });
  window.addEventListener("resize", () => activeClaim && positionEventPreview(activeClaim));
}

let lastScienceSlot;
function refreshScienceRotation() {
  const slot = `${scienceDate(new Date().toISOString())}:${scienceRotationSlot()}`;
  if (!document.hidden && slot !== lastScienceSlot) renderStories("geeknews");
}

function renderStories(kind) {
  const data = state.data[kind];
  const selector = kind === "news" ? "#news-list" : "#geek-list";
  const noteSelector = kind === "news" ? "#news-note" : "#geek-note";
  const section = kind === "news" ? "news" : "science-technology";
  const editionAvailable = data?.editionDate && data.editionDate <= state.selectedDate;
  const availableStories = editionAvailable ? (fullStories[kind]?.stories || data.stories || []) : [];
  const scienceNow = Date.now();
  const scienceDoc = { ...data, stories: availableStories };
  const digest = kind === "geeknews" ? scienceView(scienceDoc, state.selectedDate, scienceNow) : null;
  if (digest) lastScienceSlot = `${scienceDate(new Date(scienceNow).toISOString())}:${scienceRotationSlot(scienceNow)}`;
  const stories = kind === "news"
    ? orderRankedNews(availableStories, { now: Date.now(), preferences: preferences.get() }).slice(0,storyLimits[kind])
    : digest.stories;
  const renderStory = (story) => {
    const title = node("h3");
    title.append(safeLink(story.title, story.url));
    const source = [story.source?.name, story.source?.publication].filter(Boolean).join(" · ");
    return node("article", { className: "story" }, [
      node("div", {}, [title, node("p", { text: story.finding || story.summary }),
        kind === "geeknews" && story.significance ? node("details", {}, [node("summary", { text: "Why it matters" }), node("p", { text: story.significance })]) : null,
        kind === "geeknews" ? node("p", { className: "story__caveat", text: `Limitations: ${story.caveat || "Evidence limitations not verified."}` }) : null,
        kind === "geeknews" ? node("p", { className: "story__source", text: `Published ${scienceDate(story.publishedAt)} · ${story.evidenceType || "unknown"}${story.verifiedAt ? "" : " · archived, not reverified"}` }) : null,
        kind === "geeknews" ? safeLink(`Read the primary source: ${story.source?.name || story.title}`, story.primarySourceUrl || story.url) : null,
        kind === "geeknews" && story.paperUrl ? safeLink("Read the linked research paper", story.paperUrl) : null,
        kind === "geeknews" && digest.previously[story.id]?.length ? node("details", {}, [node("summary", { text: "Previously" }), ...digest.previously[story.id].map(previous => node("p", {}, [safeLink(`${scienceDate(previous.publishedAt)}: ${previous.title}`, previous.url)]))]) : null,
      ]),
      node("p", { className: "story__source", text: source }),
    ]);
  };
  const children = stories.map(renderStory);
  if (kind === "geeknews" && digest.archive.length) children.push(node("details", { className: "story science-archive" }, [
    node("summary", { text: `Archive / background (${digest.archive.length})` }), ...digest.archive.slice(0,storyLimits[kind]).map(renderStory),
  ]));
  replaceChildren(selector, children.length ? children : [emptyState()]);

  const more = document.querySelector(`[data-more-stories="${kind}"]`);
  more.hidden = Boolean(fullStories[kind] && storyLimits[kind] >= availableStories.length);
  if (!more.disabled) more.textContent = `More ${kind === 'news' ? 'local' : 'science'} stories · ${stories.length} shown`;
  const carryForward = data?.editionDate && data.editionDate !== state.selectedDate && editionAvailable
    ? `Latest available digest: ${displayDate(data.editionDate)}.`
    : "";
  const fallback = kind === "news" ? "What is moving around Michigan." : "Interesting machinery, ideas, and horizons.";
  const freshness = scienceFreshness(data, state.data.scienceHealth);
  const scienceNote = kind === "geeknews" ? scienceEditorial(scienceContext(scienceDoc, state.data.scienceHealth, state.selectedDate, scienceNow)).summary : "";
  const checkLabel = freshness.lastChecked ? new Date(freshness.lastChecked).toLocaleString("en-US", { timeZone: TIME_ZONE }) : "not verified";
  const scienceStatus = `Last checked: ${checkLabel}. Newest verified story: ${scienceDate(freshness.newestStory)}.${freshness.stale ? " Science collection is overdue or incomplete; showing the last valid edition." : ""}`;
  $(noteSelector).textContent = kind === "geeknews" ? [scienceNote, scienceStatus, carryForward].filter(Boolean).join(" ")
    : [message(section, "summary", fallback), carryForward].filter(Boolean).join(" ");
  if (kind === "news") $("#news-title").textContent = message(section, "section-heading", "The local signal");
  else $("#geek-title").textContent = "Science & technology";
  refreshShelfControls();
}

function launchLink(launch) {
  return launch.slug ? `https://www.rocketlaunch.live/launch/${launch.slug}` : "https://www.rocketlaunch.live/";
}

function renderRocketLaunches() {
  const target = $("#rocket-launches");
  target.replaceChildren();
  const launches = state.launches;
  target.hidden = launches.length === 0;
  if (target.hidden) return;

  const todaysLaunches = launches.filter((launch) => launchDateKey(launch) === state.today);
  const isUseful = (launch) => {
    const name = launch.name || launch.missions?.[0]?.name || "";
    return name.toLowerCase() !== "tbd" && !launch.vehicle?.name?.toLowerCase().includes("unconfirmed");
  };
  const featured = todaysLaunches.find(isUseful) || launches.find(isUseful) || todaysLaunches[0] || launches[0];
  const line = node("div", { className: "rocket-launches__line" });
  if (featured) {
    const title = todaysLaunches.length
      ? `${todaysLaunches.length} launch${todaysLaunches.length === 1 ? "" : "es"} targeted today`
      : "Next in the worldwide feed";
    line.append(node("strong", { text: title }), document.createTextNode(" · "));
    line.append(safeLink(featured.name || featured.missions?.[0]?.name || "Scheduled launch", launchLink(featured)));
    const location = featured.pad?.location?.name;
    line.append(document.createTextNode(` · ${launchDateTime(featured)}${location ? ` · ${location}` : ""}`));
    if (todaysLaunches.length > 1) line.append(document.createTextNode(` · +${todaysLaunches.length - 1} more`));
  }
  if (featured?.t0 || featured?.win_open) {
    line.title = `Source time: ${featured.t0 || featured.win_open}`;
  }
  if (state.launchCacheFallback) {
    line.append(node("span", { text: ` · Cached feed from ${new Date(state.launchFetchedAt).toLocaleString("en-US", { timeZone: TIME_ZONE })} Detroit time (live feed unavailable; maximum age 6 hours).` }));
  }

  const attribution = safeLink("Data by RocketLaunch.Live", "https://www.rocketlaunch.live/");
  if (attribution.nodeType === Node.ELEMENT_NODE) attribution.className = "rocket-launches__source";
  target.append(
    node("span", { className: "rocket-launches__icon", text: "↗", "aria-hidden": "true" }),
    line,
    attribution,
  );
}

function renderStarship() {
  const wasOpen = $("#starship-status details")?.open;
  const record = state.data.starship;
  const view = starshipView(record);
  const card = $("#starship-status");
  const timestamp = (value) => value ? new Date(value).toLocaleString("en-US", { timeZone: TIME_ZONE, timeZoneName: "short" }) : "Not yet verified";
  const official = node("p", {}, [node("strong", { text: "Official target: " }), document.createTextNode(view.officialTarget ? targetLabel(view.officialTarget.target) : "No current verified target.")]);
  if (view.officialTarget) official.append(document.createTextNode(" · "), safeLink("Operator announcement", view.officialTarget.sourceUrl));
  const children = [
    node("h3", { id: "starship-title", text: record?.mission.label || "Starship status" }),
    node("p", { className: "starship-status__summary", text: view.summary }),
    official,
    node("p", {}, [node("strong", { text: "Daily Brief forecast: " }), document.createTextNode(record?.mode === "shadow" ? "Under evaluation; no forecast published yet." : view.forecast?.summary || "Insufficient current evidence.")]),
    node("p", { className: "story__source", text: `Current status · Last checked: ${timestamp(record?.lastAttemptAt)} · Last verified: ${timestamp(view.lastVerifiedAt)}${view.fresh ? "" : " · Evidence stale or unavailable"}` }),
    node("p", { text: `What changed: ${record?.whatChanged || "Awaiting the first independent source collection."}` }),
  ];
  for (const id of record?.outsideReports || []) {
    const report = record.evidence.find((e) => e.id === id);
    if (report?.target) children.push(node("p", {}, [node("strong", { text: "Outside report (not an operator target): " }), safeLink(targetLabel(report.target), report.sourceUrl), document.createTextNode(` · observed ${timestamp(report.observedAt)}; see evidence for uncertainty.`)]));
  }
  const details = node("details", {}, [node("summary", { text: "Evidence, uncertainty and source health" })]);
  details.open = Boolean(wasOpen);
  details.append(node("p", { text: (view.uncertainty || []).join(" ") }));
  if (record?.officialTarget) details.append(node("p", { text: `Last recorded operator wording (${view.officialTarget ? "current" : "historical or disputed"}): ${record.officialTarget.target.label}. Precision: ${record.officialTarget.target.precision}; NET: ${record.officialTarget.target.net ? "yes" : "no"}; source timezone: ${record.officialTarget.target.timeZone}. Announced: ${timestamp(record.officialTarget.announcedAt)}.` }));
  for (const conflict of record?.conflicts || []) details.append(node("p", { text: conflict.explanation }));
  for (const evidence of record?.evidence || []) {
    details.append(node("p", {}, [safeLink(`${evidence.sourceType} · ${evidence.claimType} · ${evidence.verification}`, evidence.sourceUrl), document.createTextNode(` — ${evidence.excerpt} Published: ${evidence.publishedAt ? timestamp(evidence.publishedAt) : "unknown"}; observed: ${timestamp(evidence.observedAt)}. Claim confidence: ${evidence.claimConfidence}${evidence.target ? `; precision: ${evidence.target.precision}; NET: ${evidence.target.net ? "yes" : "no"}` : ""}.`)]));
  }
  for (const source of record?.sourceHealth || []) details.append(node("p", {}, [safeLink(source.id, source.url), document.createTextNode(`: ${source.state}. ${source.detail}`)]));
  for (const outcome of record?.outcomes || []) details.append(node("p", { text: `Recorded mission outcome: ${outcome.missionId} — ${outcome.outcome}${outcome.actualLiftoffAt ? `; liftoff ${timestamp(outcome.actualLiftoffAt)}` : ""}. This does not establish the next mission's target.` }));
  if (record?.previousSnapshot) details.append(safeLink("Previous evidence snapshot", new URL(record.previousSnapshot, window.location.href).href));
  children.push(details);
  card.replaceChildren(...children);
}

function readLaunchCache({ allowStale = false } = {}) {
  try {
    const cached = JSON.parse(localStorage.getItem(LAUNCH_CACHE_KEY) || "null");
    return usableLaunchCache(cached, allowStale);
  } catch {
    return null;
  }
}

function writeLaunchCache(launches) {
  try {
    localStorage.setItem(LAUNCH_CACHE_KEY, JSON.stringify({ fetchedAt: Date.now(), launches }));
  } catch { /* Storage is an optimization; the live request still works. */ }
}

async function loadRocketLaunches() {
  const cached = readLaunchCache();
  if (cached) {
    state.launches = cached.launches;
    state.launchFetchedAt = cached.fetchedAt;
    state.launchCacheFallback = false;
    renderRocketLaunches();
    return;
  }
  try {
    const response = await fetch(LAUNCH_API_URL, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error(`RocketLaunch.Live returned ${response.status}`);
    const payload = await response.json();
    const launchEnvelope = payload.response || payload;
    if (!Array.isArray(launchEnvelope.result)) throw new Error("RocketLaunch.Live returned an unexpected response");
    state.launches = launchEnvelope.result;
    state.launchCacheFallback = false;
    state.launchFetchedAt = Date.now();
    writeLaunchCache(state.launches);
  } catch {
    const fallback = readLaunchCache({ allowStale: true });
    state.launches = fallback?.launches || [];
    state.launchFetchedAt = fallback?.fetchedAt || null;
    state.launchCacheFallback = Boolean(fallback);
  }
  renderRocketLaunches();
}

function stopPhotoShow() {
  window.clearInterval(state.photoTimer);
  state.photoTimer = null;
}

function showPhoto(index, { restart = true } = {}) {
  const slides = [...document.querySelectorAll(".memory__slide")];
  const dots = [...document.querySelectorAll(".memory__dot")];
  if (!slides.length) return;
  state.photoIndex = (index + slides.length) % slides.length;
  slides.forEach((slide, position) => {
    const active = position === state.photoIndex;
    slide.classList.toggle("is-active", active);
    slide.setAttribute("aria-hidden", String(!active));
  });
  dots.forEach((dot, position) => {
    const active = position === state.photoIndex;
    dot.classList.toggle("is-active", active);
    dot.setAttribute("aria-current", active ? "true" : "false");
  });
  if (restart) startPhotoShow();
}

function startPhotoShow() {
  stopPhotoShow();
  const slideCount = document.querySelectorAll(".memory__slide").length;
  if (slideCount < 2 || document.hidden || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  state.photoTimer = window.setInterval(() => showPhoto(state.photoIndex + 1, { restart: false }), 11000);
}

function renderPhoto() {
  stopPhotoShow();
  state.photoIndex = 0;
  const day = state.data.photos?.days?.find((item) => item.date === state.selectedDate);
  const photos = (day?.photos || []).filter((photo) => safeImageUrl(photo.imageUrl));
  const section = $("#photo-section");
  section.hidden = photos.length === 0;
  replaceChildren("#photo-slides", []);
  replaceChildren("#photo-position", []);
  if (!photos.length) return;

  const slides = photos.map((photo, index) => {
    const image = node("img", {
      src: safeImageUrl(photo.imageUrl),
      alt: photo.description,
      loading: index === 0 ? "eager" : "lazy",
      decoding: "async",
    });
    const overlay = node("figcaption", { className: "memory__overlay" }, [
      node("p", { className: "memory__meta" }, [
        node("span", { text: `⌖ ${photo.location}` }),
        node("time", { text: displayDate(photo.takenDate, { year: true }), datetime: photo.takenDate }),
      ]),
    ]);
    return node("figure", {
      className: `memory__slide${index === 0 ? " is-active" : ""}`,
      "aria-hidden": String(index !== 0),
    }, [image, overlay]);
  });
  replaceChildren("#photo-slides", slides);

  const controls = $("#photo-controls");
  controls.hidden = photos.length < 2;
  if (photos.length > 1) {
    const dots = photos.map((photo, index) => {
      const dot = node("button", {
        className: `memory__dot${index === 0 ? " is-active" : ""}`,
        type: "button",
        "aria-label": `Show photo ${index + 1}: ${photo.description}`,
        "aria-current": index === 0 ? "true" : "false",
      });
      dot.addEventListener("click", () => showPhoto(index));
      return dot;
    });
    replaceChildren("#photo-position", dots);
    $("#photo-previous").onclick = () => showPhoto(state.photoIndex - 1);
    $("#photo-next").onclick = () => showPhoto(state.photoIndex + 1);
    section.onmouseenter = stopPhotoShow;
    section.onmouseleave = startPhotoShow;
    section.onfocusin = stopPhotoShow;
    section.onfocusout = startPhotoShow;
    startPhotoShow();
  }
}

function renderErrors() {
  const section = $("#data-errors");
  section.hidden = state.errors.length === 0;
  replaceChildren("#data-error-list", state.errors.map((error) => node("li", { text: error })));
}

function renderUpdatedLabel() {
  const timestamps = DOCUMENTS
    .map(([name]) => state.data[name]?.generatedAt)
    .filter(Boolean)
    .map((value) => new Date(value))
    .filter((value) => !Number.isNaN(value.valueOf()));
  if (!timestamps.length) return;
  const latest = new Date(Math.max(...timestamps));
  $("#updated-label").textContent = `Source data updated ${new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(latest)}.`;
}

function updateAtmosphere(forecast) {
  const root = document.documentElement;
  const month = Number(state.selectedDate.slice(5, 7));
  root.dataset.season = [12, 1, 2].includes(month) ? "winter" : [3, 4, 5].includes(month) ? "spring" : [6, 7, 8].includes(month) ? "summer" : "autumn";
  const appearance = weatherAppearance({ date: state.selectedDate, today: state.today, forecast, current: state.data.weather?.current });
  root.dataset.weather = appearance.family;
  root.dataset.wind = appearance.wind;
  root.dataset.precipitation = appearance.intensity;
  const label = $("#weather-atmosphere-note");
  label.hidden = appearance.source === "unavailable";
  label.textContent = appearance.label;
  // One bounded set of decorative particles, reused across refreshes and dates.
  const precipitation = $("#sky-precipitation");
  if (!precipitation.childElementCount) {
    precipitation.append(...Array.from({ length: 48 }, (_, index) => {
      const particle = node("i");
      particle.style.setProperty("--x", `${(index * 37) % 103}%`);
      particle.style.setProperty("--delay", `${-((index * 13) % 31)}s`);
      particle.style.setProperty("--duration", `${7 + (index % 7)}s`);
      particle.style.setProperty("--size", `${2 + (index % 4)}px`);
      return particle;
    }));
  }
  updateSky();
}

function updateSky() {
  const now = new Date();
  const root = document.documentElement;
  const todayForecast = state.data.weather?.daily?.find((day) => day.date === state.today);
  const sunrise = todayForecast?.sunrise ? new Date(todayForecast.sunrise) : new Date(`${state.today}T06:45:00-04:00`);
  const sunset = todayForecast?.sunset ? new Date(todayForecast.sunset) : new Date(`${state.today}T20:15:00-04:00`);
  const dayLength = sunset - sunrise;
  const isDay = now >= sunrise && now <= sunset;
  let progress;

  if (isDay) {
    progress = Math.min(1, Math.max(0, (now - sunrise) / dayLength));
  } else {
    const previousSunset = now < sunrise ? new Date(sunset.getTime() - 86400000) : sunset;
    const nextSunrise = now < sunrise ? sunrise : new Date(sunrise.getTime() + 86400000);
    progress = Math.min(1, Math.max(0, (now - previousSunset) / (nextSunrise - previousSunset)));
  }

  const x = 8 + progress * 84;
  const y = 72 - Math.sin(progress * Math.PI) * 58;
  root.style.setProperty("--celestial-x", `${x.toFixed(2)}%`);
  root.style.setProperty("--celestial-y", `${y.toFixed(2)}%`);
  root.style.setProperty("--sun-visible", isDay ? "1" : "0");
  root.style.setProperty("--moon-visible", isDay ? "0" : "1");

  const hour = Number(new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, hour: "numeric", hourCycle: "h23" }).format(now));
  root.dataset.skyPhase = hour < 5 ? "night" : hour < 8 ? "dawn" : hour < 12 ? "morning" : hour < 18 ? "afternoon" : hour < 21 ? "dusk" : "night";
  // Continuous inputs let the theme interpolate rather than jump at the clock boundaries.
  const daylight = isDay ? Math.sin(progress * Math.PI) : 0;
  root.style.setProperty("--sky-light", `${(isDay ? 24 + daylight * 10 : 10 + Math.sin(progress * Math.PI) * 5).toFixed(1)}%`);
  root.style.setProperty("--sky-hue", `${(isDay ? 198 - progress * 8 : 218 + Math.sin(progress * Math.PI) * 10).toFixed(1)}`);
}

function updateNavigation() {
  const previous = $("#previous-day");
  const next = $("#next-day");
  previous.disabled = state.selectedDate <= shiftDate(state.today, -1);
  next.disabled = state.selectedDate >= shiftDate(state.today, 1);
  $("#today-button").disabled = state.selectedDate === state.today;
  previous.setAttribute("aria-label", `Show ${displayDate(shiftDate(state.selectedDate, -1))}`);
  next.setAttribute("aria-label", `Show ${displayDate(shiftDate(state.selectedDate, 1))}`);
}

function updateShelfControl(shelf) {
  const track = shelf.querySelector(".shelf__track");
  const previous = shelf.querySelector("[data-shelf-previous]");
  const next = shelf.querySelector("[data-shelf-next]");
  if (!track || !previous || !next) return;
  const overflow = track.scrollWidth > track.clientWidth + 2;
  previous.hidden = !overflow;
  next.hidden = !overflow;
  if (!overflow) return;
  previous.disabled = track.scrollLeft <= 2;
  next.disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 2;
}

function refreshShelfControls() {
  window.requestAnimationFrame(() => {
    document.querySelectorAll("[data-shelf]").forEach(updateShelfControl);
  });
}

function bindShelfControls() {
  document.querySelectorAll("[data-shelf]").forEach((shelf) => {
    const track = shelf.querySelector(".shelf__track");
    const previous = shelf.querySelector("[data-shelf-previous]");
    const next = shelf.querySelector("[data-shelf-next]");
    if (!track || !previous || !next) return;
    const move = (direction) => track.scrollBy({ left: direction * track.clientWidth * .88, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    previous.addEventListener("click", () => move(-1));
    next.addEventListener("click", () => move(1));
    track.addEventListener("scroll", () => updateShelfControl(shelf), { passive: true });
    updateShelfControl(shelf);
  });
  window.addEventListener("resize", refreshShelfControls);
}

function render() {
  renderMasthead();
  renderWeather();
  renderCalendar();
  renderEvents();
  renderPhoto();
  renderStories("news");
  renderStories("geeknews");
  renderRocketLaunches();
  renderStarship();
  renderUpdatedLabel();
  updateNavigation();
  refreshShelfControls();
  if ($("#calendar-dialog")?.open) renderEventCalendar();
}

function selectDate(key, { history = true } = {}) {
  const requestedDate = key;
  state.selectedDate = clampDate(requestedDate);
  if (history) {
    const url = new URL(window.location);
    if (state.selectedDate === state.today) url.searchParams.delete("date");
    else url.searchParams.set("date", state.selectedDate);
    window.history.pushState({ date: state.selectedDate }, "", url);
  } else if (requestedDate !== state.selectedDate) {
    const url = new URL(window.location);
    if (state.selectedDate === state.today) url.searchParams.delete("date");
    else url.searchParams.set("date", state.selectedDate);
    window.history.replaceState({ date: state.selectedDate }, "", url);
  }
  render();
  loadJoke();
  window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
}

const fallbackJokes = [
  "I only know 25 letters of the alphabet. I don’t know y.",
  "What do you call a factory that makes okay products? A satisfactory.",
  "I used to hate facial hair, but then it grew on me.",
];

async function loadJoke({ force = false } = {}) {
  const key = `daily-brief-joke:${state.selectedDate}`;
  if (!force) {
    try {
      const cached = localStorage.getItem(key);
      if (cached) {
        $("#dad-joke").textContent = cached;
        return;
      }
    } catch { /* Storage may be unavailable; the joke still works. */ }
  }

  $("#dad-joke").textContent = "Warming up the punchline…";
  try {
    const response = await fetch("https://icanhazdadjoke.com/", { signal:AbortSignal.timeout(8000), headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("joke service unavailable");
    const { joke } = await response.json();
    if (!joke) throw new Error("empty joke");
    $("#dad-joke").textContent = joke;
    try { localStorage.setItem(key, joke); } catch { /* Nonessential cache. */ }
  } catch {
    const index = Number(state.selectedDate.replaceAll("-", "")) % fallbackJokes.length;
    $("#dad-joke").textContent = fallbackJokes[index];
  }
}

function bindEvents() {
  $("#previous-day").addEventListener("click", () => selectDate(shiftDate(state.selectedDate, -1)));
  $("#next-day").addEventListener("click", () => selectDate(shiftDate(state.selectedDate, 1)));
  $("#today-button").addEventListener("click", () => selectDate(state.today));
  $("#new-joke").addEventListener("click", () => loadJoke({ force: true }));
  document.querySelectorAll('[data-more-stories]').forEach(button=>button.addEventListener('click',async()=>{
    const kind=button.dataset.moreStories;button.disabled=true;button.textContent='Loading archive…';
    const editionId=manifest?.editionId;
    try {
      if(!fullStories[kind]) {
        const data=validateSection(kind,await fetchJSON(`${kind}.json`,{sha256:manifest?.archives?.[kind]?.sha256,timeout:12000}));
        if(manifest?.editionId!==editionId) throw new Error('Edition changed. Try again.');
        fullStories[kind]=data;
      }
      storyLimits[kind]+=20;button.disabled=false;renderStories(kind);
    } catch(error){button.disabled=false;button.textContent=`Retry archive: ${error.message}`;}
  }));
  $('#events-section').addEventListener('toggle',async event=>{
    if((event.target.closest('#recommendation-controls') || event.target.classList.contains('recommendation-details')) && event.target.open) {
      try {await ensureFullEvents();} catch(error){feedbackNotice=`Full candidates unavailable: ${error.message}`;}
    }
  },true);
  $('#refresh-brief').addEventListener('click',()=>{$('#refresh-brief').disabled=true;refreshData();});
  $('#event-details-close').addEventListener('click',()=>$('#event-details-dialog').close());
  $('#family-calendar').addEventListener('toggle',()=>{if($('#family-calendar').open) loadFamilyCalendar();});
  $('#events-all').addEventListener('toggle',async()=>{
    showAllEvents=$('#events-all').open;
    if(!showAllEvents) return;
    $('#events-all-summary').textContent='Loading all events…';
    try {await ensureFullEvents();renderEvents();} catch(error){$('#events-all-summary').textContent=`Unavailable. Close and reopen to retry. ${error.message}`;}
  });
  document.querySelectorAll('.section-nav a').forEach(link=>link.addEventListener('click',event=>{
    const target=document.querySelector(link.getAttribute('href'));if(!target) return;
    event.preventDefault();target.focus({preventScroll:true});
    window.scrollTo({top:target.getBoundingClientRect().top+window.scrollY-document.querySelector('.section-nav').getBoundingClientRect().height-12,behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth'});
    history.replaceState(history.state,'',link.getAttribute('href'));
  }));
  bindShelfControls();
  bindClaimDetails();
  document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('keydown',event=>{
    if(event.key!=='Tab') return;
    const focusable=[...dialog.querySelectorAll('button:not(:disabled),a[href],summary,[tabindex="0"]')].filter(el=>el.getClientRects().length);
    const first=focusable[0],last=focusable.at(-1);
    if(event.shiftKey && document.activeElement===first) {event.preventDefault();last?.focus();}
    else if(!event.shiftKey && document.activeElement===last) {event.preventDefault();first?.focus();}
  }));
  bindEventCalendar();
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden) stopPhotoShow();
    else {startPhotoShow();refreshScienceRotation();if(Date.now()-lastCheck>REFRESH_MS || state.today!==dateKey(new Date())) refreshData();}
  });
  window.addEventListener('online',()=>refreshData());
  window.addEventListener("popstate", () => selectDate(new URL(window.location).searchParams.get("date") || state.today, { history: false }));
}

async function init() {
  const requestedDate = new URL(window.location).searchParams.get("date") || state.today;
  state.selectedDate = clampDate(requestedDate);
  state.calendarSelected = state.selectedDate;
  state.calendarMonth = state.selectedDate.slice(0, 7);
  if (requestedDate !== state.selectedDate) {
    const url = new URL(window.location);
    if (state.selectedDate === state.today) url.searchParams.delete("date");
    else url.searchParams.set("date", state.selectedDate);
    window.history.replaceState({ date: state.selectedDate }, "", url);
  }
  configureRanking();
  bindEvents();
  updateNavigation();
  updateSky();
  $('#weather-details').open=!window.matchMedia('(max-width:620px)').matches;
  $('#edition-notes').open=!window.matchMedia('(max-width:620px)').matches;
  const saved=readSavedEdition();
  if(saved) {manifest=saved.manifest;Object.assign(state.data,saved.data);configureRanking(saved.data.rankingConfig);savedMode=true;render();updateFreshness();}
  else {for(const name of Object.keys(sectionSelectors)) sectionStates[name]='loading';render();updateFreshness();}
  refreshData({initial:true});loadJoke();loadRocketLaunches();
  if('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(()=>{state.errors.push('Offline shell unavailable in this browser.');renderErrors();});
  const photosObserver=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)) {
    photosObserver.disconnect();loadDocument(DOCUMENTS.find(([name])=>name==='photos')).then(result=>{if(result.data){state.data.photos=result.data;renderPhoto();}});
  }},{rootMargin:'200px'});
  photosObserver.observe($('#geek-section'));
  window.setInterval(refreshData, REFRESH_MS);
  window.setInterval(()=>{if(!document.hidden) loadRocketLaunches();}, REFRESH_MS);
  window.setInterval(() => {
    updateSky();
    renderStarship();
    if (state.launchFetchedAt && !usableLaunchCache({ fetchedAt: state.launchFetchedAt, launches: state.launches }, true)) {
      state.launches = [];
      renderRocketLaunches();
    }
  }, 60 * 1000);
}

init();
    refreshScienceRotation();
