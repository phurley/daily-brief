import json
import unittest
from pathlib import Path
from validate import validate

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.record = json.loads((Path(__file__).resolve().parent.parent / "starship.json").read_text())
        self.claim = next(e for e in self.record["evidence"] if e["claimType"] == "target")

    def test_published_document(self):
        validate(self.record)

    def test_day_cannot_be_timestamp(self):
        self.claim["target"]["lower"] = "2026-09-18T00:00:00Z"
        with self.assertRaises(AssertionError):
            validate(self.record)

    def test_exact_time_needs_timezone(self):
        self.claim["target"].update(precision="exact", lower="2026-09-18T09:00:00")
        with self.assertRaises(AssertionError):
            validate(self.record)

    def test_verified_target_needs_mission_and_publication(self):
        self.claim["verification"] = "verified"
        self.claim["missionId"] = None
        with self.assertRaises(AssertionError):
            validate(self.record)

    def test_generic_regulatory_update_cannot_be_authorization(self):
        self.claim.update(claimType="regulatory", sourceType="regulator", authorization=True, missionId=None)
        with self.assertRaises(AssertionError):
            validate(self.record)

    def test_dangling_reference_is_rejected(self):
        self.record["forecast"]["basisIds"] = ["missing-evidence"]
        with self.assertRaises(AssertionError):
            validate(self.record)

if __name__ == "__main__": unittest.main()
