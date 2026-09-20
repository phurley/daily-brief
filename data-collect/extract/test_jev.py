#!/usr/bin/env python3
"""Offline tests for the Jev interface (no API key or network needed).

Run: ``python3 -m unittest extract.test_jev``
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import jev

CANNED = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "is_content": {"type": "noul", "noul": 0.93},
        "kind": {
            "type": "choice",
            "choice": "event",
            "probabilities": {"event": 0.9, "news": 0.1, "nav": 0.0},
            "confidence": 0.9,
        },
        "dated": {"type": "noul", "noul": 0.97},
        "timeframe": {
            "type": "choice",
            "choice": "future",
            "probabilities": {"future": 0.9, "past": 0.1},
            "confidence": 0.8,
        },
        "relevance": {
            "type": "score",
            "score": 3.4,
            "legend": {"0": "none", "1": "low", "2": "mid", "3": "high", "4": "top"},
            "probabilities": {"3": 0.7, "4": 0.2, "2": 0.1},
            "confidence": 0.6,
        },
    },
    "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.00001},
}


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    failures_remaining = 0

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body),
        })
        if type(self).failures_remaining > 0:
            type(self).failures_remaining -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"rate limited"}}')
            return
        payload = json.dumps(CANNED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


class JevInterfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/api/alpha/decisions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.requests = []
        _Handler.failures_remaining = 0

    def test_build_questions_shape(self):
        q = jev.build_questions()
        self.assertEqual(q["kind"]["type"], "choice")
        self.assertEqual(set(q["kind"]["criteria"]), set(jev.KINDS))
        self.assertEqual(q["relevance"]["type"], "score")
        self.assertEqual(len(q["relevance"]["criteria"]), 5)
        self.assertEqual(q["is_content"]["type"], "noul")
        self.assertEqual(set(q["is_content"]["criteria"]), {"true", "false"})

    def test_parse_answers(self):
        d = jev.parse_answers(jev.CANDIDATE_GATE, CANNED["answers"])
        self.assertTrue(d["is_content"]["value"])
        self.assertAlmostEqual(d["is_content"]["confidence"], 0.93, places=2)
        self.assertEqual(d["kind"]["value"], "event")
        self.assertEqual(d["relevance"]["value"], 3)
        self.assertAlmostEqual(d["relevance"]["confidence"], 0.6, places=2)

    def test_http_client_roundtrip(self):
        client = jev.HttpJevClient(
            endpoint=self.endpoint, model="typesafe/jev-1.13", api_key="test-key"
        )
        raw = client.decide(jev.CANDIDATE_GATE, "state text")
        self.assertEqual(raw["kind"]["value"], "event")
        req = _Handler.requests[-1]
        self.assertEqual(req["path"], "/api/alpha/decisions")
        self.assertEqual(req["auth"], "Bearer test-key")
        self.assertEqual(req["body"]["model"], "typesafe/jev-1.13")
        self.assertIn("questions", req["body"])
        self.assertEqual(req["body"]["state"], "state text")

    def test_http_client_retries_on_429(self):
        _Handler.failures_remaining = 1
        client = jev.HttpJevClient(
            endpoint=self.endpoint, model="m", api_key="k", retries=2, backoff=0.0
        )
        raw = client.decide(jev.CANDIDATE_GATE, "s")
        self.assertEqual(raw["kind"]["value"], "event")
        self.assertEqual(len(_Handler.requests), 2)

    def test_http_client_fatal_error(self):
        client = jev.HttpJevClient(
            endpoint=self.endpoint, model="m", api_key="k", retries=0
        )
        # 200 path is fine; missing key is fatal.
        with self.assertRaises(jev.JevError):
            jev.HttpJevClient(endpoint=self.endpoint, model="m", api_key="")

    def test_stub_and_gate(self):
        client = jev.StubJevClient()
        record = {
            "chunk_id": "x",
            "url_class": "event",
            "signals": {"has_date": True, "event_terms": ["concert"], "news_terms": []},
            "text": "A concert on Sept 30.",
        }
        result = jev.gate_chunks([record], client)
        self.assertEqual(len(result.accepted), 1)

    def test_record_text_handles_feed_records(self):
        # Feed items have no `text` key; the state must not be empty.
        feed = {
            "item_id": "f1",
            "title": "Auditions open",
            "summary": "Applications close Sept 25, 2026.",
            "venue": "The Park",
            "author": "John Kreger",
            "categories": ["Main", "Events"],
        }
        text = jev.record_text(feed)
        self.assertIn("Auditions open", text)
        self.assertIn("Applications close", text)
        self.assertIn("John Kreger", text)
        state = jev.chunk_state(feed)
        self.assertTrue(state.split("CHUNK:", 1)[1].strip())
        # chunk records still use their text unchanged
        self.assertEqual(jev.record_text({"text": "body"}), "body")


if __name__ == "__main__":
    unittest.main()