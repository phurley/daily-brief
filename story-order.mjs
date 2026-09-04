const SCIENCE_SHUFFLE_WINDOW_MS = 2 * 24 * 60 * 60 * 1000;

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

function stableUnitInterval(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x85ebca6b);
  hash ^= hash >>> 13;
  hash = Math.imul(hash, 0xc2b2ae35);
  hash ^= hash >>> 16;
  return (hash >>> 0) / 4294967296;
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

export function orderScienceStories(stories, shuffleSeed) {
  return [...stories].sort((a, b) => {
    const aScore = storyTime(a) + stableUnitInterval(`${shuffleSeed}:${a?.id || a?.title || ""}`) * SCIENCE_SHUFFLE_WINDOW_MS;
    const bScore = storyTime(b) + stableUnitInterval(`${shuffleSeed}:${b?.id || b?.title || ""}`) * SCIENCE_SHUFFLE_WINDOW_MS;
    return bScore - aScore || storyTieBreak(a, b);
  });
}
