import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from extract import jev, llm


def jev_response(answer="yes"):
    body = {"answers": {"q": answer}, "usage": {"input_tokens": 5, "output_tokens": 1}}
    return io.BytesIO(json.dumps(body).encode())


class RestartCacheTest(unittest.TestCase):
    """A stopped collect run keeps its paid Jev and LLM answers for a restart."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["LLM_USAGE_LOG"] = os.path.join(self.dir.name, "usage.jsonl")
        self.cache = llm.ResponseCache(os.path.join(self.dir.name, "cache.sqlite"))

    def tearDown(self):
        os.environ.pop("LLM_USAGE_LOG", None)
        self.dir.cleanup()

    def client(self, **kw):
        return jev.HttpJevClient(endpoint="https://jev.example/v1", model="jev-x", api_key="k",
                                 retries=0, cache=self.cache, **kw)

    def test_identical_jev_decision_is_paid_once(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: jev_response()) as post:
            first = self.client().decide_raw("state", {"q": {"type": "bool"}})
            again = self.client().decide_raw("state", {"q": {"type": "bool"}})
            self.client().decide_raw("changed state", {"q": {"type": "bool"}})
        self.assertEqual(first, again)
        self.assertEqual(post.call_count, 2)

    def test_jev_failures_are_not_cached(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("reset")):
            with self.assertRaises(jev.JevError):
                self.client().decide_raw("state", {})
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: jev_response()) as post:
            self.client().decide_raw("state", {})
        self.assertEqual(post.call_count, 1)

    def test_answers_older_than_max_age_are_asked_again(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: jev_response("old")):
            self.client(cache_max_age=3600).decide_raw("state", {})
        later = time.time() + 2 * 3600
        with mock.patch("time.time", return_value=later), \
             mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: jev_response("new")) as post:
            fresh = self.client(cache_max_age=3600).decide_raw("state", {})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(fresh["answers"]["q"], "new")

    def test_chat_cache_respects_max_age(self):
        body = {"model": "m", "choices": [{"message": {"content": "{\"ok\": 1}"}}], "usage": {"cost": 0.001}}
        chat = llm.ChatClient("k", model="m", retries=0, cache=self.cache, cache_max_age=3600)
        messages = [{"role": "user", "content": "page"}]
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(json.dumps(body).encode())) as post:
            chat.complete_json(messages)
            chat.complete_json(messages)
            self.assertEqual(post.call_count, 1)
            with mock.patch("time.time", return_value=time.time() + 7200):
                chat.complete_json(messages)
            self.assertEqual(post.call_count, 2)

    def test_collect_pipeline_passes_the_restart_cache(self):
        from extract import pipeline
        with mock.patch.dict(os.environ, {"LLM_CACHE": "on"}), \
             mock.patch.object(pipeline.llm, "ResponseCache", return_value=self.cache), \
             mock.patch.object(pipeline.jev, "make_client", return_value=jev.StubJevClient()) as make:
            pipeline.run([], workers=1, extract_limit=0, model=None)
        self.assertIs(make.call_args.kwargs["cache"], self.cache)
        self.assertEqual(make.call_args.kwargs["cache_max_age"], pipeline.RESTART_CACHE_HOURS * 3600)


if __name__ == "__main__":
    unittest.main()
