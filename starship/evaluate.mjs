#!/usr/bin/env node
// Descriptive evaluation only; there is not enough history for probabilities.
import fs from "node:fs";
const history = new URL("./history/", import.meta.url);
const records = fs.readdirSync(history).filter((n) => n.endsWith(".json")).sort().map((n) => JSON.parse(fs.readFileSync(new URL(n, history))));
const outcomes = new Map(records.flatMap((r) => r.outcomes).filter((o) => o.actualLiftoffAt).map((o) => [o.missionId, o]));
const result = [];
for (const [missionId, outcome] of outcomes) {
  const actual = new Date(outcome.actualLiftoffAt);
  // Outcomes are joined after forecasts are frozen, never fed into earlier rules.
  const forecasts = records.filter((r) => r.mission.id === missionId && Date.parse(r.generatedAt) < +actual);
  const covered = (t) => {
    if (!t || t.net || !t.lower || t.precision === "unknown") return null;
    if (["exact", "window"].includes(t.precision)) return +actual >= Date.parse(t.lower) && +actual <= Date.parse(t.upper || t.lower);
    const day = new Intl.DateTimeFormat("en-CA", { timeZone: t.timeZone }).format(actual);
    const value = t.precision === "month" ? day.slice(0, 7) : t.precision === "quarter" ? `${day.slice(0, 4)}-Q${Math.ceil(Number(day.slice(5, 7)) / 3)}` : day;
    return value >= t.lower && value <= (t.upper || t.lower);
  };
  result.push({ missionId, actualLiftoffAt: outcome.actualLiftoffAt, snapshots: forecasts.length,
    firstUsefulLeadHours: forecasts.find((r) => r.officialTarget) ? (+actual - Date.parse(forecasts.find((r) => r.officialTarget).generatedAt)) / 3600000 : null,
    targetChanges: forecasts.reduce((n, r, i) => n + Number(i > 0 && JSON.stringify(r.officialTarget?.target) !== JSON.stringify(forecasts[i - 1].officialTarget?.target)), 0),
    baselineCoverage: forecasts.map((r) => covered(r.officialTarget?.target)).filter((v) => v !== null),
    forecastCoverage: forecasts.map((r) => covered(r.forecast.window)).filter((v) => v !== null) });
}
console.log(JSON.stringify({ snapshots: records.length, completedMissionsWithLiftoff: result.length, caveat: "Descriptive small-sample results only. No calibrated probabilities or statistical superiority claims. NET-only targets are not bounded coverage trials.", missions: result }, null, 2));
