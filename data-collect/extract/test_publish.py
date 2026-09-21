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