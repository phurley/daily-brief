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
