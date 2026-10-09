function storyTime(story) {
  const value = Date.parse(story?.publishedAt || "");
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

function addedTime(story) {
  const value = Date.parse(story?.addedAt || story?.publishedAt || "");
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

function addedDate(story, dateKey) {
  const value = addedTime(story);
  return Number.isFinite(value) ? dateKey(new Date(value)) : "";
}

function localityIndex(story) {
  return Number.isInteger(story?.localityIndex) ? story.localityIndex : Number.POSITIVE_INFINITY;
}

function storyTieBreak(a, b) {
  return String(a?.id || a?.title || "").localeCompare(String(b?.id || b?.title || ""));
}

export function orderNewsStories(stories, { today, dateKey }) {
  return [...stories].sort((a, b) => {
    const aDate = addedDate(a, dateKey);
    const bDate = addedDate(b, dateKey);
    if (aDate !== bDate) return bDate.localeCompare(aDate);

    // Once today's newly collected stories are at the front, proximity matters
    // more than the exact minute they arrived. Older batches remain newest-first.
    if (aDate === today) {
      const localityDifference = localityIndex(a) - localityIndex(b);
      if (localityDifference) return localityDifference;
    }

    const addedDifference = addedTime(b) - addedTime(a);
    if (addedDifference) return addedDifference;

    const publishedDifference = storyTime(b) - storyTime(a);
    if (publishedDifference) return publishedDifference;
    return storyTieBreak(a, b);
  });
}
