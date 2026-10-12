#!/usr/bin/env python3
"""Offline tests for publish-side scoring passthrough and content drops.

Run: ``python3 -m unittest extract.test_publish``
"""

from __future__ import annotations

import unittest
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from . import publish


class PublishLocalityTest(unittest.TestCase):
    def story(self, **changes):
        return dict(dict(kind="news", id="chelsea-story", title="News in Chelsea",
                         summary="A neighborhood update in Chelsea.",
                         url="https://patch.com/new-york/chelsea-ny/news",
                         publishedAt="2026-10-12T10:29:00-04:00",
                         addedAt="2026-10-12T15:29:41-04:00", localityIndex=0,
                         locations=["Chelsea, NY"], topics=["community"],
                         source={"name": "Reporter", "publication": "Canton Patch"}), **changes)

    def test_existing_out_of_state_record_cannot_publish_from_canton_feed(self):
        record = self.story()
        original = copy.deepcopy(record)
        self.assertIsNone(publish.to_story(record))
        self.assertEqual(record, original)
        # Extracted locations remain decisive when a URL has no state scope.
        self.assertIsNone(publish.to_story(self.story(url="https://news.example/chelsea")))

    def test_item_evidence_corrects_feed_rating_and_preserves_michigan_story(self):
        record = self.story(url="https://news.example/chelsea", locations=["Chelsea, MI"])
        published = publish.to_story(record)
        self.assertEqual(published["localityIndex"], 2)
        self.assertEqual(published["source"], record["source"])
        self.assertEqual(published["url"], record["url"])
        self.assertEqual(record["localityIndex"], 0)
        detroit = self.story(title="Detroit delegation visits Canton", summary="A visit to Canton, Ohio.",
                             locations=["Detroit, MI", "Canton, Ohio"], url="https://news.example/visit")
        self.assertEqual(publish.to_story(detroit)["localityIndex"], 3)
        road = self.story(title="Incident on Plymouth Road in Detroit", summary="An incident on Plymouth Road.",
                          locations=["Detroit"], url="https://news.example/incident")
        self.assertEqual(publish.to_story(road)["localityIndex"], 3)
        visitor = self.story(title="Utah resident involved in Brighton incident", summary="An incident in Brighton.",
                             locations=["Brighton Township"], url="https://news.example/brighton", localityIndex=3)
        self.assertEqual(publish.to_story(visitor)["localityIndex"], 3)
        statewide = self.story(title="Michigan artist wins Nashville award",
                               summary="Millington, Michigan artist wins in Nashville, Tennessee.",
                               locations=["Millington, Michigan", "Nashville, Tennessee"],
                               url="https://news.example/award")
        self.assertEqual(publish.to_story(statewide)["localityIndex"], 4)

    def test_unknown_uses_prior_without_treating_source_name_as_location(self):
        record = self.story(title="Neighborhood update", summary="A new community program.",
                            url="https://news.example/story", locations=[], localityIndex=4)
        self.assertEqual(publish.to_story(record)["localityIndex"], 4)
        self.assertIsNone(publish.to_story({**record, "localityIndex": None}))

    def test_publication_rechecks_stored_news_in_legacy_and_canonical_modes(self):
        local = self.story(id="local-story", title="Chelsea Michigan news",
                           summary="An update in Chelsea, Michigan.", locations=["Chelsea, MI"],
                           url="https://news.example/local")
        records = [self.story(), local]
        before = copy.deepcopy(records)
        for mode in ("legacy", "canonical"):
            with self.subTest(mode=mode):
                _, news, _ = publish.build(records, "2026-10-12", "2026-10-12T20:00:00Z",
                                          identity_mode=mode)
                self.assertEqual([s["id"] for s in news["stories"]], ["local-story"])
                self.assertEqual(news["stories"][0]["localityIndex"], 2)
                self.assertTrue(publish._doc_validator("news.schema.json").is_valid(news))
        self.assertEqual(records, before)


