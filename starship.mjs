// Shared by the collector, browser and editorial generator. No network or DOM.
export const MODEL_VERSION = "rules-1.0.1";
export const TIME_ZONE = "America/Detroit";
export const dayKey = (date, zone = TIME_ZONE) => new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
const validInstant = (value) => typeof value === "string" && /T.*(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));

export function targetPassed(target, now = new Date()) {
  if (!target || !target.lower) return false;
  // NET is a lower bound, not a promise; passing it only requests an update.
  const bound = target.upper || target.lower;
  if (target.precision === "exact" || target.precision === "window") return validInstant(bound) && Date.parse(bound) < +now;
  const today = dayKey(now, target.timeZone || TIME_ZONE);
  if (target.precision === "day") return bound < today;
  if (target.precision === "month") return bound < today.slice(0, 7);
  if (target.precision === "quarter") return bound < `${today.slice(0, 4)}-Q${Math.ceil(Number(today.slice(5, 7)) / 3)}`;
  return false;
}

export function targetLabel(target) {
  if (!target) return "Date unannounced";
  if (["exact", "window"].includes(target.precision) && validInstant(target.lower)) {
    const fmt = (value) => new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(value));
    return `${target.net ? "No earlier than " : ""}${target.precision === "window" ? "Window opens " : "Target time "}${fmt(target.lower)}${target.upper ? ` – ${fmt(target.upper)}` : ""}`;
  }
  return target.label;
}

