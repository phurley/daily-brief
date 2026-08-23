const TIME_ZONE = "America/Detroit";
const REFRESH_MS = 15 * 60 * 1000;
const LAUNCH_CACHE_MS = 30 * 60 * 1000;
const LAUNCH_CACHE_KEY = "daily-brief-rocket-launches:v1";
const LAUNCH_API_URL = "https://fdo.rocketlaunch.live/json/launches/next/5";

// Each data document is paired with its repository schema. Adding a new
// schema-backed document is a one-line registry change, not a rendering rewrite.
const DOCUMENTS = [
  ["weather", "weather.json", "schemas/weather.schema.json", true],
  ["almanac", "almanac.json", "schemas/almanac.schema.json", true],
  ["calendar", "calendar.json", "schemas/calendar.schema.json", true],
  ["events", "events.json", "schemas/events.schema.json", true],
  ["news", "news.json", "schemas/news.schema.json", true],
  ["geeknews", "geeknews.json", "schemas/geeknews.schema.json", true],
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
  launches: [],
};

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

async function fetchJson(path, stamp) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${separator}v=${stamp}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadDocument([name, dataPath, schemaPath, required], stamp) {
  try {
    const [data, schema] = await Promise.all([fetchJson(dataPath, stamp), fetchJson(schemaPath, stamp)]);
    validateTopLevel(data, schema, dataPath);
    return { name, data, signature: JSON.stringify(data), required };
  } catch (error) {
    return { name, error: error.message, required };
  }
}

async function refreshData({ initial = false } = {}) {
  const status = $("#live-status");
  if (!initial) status.textContent = "Checking for updates…";
  const previousToday = state.today;
  state.today = dateKey(new Date());
  if (state.today !== previousToday && state.selectedDate === previousToday) {
    state.selectedDate = state.today;
    const url = new URL(window.location);
    url.searchParams.delete("date");
    window.history.replaceState({ date: state.today }, "", url);
    loadJoke();
  }
  const results = await Promise.all(DOCUMENTS.map((document) => loadDocument(document, Date.now())));
  const nextErrors = [];
  let changed = initial;

  for (const result of results) {
    if (result.error) {
      if (result.required) nextErrors.push(`${result.name}: ${result.error}`);
      continue;
    }
    if (state.signatures.get(result.name) !== result.signature) {
      state.data[result.name] = result.data;
      state.signatures.set(result.name, result.signature);
      changed = true;
    }
  }

  state.errors = nextErrors;
  if (changed) render();
  renderErrors();
  const checked = new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, hour: "numeric", minute: "2-digit" }).format(new Date());
  status.textContent = changed && !initial ? `Updated at ${checked}` : `Live · checked ${checked}`;
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
  $("#page-title").textContent = message("masthead", "headline", relative === "Today" ? "A good day, well considered." : `${relative}, in view.`);
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
  texture.setAttribute("x", "4");
  texture.setAttribute("y", "4");
  texture.setAttribute("width", "92");
  texture.setAttribute("height", "92");
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
    node("p", { className: "forecast-day__detail", text: `${forecast.precipitationChancePercent}% rain · ${forecast.narrative}` }),
  ]));
  return node("article", { className: "weather-card weather-card--forecast" }, columns);
}

function skyWeatherCard(day, forecast, holiday) {
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
    holiday ? node("p", { className: "sky-holiday", text: holiday.description }) : null,
  ]);
}

