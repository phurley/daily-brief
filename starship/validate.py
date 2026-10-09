#!/usr/bin/env python3
"""Validate the full public contract, reference integrity and date precision."""
import json
import re
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent

def validate(record):
    schema = json.loads((ROOT / "schemas/starship.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(record)
    ids = [e["id"] for e in record["evidence"]]
    assert len(ids) == len(set(ids)), "Duplicate evidence IDs"
    for e in record["evidence"]:
        if e["sourceType"] == "community":
            assert e["verification"] != "verified" and e["claimType"] == "discussion" and not e.get("authorization"), "Community leads cannot authenticate mission claims"
        if e["verification"] == "verified" and e["claimType"] in ("target", "readiness", "delay", "scrub", "underway", "outcome"):
            assert e["missionId"] and e["publishedAt"], "Mission claims need mission association and publication date"
        if e["claimType"] == "target":
            assert e.get("target"), "Target claim needs structured target"
        if e["claimType"] == "outcome":
            assert e.get("outcome") and e["missionId"], "Outcome needs mission and explicit outcome"
        if e.get("authorization"):
            assert e["claimType"] == "regulatory" and e["sourceType"] == "regulator" and e["missionId"] and e["publishedAt"], "Authorization needs mission-specific dated regulator evidence"
        assert all(i in ids and i != e["id"] for i in e["supersedes"]), "Unknown/self supersession"
        if "target" in e:
            target = e["target"]
            ZoneInfo(target["timeZone"])
            precision = target["precision"]
            for bound in (target["lower"], target["upper"]):
                if bound is None:
                    continue
                if precision == "day":
                    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", bound)
                    date.fromisoformat(bound)
                elif precision == "month":
                    assert re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", bound)
                elif precision == "quarter":
                    assert re.fullmatch(r"\d{4}-Q[1-4]", bound)
                elif precision in ("window", "exact"):
                    assert "T" in bound and datetime.fromisoformat(bound.replace("Z", "+00:00")).tzinfo
                else:
                    raise AssertionError("Unknown precision cannot have bounds")
            assert precision == "unknown" or target["lower"], "Known precision needs lower bound"
            if target["upper"]:
                if precision in ("window", "exact"):
                    assert datetime.fromisoformat(target["lower"].replace("Z", "+00:00")) <= datetime.fromisoformat(target["upper"].replace("Z", "+00:00"))
                else:
                    assert target["lower"] <= target["upper"]
    references = record["outsideReports"] + record["forecast"]["basisIds"] + [o["evidenceId"] for o in record["outcomes"]]
    if record["officialTarget"]:
        references.append(record["officialTarget"]["evidenceId"])
    references.extend(i for c in record["conflicts"] for i in c["evidenceIds"])
    assert all(i in ids for i in references), "Dangling evidence reference"
    assert bool(record["lastVerifiedAt"]) == bool(record["expiresAt"]), "Verification/expiry must be paired"

if __name__ == "__main__":
    validate(json.load(sys.stdin))
