import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import health_report as hr

NOW = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
LOG = """2026-11-05 01:30:00 [start] collect
noise
2026-11-05 03:00:00 [done] exit=0
2026-11-06 01:30:00 [start] collect
Traceback (most recent call last):
2026-11-06 06:00:00 [done] exit=0
"""


class JobsTest(unittest.TestCase):
    def test_runs_parse_with_problems_and_durations(self):
        runs = hr.job_runs(LOG.splitlines(), r"\[start\] collect", r"\[done\] exit=(\d+)")
        self.assertEqual([r["exit"] for r in runs], [0, 0])
        self.assertEqual(runs[1]["problems"], ["Traceback (most recent call last):"])
        self.assertEqual(runs[1]["end"] - runs[1]["start"], timedelta(hours=4, minutes=30))

    def test_job_check_flags_slow_runs_problems_and_missing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cron.log"; log.write_text(LOG)
            now = datetime(2026, 11, 6, 8, 0).astimezone()
            [collect, missing, failed] = hr.jobs(now, {
                "collect": (log, r"\[start\] collect", r"\[done\] exit=(\d+)", 3.0),
                "science": (log, r"\[start\] science", r"\[done\] science.*exit=(\d+)", 1.0),
                "nolog": (Path(tmp) / "none.log", "x", "y", 1.0)}) + []
            self.assertEqual(collect["status"], "warn")
            self.assertIn("longest 4.5h", collect["detail"])
            self.assertEqual(missing["status"], "fail")
            self.assertEqual(failed["status"], "warn")


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def test_freshness_and_sections(self):
        self.write("events.json", {"generatedAt": (NOW - timedelta(hours=2)).isoformat(), "events": [1, 2]})
        self.write("news.json", {"generatedAt": (NOW - timedelta(hours=40)).isoformat(), "stories": []})
        self.write("weather.json", {"generatedAt": (NOW - timedelta(hours=30)).isoformat(), "daily": [1]})
        fresh = {c["check"]: c["status"] for c in hr.freshness(NOW, self.tmp)}
        self.assertEqual((fresh["fresh:events.json"], fresh["fresh:news.json"], fresh["fresh:weather.json"]),
                         ("ok", "warn", "fail"))
        self.assertEqual(fresh["fresh:vibe.json"], "fail")  # missing
        checks, counts = hr.sections(self.tmp, {"counts": {"events.json": 9}})
        status = {c["check"]: (c["status"], c["detail"]) for c in checks}
        self.assertEqual(status["section:events.json"], ("warn", "events: 2 (was 9)"))
        self.assertEqual(status["section:news.json"][0], "fail")
        self.assertEqual(counts["events.json"], 2)

    def test_spend_flags_budget_failures_and_fallbacks(self):
        log = self.tmp / "usage.jsonl"
        at = (NOW - timedelta(hours=1)).isoformat()
        rows = ([{"at": at, "caller": "pipeline", "kind": "chat", "model": "google/gemini-3.1-flash-lite (gemini free)", "cost": 0}] * 8
                + [{"at": at, "caller": "pipeline", "kind": "chat", "model": "google/gemini-3.1-flash-lite (gemini flex)", "cost": 0.6}] * 2
                + [{"at": at, "caller": "science", "kind": "chat", "model": "google/gemini-2.5-flash", "cost": 0.1}]
                + [{"at": at, "caller": "science", "kind": "error", "model": "google/gemini-3.8-flash (gemini default)"}] * 3)
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET": "1"}):
            os.environ.pop("LLM_BUDGET", None)
            result = {c["check"]: c for c in hr.spend(NOW, log)}
        self.assertEqual(result["spend:total"]["status"], "fail")  # $1.30 > $1
        self.assertIn("free key served 80% of 10 extraction calls (gemini free: 8, gemini flex: 2)",
                      result["spend:free-tier"]["detail"])
        self.assertEqual(result["spend:failed-attempts"]["status"], "warn")
        self.assertEqual(result["spend:science-fallback"]["status"], "warn")

    def test_sources_and_quality(self):
        self.write("science-health.json", {"sources": {
            "esa": {"errors": ["HTTPError: 404"], "lastSuccessAt": "2026-11-04T11:00:00Z"},
            "nasa": {"errors": [], "lastSuccessAt": "2026-11-06T06:00:00Z"}}})
        (self.tmp / "processed").mkdir()
        (self.tmp / "processed" / "calendar_health.json").write_text(json.dumps({"sources": [
            {"slug": "a", "lastSuccessfulFetch": "2026-11-06T01:00:00Z"}, {"slug": "b", "lastSuccessfulFetch": None}]}))
        result = {c["check"]: c for c in hr.sources(NOW, self.tmp, self.tmp / "processed")}
        self.assertEqual(result["sources:science"]["status"], "warn")
        self.assertIn("esa (since 11-04", result["sources:science"]["detail"])
        self.assertIn("1 of 2 crawl sources overdue", result["sources:crawl"]["detail"])
        quality = self.tmp / "q.jsonl"
        self.assertEqual(hr.quality(NOW, quality)[0]["status"], "warn")
        quality.write_text(json.dumps({"at": NOW.isoformat(), "role": "science", "model": "m", "judge": 4.4,
                                       "regressions": ["judge 3.9 vs median 4.4"]}) + "\n")
        [row] = hr.quality(NOW, quality)
        self.assertEqual((row["status"], row["check"]), ("warn", "quality:science"))

    def test_markdown_lists_problems_first(self):
        text = hr.markdown({"date": "2026-11-06", "status": "warn", "checks": [
            hr.check("a", "ok", "fine"), hr.check("b", "warn", "slow")]})
        self.assertLess(text.index("[WARN] b"), text.index("[ok] a"))


if __name__ == "__main__":
    unittest.main()