function renderWeather() {
  const weather = state.data.weather;
  const forecast = weather?.daily?.find((day) => day.date === state.selectedDate);
  const almanac = state.data.almanac;
  const day = almanac?.days?.find((item) => item.date === state.selectedDate);
  const holiday = almanac?.holidays?.find((item) => item.date === state.selectedDate);
  const current = state.selectedDate === state.today ? weather?.current : null;
  const nextForecast = weather?.daily?.find((item) => item.date === shiftDate(state.selectedDate, 1));
  const cards = [];

  if (forecast) {
    cards.push(weatherCard(
      current ? "Right now" : "Forecast",
      current?.conditionText || forecast.conditionText,
      `${Math.round(current?.temperatureF ?? forecast.highF)}°`,
      forecast.narrative,
      current ? [["Feels", `${Math.round(current.feelsLikeF)}°`], ["Wind", `${Math.round(current.wind.speedMph)} mph ${current.wind.direction}`]] : [["High", `${Math.round(forecast.highF)}°`], ["Low", `${Math.round(forecast.lowF)}°`]],
      { icon: current?.icon || forecast.icon, className: "weather-card--now" },
    ));
    cards.push(forecastRangeCard([forecast, nextForecast]));
  }
  if (forecast || day) {
    cards.push(skyWeatherCard(day, forecast, holiday));
  }

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
  const publicHolidays = (state.data.almanac?.holidays || [])
    .filter((holiday) => holiday.date === key)
    .map((holiday) => ({
      id: `almanac-${holiday.date}-${holiday.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "")}`,
      type: "holiday",
      title: holiday.name,
      date: holiday.date,
      occurrenceDate: key,
      description: holiday.description,
      source: "almanac",
    }));
  const seen = new Set();
  return [...calendarItems, ...publicHolidays]
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
  const children = items.map((item) => {
    const details = [
      item.status === "tentative" ? "Tentative" : "",
      item.person,
      item.location,
      item.description,
    ].filter(Boolean).join(" · ");
    const title = node("h3");
    title.append(item.url ? safeLink(item.title, item.url) : document.createTextNode(item.title));
    const time = node("time", { className: "timeline__time", datetime: item.startTime || state.selectedDate }, [
      node("span", { text: CALENDAR_ICONS[item.type] || "•", "aria-hidden": "true" }),
      document.createTextNode(` ${item.startTime ? displayTime(item.startTime) : item.type}`),
    ]);
    return node("article", { className: "timeline__item" }, [
      time,
      node("div", {}, [title, details ? node("p", { text: details }) : null]),
    ]);
  });
  replaceChildren("#calendar-list", children.length ? children : [emptyState()]);
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

function uniqueEvents() {
  const events = state.data.events;
  const map = new Map();
  for (const item of [...(events?.pastWeek || []), ...(events?.today || []), ...(events?.planAhead || [])]) map.set(item.id, item);
  return [...map.values()];
}

function eventStartDate(event) {
  return event.start?.slice(0, 10) || "";
}

function eventEndDate(event) {
  return (event.end || event.start)?.slice(0, 10) || "";
}

function eventIsActiveOn(event, key) {
  const start = eventStartDate(event);
  const end = eventEndDate(event);
  return Boolean(start && end && start <= key && end >= key);
}

function renderEvents() {
  const allEvents = uniqueEvents();
  const events = allEvents
    .filter((event) => eventIsActiveOn(event, state.selectedDate))
    .sort((a, b) => (b.score || 0) - (a.score || 0) || a.start.localeCompare(b.start));
  const cards = events.map((event) => {
    const title = node("h3");
    title.append(safeLink(event.title, event.url));
    return node("article", { className: "card" }, [
      node("span", { className: "card-meta", text: [event.dateLabel, event.category].filter(Boolean).join(" · ") }),
      title,
      node("p", { text: event.summary }),
      node("p", { className: "card__footer", text: [event.venue, event.city, event.price, event.distanceMiles != null ? `${event.distanceMiles} mi` : ""].filter(Boolean).join(" · ") }),
    ]);
  });
  replaceChildren("#events-list", cards.length ? cards : [emptyState()]);
  $("#events-title").textContent = message("today", "section-heading", "Nearby & notable");
  $("#events-note").textContent = message("today", "recommendation", state.data.events?.easyAnswer || "Good reasons to leave the house.");

  const end = shiftDate(state.selectedDate, 30);
  const future = allEvents
    .filter((event) => eventEndDate(event) >= state.selectedDate && eventStartDate(event) > state.selectedDate && eventStartDate(event) <= end)
    .sort((a, b) => a.start.localeCompare(b.start));
  const plan = $("#plan-ahead");
  plan.hidden = future.length === 0;
  $("#plan-ahead-title").textContent = message("plan-ahead", "section-heading", "Worth penciling in");
  const claims = [];
  if (future.length) {
    for (const event of future) {
      const title = node("strong");
      title.append(safeLink(event.title, event.url));
      claims.push(node("article", { className: "claim" }, [
        node("time", { text: event.dateLabel, datetime: eventStartDate(event) }),
        title,
      ]));
    }
  }
  replaceChildren("#claims-list", claims);
  refreshShelfControls();
}

function renderStories(kind) {
  const data = state.data[kind];
  const selector = kind === "news" ? "#news-list" : "#geek-list";
  const noteSelector = kind === "news" ? "#news-note" : "#geek-note";
  const section = kind === "news" ? "news" : "science-technology";
  const editionAvailable = data?.editionDate && data.editionDate <= state.selectedDate;
  const stories = editionAvailable ? data.stories || [] : [];
  const children = stories.map((story) => {
    const title = node("h3");
    title.append(safeLink(story.title, story.url));
    const source = [story.source?.name, story.source?.publication].filter(Boolean).join(" · ");
    return node("article", { className: "story" }, [
      node("div", {}, [title, node("p", { text: story.summary })]),
      node("p", { className: "story__source", text: source }),
    ]);
  });
  replaceChildren(selector, children.length ? children : [emptyState()]);

  const carryForward = data?.editionDate && data.editionDate !== state.selectedDate && editionAvailable
    ? `Latest available digest: ${displayDate(data.editionDate)}.`
    : "";
  const fallback = kind === "news" ? "What is moving around Michigan." : "Interesting machinery, ideas, and horizons.";
  $(noteSelector).textContent = [message(section, "summary", fallback), carryForward].filter(Boolean).join(" ");
  if (kind === "news") $("#news-title").textContent = message(section, "section-heading", "The local signal");
  else $("#geek-title").textContent = message(section, "section-heading", "Science & technology");
  refreshShelfControls();
}

function launchDateKey(launch) {
  const instant = launch.t0 || launch.win_open;
  if (instant) return dateKey(new Date(instant));
  const estimate = launch.est_date;
  if (estimate?.year && estimate?.month && estimate?.day) {
    return `${estimate.year}-${String(estimate.month).padStart(2, "0")}-${String(estimate.day).padStart(2, "0")}`;
  }
  return "";
}

function launchDateTime(launch) {
  const instant = launch.t0 || launch.win_open;
  if (!instant) return launch.date_str || "Date to be confirmed";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: TIME_ZONE,
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(instant));
}

