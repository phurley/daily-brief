const TIME_ZONE = "America/Detroit";
const REFRESH_MS = 15 * 60 * 1000;

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
}

function weatherCard(label, title, big, body, facts = []) {
  const card = node("article", { className: "weather-card" }, [
    node("span", { className: "weather-card__label", text: label }),
    big ? node("strong", { className: "weather-card__big", text: big }) : null,
    node("h3", { text: title }),
    body ? node("p", { text: body }) : null,
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

function renderWeather() {
  const weather = state.data.weather;
  const forecast = weather?.daily?.find((day) => day.date === state.selectedDate);
  const almanac = state.data.almanac;
  const day = almanac?.days?.find((item) => item.date === state.selectedDate);
  const holiday = almanac?.holidays?.find((item) => item.date === state.selectedDate);
  const current = state.selectedDate === state.today ? weather?.current : null;
  const cards = [];

  if (forecast) {
    cards.push(weatherCard(
      current ? "Right now" : "Forecast",
      current?.conditionText || forecast.conditionText,
      `${Math.round(current?.temperatureF ?? forecast.highF)}°`,
      forecast.narrative,
      current ? [["Feels", `${Math.round(current.feelsLikeF)}°`], ["Wind", `${Math.round(current.wind.speedMph)} mph ${current.wind.direction}`]] : [["High", `${Math.round(forecast.highF)}°`], ["Low", `${Math.round(forecast.lowF)}°`]],
    ));
    cards.push(weatherCard("The range", "High & low", `${Math.round(forecast.highF)}° / ${Math.round(forecast.lowF)}°`, forecast.conditionText, [["Rain", `${forecast.precipitationChancePercent}%`], ["Icon", forecast.icon]]));
  }
  if (forecast || day) {
    cards.push(weatherCard("Daylight", "Sunrise to sunset", "", "", [
      ["Rise", displayTime(forecast?.sunrise || day?.sunrise)],
      ["Set", displayTime(forecast?.sunset || day?.sunset)],
    ]));
  }
  if (day?.moon) {
    cards.push(weatherCard("Night sky", day.moon.summary, `${Math.round(day.moon.illuminationPercent)}%`, holiday?.description || "", [
      ["Moonrise", displayTime(day.moon.moonrise) || "—"],
      ["Phase", day.moon.phase.replaceAll("-", " ")],
    ]));
  } else if (holiday) {
    cards.push(weatherCard("Almanac", holiday.name, "", holiday.description));
  }

  replaceChildren("#weather-grid", cards.length ? cards : [emptyState()]);
  $("#weather-intro").textContent = message("almanac", "note", "The shape of the day, from first light onward.");
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

function renderCalendar() {
  const items = (state.data.calendar?.items || [])
    .filter((item) => occursOn(item, state.selectedDate) && item.status !== "cancelled")
    .sort((a, b) => (a.startTime || "99:99").localeCompare(b.startTime || "99:99"));
  const children = items.map((item) => {
    const details = [item.location, item.description].filter(Boolean).join(" · ");
    return node("article", { className: "timeline__item" }, [
      node("time", { className: "timeline__time", text: item.startTime ? displayTime(item.startTime) : item.type, datetime: item.startTime || state.selectedDate }),
      node("div", {}, [node("h3", { text: item.title }), details ? node("p", { text: details }) : null]),
    ]);
  });
  replaceChildren("#calendar-list", children.length ? children : [emptyState()]);
  $("#calendar-note").textContent = message("calendar", "note", "The things worth remembering.");
}

function uniqueEvents() {
  const events = state.data.events;
  const map = new Map();
  for (const item of [...(events?.pastWeek || []), ...(events?.today || []), ...(events?.planAhead || [])]) map.set(item.id, item);
  return [...map.values()];
}

function renderEvents() {
  const allEvents = uniqueEvents();
  const events = allEvents.filter((event) => event.start?.slice(0, 10) === state.selectedDate);
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
    .filter((event) => event.start?.slice(0, 10) > state.selectedDate && event.start.slice(0, 10) <= end)
    .sort((a, b) => a.start.localeCompare(b.start))
    .slice(0, 6);
  const plan = $("#plan-ahead");
  plan.replaceChildren();
  if (future.length) {
    plan.append(node("h3", { text: message("plan-ahead", "section-heading", "Worth penciling in") }));
    const list = node("ol");
    for (const event of future) {
      const item = node("li");
      item.append(safeLink(event.title, event.url), document.createTextNode(` — ${event.dateLabel}`));
      list.append(item);
    }
    plan.append(list);
  }
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
}

function renderStarship() {
  const launch = state.data.geeknews?.starshipEstimatedLaunch;
  const target = $("#starship");
  target.replaceChildren();
  if (!launch || state.data.geeknews.editionDate > state.selectedDate) return;
  target.append(node("strong", { text: `Starship · ${launch.estimateLabel}` }), document.createTextNode(` — ${message("starship", "note", launch.summary)}`));
}

function renderPhoto() {
  const day = state.data.photos?.days?.find((item) => item.date === state.selectedDate);
  const photo = day?.photos?.[0];
  const section = $("#photo-section");
  section.hidden = !photo;
  if (!photo) return;
  $("#photo-image").src = photo.imageUrl;
  $("#photo-image").alt = photo.description;
  $("#photo-caption").textContent = `${photo.description} · ${photo.location} · ${photo.takenDate.slice(0, 4)}`;
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

function render() {
  renderMasthead();
  renderWeather();
  renderCalendar();
  renderEvents();
  renderPhoto();
  renderStories("news");
  renderStories("geeknews");
  renderStarship();
  renderUpdatedLabel();
  updateNavigation();
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
  await Promise.all([refreshData({ initial: true }), loadJoke()]);
  window.setInterval(refreshData, REFRESH_MS);
  window.setInterval(updateSky, 60 * 1000);
}

init();
