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
            "live": {"value": False, "confidence": 0.8, "probability": 0.2},
            "ignored": {"value": True, "confidence": 0.5},  # no probability
        }
        signals = scoring.signal_probabilities(raw)
        self.assertEqual(signals["distinctive"], 0.8123)
        self.assertEqual(signals["live"], 0.2)
        self.assertNotIn("ignored", signals)

    def test_detail_includes_score_only_when_given(self):
        raw = {"distinctive": {"probability": 0.5}}
        self.assertEqual(scoring.detail(raw), {"signals": {"distinctive": 0.5}})
        self.assertEqual(scoring.detail(raw, score=77)["score"], 77)


if __name__ == "__main__":
    unittest.main()