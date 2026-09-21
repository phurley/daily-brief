#!/usr/bin/env python3
"""Offline tests for publish-side scoring passthrough and content drops.

Run: ``python3 -m unittest extract.test_publish``
"""

from __future__ import annotations

import unittest

from . import publish


class PublishScoringTest(unittest.TestCase):
    def test_scoring_passthrough_sanitizes(self):
        record = {
            "scoring": {
                "score": 150,
                "signals": {"distinctive": 0.5, "bad": "x", "neg": -1},
            }
        }
        out = publish._scoring(record)
        self.assertEqual(out["score"], 100)
        self.assertEqual(out["signals"], {"distinctive": 0.5, "neg": 0.0})
        self.assertIsNone(publish._scoring({}))
        self.assertIsNone(publish._scoring({"scoring": {"signals": {}}}))

    def test_dedupe_keeps_richest_same_day(self):
        thin = {"kind": "event", "title": "Witches Night Bazaar: Autumnal Equinox",
                "start": "2026-09-20T17:00:00-04:00", "summary": "short", "venue": "Tangent Gallery"}
        rich = {"kind": "event", "title": "Witches Night Bazaar",
                "start": "2026-09-20T18:30:00-04:00", "summary": "a much longer summary here",
                "venue": "Tangent Gallery", "city": "Detroit", "imageUrl": "http://x/i.jpg", "score": 100}
        out = publish._dedupe([thin, rich])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Witches Night Bazaar")

    def test_dedupe_keeps_distinct_days_and_titles(self):
        a = {"kind": "event", "title": "Live Music: Band A", "start": "2026-09-20T19:00:00-04:00"}
        b = {"kind": "event", "title": "Live Music: Band B", "start": "2026-09-20T20:00:00-04:00"}
        c = {"kind": "event", "title": "Live Music: Band A", "start": "2026-09-21T19:00:00-04:00"}
        self.assertEqual(len(publish._dedupe([a, b, c])), 3)

    def test_dedupe_keeps_distinct_sessions(self):
        # Same base title + day but different venue/time => distinct sessions.
        a = {"kind": "event", "title": "Dolly Day",
             "start": "2026-09-25T17:30:00-04:00", "venue": "Downtown Library: Lower Level"}
        b = {"kind": "event", "title": "Dolly Day | Screening: 9 to 5",
             "start": "2026-09-25T11:00:00-04:00", "venue": "Downtown Library: 4th Floor"}
        self.assertEqual(len(publish._dedupe([a, b])), 2)

    def test_dedupe_merges_close_same_venue(self):
        a = {"kind": "event", "title": "Witches Night Bazaar",
             "start": "2026-09-20T18:30:00-04:00",
             "venue": "Tangent Gallery / Hastings Street Ballroom",
             "summary": "a longer summary here indeed"}
        b = {"kind": "event", "title": "Witches Night Bazaar: Autumnal Equinox",
             "start": "2026-09-20T17:00:00-04:00",
             "venue": "Tangent Gallery & Hastings Street Ballroom", "summary": "short"}
        out = publish._dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Witches Night Bazaar")

    def test_same_title_helper(self):
        self.assertTrue(publish._same_title(
            "Witches Night Bazaar", "Witches Night Bazaar: Autumnal Equinox"))
        self.assertFalse(publish._same_title("Witches Night Bazaar", "Witches Night Bazaar Extra"))

    def test_content_drop_thresholds(self):
        self.assertTrue(publish._content_dropped(
            {"contentFlags": {"is_lottery": True, "lotteryConfidence": 0.9}}))
        self.assertFalse(publish._content_dropped(
            {"contentFlags": {"is_lottery": True, "lotteryConfidence": 0.2}}))
        self.assertTrue(publish._content_dropped(
            {"contentFlags": {"is_sports": True, "sportsConfidence": 0.7}}))
        self.assertFalse(publish._content_dropped(
            {"contentFlags": {"is_sports": True, "sportsConfidence": 0.1}}))
        self.assertFalse(publish._content_dropped({}))


if __name__ == "__main__":
    unittest.main()