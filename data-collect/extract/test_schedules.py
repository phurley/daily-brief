"""Offline recurrence regressions, including the Metro Times production failure."""
import copy
import json
import unittest
from datetime import date, datetime

from . import feeds, identity, pipeline, publish, repair_schedules, schedules


def event(**changes):
    return dict(dict(kind="event", id="old-span", title="Big Fun Murder", summary="An improv show",
                     url="https://venue.test/event/murder", venue="Go Comedy", city="Ferndale", category="Improv",
                     start="2026-10-09T21:30:00-04:00", end="2026-11-06T21:30:00-05:00",
                     sourceId="metro-times", observedAt="2026-10-09T12:00:00-04:00"), **changes)


def candidate(line="Fridays, 9:30 p.m. Continues through Nov. 6", copy=""):
    return dict(source_slug="metro-times", chunk_id="chunk", url="https://venue.test/listing",
                text="* [Big Fun Murder](https://venue.test/event/murder)\n" + line + "\n" + copy)


class ScheduleTests(unittest.TestCase):
    def expanded(self, r, first=date(2026, 10, 12), last=date(2026, 11, 10)):
        result, errors = schedules.expand([r], first, last)
        self.assertEqual(errors, [])
        return result

    def test_production_fridays_and_dst_without_invented_end(self):
        r = schedules.attach(event(), candidate(copy="EVERY Friday in October!"))
        rows = self.expanded(r)
        self.assertEqual([v["start"] for v in rows], ["2026-10-16T21:30:00-04:00", "2026-10-23T21:30:00-04:00",
                                                      "2026-10-30T21:30:00-04:00", "2026-11-06T21:30:00-05:00"])
        self.assertTrue(all("end" not in v for v in rows))
        self.assertEqual(len({v["seriesId"] for v in rows}), 1)
        self.assertEqual(len({v["sourceRecordId"] for v in rows}), 4)
        self.assertEqual(rows, self.expanded(r))

    def test_neighbors_do_not_contaminate_the_schedule(self):
        c = candidate(copy="* [Other show](https://venue.test/event/other)\nSaturdays, 8-10 p.m.")
        r = schedules.attach(event(), c)
        self.assertEqual(r["schedule"]["rules"][0]["daysOfWeek"], ["FR"])
        self.assertNotIn("Other show", r["schedule"]["evidence"])
        self.assertIsNone(schedules.from_text(event(), schedules.source_section(event(title="Wrong"), c)))

    def test_related_event_footer_is_not_the_primary_schedule(self):
        c = dict(url="https://venue.test/event/murder", text="Admission $20\n## Related Events\nHarvest Festival every Saturday in October\nOct 31 America: 250 years")
        r = schedules.attach(event(), c)
        self.assertNotIn("schedule", r)

    def test_weekday_range_and_inherited_meridian(self):
        r = schedules.attach(event(), candidate("Tuesdays-Saturdays, 11 a.m.-6 p.m. Continues through Nov. 6"))
        self.assertEqual(r["schedule"]["rules"][0]["daysOfWeek"], ["TU", "WE", "TH", "FR", "SA"])
        r = schedules.attach(event(), candidate("Fridays, 11-1 p.m. Continues through Nov. 6"))
        self.assertEqual(r["schedule"]["rules"][0]["startTime"], "11:00:00")
        self.assertEqual(r["schedule"]["rules"][0]["endTime"], "13:00:00")

    def test_multiple_weekdays_and_two_showtimes(self):
        r = schedules.attach(event(), candidate("Fridays, 7:30-9 p.m., Fridays, 10-11:30 p.m. and Saturdays, 7:30-9 & 10-11:30 p.m. Continues through Nov. 6"))
        rows = self.expanded(r, date(2026, 10, 16), date(2026, 10, 17))
        self.assertEqual([(v["start"][11:16], v["end"][11:16]) for v in rows],
                         [("19:30", "21:00"), ("22:00", "23:30"), ("19:30", "21:00"), ("22:00", "23:30")])
        r = schedules.attach(event(), candidate("Fridays, Sundays, 8-10 p.m. Continues through Nov. 6"))
        self.assertEqual(r["schedule"]["rules"][0]["daysOfWeek"], ["FR", "SU"])

    def test_skipped_day_and_graduation_show(self):
        r = schedules.attach(event(start="2026-10-25T13:00:00-04:00", end="2026-12-06T15:00:00-05:00"),
                             candidate("Sundays, 1-3 p.m. Continues through Dec. 6",
                                       "Classes excluding November 29, with a graduation show on December 9th at 7:15pm."))
        rows = self.expanded(r, date(2026, 11, 22), date(2026, 12, 10))
        self.assertEqual([v["start"][:10] for v in rows], ["2026-11-22", "2026-12-06", "2026-12-09"])
        self.assertNotIn("end", rows[-1])

    def test_true_continuous_event_unchanged(self):
        r = event(schedule={"kind": "continuous"})
        before = copy.deepcopy(r)
        self.assertEqual(self.expanded(r), [r])
        self.assertEqual(r, before)

    def test_unknown_and_invalid_time_are_quarantined(self):
        for schedule in ({"kind": "unknown", "evidence": "Every second week"},
                         {"kind": "weekly", "rules": [{"daysOfWeek": ["FR"], "startTime": "99:00"}]}):
            rows, errors = schedules.expand([event(schedule=schedule)], date(2026, 10, 12), date(2026, 11, 10))
            self.assertEqual(rows, [])
            self.assertEqual(errors[0]["errors"][0]["field"], "schedule")
            self.assertFalse(pipeline.valid_record(event(schedule=schedule)))

    def test_model_schedule_evidence_and_singleton_days(self):
        r = event(schedule={"kind": "weekly", "rules": [{"daysOfWeek": ["TU"], "startTime": "19:00"}], "evidence": "Invented quote"})
        c = dict(url=r["url"], text="Concert on Tuesdays.")
        self.assertEqual(schedules.attach(r, c)["schedule"]["kind"], "unknown")

    def test_schedule_only_jsonld_without_event_start_date(self):
        obj = {"@type": "Event", "name": "Tai chi", "description": "Weekly exercise", "url": "https://venue.test/event/tai-chi",
               "location": {"name": "Gym", "address": {"addressLocality": "Canton"}},
               "eventSchedule": {"@type": "Schedule", "repeatFrequency": "P1W", "byDay": "https://schema.org/Wednesday",
                                 "startDate": "2026-10-01", "endDate": "2026-12-31", "startTime": "19:00:00", "endTime": "20:00:00"}}
        parsed = feeds.parse_jsonld_markdown("# Structured data (JSON-LD): https://venue.test/event/tai-chi\n```json\n" + json.dumps(obj) + "\n```")[0]
        base = pipeline.structured_event(parsed)
        self.assertIsNotNone(base)
        base["id"] = "tai-chi"
        self.assertTrue(pipeline.valid_record(base))
        rows = self.expanded(base, date(2026, 10, 12), date(2026, 10, 18))
        self.assertEqual(rows[0]["start"], "2026-10-14T19:00:00-04:00")

    def test_ics_timezone_exclusion_rdate_and_cancelled_override(self):
        body = """# Calendar feed (ICS): https://venue.test/calendar
- 2026-10-09 21:30 to 2026-10-09 23:00: Big Fun Murder @ Go Comedy -- Weekly show
  uid: murder
  url: https://venue.test/event/murder
  recurrence_rule: FREQ=WEEKLY;BYDAY=FR;UNTIL=20261107T050000Z
  start_timezone: America/Chicago
  end_timezone: America/Chicago
  excluded_dates: EXDATE;TZID=America/Chicago:20261023T213000
  recurrence_dates: RDATE;TZID=America/Chicago:20261018T213000
"""
        r = feeds.parse_ics_markdown(body)[0]
        self.assertEqual(r["start"], "2026-10-09T21:30:00-05:00")
        r.update(kind="event", sourceId="venue", city="Canton", category="Improv")
        cancelled = dict(r, recurrenceId="2026-10-30T21:30:00-05:00", start="2026-10-30T21:30:00-05:00", end=None,
                         id="cancellation", status="canceled", statusEvidence="STATUS:CANCELLED")
        rows, errors = schedules.expand([r, cancelled], date(2026, 10, 12), date(2026, 11, 7))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(v["start"][:10] for v in rows), ["2026-10-16", "2026-10-18", "2026-10-30", "2026-11-06"])
        self.assertEqual(sum(v.get("status") == "canceled" for v in rows), 1)
        self.assertTrue(any(v["start"] == "2026-11-06T21:30:00-06:00" for v in rows))

    def test_explicit_dates_overnight_and_rolling_window(self):
        r = event(schedule={"kind": "dates", "dates": [{"start": "2026-10-16T23:00:00-04:00", "end": "2026-10-17T01:00:00-04:00"},
                                                        {"start": "2026-10-30T21:30:00-04:00", "end": None}]})
        first = self.expanded(r)
        later = self.expanded(r, date(2026, 10, 20), date(2026, 11, 10))
        self.assertEqual(later[0]["sourceRecordId"], first[1]["sourceRecordId"])
        self.assertEqual(first[0]["end"], "2026-10-17T01:00:00-04:00")

    def test_unknown_time_and_spring_gap(self):
        r = schedules.attach(event(), candidate("Fridays. Continues through Nov. 6"))
        self.assertEqual(self.expanded(r)[0]["timePrecision"], "date")
        r["schedule"].update(startDate="2026-03-01", endDate="2026-03-31",
                             rules=[{"daysOfWeek": ["SU"], "startTime": "02:30:00"}])
        rows, errors = schedules.expand([r], date(2026, 3, 8), date(2026, 3, 8))
        self.assertEqual(rows, [])
        self.assertIn("Nonexistent", errors[0]["errors"][0]["reason"])

    def test_repairs_idempotent_and_supersedes_old_span(self):
        old = event(origin={"source_slug": "metro-times", "chunk_id": "chunk"})
        records, report = repair_schedules.repair([old], [candidate()])
        self.assertEqual(len(report["repaired"]), 1)
        self.assertNotIn("schedule", old)
        again, report = repair_schedules.repair(records, [candidate()])
        self.assertEqual(records, again)
        self.assertEqual(report["repaired"], [])
        expanded, errors = schedules.expand([old, records[0]], date(2026, 10, 12), date(2026, 11, 10))
        self.assertEqual(errors, [])
        self.assertEqual(len(expanded), 4)
        self.assertTrue(all(v.get("recurrenceId") for v in expanded))

    def test_weekly_master_preserves_separate_explicit_performance(self):
        master = schedules.attach(event(), candidate())
        special = event(id="special", start="2026-10-17T19:00:00-04:00", end="2026-10-17T21:00:00-04:00")
        rows, errors = schedules.expand([master, special], date(2026, 10, 12), date(2026, 11, 10))
        self.assertEqual(errors, [])
        self.assertIn(special, rows)

    def test_session_end_is_not_a_series_cutoff_for_new_extraction(self):
        r = schedules.attach(event(end="2026-10-09T23:00:00-04:00"), candidate("Fridays, 9:30 p.m."))
        self.assertIsNone(r["schedule"]["endDate"])
        self.assertEqual(len(self.expanded(r)), 4)

    def test_publisher_and_registry_repeat_without_merging_showtimes(self):
        r = schedules.attach(event(), candidate("Fridays, 7:30-9 p.m. and Fridays, 10-11:30 p.m. Continues through Oct. 23"))
        doc, _, stats = publish.build([r], "2026-10-12", "2026-10-12T12:00:00Z", identity_mode="canonical")
        again, _, _ = publish.build([r], "2026-10-13", "2026-10-13T12:00:00Z", identity_mode="canonical", registry=stats["identityRegistry"])
        expected = {v["start"]: v["occurrenceId"] for v in doc["events"]}
        self.assertEqual(expected, {v["start"]: v["occurrenceId"] for v in again["events"]})
        self.assertEqual(len(expected), 4)  # Finished sessions are excluded, even within the history window.
        self.assertTrue(publish._doc_validator("events.schema.json").is_valid(doc))

    def test_old_series_survives_stale_filter_and_store_pruning(self):
        r = dict(event(start="2025-01-03T21:30:00-05:00"), recurrenceRule="FREQ=WEEKLY;BYDAY=FR",
                 recurrenceTimezone="America/Detroit", observedAt="2026-10-12T00:00:00-04:00")
        ref = datetime.fromisoformat("2026-10-12T12:00:00-04:00")
        self.assertGreater(pipeline.candidate_days(r, ref), 0)
        self.assertEqual(pipeline._prune_store([r], ref, 183), [r])
        r["observedAt"] = "2025-01-03T12:00:00-05:00"
        self.assertEqual(pipeline._prune_store([r], ref, 183), [r])
        a = dict(r, _kind="feed", feed_kind="ics", item_id="one", source_slug="venue")
        self.assertNotEqual(pipeline._fingerprint(a), pipeline._fingerprint(dict(a, recurrenceRule="FREQ=WEEKLY;BYDAY=SA")))


if __name__ == "__main__":
    unittest.main()
