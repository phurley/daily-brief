// Shared publication/browser selection. Review decisions are made by the source
// editor, never inferred from clicks or from the size of the available archive.
export const STORY_INPUT_FIELDS = ['id', 'title', 'url', 'publishedAt', 'summary', 'finding', 'sourceUpdatedAt'];
export function storyInput(story) {
  return Object.fromEntries(STORY_INPUT_FIELDS.map(key => [key, story[key] ?? null]));
}
const dayOf = value => Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Detroit', year: 'numeric', month: '2-digit', day: '2-digit',
}).format(new Date(value)) : '';
export function currentReviewedStories(raw = [], reviewed = []) {
  const current = new Map(raw.map(story => [story.id, story]));
  return reviewed.filter(story => {
    const original = current.get(story.id);
    return original && JSON.stringify(storyInput(original)) === JSON.stringify(story.editorial?.inputFacts);
  });
}
// Correct even rejected stories when readers explicitly browse the archive.
// Publication evidence is independent of editorial relevance or summary approval.
export function applyStoryDates(stories = [], corrections = []) {
  const byId = new Map(corrections.map(row => [row.id, row]));
  return stories.map(story => {
    const correction = byId.get(story.id);
    if (!correction || JSON.stringify(storyInput(story)) !== JSON.stringify(correction.inputFacts)) return story;
    return { ...story, publishedAt: correction.publishedAt, publicationVerified: true };
  });
}
export function selectEditedStories(doc = {}, section, date, now = Date.now()) {
  const policy = doc.editorial;
  if (policy?.version !== 1) return null; // Compatibility with saved pre-editor editions.
  const ids = policy.days?.find(day => day.date === date)?.selectedIds || [];
  const pool = new Map((doc.stories || []).map(story => [story.id, story]));
  const maxAge = section === 'news' ? 7 : 14;
  return ids.map(id => pool.get(id)).filter(story => {
    const review = story?.editorial;
    const published = dayOf(story?.publishedAt);
    const age = (Date.parse(date) - Date.parse(published)) / 86400000;
    return review?.state === 'verified' && review.grounded === true && Date.parse(review.reviewedAt) <= now
      && Date.parse(review.expiresAt) > now && Date.parse(story.publishedAt) <= now
      && published <= date && age >= 0 && age <= maxAge;
  }).slice(0, section === 'news' ? 5 : 3);
}
export function editedStoryDocument(source, review, section) {
  if (!review || review.schemaVersion !== '1.0.0' || review.section !== section
      || !Array.isArray(review.stories) || !Array.isArray(review.days)) {
    throw new Error(`Missing or invalid ${section} editorial review`);
  }
  return { ...source, stories: currentReviewedStories(source.stories, review.stories),
    editorial: { version: 1, generatedAt: review.generatedAt, days: review.days, corrections: review.corrections || [] } };
}
