import json
import tempfile
import unittest
from pathlib import Path

import quality_canary as qc


def row(model="m", judge=4.0, valid=1.0, role="extract", sample="s1"):
    return {"role": role, "model": model, "sample": sample, "judge": judge, "valid_rate": valid}


class RegressionTest(unittest.TestCase):
    def test_first_run_has_no_baseline(self):
        self.assertEqual(qc.regressions(row(), []), [])

    def test_drops_beyond_threshold_are_flagged(self):
        earlier = [row(judge=4.2), row(judge=4.0), row(judge=4.1)]
        self.assertEqual(qc.regressions(row(judge=3.9), earlier), [])
        [judge_drop] = qc.regressions(row(judge=3.7), earlier)
        self.assertIn("judge 3.70 vs median 4.10", judge_drop)
        [rate_drop] = qc.regressions(row(valid=0.8), [row(valid=1.0)])
        self.assertIn("valid_rate 80% vs median 100%", rate_drop)

    def test_science_uses_the_accept_rate_and_ignores_other_models(self):
        earlier = [{"role": "science", "model": "old", "judge": 5.0, "accepted_rate": 0.9}]
        self.assertEqual(qc.regressions({"role": "science", "model": "new", "judge": 3.0, "accepted_rate": 0.1}, earlier), [])
        earlier.append({"role": "science", "model": "new", "judge": 4.5, "accepted_rate": 0.6})
        self.assertEqual(len(qc.regressions({"role": "science", "model": "new", "judge": 4.0, "accepted_rate": 0.4}, earlier)), 2)

    def test_history_filters_by_role_and_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "q.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in [row(), row(sample="s2"), row(role="science")]) + "\n")
            self.assertEqual(len(qc.history("extract", "s1", log)), 1)
            self.assertEqual(qc.history("extract", "s1", Path(tmp) / "missing.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
