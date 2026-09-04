import assert from "node:assert/strict";
import test from "node:test";

import { orderNewsStories, orderScienceStories } from "../story-order.mjs";

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

test("science shuffle is stable for a visit and changes with its seed", () => {
  const stories = Array.from({ length: 8 }, (_, index) => ({
    id: `story-${index}`,
    publishedAt: "2026-09-04T12:00:00Z",
  }));
  const first = orderScienceStories(stories, 101).map(({ id }) => id);

  assert.deepEqual(orderScienceStories(stories, 101).map(({ id }) => id), first);
  assert.notDeepEqual(orderScienceStories(stories, 202).map(({ id }) => id), first);
});

test("science freshness bias prevents a much older story from jumping ahead", () => {
  const stories = [
    { id: "week-old", publishedAt: "2026-08-28T12:00:00Z" },
    { id: "new", publishedAt: "2026-09-04T12:00:00Z" },
  ];

  for (const seed of [1, 2, 3, 4, 5, 999]) {
    assert.equal(orderScienceStories(stories, seed)[0].id, "new");
  }
});