class PublishDateValidationTest(unittest.TestCase):
    def event(self, **changes):
        return dict(dict(kind="event", id="valid-event", title="A valid event",
                         summary="An evening performance", url="https://example.com/event",
                         start="2026-10-10T17:00:00-04:00", venue="The venue",
                         city="Canton", category="Music"), **changes)

    def test_dates_without_optional_jsonschema_checker(self):
        import jsonschema
        with patch.dict(jsonschema.FormatChecker.checkers, clear=True):
            validator = publish._validator("events.schema.json", "event")
            for start in ("2026-10-10T17:00:00-04:00", "2026-10-10T21:00:00Z",
                          "2026-10-10T21:00:00.123+00:00"):
                self.assertTrue(validator.is_valid(publish.to_event(self.event(start=start))))
            for start in ("T17:00:00-04:00", "T10:00:00-04:00", "2026-02-30T17:00:00-04:00",
                          "2026-10-10", "2026-10-10T17:00:00", "2026-10-10T25:00:00Z",
                          "2026-10-10T17:00:00+04:99"):
                with self.subTest(start=start):
                    event = publish.to_event(self.event(start=start))
                    self.assertFalse(validator.is_valid(event))
                    doc = dict(schemaVersion="2.0.0", generatedAt="2026-10-10T12:00:00Z",
                               editionDate="2026-10-10", events=[event])
                    self.assertFalse(publish._doc_validator("events.schema.json").is_valid(doc))

    def test_three_production_failures_quarantined_valid_record_unchanged(self):
        valid = self.event()
        bad = [self.event(id=str(i), title=title, start=start, end=end,
                         origin={"source_slug": "source", "text": "Original source evidence"})
               for i, (title, start, end) in enumerate([
                   ("Immigration Office Hours", "T17:00:00-04:00", "T20:00:00-04:00"),
                   ("Free Laundry Service", "T10:00:00-04:00", "T16:00:00-04:00"),
                   ("Free Showers", "T10:00:00-04:00", "T18:00:00-04:00")])]
        records = [valid, *bad]
        before = copy.deepcopy(records)
        for mode in ("legacy", "canonical"):
            with self.subTest(mode=mode):
                expected = publish.build([valid], "2026-10-10", "2026-10-10T12:00:00Z", identity_mode=mode)[0]
                events, _, stats = publish.build(records, "2026-10-10", "2026-10-10T12:00:00Z", identity_mode=mode)
                self.assertEqual(events, expected)
                self.assertEqual(stats["events_quarantined"], 3)
                self.assertEqual(stats["events_stale"], 0)
                self.assertEqual({q["record"]["title"] for q in stats["eventQuarantine"]},
                                 {r["title"] for r in bad})
                self.assertTrue(all({e["field"] for e in q["errors"]} == {"start", "end"}
                                    for q in stats["eventQuarantine"]))
        self.assertEqual(records, before)

    def test_bad_end_is_quarantined_not_treated_as_stale(self):
        events, _, stats = publish.build([self.event(start="2026-09-01T17:00:00-04:00", end="T20:00:00-04:00")],
                                         "2026-10-10", "2026-10-10T12:00:00Z")
        self.assertEqual(events["events"], [])
        self.assertEqual(stats["events_quarantined"], 1)
        self.assertEqual(stats["events_stale"], 0)

    def test_end_of_day_midnight_preserved_and_month_only_quarantined(self):
        midnight = self.event(end="2026-10-10T24:00:00-04:00")
        month_only = self.event(id="month-only", title="Unknown day", start="2026-08", end="2026-08")
        events, _, stats = publish.build([midnight, month_only], "2026-10-10", "2026-10-10T12:00:00Z")
        self.assertEqual(len(events["events"]), 1)
        self.assertEqual(events["events"][0]["end"], "2026-10-11T00:00:00-04:00")
        self.assertEqual(stats["events_quarantined"], 1)
        self.assertEqual(midnight["end"], "2026-10-10T24:00:00-04:00")

    def test_already_incomplete_records_do_not_inflate_quarantine(self):
        _, _, stats = publish.build([self.event(start=None)], "2026-10-10", "2026-10-10T12:00:00Z")
        self.assertEqual(stats["events_quarantined"], 0)

    def test_private_quarantine_and_dry_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "private" / "records.jsonl"
            records.parent.mkdir()
            bad = self.event(start="T17:00:00-04:00", origin={"text": "Source evidence"})
            news = dict(kind="news", id="news", title="News", summary="Local news",
                        url="https://example.com/news", source={"name": "Local paper"},
                        publishedAt="2026-10-10T12:00:00Z", localityIndex=0)
            records.write_text(json.dumps(bad) + "\n" + json.dumps(news) + "\n")
            original = records.read_bytes()
            out = root / "public"
            args = ["--records", str(records), "--out", str(out),
                    "--edition-date", "2026-10-10", "--identity-mode", "legacy"]
            self.assertEqual(publish.main([*args, "--dry-run"]), 0)
            self.assertFalse(out.exists())
            quarantine = records.with_name("event_quarantine.json")
            self.assertFalse(quarantine.exists())
            self.assertEqual(publish.main(args), 0)
            report = json.loads(quarantine.read_text())
            self.assertEqual(report["count"], 1)
            self.assertEqual(report["records"][0]["record"], bad)
            self.assertEqual(records.read_bytes(), original)
            self.assertEqual(json.loads((out / "events.json").read_text())["events"], [])
            self.assertFalse((out / "event_quarantine.json").exists())


class PublishScoringTest(unittest.TestCase):
    def test_event_distance_status_and_rescoring(self):
        record = dict(kind='event', id='market', title='Market', summary='Produce',
                      url='https://example.com', start='2026-10-09T12:00:00-04:00',
                      venue='Market', city='Canton', category='market', distanceMiles=0,
                      status='cancelled', occurrenceId='market-friday',
                      scoring={'score': 8, 'signals': {'farmer_market': 1, 'recurring': 1, 'market_or_shop': 1}})
        event = publish.to_event(record)
        self.assertEqual(event['distanceMiles'], 0)
        self.assertEqual(event['status'], 'cancelled')
        self.assertEqual(event['occurrenceId'], 'market-friday')
        self.assertEqual(event['score'], 76)
        self.assertEqual(event['scoring']['rulesVersion'], 2)
        self.assertEqual(event['localityTier'], 'nearby')
        for invalid in (-1, True, float('nan'), '12'):
            self.assertNotIn('distanceMiles', publish.to_event({**record, 'distanceMiles': invalid}))

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
