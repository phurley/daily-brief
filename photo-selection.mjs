// Shuffle the complete candidate pool once per date/data revision. Stable photo
// IDs belong to the source, not this presentation order, so ratings can follow.
export function shufflePhotos(photos, random = Math.random) {
  const result = [...photos];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// Dimensions describe the served image after SmugMug's stored rotation.
// Unknown orientation is excluded rather than guessing from a filename.
export function isLandscapePhoto(photo) {
  return Number.isFinite(photo.width) && Number.isFinite(photo.height)
    && photo.height > 0 && photo.width > photo.height;
}

export function photoDotWindow(total, current, capacity) {
  const count = Math.min(total, Math.max(1, Math.floor(capacity)));
  const start = Math.max(0, Math.min(current - Math.floor(count / 2), total - count));
  return Array.from({ length: count }, (_, offset) => start + offset);
}

const photoMonths = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sept', 'Oct', 'Nov', 'Dec'];

export function photoDateLabel(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return '';
  const date = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(date.valueOf()) || date.toISOString().slice(0, 10) !== value) return '';
  // Safari abbreviates September as "Sep" even with en-GB; keep one format.
  return `${date.getUTCDate()} ${photoMonths[date.getUTCMonth()]} ${date.getUTCFullYear()}`;
}
