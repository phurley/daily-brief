// One daily selection; deterministic browser ordering changes every four hours.
const DAY = 86400000;
const time = (value) => Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
export const scienceDate = (value) => time(value) ? new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Detroit", year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date(value)) : "Unknown date";

export function selectScienceDigest(doc = {}, date, { limit = 6, reset = false } = {}) {
  const ageDays = s => (Date.parse(`${date}T00:00:00Z`) - Date.parse(`${scienceDate(s.publishedAt)}T00:00:00Z`)) / DAY;
  const all = [...(doc.stories || [])].filter(s => time(s.publishedAt) && scienceDate(s.publishedAt) <= date)
    .sort((a, b) => time(b.publishedAt) - time(a.publishedAt) || a.id.localeCompare(b.id));
  const previously = {};
  const clusters = new Set();
  const candidates = all.filter(s => {
    // Only explicitly verified mission milestones share a timeline. Discoveries
    // concerning the same telescope remain separate findings.
    if (!s.storyClusterId || s.evidenceType !== "mission-milestone") return true;
    if (clusters.has(s.storyClusterId)) return false;
    clusters.add(s.storyClusterId);
    previously[s.id] = all.filter(p => p.id !== s.id && p.storyClusterId === s.storyClusterId && p.evidenceType === "mission-milestone");
    return true;
  });
  const fresh = candidates.filter(s => s.verifiedAt && ageDays(s) <= 14 && s.evidenceType !== "explanatory-background");
  const pool = [...fresh];
  const selected = [];
  const reasons = {};
  const sourceCounts = {}, topicCounts = {};
  const interests = new Set(doc.scienceInterests || []);
  const score = s => {
    const source = s.sourceGroup || s.source?.name || "unknown";
    const topic = s.topics?.[0] || "science";
    return 30 - ageDays(s)
      + (s.sourceQuality ?? 1) * 2 + (s.topics?.some(t => interests.has(t)) ? 4 : 0)
      - (sourceCounts[source] || 0) * 12 - (topicCounts[topic] || 0) * 5;
  };
  const add = s => {
    const source = s.sourceGroup || s.source?.name || "unknown";
    const topic = s.topics?.[0] || "science";
    reasons[s.id] = { score: score(s), source, topic, publishedAt: s.publishedAt,
      interestMatch: Boolean(s.topics?.some(t => interests.has(t))) };
    selected.push(s);
    sourceCounts[source] = (sourceCounts[source] || 0) + 1;
    topicCounts[topic] = (topicCounts[topic] || 0) + 1;
    pool.splice(pool.indexOf(s), 1);
  };
  if (!reset && doc.selection?.date === date) {
    for (const id of doc.selection.selectedIds || []) {
      const story = pool.find(s => s.id === id);
      if (story && selected.length < limit) add(story);
    }
  }
  while (selected.length < limit && pool.length) {
    pool.sort((a, b) => score(b) - score(a) || time(b.publishedAt) - time(a.publishedAt) || a.id.localeCompare(b.id));
    add(pool[0]);
  }
  const ids = new Set(selected.map(s => s.id));
  return { stories: selected, selectedIds: [...ids], reasons, previously,
    archive: all.filter(s => !ids.has(s.id)), date };
}

// Rotation is presentation only: the publisher keeps the daily selection intact.
// Detroit wall-clock slots also remain stable through the repeated DST hour.
export function scienceRotationSlot(now = Date.now()) {
  const hour = Number(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Detroit", hour: "2-digit", hourCycle: "h23",
  }).format(new Date(now)));
  return Math.floor(hour / 4);
}

export function scienceView(doc, date, now = Date.now()) {
  const digest = selectScienceDigest(doc, date);
  const slot = date === scienceDate(new Date(now).toISOString()) ? scienceRotationSlot(now) : 0;
  const offset = digest.stories.length ? slot % digest.stories.length : 0;
  const stories = [...digest.stories.slice(offset), ...digest.stories.slice(0, offset)];
  return { ...digest, stories, selectedIds: stories.map(story => story.id) };
}

export function scienceFreshness(doc = {}, health, now = Date.now()) {
  const sources = Object.values(health?.sources || {});
  const overdue = sources.filter(s => !time(s.lastSuccessAt) || now - time(s.lastSuccessAt) > (s.staleAfterHours || 36) * 3600000);
  const stale = !health || health.status !== "ok" || now - time(health.generatedAt) > 36 * 3600000 || overdue.length > 0;
  const newest = (doc.stories || []).filter(s => s.verifiedAt).sort((a,b) => time(b.publishedAt)-time(a.publishedAt))[0]?.publishedAt;
  return { stale, lastChecked: health?.lastSuccessfulCheckAt || null, newestStory: newest || null,
    overdueSources: overdue.map(s => s.name), status: health?.status || "unknown" };
}

export function scienceContext(doc, health, date, now = Date.now()) {
  const digest = scienceView(doc, date, now);
  return { ...scienceFreshness(doc, health, now), selectedIds: digest.selectedIds,
    stories: digest.stories.map(s => ({ id: s.id, title: s.title, summary: s.summary,
      finding: s.finding, significance: s.significance, caveat: s.caveat || "Evidence limitations not verified.",
      publishedAt: s.publishedAt, evidenceType: s.evidenceType || "unknown", source: s.source?.name,
      primarySourceUrl: s.primarySourceUrl || s.url, localConnection: s.localConnection })) };
}

// Use dated, deterministic science copy: model wording cannot turn old findings
// into "new today" or silently remove a material limitation.
export function scienceEditorial(context) {
  const first = context.stories[0];
  return { heading: "Science & technology", summary: first
    ? `Published ${scienceDate(first.publishedAt)}: ${first.title}. ${first.caveat}${context.stale ? " Collection is overdue or incomplete." : ""}`
    : `No verified recent science stories are available.${context.stale ? " Collection is overdue or incomplete." : " Older coverage is in the archive."}` };
}
