import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from extract import llm, usage

NOW = datetime.now(timezone.utc)


def reply(content):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
    return io.BytesIO(json.dumps(body).encode())


class RecentRowsTest(unittest.TestCase):
    def test_reads_back_to_the_cutoff_across_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "u.jsonl"
            rows = [{"at": (NOW - timedelta(hours=h)).isoformat(timespec="seconds"), "kind": "chat", "cost": 0.5, "n": h}
                    for h in (30, 20, 10, 1)]
            log.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
            recent = usage.recent_rows(log, NOW - timedelta(hours=24), block=16)  # forces many partial reads
            self.assertEqual([r["n"] for r in recent], [20, 10, 1])
            self.assertAlmostEqual(usage.spend_since(NOW - timedelta(hours=24), log), 1.5)
            self.assertEqual(usage.recent_rows(Path(tmp) / "missing", NOW), [])


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.log = Path(self.dir.name) / "usage.jsonl"
        env = mock.patch.dict(os.environ, {"LLM_USAGE_LOG": str(self.log), "GEMINI_API_KEY": "paid-key",
                                           "FREE_TIER_GEMINI_API_KEY": "free-key", "OPENROUTER_API_KEY": "or-key",
                                           "LLM_DAILY_BUDGET": "1", "LLM_HARD_DAILY_BUDGET": "2"})
        env.start(); self.addCleanup(env.stop)
        for name in ("LLM_BUDGET", "LLM_PROVIDER", "GEMINI_TIERS"):
            os.environ.pop(name, None)
        llm._spend_cache.update(checked=float("-inf"), dollars=0.0)
        self.addCleanup(llm._spend_cache.update, checked=float("-inf"), dollars=0.0)
        llm._free_blocked_until = 0.0

    def spent(self, dollars):
        self.log.write_text(json.dumps({"at": NOW.isoformat(timespec="seconds"), "kind": "chat", "cost": dollars}) + "\n")
        llm._spend_cache["checked"] = float("-inf")

    def call(self, client, answers):
        answers = iter(answers)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(answers)) as post:
            result = client.complete_json([{"role": "user", "content": "x"}])
        sent = [(c[0][0].full_url, c[0][0].get_header("Authorization"), json.loads(c[0][0].data)["model"])
                for c in post.call_args_list]
        return result, sent

    def test_under_budget_calls_go_as_configured(self):
        self.spent(0.5)
        _, sent = self.call(llm.ChatClient(model="qwen/qwen3-32b", retries=0), [reply({"ok": 1})])
        self.assertEqual(sent, [(llm.DEFAULT_ENDPOINT, "Bearer or-key", "qwen/qwen3-32b")])

    def test_past_daily_budget_paid_calls_switch_to_the_cheap_model(self):
        self.spent(1.2)
        client = llm.ChatClient(model="google/gemini-3.8-flash", retries=0, caller="science", max_tokens=99)
        result, sent = self.call(client, [reply({"ok": 1})])
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sent, [(llm.GEMINI_ENDPOINT, "Bearer free-key", "gemini-3.1-flash-lite-preview")])
        rows = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(rows[-1]["caller"], "science (over budget)")
        # The cheap model itself carries on normally.
        _, sent = self.call(llm.ChatClient(model="google/gemini-3.1-flash-lite", retries=0), [reply({"ok": 2})])
        self.assertEqual(sent[0][1], "Bearer free-key")

    def test_past_hard_budget_only_the_free_tier_runs(self):
        self.spent(2.5)
        client = llm.ChatClient(model="qwen/qwen3-32b", retries=0)
        _, sent = self.call(client, [reply({"ok": 1})])
        self.assertEqual(sent, [(llm.GEMINI_ENDPOINT, "Bearer free-key", "gemini-3.1-flash-lite-preview")])
        quota = llm.urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"RESOURCE_EXHAUSTED PerDay"))

        def refuse(*a, **k):
            raise quota
        with mock.patch("urllib.request.urlopen", side_effect=refuse):
            with self.assertRaises(llm.LLMError):  # no paid rung to fall to
                client.complete_json([{"role": "user", "content": "y"}])

    def test_past_hard_budget_without_a_free_key_fails_fast(self):
        self.spent(2.5)
        with mock.patch.dict(os.environ, {"FREE_TIER_GEMINI_API_KEY": ""}):
            client = llm.ChatClient(model="qwen/qwen3-32b", retries=0)
            with mock.patch("urllib.request.urlopen") as post, self.assertRaises(llm.BudgetExceeded):
                client.complete_json([{"role": "user", "content": "x"}])
        post.assert_not_called()

    def test_budget_off(self):
        self.spent(9)
        with mock.patch.dict(os.environ, {"LLM_BUDGET": "off"}):
            _, sent = self.call(llm.ChatClient(model="qwen/qwen3-32b", retries=0), [reply({"ok": 1})])
        self.assertEqual(sent[0][2], "qwen/qwen3-32b")


if __name__ == "__main__":
    unittest.main()