function launchLink(launch) {
  return launch.slug ? `https://www.rocketlaunch.live/launch/${launch.slug}` : "https://www.rocketlaunch.live/";
}

function renderRocketLaunches() {
  const target = $("#rocket-launches");
  target.replaceChildren();
  const launches = state.launches;
  const starship = state.data.geeknews?.editionDate <= state.selectedDate
    ? state.data.geeknews?.starshipEstimatedLaunch
    : null;
  target.hidden = launches.length === 0 && !starship;
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
      ? `${todaysLaunches.length} launch${todaysLaunches.length === 1 ? "" : "es"} today`
      : "No launches today · Next";
    line.append(node("strong", { text: title }), document.createTextNode(" · "));
    line.append(safeLink(featured.name || featured.missions?.[0]?.name || "Scheduled launch", launchLink(featured)));
    const location = featured.pad?.location?.name;
    line.append(document.createTextNode(` · ${launchDateTime(featured)}${location ? ` · ${location}` : ""}`));
    if (todaysLaunches.length > 1) line.append(document.createTextNode(` · +${todaysLaunches.length - 1} more`));
  }
  if (starship) {
    if (featured) line.append(document.createTextNode(" · "));
    line.append(node("strong", { text: `Starship ${starship.estimateLabel}` }));
  }

  const attribution = safeLink("Data by RocketLaunch.Live", "https://www.rocketlaunch.live/");
  if (attribution.nodeType === Node.ELEMENT_NODE) attribution.className = "rocket-launches__source";
  target.append(
    node("span", { className: "rocket-launches__icon", text: "↗", "aria-hidden": "true" }),
    line,
    attribution,
  );
}

function readLaunchCache({ allowStale = false } = {}) {
  try {
    const cached = JSON.parse(localStorage.getItem(LAUNCH_CACHE_KEY) || "null");
    if (!cached || !Array.isArray(cached.launches) || typeof cached.fetchedAt !== "number") return null;
    if (!allowStale && Date.now() - cached.fetchedAt > LAUNCH_CACHE_MS) return null;
    return cached.launches;
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
    state.launches = cached;
    renderRocketLaunches();
    return;
  }
  try {
    const response = await fetch(LAUNCH_API_URL, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`RocketLaunch.Live returned ${response.status}`);
    const payload = await response.json();
    const launchEnvelope = payload.response || payload;
    if (!Array.isArray(launchEnvelope.result)) throw new Error("RocketLaunch.Live returned an unexpected response");
    state.launches = launchEnvelope.result;
    writeLaunchCache(state.launches);
  } catch {
    state.launches = readLaunchCache({ allowStale: true }) || [];
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
      node("p", { className: "section-number", text: `From this day · Photo ${index + 1} of ${photos.length}` }),
      node("h3", { text: photo.description }),
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
  const month = Number(state.today.slice(5, 7));
  root.dataset.season = [12, 1, 2].includes(month) ? "winter" : [3, 4, 5].includes(month) ? "spring" : [6, 7, 8].includes(month) ? "summer" : "autumn";
  const text = (forecast?.conditionText || state.data.weather?.current?.conditionText || "").toLowerCase();
  root.dataset.weather = /rain|storm|shower/.test(text) ? "rain" : /cloud|overcast|fog/.test(text) ? "cloud" : "clear";
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
    const move = (direction) => track.scrollBy({ left: direction * track.clientWidth * .88, behavior: "smooth" });
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
  renderUpdatedLabel();
  updateNavigation();
  refreshShelfControls();
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
  window.scrollTo({ top: 0, behavior: "smooth" });
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
    const response = await fetch("https://icanhazdadjoke.com/", { headers: { Accept: "application/json" } });
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
  bindShelfControls();
  document.addEventListener("visibilitychange", () => document.hidden ? stopPhotoShow() : startPhotoShow());
  window.addEventListener("popstate", () => selectDate(new URL(window.location).searchParams.get("date") || state.today, { history: false }));
}

async function init() {
  const requestedDate = new URL(window.location).searchParams.get("date") || state.today;
  state.selectedDate = clampDate(requestedDate);
  if (requestedDate !== state.selectedDate) {
    const url = new URL(window.location);
    if (state.selectedDate === state.today) url.searchParams.delete("date");
    else url.searchParams.set("date", state.selectedDate);
    window.history.replaceState({ date: state.selectedDate }, "", url);
  }
  bindEvents();
  updateNavigation();
  updateSky();
  await Promise.all([refreshData({ initial: true }), loadJoke(), loadRocketLaunches()]);
  window.setInterval(refreshData, REFRESH_MS);
  window.setInterval(loadRocketLaunches, REFRESH_MS);
  window.setInterval(updateSky, 60 * 1000);
}

init();
