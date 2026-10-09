import assert from "node:assert/strict";
import test from "node:test";

import { orderNewsStories } from "../story-order.mjs";

const utcDateKey = (date) => date.toISOString().slice(0, 10);

test("news puts today's stories before older stories regardless of locality", () => {
  const stories = [
    { id: "older-local", publishedAt: "2026-09-04T20:00:00Z", addedAt: "2026-09-03T23:59:00Z", localityIndex: 0 },
    { id: "today-statewide", publishedAt: "2026-09-03T08:00:00Z", addedAt: "2026-09-04T00:01:00Z", localityIndex: 4 },
  ];

  assert.deepEqual(
    orderNewsStories(stories, { today: "2026-09-04", dateKey: utcDateKey }).map(({ id }) => id),
    ["today-statewide", "older-local"],
  );
});

test("news uses locality before publication time within today", () => {
  const stories = [
    { id: "later-statewide", publishedAt: "2026-09-04T20:00:00Z", addedAt: "2026-09-04T09:02:00Z", localityIndex: 4 },
    { id: "earlier-canton", publishedAt: "2026-09-04T08:00:00Z", addedAt: "2026-09-04T09:00:00Z", localityIndex: 0 },
    { id: "mid-metro", publishedAt: "2026-09-04T12:00:00Z", addedAt: "2026-09-04T09:01:00Z", localityIndex: 3 },
  ];

  assert.deepEqual(
    orderNewsStories(stories, { today: "2026-09-04", dateKey: utcDateKey }).map(({ id }) => id),
    ["earlier-canton", "mid-metro", "later-statewide"],
  );
});

test("news keeps older days in reverse chronological order", () => {
  const stories = [
    { id: "older-local", publishedAt: "2026-09-03T20:00:00Z", addedAt: "2026-09-03T08:00:00Z", localityIndex: 0 },
    { id: "newer-statewide", publishedAt: "2026-09-03T08:00:00Z", addedAt: "2026-09-03T20:00:00Z", localityIndex: 4 },
  ];

  assert.deepEqual(
    orderNewsStories(stories, { today: "2026-09-04", dateKey: utcDateKey }).map(({ id }) => id),
    ["newer-statewide", "older-local"],
  );
});