export function reconcile({ evidence, previous = null, mode = "shadow", ttlHours = 6, now = new Date(), sourceHealth = [], activeMission = null }) {
  const at = now.toISOString();
  const superseded = new Set(evidence.flatMap((e) => e.verification === "verified" && e.publishedAt && Date.parse(e.publishedAt) <= +now && Date.parse(e.observedAt) <= +now ? e.supersedes : []));
  const active = evidence.filter((e) => e.verification === "verified" && !superseded.has(e.id) && e.publishedAt && Date.parse(e.publishedAt) <= +now && Date.parse(e.observedAt) <= +now);
  const byRecent = (a, b) => Date.parse(b.publishedAt) - Date.parse(a.publishedAt);
  const terminal = active.filter((e) => e.claimType === "outcome" && e.sourceType === "operator");
  const finished = new Set(terminal.map((e) => e.missionId));
  const candidates = active.filter((e) => e.missionId && !finished.has(e.missionId)).sort(byRecent);
  const missionId = activeMission || candidates.find((e) => e.sourceType === "operator" && ["target", "delay", "scrub", "underway"].includes(e.claimType))?.missionId || null;
  const mission = { id: missionId, label: missionId ? missionId.replace("starship-flight-", "Starship Flight ") : "Next Starship mission", vehicleIds: [] };
  const relevant = candidates.filter((e) => e.missionId === missionId && missionId);
  const current = relevant.filter((e) => +now - Date.parse(e.observedAt) <= ttlHours * 3600000);
  const targets = relevant.filter((e) => e.claimType === "target" && e.target).sort(byRecent);
  const official = targets.filter((e) => e.sourceType === "operator");
  const latest = official[0] || null;
  const targetKey = (t) => JSON.stringify([t.precision, t.lower, t.upper, t.net, t.timeZone]);
  const conflicts = [];
  for (const e of targets) {
    if (latest && e.id !== latest.id && targetKey(e.target) !== targetKey(latest.target)) {
      conflicts.push({ evidenceIds: [latest.id, e.id], explanation: "Sources give different targets; no dates have been averaged. Supersede an older claim only after verification." });
    }
  }
  const lastVerifiedAt = current.length ? current.map((e) => e.observedAt).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1) : (previous?.mission.id === missionId ? previous.lastVerifiedAt : null);
  const expiresAt = lastVerifiedAt ? new Date(Date.parse(lastVerifiedAt) + ttlHours * 3600000).toISOString() : null;
  const fresh = Boolean(expiresAt && Date.parse(expiresAt) > +now);
  const targetFresh = latest && +now - Date.parse(latest.observedAt) <= ttlHours * 3600000;
  let status = "unannounced", summary = "Date unannounced; insufficient current evidence.";
  const latestAction = relevant.filter((e) => e.sourceType === "operator" && ["target", "delay", "scrub", "underway"].includes(e.claimType)).sort(byRecent)[0];
  if (fresh && latestAction?.claimType === "underway") { status = "underway"; summary = "Attempt underway, according to the operator."; }
  else if (latestAction && ["delay", "scrub"].includes(latestAction.claimType)) { status = "delayed"; summary = "Operator reported a delay or scrub; awaiting an updated target."; }
  else if (latest && targetPassed(latest.target, now)) { status = "target-passed"; summary = "Previous target passed; awaiting update."; }
  else if (latest && fresh && targetFresh && !conflicts.length) { status = "targeted"; summary = `Operator targeting ${targetLabel(latest.target)}.`; }
  else if (conflicts.length) { status = "uncertain"; summary = "Conflicting target reports; awaiting clarification."; }
  // Historical estimates can explain the old display, but never establish an official target.
  const historical = evidence.filter((e) => e.sourceType === "editorial" && e.target && targetPassed(e.target, now));
  if (!latestAction && !missionId && historical.length) { status = "target-passed"; summary = "Previous target passed; awaiting update."; }
  const basis = current.filter((e) => ["target", "readiness", "regulatory", "notice"].includes(e.claimType));
  const origins = new Set(basis.map((e) => e.originId));
  const nearTerm = latest && !targetPassed(latest.target, now) && ["day", "window", "exact"].includes(latest.target.precision) && Date.parse(latest.target.lower) - +now < 7 * 86400000;
  const plausible = fresh && status === "targeted" && nearTerm && origins.size >= 3 && basis.some((e) => e.claimType === "readiness") && basis.some((e) => e.claimType === "regulatory" && e.authorization === true);
  const forecast = {
    state: plausible ? "plausible" : status,
    summary: plausible ? "Near-term attempt plausible; readiness and mission-specific authorization evidence support the operator target. Launch remains uncertain." : summary,
    window: plausible ? latest.target : null,
    basisIds: basis.map((e) => e.id),
    uncertainty: ["Targets can change; no numerical launch probability is assigned.", ...(basis.some((e) => e.claimType === "readiness") ? [] : ["Vehicle readiness is not verified."]), ...(basis.some((e) => e.authorization === true) ? [] : ["Mission-specific launch authorization is not verified."]), ...(fresh ? [] : ["No freshly verified mission evidence."])],
  };
  const signature = JSON.stringify({ mission, status, officialTarget: latest?.target, forecast, conflicts });
  const oldSignature = previous ? JSON.stringify({ mission: previous.mission, status: previous.status, officialTarget: previous.officialTarget?.target, forecast: previous.forecast, conflicts: previous.conflicts }) : null;
  return {
    schemaVersion: "1.0.0", modelVersion: MODEL_VERSION, mode, generatedAt: at, lastAttemptAt: at,
    lastSuccessAt: sourceHealth.some((s) => s.state === "ok") ? at : previous?.lastSuccessAt || null,
    lastVerifiedAt, expiresAt, mission, status, statusEffectiveAt: previous?.status === status && previous?.mission.id === missionId ? previous.statusEffectiveAt : at,
    officialTarget: latest ? { evidenceId: latest.id, sourceUrl: latest.sourceUrl, announcedAt: latest.publishedAt, target: latest.target } : null,
    outsideReports: targets.filter((e) => e.sourceType === "reporter").map((e) => e.id),
    forecast, evidence: evidence.map((e) => superseded.has(e.id) ? { ...e, verification: "superseded" } : e), conflicts, sourceHealth,
    outcomes: terminal.map((e) => ({ missionId: e.missionId, evidenceId: e.id, outcome: e.outcome, actualLiftoffAt: e.actualLiftoffAt || null, recordedAt: e.observedAt })),
    whatChanged: signature === oldSignature ? "No material change in the evidence-backed outlook." : previous ? `${summary} Evidence and source-health details are retained below.` : "Independent Starship tracking started; the old digest estimate is historical only.",
    previousSnapshot: previous?.snapshot || null, snapshot: null,
  };
}

