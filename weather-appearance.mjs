// WMO codes match scripts/update_weather.py. Text is a fallback for other feeds.
export function weatherFamily(condition = {}) {
  const code = condition.conditionCode;
  const text = (condition.conditionText || "").toLowerCase();
  if ([95, 96, 99].includes(code) || /thunder|storm|hail/.test(text)) return "storm";
  if ([71, 73, 75, 77, 85, 86].includes(code) || /snow|sleet|blizzard/.test(text)) return "snow";
  if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code) || /rain|drizzle|shower/.test(text)) return "rain";
  if ([45, 48].includes(code) || /fog|mist/.test(text)) return "fog";
  if (code === 2 || /partly|scattered|broken cloud/.test(text)) return "partly-cloudy";
  if (code === 3 || /cloud|overcast/.test(text)) return "cloud";
  return "clear";
}

export function weatherAppearance({ date, today, forecast, current, now = Date.now() }) {
  // Do not let an overnight observation override the daytime forecast, or carry
  // today's wind into another edition. The feed can run late; label the fallback.
  const age = now - Date.parse(current?.observedAt);
  const fresh = date === today && age >= 0 && age <= 90 * 60 * 1000;
  const condition = (fresh ? current : forecast) || {};
  const family = weatherFamily(condition);
  const speed = fresh ? current.wind?.speedMph || 0 : 0;
  const gust = fresh ? current.wind?.gustMph || 0 : 0;
  const windy = speed >= 18 || gust >= 28 || /windy|gale|blustery/i.test(condition.conditionText || "");
  const wind = windy ? "windy" : speed >= 10 || gust >= 18 ? "breezy" : "calm";
  const heavy = [65, 67, 75, 82, 86, 95, 96, 99].includes(condition.conditionCode) || /heavy|storm|blizzard/i.test(condition.conditionText || "");
  const light = /light|drizzle/i.test(condition.conditionText || "");
  return {
    family,
    wind,
    intensity: heavy ? "heavy" : light ? "light" : "normal",
    source: fresh ? "current" : forecast ? "forecast" : "unavailable",
    label: [fresh ? "Now" : "Forecast", condition.conditionText, windy ? "Windy" : wind === "breezy" ? "Breezy" : ""].filter(Boolean).join(" · "),
  };
}
