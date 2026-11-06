import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from extract import llm

MODEL = "google/gemini-3.1-flash-lite"
FLEX_COST = (1000 * 0.25 + 200 * 1.50) / 2 / 1e6


def reply(content, prompt=1000, completion=200, total=None):
    body = {"model": "gemini-3.1-flash-lite-preview",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": total if total is not None else prompt + completion}}
    return io.BytesIO(json.dumps(body).encode())


def http_error(code, body=b"busy"):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


PER_MINUTE = b'{"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}}'
PER_DAY = b'{"error": {"status": "RESOURCE_EXHAUSTED", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'


class GeminiLadderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "usage.jsonl")
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "paid-key", "FREE_TIER_GEMINI_API_KEY": "free-key",
                                                "OPENROUTER_API_KEY": "or-key", "LLM_USAGE_LOG": self.log})
        self.env.start()
        for name in ("LLM_PROVIDER", "GEMINI_TIERS"):
            os.environ.pop(name, None)
        llm._free_blocked_until = 0.0
        self.addCleanup(setattr, llm, "_free_blocked_until", 0.0)
        self.messages = [{"role": "user", "content": "page"}]

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def rows(self):
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def run_calls(self, client, answers, calls=1):
        answers = iter(answers)

        def urlopen(*a, **k):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch("urllib.request.urlopen", side_effect=urlopen) as post:
            results = [client.complete_json(self.messages, {"type": "object"}) for _ in range(calls)]
        sent = [(c[0][0].get_header("Authorization"), json.loads(c[0][0].data).get("service_tier"))
                for c in post.call_args_list]
        return results, sent, post

    def test_free_key_first_at_no_cost(self):
        client = llm.ChatClient(model=MODEL, retries=0, caller="t")
        results, sent, post = self.run_calls(client, [reply({"ok": 1})])
        self.assertEqual(results, [{"ok": 1}])
        self.assertEqual(sent, [("Bearer free-key", None)])
        request = post.call_args[0][0]
        self.assertEqual(request.full_url, llm.GEMINI_ENDPOINT)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gemini-3.1-flash-lite-preview")
        self.assertNotIn("usage", body)  # OpenRouter-only field
        [row] = self.rows()
        self.assertEqual((row["model"], row["cost"]), (MODEL + " (gemini free)", 0.0))

    def test_rate_limited_free_drops_to_paid_flex_then_cools_down(self):
        client = llm.ChatClient(model=MODEL, retries=0)
        results, sent, _ = self.run_calls(client, [http_error(429, PER_MINUTE), reply({"ok": 1}), reply({"ok": 2})], calls=2)
        self.assertEqual(results, [{"ok": 1}, {"ok": 2}])
        # The second call skips the free rung during the cooldown.
        self.assertEqual(sent, [("Bearer free-key", None), ("Bearer paid-key", "flex"), ("Bearer paid-key", "flex")])
        self.assertEqual([r["kind"] for r in self.rows()], ["error", "chat", "chat"])
        self.assertAlmostEqual(self.rows()[1]["cost"], FLEX_COST)
        llm._free_blocked_until = 0.0  # cooldown over
        _, sent, _ = self.run_calls(client, [reply({"ok": 3})])
        self.assertEqual(sent, [("Bearer free-key", None)])

    def test_daily_free_quota_stays_off_for_the_run(self):
        client = llm.ChatClient(model=MODEL, retries=0)
        self.run_calls(client, [http_error(429, PER_DAY), reply({"ok": 1})])
        self.assertEqual(llm._free_blocked_until, float("inf"))
        _, sent, _ = self.run_calls(client, [reply({"ok": 2})])
        self.assertEqual(sent, [("Bearer paid-key", "flex")])

    def test_busy_flex_finishes_on_the_standard_tier(self):
        with mock.patch.dict(os.environ, {"FREE_TIER_GEMINI_API_KEY": ""}):
            client = llm.ChatClient(model=MODEL, retries=0, backoff=0)
        self.assertEqual(client.tiers, ["flex", "default"])
        _, sent, _ = self.run_calls(client, [http_error(503), reply({"ok": 2})])
        self.assertEqual(sent, [("Bearer paid-key", "flex"), ("Bearer paid-key", "default")])
        self.assertAlmostEqual(self.rows()[1]["cost"], FLEX_COST * 2)

    def test_bottom_rung_still_retries_and_then_fails(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIERS": "default"}):
            client = llm.ChatClient(model=MODEL, retries=1, backoff=0)
        with self.assertRaises(llm.LLMError):
            self.run_calls(client, [http_error(503), http_error(503), reply({"never": 1})])

    def test_thinking_tokens_are_billed_as_output(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIERS": "flex"}):
            client = llm.ChatClient(model=MODEL, retries=0)
        self.run_calls(client, [reply({"ok": 1}, 1000, 100, 1600)])
        self.assertAlmostEqual(self.rows()[0]["cost"], (1000 * 0.25 + 600 * 1.50) / 2 / 1e6)

    def test_flash_38_uses_the_paid_standard_tier_with_low_thinking(self):
        client = llm.ChatClient(model="google/gemini-3.8-flash", retries=0, reasoning_effort="low")
        self.assertEqual(client.tiers, ["default"])  # never the free key or Flex
        _, sent, post = self.run_calls(client, [reply({"ok": 1})])
        self.assertEqual(sent, [("Bearer paid-key", "default")])
        body = json.loads(post.call_args[0][0].data)
        self.assertEqual((body["model"], body["reasoning_effort"]), ("gemini-3.8-flash", "low"))
        price_in, price_out = llm.models.price("google/gemini-3.8-flash")  # dated: rises 2027-01-01
        self.assertAlmostEqual(self.rows()[0]["cost"], (1000 * price_in + 200 * price_out) / 1e6)

    def test_reasoning_effort_uses_openrouter_syntax_there(self):
        client = llm.ChatClient("k", model="google/gemini-2.5-flash", retries=0, reasoning_effort="low")
        _, _, post = self.run_calls(client, [reply({"ok": 1})])
        body = json.loads(post.call_args[0][0].data)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning_effort", body)

    def test_openrouter_for_unlisted_models_and_on_request(self):
        # Google 404s gemini-2.5-flash for new projects, so it stays on OpenRouter.
        self.assertEqual(llm.ChatClient(model="google/gemini-2.5-flash").provider, "openrouter")
        self.assertEqual(llm.ChatClient("explicit", model=MODEL).provider, "openrouter")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}):
            client = llm.ChatClient(model=MODEL)
        self.assertEqual((client.provider, client.endpoint), ("openrouter", llm.DEFAULT_ENDPOINT))
        with mock.patch.dict(os.environ, {"FREE_TIER_GEMINI_API_KEY": "", "GEMINI_API_KEY": ""}):
            self.assertEqual(llm.ChatClient(model=MODEL).provider, "openrouter")


if __name__ == "__main__":
    unittest.main()
