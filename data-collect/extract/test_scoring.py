#!/usr/bin/env python3
"""Offline tests for the persisted scoring breakdown (no API key needed).

Run: ``python3 -m unittest extract.test_scoring``
"""

from __future__ import annotations

import unittest

from . import scoring


class ScoringBreakdownTest(unittest.TestCase):
    def test_signal_probabilities(self):
        raw = {
            "distinctive": {"value": True, "confidence": 0.9, "probability": 0.81234},
            "live_music": {"value": False, "confidence": 0.8, "probability": 0.2},
            "ignored": {"value": True, "confidence": 0.5},  # no probability
        }
        signals = scoring.signal_probabilities(raw)
        self.assertEqual(signals["distinctive"], 0.8123)
        self.assertEqual(signals["live_music"], 0.2)
        self.assertNotIn("ignored", signals)

    def test_detail_includes_score_only_when_given(self):
        raw = {"distinctive": {"probability": 0.5}}
        self.assertEqual(scoring.detail(raw), {"signals": {"distinctive": 0.5}})
        with_score = scoring.detail(raw, with_score=True)
        self.assertEqual(with_score["score"], scoring.score_probabilities({"distinctive": 0.5}))

    def test_score_is_reproducible_from_rounded_signals(self):
        # A score must be exactly re-derivable from the stored (rounded) signals.
        raw = {"distinctive": {"probability": 0.123456789},
               "sporting": {"probability": 0.987654321}}
        detail = scoring.detail(raw, with_score=True)
        self.assertEqual(detail["score"], scoring.score_probabilities(detail["signals"]))
        self.assertEqual(scoring.score_answers(raw), detail["score"])

    def test_signal_set_includes_new_categories(self):
        expected = {
            "outdoors", "theatre",
            "substance_recovery", "popular_music_cover_band", "market_or_shop",
            "punk_metal_or_rock", "dance", "sales_related", "children_activity",
            "running", "exercise", "employment_related",
        }
        self.assertEqual(len(scoring.SIGNAL_NAMES), 26)
        self.assertNotIn("live", scoring.SIGNAL_NAMES)  # too broad; replaced by live_music/live_comedy
        self.assertTrue(expected.issubset(set(scoring.SIGNAL_NAMES)))
        for name in expected:
            self.assertIn(name, scoring.POS_WEIGHTS if name in scoring.POS_WEIGHTS else scoring.NEG_WEIGHTS)
        # A farmers market must net positive against the market_or_shop penalty.
        self.assertGreater(scoring.POS_WEIGHTS["farmer_market"], scoring.NEG_WEIGHTS["market_or_shop"])


if __name__ == "__main__":
    unittest.main()