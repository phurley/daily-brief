"""Offline regressions for persistent retries, repair, and bounded migration."""
import copy
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import io

from . import enrich, jev, llm, pipeline, retry

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def candidate(cid="one", **kwargs):
    return dict(dict(source_slug="venue", source_name="Venue", locality_index=0,
                     chunk_id=cid, _kind="chunk", candidate_hint="event",
                     url="https://venue.test/event", text="Concert October 23, 2026 at 7 PM"), **kwargs)


def event(**kwargs):
    return dict(dict(kind="event", title="Concert", summary="A concert.",
                     url="https://venue.test/event", start="2026-10-23T19:00:00-04:00",
                     venue="Venue", city="Canton", category="Music"), **kwargs)


def triage(accepted=True, count="1"):
    return jev.Triage("one", accepted, count, "event", True, 4, "event", 1.0)


class RetryTests(unittest.TestCase):
    def clients(self, response=None, tri=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        chat = Mock()
        chat.complete_json.return_value = response if response is not None else event()
        stack.enter_context(patch.object(jev, "make_client", return_value=object()))
        gate = stack.enter_context(patch.object(jev, "triage_record", return_value=tri or triage()))
        stack.enter_context(patch.object(llm, "make_chat_client", return_value=chat))
        stack.enter_context(patch.object(pipeline.scoring, "score_record", return_value=(0, {})))
        stack.enter_context(patch.object(enrich, "learned_strategies", return_value={}))
        stack.enter_context(patch.object(enrich, "_learn_strategy"))
        return chat, gate

    def run_items(self, items=None, **kwargs):
        return pipeline.run(items or [candidate()], workers=1, extract_limit=kwargs.pop("extract_limit", 10),
                            model=None, enrich_events=kwargs.pop("enrich_events", False), **kwargs)

    def test_failure_retry_success_then_terminal(self):
        chat, _ = self.clients()
        chat.complete_json.side_effect = [llm.LLMError("503"), event()]
        c = candidate()
        records, stats, outcomes = self.run_items([c])
        self.assertEqual(records, [])
        self.assertEqual(outcomes[0][1], "retry:generation_failure")
        row = retry.entry(pipeline._fingerprint(c), outcomes[0][1], {}, NOW,
                          max_attempts=3, backoff_hours=1, record_ids=[])
        self.assertFalse(retry.due(row, NOW + timedelta(minutes=59), 3))
        self.assertTrue(retry.due(row, NOW + timedelta(hours=1), 3))
        records, _, outcomes = self.run_items([c])
        self.assertTrue(pipeline.valid_record(records[0]))
        self.assertEqual(outcomes[0][1], "extracted")
        self.assertEqual(records[0]["origin"]["fingerprint"], pipeline._fingerprint(c))
        terminal = retry.entry(row["fp"], "extracted", row, NOW + timedelta(hours=1),
                               max_attempts=3, backoff_hours=1, record_ids=[records[0]["id"]])
        self.assertFalse(retry.due(terminal, NOW + timedelta(days=1), 3))

    def test_last_state_wins_and_exhausts_at_three(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "index.jsonl"
            row = {"fp": "fp", "outcome": "extracted"}
            pipeline.append_index(path, [row])
            for attempt in range(1, 4):
                row = retry.entry("fp", "retry:empty_output", row, NOW,
                                  max_attempts=3, backoff_hours=1, record_ids=[])
                pipeline.append_index(path, [row])
                self.assertEqual(row["attempts"], attempt)
                if attempt < 3:
                    self.assertNotIn("fp", pipeline.load_index(path))
                    self.assertEqual(retry.timestamp(row["nextRetryAt"]), NOW + timedelta(hours=4 ** (attempt-1)))
            self.assertEqual(row["outcome"], "exhausted")
            self.assertIn("fp", pipeline.load_index(path))
            self.assertFalse(retry.due(row, NOW + timedelta(days=100), 3))

    def test_empty_and_partial_output_are_not_complete(self):
        chat, _ = self.clients(tri=triage(count="2"))
        chat.complete_json.return_value = {"events": []}
        self.assertEqual(self.run_items()[2][0][1], "retry:empty_output")
        chat.complete_json.return_value = {"events": [event(), event(title="Other", start=None)]}
        records, _, outcomes = self.run_items()
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(pipeline.valid_record(r) for r in records), 1)
        self.assertEqual(outcomes[0][1], "retry:incomplete")
        chat.complete_json.return_value = {"events": [event(), event(title=42)]}
        records, _, outcomes = self.run_items()
        self.assertEqual(len(records), 1)
        self.assertEqual(outcomes[0][1], "retry:invalid_output")

    def test_triage_failure_isolated_and_deferred_does_not_spend_attempt(self):
        _, gate = self.clients()
        gate.side_effect = [jev.JevError("503"), triage(), triage()]
        records, stats, outcomes = self.run_items([candidate("a"), candidate("b"), candidate("c")], extract_limit=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual({c["chunk_id"]: s for c,s in outcomes}, {"a": "retry:triage_failure", "b": "extracted"})

    def test_repair_uses_matching_structured_page_without_generation(self):
        chat, _ = self.clients()
        c = candidate()
        old = pipeline.finalize(event(start=None, venue=None, city=None), c)
        before = copy.deepcopy(old)
        html = '<script type="application/ld+json">' + json.dumps([
            {"@type": "Event", "name": "Wrong event", "startDate": "2026-11-01T20:00:00-05:00"},
            {"@type": "Event", "name": "Concert", "startDate": "2026-10-23T19:00:00-04:00",
             "location": {"name": "Venue", "address": {"addressLocality": "Canton"}}}]) + '</script>'
        with patch.object(enrich, "fetch_html", return_value=html), patch.object(enrich, "learned_strategies", return_value={"venue": "none"}):
            records, stats, outcomes = self.run_items([c], enrich_events=True,
                                                     previous={pipeline._fingerprint(c): [old]})
        self.assertEqual(outcomes[0][1], "extracted")
        self.assertEqual(records[0]["start"], event()["start"])
        self.assertEqual(records[0]["timePrecision"], "time")
        self.assertTrue(pipeline.valid_record(records[0]))
        self.assertEqual(stats["generation_calls"], 0)
        chat.complete_json.assert_not_called()
        self.assertEqual(old, before)

    def test_null_fields_are_filled_by_touchup(self):
        chat, _ = self.clients()
        chat.complete_json.return_value = {"start": event()["start"], "venue": "Venue", "city": "Canton"}
        record = event(start=None, venue=None, city=None)
        with patch.object(enrich, "fetch_html", return_value="<p>Concert October 23 at Venue, Canton.</p>"):
            enrich.enrich_event(record, chat)
        self.assertEqual(record["start"], event()["start"])
        self.assertEqual(record["venue"], "Venue")

    def test_unrelated_structured_event_is_not_used(self):
        raw = '<script type="application/ld+json">'+json.dumps({"@type":"Event", "name":"Other", "startDate":event()["start"]})+'</script>'
        self.assertEqual(enrich._jsonld_event_fields(raw, "Concert"), {})

    def test_calendar_repair_requires_one_matching_event(self):
        raw = (b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Other\nDTSTART:20261022T230000Z\nEND:VEVENT\n"
               b"BEGIN:VEVENT\nSUMMARY:Concert\nDTSTART:20261023T230000Z\nEND:VEVENT\nEND:VCALENDAR")
        with patch.object(enrich.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            self.assertEqual(enrich._ical_times("https://venue.test/?ical=1", "Concert")[0],
                             "2026-10-23T23:00:00+00:00")
        with patch.object(enrich.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            self.assertEqual(enrich._ical_times("https://venue.test/?ical=1", "Absent"), (None, None))

    def test_changed_feed_content_has_a_separate_retry_budget(self):
        a = candidate(_kind="feed", item_id="feed", title="Concert", summary="Original")
        b = dict(a, summary="Updated details")
        exhausted = {pipeline._fingerprint(a): {"outcome":"exhausted", "attempts":3, "stateVersion":2}}
        selected, _, _ = self.select([b], exhausted)
        self.assertEqual(selected, [b])

    def test_zero_retry_limit_leaves_fresh_work_enabled(self):
        a,b = candidate("a"),candidate("b")
        states = {pipeline._fingerprint(a): {"outcome":"retry", "attempts":1, "nextRetryAt":NOW.isoformat()}}
        self.assertEqual(self.select([a,b], states, retry_limit=0)[0], [b])

    def select(self, candidates, states, store=(), archived=None, **kwargs):
        defaults = dict(max_items=4, retry_limit=1, max_attempts=3, replay_days=14, stale_days=2)
        defaults.update(kwargs)
        return pipeline.select_work(candidates, states, archived or {}, list(store), NOW, **defaults)

    def test_retry_budget_does_not_starve_fresh_candidates(self):
        cs = [candidate(str(n)) for n in range(8)]
        states = {pipeline._fingerprint(c): {"outcome":"retry", "attempts":1, "nextRetryAt":NOW.isoformat()} for c in cs[:4]}
        selected, _, stats = self.select(cs, states)
        self.assertEqual(len(selected), 4)
        self.assertEqual(stats["retry_selected"], 1)
        self.assertEqual(sum(pipeline._fingerprint(c) not in states for c in selected), 3)

    def test_migration_replays_only_recent_incomplete_or_missing_event_outputs(self):
        cs = [candidate(str(n)) for n in range(6)]
        states = {pipeline._fingerprint(c): {"outcome":"extracted", "ts":(NOW-timedelta(days=1)).isoformat()} for c in cs}
        states[pipeline._fingerprint(cs[3])]["outcome"] = "rejected"
        states[pipeline._fingerprint(cs[4])]["ts"] = (NOW-timedelta(days=15)).isoformat()
        cs[5]["candidate_hint"] = "news"
        good = pipeline.finalize(event(), cs[0])
        incomplete = pipeline.finalize(event(start=None), cs[1])
        selected, _, _ = self.select(cs, states, [good, incomplete], retry_limit=10, max_items=10)
        self.assertEqual({c["chunk_id"] for c in selected}, {"1", "2"})

    def test_archived_absolute_dates_enforce_staleness(self):
        c = candidate(signals={"date_spans":[{"iso":"2026-09-01T12:00:00-04:00"}]}, age_days=5)
        fp = pipeline._fingerprint(c)
        states = {fp:{"outcome":"extracted", "ts":(NOW-timedelta(days=1)).isoformat()}}
        self.assertEqual(self.select([], states, archived={fp:{"fp":fp,"candidate":c}})[0], [])
        c["signals"] = {"date_spans":[{"iso":"2026-10-23T12:00:00-04:00"}]}
        self.assertEqual(len(self.select([], states, archived={fp:{"fp":fp,"candidate":c}})[0]), 1)

    def test_main_persists_retry_then_success_and_skips_third_run(self):
        chat, _ = self.clients()
        chat.complete_json.side_effect = [llm.LLMError("503"), event()]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "venue"
            source.mkdir()
            (source / "chunks.jsonl").write_text(json.dumps(candidate())+"\n")
            out, index = root / "records.jsonl", root / "index.jsonl"
            args = ["--processed", str(root), "--out", str(out), "--index", str(index),
                    "--source-file", str(root / "missing.json"), "--no-enrich", "--workers", "1"]
            self.assertEqual(pipeline.main([*args, "--dry-run"]), 0)
            self.assertFalse(out.exists())
            chat.complete_json.assert_not_called()
            self.assertEqual(pipeline.main(args), 0)
            state = retry.load_states(index)[pipeline._fingerprint(candidate())]
            self.assertEqual(state["outcome"], "retry")
            self.assertEqual(pipeline.load_store(out), [])
            # Before backoff expires, no second paid call.
            self.assertEqual(pipeline.main(args), 0)
            self.assertEqual(chat.complete_json.call_count, 1)
            with patch.object(pipeline.dates, "parse_reference", return_value=retry.timestamp(state["nextRetryAt"])+timedelta(seconds=1)):
                self.assertEqual(pipeline.main(args), 0)
                self.assertEqual(pipeline.main(args), 0)
            state = retry.load_states(index)[pipeline._fingerprint(candidate())]
            self.assertEqual(state["outcome"], "extracted")
            self.assertEqual(state["attempts"], 2)
            self.assertEqual(len(pipeline.load_store(out)), 1)
            self.assertEqual(chat.complete_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
