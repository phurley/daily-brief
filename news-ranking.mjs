// A separate news rubric. Event attendance/scoring signals are never read.
export function rankNewsStory(story, { now = Date.now(), preferences = {} } = {}) {
  const published = Date.parse(story.publishedAt);
  const ageDays = Number.isFinite(published) && published <= now + 300000 ? Math.max(0, (now - published) / 86400000) : null;
  const freshness = ageDays === null ? 0 : Math.max(0, 30 - ageDays * 5);
  const locality = Number.isInteger(story.localityIndex) && story.localityIndex >= 0 ? Math.max(0, 20 - story.localityIndex * 5) : 0;
  const topics = [...new Set((Array.isArray(story.topics) ? story.topics : []).map(t => String(t).toLowerCase()))];
  const affinity = Math.max(-40, Math.min(40, topics.reduce((sum, topic) => {
    const value = preferences.newsTopicAffinities?.[topic];
    return sum + (Number.isFinite(value) ? value : 0);
  }, 0)));
  const evidence = { primary: 6, reported: 3, opinion: 0, unknown: 0 }[story.evidenceQuality] || 0;
  // Only explicit, recent local alerts override taste. No headline keyword guesses.
  const urgent = story.urgentLocal === true && locality >= 15 && ageDays !== null && ageDays <= 3;
  return { score: freshness + locality + affinity + evidence, urgent, components: { freshness, locality, affinity, evidence } };
}
export function orderRankedNews(stories, options = {}) {
  return stories.map(story => ({ story, ...rankNewsStory(story, options) })).sort((a, b) => Number(b.urgent) - Number(a.urgent) || b.score - a.score || (Date.parse(b.story.publishedAt) || 0) - (Date.parse(a.story.publishedAt) || 0) || String(a.story.id).localeCompare(String(b.story.id))).map(row => row.story);
}
