import io
import json
import os
import tempfile
import unittest
from unittest import mock

from extract import llm, usage


def response(content, cost=0.001):
    body = {"model": "m", "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": cost}}
    return io.BytesIO(json.dumps(body).encode())


class ResponseCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "usage.jsonl")
        os.environ["LLM_USAGE_LOG"] = self.log
        self.cache = llm.ResponseCache(os.path.join(self.dir.name, "cache.sqlite"))
        self.client = llm.ChatClient("k", model="m", retries=0, caller="test", cache=self.cache)
        self.messages = [{"role": "user", "content": "same page"}]

    def tearDown(self):
        os.environ.pop("LLM_USAGE_LOG", None)
        self.dir.cleanup()

    def test_identical_request_is_paid_once_and_logged(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: response({"ok": 1})) as post:
            self.assertEqual(self.client.complete_json(self.messages, {"type": "object"}), {"ok": 1})
            self.assertEqual(self.client.complete_json(self.messages, {"type": "object"}), {"ok": 1})
        self.assertEqual(post.call_count, 1)
        sent = json.loads(post.call_args[0][0].data)
        self.assertEqual(sent["usage"], {"include": True})
        rows = [json.loads(line) for line in open(self.log)]
        self.assertEqual([r["kind"] for r in rows], ["chat", "cache-hit"])
        self.assertEqual(rows[0]["cost"], 0.001)
        self.assertEqual(rows[0]["caller"], "test")
        summary = usage.summarize(usage.Path(self.log), days=1)
        self.assertEqual(summary[0][2], 2)
        self.assertAlmostEqual(summary[0][3], 0.001)

    def test_changed_input_or_model_misses(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: response({"ok": 1})) as post:
            self.client.complete_json(self.messages)
            self.client.complete_json([{"role": "user", "content": "changed page"}])
            llm.ChatClient("k", model="other", retries=0, cache=self.cache).complete_json(self.messages)
        self.assertEqual(post.call_count, 3)

    def test_failures_are_not_cached(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(b'{"choices": []}')):
            with self.assertRaises(llm.LLMError):
                self.client.complete_json(self.messages)
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: response({"ok": 2})) as post:
            self.assertEqual(self.client.complete_json(self.messages), {"ok": 2})
        self.assertEqual(post.call_count, 1)

    def test_cache_is_opt_in(self):
        self.assertIsNone(llm.ChatClient("k", model="m").cache)
        with mock.patch.dict(os.environ, {"LLM_CACHE": "off"}):
            self.assertIsNone(llm.ChatClient("k", model="m", cache=True).cache)


    def test_truncated_output_is_capped_and_not_retried(self):
        client = llm.ChatClient("k", model="m", retries=3, backoff=0, max_tokens=50)
        body = {"choices": [{"finish_reason": "length", "message": {"content": '{"quote": "aaaa'}}], "usage": {"cost": 0.01}}
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(json.dumps(body).encode())) as post:
            with self.assertRaises(llm.TruncatedOutput):
                client.complete_json(self.messages)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads(post.call_args[0][0].data)["max_tokens"], 50)


    def test_cached_truncation_skips_the_paid_runaway(self):
        client = llm.ChatClient("k", model="m", retries=0, max_tokens=50, cache=self.cache)
        body = {"choices": [{"finish_reason": "length", "message": {"content": '{"q": "aa'}}]}
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(json.dumps(body).encode())) as post:
            for _ in range(2):
                with self.assertRaises(llm.TruncatedOutput):
                    client.complete_json(self.messages)
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
