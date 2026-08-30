import test from "node:test";
import assert from "node:assert/strict";
import { weatherFamily, weatherAppearance } from "../weather-appearance.mjs";

const now = Date.parse("2026-08-30T10:00:00-04:00");
const base = { date: "2026-08-30", today: "2026-08-30", now, forecast: { conditionCode: 51, conditionText: "Light drizzle" } };
const observation = { observedAt: "2026-08-30T09:30:00-04:00", conditionCode: 0, conditionText: "Clear", wind: { speedMph: 20, gustMph: 32 } };

test("drizzle and freezing drizzle get rain; snow showers never get rain", () => {
  for (const code of [51, 53, 55, 56, 57]) assert.equal(weatherFamily({ conditionCode: code }), "rain");
  assert.equal(weatherFamily({ conditionCode: 85, conditionText: "Snow showers" }), "snow");
  assert.equal(weatherFamily({ conditionText: "Light drizzle" }), "rain");
  assert.equal(weatherFamily({ conditionCode: 48 }), "fog");
  assert.equal(weatherFamily({ conditionCode: 2 }), "partly-cloudy");
  assert.equal(weatherFamily({ conditionCode: 3 }), "cloud");
  assert.equal(weatherFamily({ conditionCode: 95 }), "storm");
});
test("an old clear overnight reading cannot suppress today's drizzle", () => {
  const result = weatherAppearance({ ...base, current: { ...observation, observedAt: "2026-08-30T01:15:00-04:00" } });
  assert.equal(result.family, "rain");
  assert.equal(result.source, "forecast");
  assert.equal(result.wind, "calm");
  assert.equal(result.intensity, "light");
});
test("fresh conditions override the forecast and wind coexists with precipitation", () => {
  assert.equal(weatherAppearance({ ...base, current: observation }).family, "clear");
  const result = weatherAppearance({ ...base, current: { ...observation, conditionCode: 75, conditionText: "Heavy snow" } });
  assert.equal(result.family, "snow");
  assert.equal(result.wind, "windy");
  assert.equal(result.intensity, "heavy");
  assert.equal(result.source, "current");
});
test("tomorrow uses its own forecast, never today's wind", () => {
  const result = weatherAppearance({ ...base, date: "2026-08-31", current: observation, forecast: { conditionCode: 2, conditionText: "Partly cloudy" } });
  assert.equal(result.family, "partly-cloudy");
  assert.equal(result.wind, "calm");
  assert.equal(result.source, "forecast");
});
test("missing, invalid, or future observations cannot claim current conditions", () => {
  for (const observedAt of [undefined, "invalid", "2026-08-30T11:00:00-04:00"]) {
    assert.equal(weatherAppearance({ ...base, current: { ...observation, observedAt } }).source, "forecast");
  }
  assert.equal(weatherAppearance({ date: base.date, today: base.today, now }).source, "unavailable");
});
