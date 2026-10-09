"""Browser regression smoke test, using local fixtures rather than live APIs.
Run with the crawler Python: python starship/test_browser.py
"""
import functools
import http.server
import json
from datetime import datetime, timezone, timedelta
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT)))
threading.Thread(target=server.serve_forever, daemon=True).start()
# The immutable first snapshot is a reproducible passed-target fixture.
fixture = json.loads((ROOT / "starship/history/2026-10-09T18-53-41.415Z.json").read_text())
at = datetime.now(timezone.utc).isoformat()
fixture["sourceHealth"].append({"id": "reddit-spacex", "url": "https://www.reddit.com/r/spacex/", "state": "ok", "lastAttemptAt": at, "lastSuccessAt": at, "parserVersion": "fixture", "contentHash": None, "detail": "Fixture community feed"})
fixture["evidence"].append({"id": "community-fixture", "sourceId": "reddit-spacex", "sourceUrl": "https://www.reddit.com/r/spacex/comments/fixture/outlook/", "sourceType": "community", "publishedAt": at, "observedAt": at, "missionId": None, "claimType": "discussion", "excerpt": "Starship next launch could be months away — a community guess.", "verification": "unverified", "claimConfidence": "reported", "originId": "fixture", "supersedes": [], "parserVersion": "fixture", "communityKind": "speculation", "linkedSourceUrls": ["https://www.spacex.com/launches/"]})
fixture["communityEstimate"] = {"state": "estimated", "missionId": "starship-flight-15", "summary": "Best guess: November or later; timing remains tentative.", "rationale": "The development thread reports a tentative NET target.", "windowStart": None, "windowEnd": None, "caveats": ["The target can slip."], "sources": [{"id": "fixture", "url": "https://www.reddit.com/r/spacex/comments/fixture/outlook/", "title": "Development thread", "excerpt": "Flight 15 has a tentative target; this is community speculation.", "observedAt": at}], "generatedAt": at, "expiresAt": (datetime.now(timezone.utc)+timedelta(hours=24)).isoformat(), "model": "fixture", "version": "fixture"}
launch = {"name": "Test window launch", "slug": "fixture", "win_open": "2026-10-10T15:30:00Z"}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in (1280, 390):
                page = browser.new_page(service_workers="block", viewport={"width": width, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/brief-manifest.json*", lambda route: route.fulfill(status=404, body="Compatibility-view fixture"))
                page.route("**/starship.json*", lambda route: route.fulfill(json=fixture))
                page.route("**/json/launches/next/5", lambda route: route.fulfill(json={"result": [launch]}))
                page.route("https://icanhazdadjoke.com/**", lambda route: route.fulfill(json={"joke": "A fixture joke."}))
                page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="networkidle")
                card = page.locator("#starship-estimate-details")
                card.wait_for()
                card.locator("summary").click()
                assert "RocketLaunch.Live" not in card.inner_text()
                assert "Best guess: November or later" in card.inner_text()
                assert "Not an official launch announcement" in card.inner_text()
                assert "Window opens at" in page.locator("#rocket-launches").inner_text()
                assert "/r/spacex/" in card.locator("a").first.get_attribute("href")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
                card.screenshot(path=f"/tmp/starship-{width}.png")
                # Feed success must not change canonical freshness; fallback is
                # clearly labeled, and >6h fallback is removed altogether.
                for age_hours in (2, 7):
                    page.evaluate("([launch, age]) => localStorage.setItem('daily-brief-rocket-launches:v1', JSON.stringify({fetchedAt: Date.now() - age * 3600000, launches: [launch]}))", [launch, age_hours])
                    page.unroute("**/json/launches/next/5")
                    page.route("**/json/launches/next/5", lambda route: route.abort())
                    page.reload(wait_until="networkidle")
                    if age_hours == 2:
                        assert "Cached feed" in page.locator("#rocket-launches").inner_text()
                    else:
                        assert "best guess" in page.locator("#rocket-launches").inner_text().lower()
                        assert "Test window launch" not in page.locator("#rocket-launches").inner_text()
                        assert "Data by RocketLaunch.Live" not in page.locator("#rocket-launches").inner_text()
                fixture["communityEstimate"]["expiresAt"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                page.reload(wait_until="networkidle")
                assert card.count() == 0
                assert "best guess date pending" in page.locator("#rocket-launches").inner_text()
                fixture["communityEstimate"]["expiresAt"] = (datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
                assert not errors, errors
                page.close()
            print("Browser checks passed: desktop/mobile, attribution, precision, stale Starship and bounded feed fallback")
        finally:
            browser.close()
finally:
    server.shutdown()
    server.server_close()
