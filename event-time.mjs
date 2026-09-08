const TIME_ZONE = "America/Detroit";
const dayFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
});
const timeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: TIME_ZONE, hour: "numeric", minute: "2-digit",
});

function dayKey(date) {
  const parts = Object.fromEntries(dayFormatter.formatToParts(date).map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

// Relative wording belongs to the reader's current day, never the collector's run.
export function eventDateLabel(event, now = new Date()) {
  const start = new Date(event.start);
  if (!event.start || Number.isNaN(start.getTime())) return "Date to be confirmed";
  const end = event.end ? new Date(event.end) : null;
  const today = dayKey(now);
  const label = (date) => {
    const day = dayKey(date);
    const offset = Math.round((Date.parse(day) - Date.parse(today)) / 86400000);
    const hour = Number(new Intl.DateTimeFormat("en-US", {
      timeZone: TIME_ZONE, hour: "numeric", hourCycle: "h23",
    }).format(date));
    if (offset === 0) return hour >= 18 ? "Tonight" : "Today";
    if (offset === 1) return "Tomorrow";
    if (offset === -1) return "Yesterday";
    return new Intl.DateTimeFormat("en-US", {
      timeZone: TIME_ZONE, weekday: "short", month: "short", day: "numeric",
      year: day.slice(0, 4) !== today.slice(0, 4) ? "numeric" : undefined,
    }).format(date);
  };
  const beginning = `${label(start)}, ${timeFormatter.format(start)}`;
  if (!end || Number.isNaN(end.getTime()) || end <= start) return beginning;
  if (dayKey(start) === dayKey(end)) return `${beginning}–${timeFormatter.format(end)}`;
  return `${beginning} – ${label(end)}, ${timeFormatter.format(end)}`;
}
