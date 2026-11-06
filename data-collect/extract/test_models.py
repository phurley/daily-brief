import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from extract import models

REGISTRY = {
    "roles": {
        "science": {"env": ["SCI_MODEL", "SCI_FALLBACK"], "max_tokens": 2500, "chain": [
            {"model": "a/first", "reasoning_effort": "low"}, {"model": "b/second"}, {"model": "c/last"}]},
        "solo": {"env": ["SOLO_MODEL"], "chain": [{"model": "a/first"}]},
    },
    "models": {
        "a/first": {"gemini": "first-preview", "tiers": "default",
                    "prices": [{"from": "2026-01-01", "input": 1, "output": 2},
                               {"from": "2027-01-01", "input": 3, "output": 4}]},
    },
}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "models.json"; path.write_text(json.dumps(REGISTRY))
        models.registry.cache_clear()
        patcher = mock.patch.object(models, "registry", lambda: json.loads(path.read_text()))
        patcher.start(); self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}); env.start(); self.addCleanup(env.stop)
        for name in ("SCI_MODEL", "SCI_FALLBACK", "SOLO_MODEL"):
            os.environ.pop(name, None)

    def test_chain_defaults_and_settings(self):
        self.assertEqual(models.chain("science")[0], {"model": "a/first", "reasoning_effort": "low"})
        self.assertEqual(models.primary("solo"), "a/first")
        self.assertEqual(models.max_tokens("science"), 2500)
        self.assertIsNone(models.max_tokens("solo"))

    def test_env_overrides_replace_a_position_and_drop_its_settings(self):
        os.environ["SCI_FALLBACK"] = "x/override"
        self.assertEqual([e["model"] for e in models.chain("science")], ["a/first", "x/override", "c/last"])
        os.environ["SCI_MODEL"] = "c/last"  # duplicates collapse, first position wins
        self.assertEqual(models.chain("science"), [{"model": "c/last"}, {"model": "x/override"}])
        os.environ["SOLO_MODEL"] = "a/first"  # same model keeps its settings
        self.assertEqual(models.chain("solo"), [{"model": "a/first"}])

    def test_gemini_metadata_and_dated_prices(self):
        self.assertEqual(models.gemini_name("a/first"), "first-preview")
        self.assertIsNone(models.gemini_name("b/second"))
        self.assertEqual(models.gemini_tiers("a/first"), "default")
        self.assertEqual(models.gemini_tiers("b/second"), "free,flex,default")
        self.assertEqual(models.price("a/first", date(2026, 12, 31)), (1, 2))
        self.assertEqual(models.price("a/first", date(2027, 1, 1)), (3, 4))
        self.assertIsNone(models.price("b/second"))


class RealRegistryTest(unittest.TestCase):
    """models.json at the repo root is well-formed and every role resolves."""
    def test_every_role_resolves(self):
        models.registry.cache_clear()
        data = models.registry()
        for role, spec in data["roles"].items():
            self.assertTrue(spec["chain"], role)
            for entry in spec["chain"]:
                self.assertIn("model", entry)
            self.assertIsInstance(spec.get("env", []), list)
        for name, info in data["models"].items():
            prices = [p["from"] for p in info.get("prices", [])]
            self.assertEqual(prices, sorted(prices), name)
            if "tiers" in info:
                self.assertLessEqual(set(info["tiers"].split(",")), {"free", "flex", "default"}, name)


if __name__ == "__main__":
    unittest.main()
