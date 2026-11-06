#!/usr/bin/env python3
"""Daily health report: one place to see whether the brief is healthy.

Checks, each ok / warn / fail:
  freshness  every published file is newer than its cadence allows
  jobs       collect, calendar, science and vibe finished, exit codes, durations
  spend      last-24h LLM spend vs the models.json budget, failed-attempt rates,
             how much extraction the free Gemini key covered, model fallbacks
  sources    science feeds and crawl sources failing for more than a day
  sections   story/event counts per section (zero, or less than half of the
             previous report)
  quality    the weekly quality canary ran and found no regressions

Writes processed/health/<date>.json and .md, prints the markdown, and notifies
only when something is warn or fail: HEALTH_NTFY_TOPIC (ntfy.sh, or
HEALTH_NTFY_SERVER) and/or HEALTH_WEBHOOK_URL (JSON {"text": ...}, Slack and
Discord compatible). Exit status 1 when anything failed.

    cd data-collect && .venv/bin/python health_report.py [--no-notify]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from extract import env, models, usage  # noqa: E402

PROCESSED = HERE / "processed"
OUT = PROCESSED / "health"
UTC = timezone.utc
RANK = {"ok": 0, "warn": 1, "fail": 2}

# Published file -> (hours before stale, list field to count).
PUBLISHED = {
    "events.json": (30, "events"), "news.json": (30, "stories"), "geeknews.json": (30, "stories"),
    "starship.json": (30, "evidence"), "calendar.json": (30, "items"), "vibe.json": (30, "messages"),
    "photos.json": (30, "days"), "recommendations.json": (30, "days"), "weather.json": (12, "daily"),
    "brief-manifest.json": (30, None),
}


def job_logs(root: Path = ROOT) -> dict[str, tuple]:
    """Job -> (log, start marker, done marker with exit code, longest normal hours)."""
    collect = root / "data-collect"
    return {
        "collect": (collect / "cron.log", r"\[start\] collect", r"\[done\] exit=(\d+)", 3.0),
        "calendar": (collect / "cron.log", r"\[start\] calendar refresh", None, 1.0),  # logs no [done] line
        "science": (collect / "science.log", r"\[start\] science", r"\[done\] science.*exit=(\d+)", 1.0),
        "vibe": (root / "vibe-check" / "vibe.log", r"\[start\] vibe-check", r"\[done\] exit=(\d+)", 0.5),
    }


STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
FAILED_ATTEMPT_RATE = 0.10


def check(name: str, status: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **data}


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def freshness(now: datetime, root: Path = ROOT) -> list[dict]:
    results = []
    for name, (hours, _) in PUBLISHED.items():
        path = root / name
        if not path.exists():
            results.append(check(f"fresh:{name}", "fail", "missing"))
            continue
        generated = _parse_time(json.loads(path.read_text()).get("generatedAt"))
        if not generated:
            results.append(check(f"fresh:{name}", "warn", "no generatedAt"))
            continue
        age = (now - generated).total_seconds() / 3600
        status = "ok" if age <= hours else ("warn" if age <= hours * 2 else "fail")
        results.append(check(f"fresh:{name}", status, f"{age:.0f}h old (limit {hours}h)", age_hours=round(age, 1)))
    return results


def job_runs(lines: Iterable[str], start: str, done: str) -> list[dict]:
    """Runs from a job log: start time, end time, exit code, problem lines."""
    runs, current, stamp = [], None, None
    for line in lines:
        match = STAMP.match(line)
        if match:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        if re.search(start, line):
            current = {"start": stamp, "end": None, "exit": None, "problems": [], "tracked": done is not None}
            runs.append(current)
        elif current is not None:
            finished = re.search(done, line) if done else None
            if finished:
                current.update(end=stamp, exit=int(finished.group(1)))
                current = None
            elif re.search(r"Traceback|refresh failed|\bfailed:|^error:", line):
                current["problems"].append(line.strip()[:160])
    return runs


def jobs(now: datetime, logs: dict) -> list[dict]:
    results, since = [], now - timedelta(hours=26)
    for name, (path, start, done, max_hours) in logs.items():
        if not path.exists():
            results.append(check(f"job:{name}", "warn", f"no log at {path.name}"))
            continue
        with path.open(errors="replace") as handle:
            runs = [r for r in job_runs(handle, start, done) if r["start"] and r["start"] >= since]
        if not runs:
            results.append(check(f"job:{name}", "fail", "did not run in the last 26h"))
            continue
        failed = [r for r in runs if r["exit"] not in (0, None)]
        unfinished = [r for r in runs if r["tracked"] and r["end"] is None
                      and (now - r["start"]).total_seconds() > max_hours * 2 * 3600]
        hours = [round((r["end"] - r["start"]).total_seconds() / 3600, 2) for r in runs if r["end"]]
        problems = [p for r in runs for p in r["problems"]]
        status = "fail" if failed or unfinished else ("warn" if problems or any(h > max_hours for h in hours) else "ok")
        detail = f"{len(runs)} run(s), {len(failed)} failed, longest {max(hours, default=0):.1f}h (limit {max_hours:g}h)"
        if unfinished:
            detail += f", {len(unfinished)} never finished"
        if problems:
            detail += f"; {problems[-1]}"
        results.append(check(f"job:{name}", status, detail, hours=hours, problems=problems[-5:]))
    return results


def spend(now: datetime, log: Path) -> list[dict]:
    rows = [r for r in usage.recent_rows(log, now - timedelta(hours=24))]
    budget = models.budget()
    paid = [r for r in rows if r.get("kind") == "chat"]
    total = sum(float(r.get("cost") or 0) for r in paid)
    limit = budget.get("daily_usd")
    status = "ok" if not limit or total <= limit * 0.8 else ("warn" if total <= limit else "fail")
    by = collections.Counter()
    for r in paid:
        by[(r.get("caller") or "?", r.get("model") or "?")] += float(r.get("cost") or 0)
    top = [f"{c} {m} ${v:.3f}" for (c, m), v in by.most_common(5)]
    results = [check("spend:total", status, f"${total:.2f} in 24h (budget ${limit or 0:.2f}); top: " + "; ".join(top[:3]),
                     total=round(total, 4), top=top)]
    calls, errors = collections.Counter(), collections.Counter()
    for r in rows:
        caller = (r.get("caller") or "?").split(":")[0]
        if r.get("kind") == "chat":
            calls[caller] += 1
        elif r.get("kind") == "error":
            errors[caller] += 1
    noisy = {c: (errors[c], calls[c]) for c in errors if errors[c] > FAILED_ATTEMPT_RATE * max(calls[c] + errors[c], 1)}
    results.append(check("spend:failed-attempts", "warn" if noisy else "ok",
                         "; ".join(f"{c}: {e} failed / {n} ok" for c, (e, n) in noisy.items()) or "below 10% for every caller"))
    extraction = [r for r in paid if r.get("caller") == "pipeline" and r.get("kind") == "chat"
                  and not (r.get("model") or "").startswith(("jev", "typesafe/"))]
    if extraction:
        routes = collections.Counter()
        for r in extraction:
            tier = re.search(r"\(gemini (\w+)\)", r.get("model") or "")
            routes[f"gemini {tier.group(1)}" if tier else "openrouter"] += 1
        free = routes["gemini free"] / len(extraction)
        results.append(check("spend:free-tier", "ok", f"free key served {free:.0%} of {len(extraction)} extraction calls "
                             f"({', '.join(f'{k}: {v}' for k, v in routes.most_common())})", routes=dict(routes)))
    science_primary = models.primary("science")
    science = [r for r in paid if r.get("caller") == "science"]
    if science:
        fallback = [r for r in science if not (r.get("model") or "").startswith(science_primary)]
        share = len(fallback) / len(science)
        results.append(check("spend:science-fallback", "warn" if share > 0.2 else "ok",
                             f"{len(fallback)}/{len(science)} science calls fell back from {science_primary}"))
    over = [r for r in rows if "(over budget)" in (r.get("caller") or "")]
    if over:
        results.append(check("spend:budget-mode", "warn", f"{len(over)} calls ran in over-budget mode"))
    return results


def sources(now: datetime, root: Path = ROOT, processed: Path = PROCESSED) -> list[dict]:
    results = []
    science = root / "science-health.json"
    if science.exists():
        stale = []
        for key, source in json.loads(science.read_text()).get("sources", {}).items():
            last = _parse_time(source.get("lastSuccessAt"))
            if source.get("errors") and (not last or now - last > timedelta(hours=36)):
                since = f"since {last:%m-%d}" if last else "never succeeded"
                stale.append(f"{key} ({since}: {str(source['errors'][0])[:60]})")
        results.append(check("sources:science", "warn" if stale else "ok",
                             "; ".join(stale) or "every feed fetched within 36h", failing=stale))
    calendar = processed / "calendar_health.json"
    if calendar.exists():
        data = json.loads(calendar.read_text())
        periods = crawl_periods(root)
        bad = []
        for source in data.get("sources", []):
            last = _parse_time(source.get("lastSuccessfulFetch"))
            period = periods.get(source.get("slug"), 86400)
            # Overdue: two crawl periods plus half a day of slack since the last success.
            if not last or (now - last).total_seconds() > 2 * period + 43200:
                bad.append(f"{source.get('slug') or source.get('name')} ({f'since {last:%m-%d}' if last else 'never'})")
        total = len(data.get("sources", []))
        results.append(check("sources:crawl", "warn" if len(bad) > max(3, total // 10) else "ok",
                             f"{len(bad)} of {total} crawl sources overdue (2x their crawl frequency)"
                             + (f": {', '.join(bad[:5])}" if bad else ""), failing=bad))
    return results


def crawl_periods(root: Path) -> dict[str, int]:
    """Crawl source slug -> seconds between crawls, from the source catalog."""
    try:
        from crawl_sources import DEFAULT_FREQUENCY, FREQUENCY_SECONDS, slugify
        catalog = json.loads((root / "data-collect" / "source.json").read_text())
    except (ImportError, OSError, ValueError):
        return {}
    raw = catalog.get("sources", []) if isinstance(catalog, dict) else catalog
    default = FREQUENCY_SECONDS[DEFAULT_FREQUENCY]
    return {slugify(s["name"]): FREQUENCY_SECONDS.get(s.get("frequency"), default) for s in raw if s.get("name")}


def sections(root: Path, previous: Optional[dict]) -> tuple[list[dict], dict[str, int]]:
    counts, results = {}, []
    before = (previous or {}).get("counts", {})
    for name, (_, field) in PUBLISHED.items():
        path = root / name
        if not field or not path.exists():
            continue
        count = len(json.loads(path.read_text()).get(field) or [])
        counts[name] = count
        prior = before.get(name)
        if count == 0:
            status, detail = "fail", f"{field}: 0"
        elif prior and count < prior / 2:
            status, detail = "warn", f"{field}: {count} (was {prior})"
        else:
            status, detail = "ok", f"{field}: {count}"
        results.append(check(f"section:{name}", status, detail))
    return results, counts


def quality(now: datetime, log: Path) -> list[dict]:
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    recent = [r for r in rows if (_parse_time(r.get("at")) or now - timedelta(days=99)) > now - timedelta(days=8)]
    if not recent:
        return [check("quality:canary", "warn", "no quality canary run in 8 days")]
    results = []
    for role in sorted({r["role"] for r in recent}):
        last = [r for r in recent if r["role"] == role][-1]
        judge = f"judge {last['judge']:.2f}" if last.get("judge") is not None else "no judge score"
        results.append(check(f"quality:{role}", "warn" if last.get("regressions") else "ok",
                             f"{last['model']} {judge}" + (f"; {'; '.join(last['regressions'])}" if last.get("regressions") else "")))
    return results


def markdown(report: dict) -> str:
    icon = {"ok": "ok", "warn": "WARN", "fail": "FAIL"}
    lines = [f"# Daily Brief health {report['date']}: {report['status'].upper()}", ""]
    for item in sorted(report["checks"], key=lambda c: (-RANK[c["status"]], c["check"])):
        lines.append(f"- [{icon[item['status']]}] {item['check']}: {item['detail']}")
    return "\n".join(lines) + "\n"


def notify(report: dict, text: str) -> list[str]:
    """Send to every configured channel; returns what was sent (best effort)."""
    sent = []
    problems = [c for c in report["checks"] if c["status"] != "ok"]
    summary = f"Daily Brief {report['status'].upper()}: " + "; ".join(f"{c['check']} {c['detail']}" for c in problems[:6])
    topic = os.getenv("HEALTH_NTFY_TOPIC")
    if topic:
        server = os.getenv("HEALTH_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        request = urllib.request.Request(f"{server}/{topic}", data=text.encode()[:4000], headers={
            "Title": f"Daily Brief health: {report['status']}", "Priority": "high" if report["status"] == "fail" else "default"})
        try:
            urllib.request.urlopen(request, timeout=20).close()
            sent.append("ntfy")
        except OSError as exc:
            print(f"ntfy failed: {exc}", file=sys.stderr)
    webhook = os.getenv("HEALTH_WEBHOOK_URL")
    if webhook:
        request = urllib.request.Request(webhook, data=json.dumps({"text": summary[:3500]}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=20).close()
            sent.append("webhook")
        except OSError as exc:
            print(f"webhook failed: {exc}", file=sys.stderr)
    return sent


def build(now: datetime, root: Path = ROOT, processed: Path = PROCESSED) -> dict:
    out = processed / "health"
    previous = None
    if (out / "latest.json").exists():
        previous = json.loads((out / "latest.json").read_text())
    section_checks, counts = sections(root, previous)
    checks = (freshness(now, root) + jobs(now, job_logs(root)) + spend(now, usage.log_path())
              + sources(now, root, processed)
              + section_checks + quality(now, processed / "model-quality.jsonl"))
    status = max((c["status"] for c in checks), key=RANK.get, default="ok")
    return {"date": now.date().isoformat(), "generatedAt": now.isoformat(timespec="seconds"),
            "status": status, "counts": counts, "checks": checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-notify", action="store_true")
    opts = ap.parse_args(argv)
    env.load_dotenv()
    now = datetime.now().astimezone()
    report = build(now)
    text = markdown(report)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{report['date']}.json").write_text(json.dumps(report, indent=1, default=str))
    (OUT / f"{report['date']}.md").write_text(text)
    (OUT / "latest.json").write_text(json.dumps(report, indent=1, default=str))
    print(text)
    if report["status"] != "ok" and not opts.no_notify:
        sent = notify(report, text)
        print(f"notified: {', '.join(sent) or 'nothing configured (HEALTH_NTFY_TOPIC / HEALTH_WEBHOOK_URL)'}")
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