export function starshipView(record, now = new Date()) {
  if (!record) return { fresh: false, status: "unannounced", summary: "Starship status unavailable; awaiting verified evidence.", officialTarget: null, forecast: null, lastVerifiedAt: null, uncertainty: ["No canonical record available."] };
  const fresh = Boolean(record.lastVerifiedAt && record.expiresAt && Date.parse(record.lastVerifiedAt) <= +now && Date.parse(record.expiresAt) > +now);
  const targetEvidence = record.evidence.find((e) => e.id === record.officialTarget?.evidenceId);
  const targetFresh = targetEvidence && +now - Date.parse(targetEvidence.observedAt) <= Date.parse(record.expiresAt) - Date.parse(record.lastVerifiedAt);
  const passed = targetPassed(record.officialTarget?.target, now);
  const invalidated = ["delayed", "underway"].includes(record.status);
  const effectiveStatus = record.status === "targeted" && !targetFresh ? "unannounced" : record.status;
  const status = passed && !invalidated ? "target-passed" : fresh ? effectiveStatus : record.status === "target-passed" || record.status === "delayed" ? record.status : "unannounced";
  const summary = status === "target-passed" ? "Previous target passed; awaiting update." : status === "delayed" ? "Operator reported a delay or scrub; awaiting an updated target." : !fresh || status === "unannounced" ? "Date unannounced; insufficient current evidence." : record.status === "targeted" ? `Operator targeting ${targetLabel(record.officialTarget?.target)}.` : record.status === "underway" ? "Attempt underway, according to the operator." : record.forecast.summary;
  return { fresh, status, summary, officialTarget: fresh && targetFresh && !passed && !invalidated && !record.conflicts.length ? record.officialTarget : null, forecast: fresh && targetFresh && !passed && record.mode === "live" ? record.forecast : null, lastVerifiedAt: record.lastVerifiedAt, uncertainty: record.forecast.uncertainty };
}

export function editorialStarship(record, now = new Date()) {
  const view = starshipView(record, now);
  return { ...view, mode: record?.mode || "shadow", conflicts: record?.conflicts || [], sources: view.officialTarget ? [view.officialTarget.sourceUrl] : [], instruction: "Use only the current summary and officialTarget. Shadow forecasts and expired targets are not current predictions. Never infer completion from a passed date." };
}

export const LAUNCH_CACHE_FRESH_MS = 30 * 60000;
export const LAUNCH_CACHE_MAX_MS = 6 * 3600000;
export function usableLaunchCache(cached, allowStale = false, now = Date.now()) {
  const age = now - cached?.fetchedAt;
  return cached && Number.isFinite(cached.fetchedAt) && Array.isArray(cached.launches) && age >= 0 && age <= (allowStale ? LAUNCH_CACHE_MAX_MS : LAUNCH_CACHE_FRESH_MS) ? cached : null;
}
export function launchDateKey(launch) {
  const instant = launch.t0 || launch.win_open;
  if (validInstant(instant)) return dayKey(new Date(instant));
  const { year, month, day } = launch.est_date || {};
  return year && month && day ? `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}` : "";
}
export function launchDateTime(launch) {
  const instant = launch.t0 || launch.win_open;
  if (!validInstant(instant)) return `Targeted ${launch.date_str || "date to be confirmed"}`;
  const label = new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(instant));
  return `${launch.t0 ? "Target time" : "Window opens at"} ${label}`;
}
