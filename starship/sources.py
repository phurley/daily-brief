#!/usr/bin/env python3
"""Conservative, dated primary-source adapters. Emit claims, never forecasts."""
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

PARSER_VERSION = "primary-1.0.0"
MONTHS = "January February March April May June July August September October November December".split()
MONTH_RE = r"(?:" + "|".join(m + "|" + m[:3] for m in MONTHS) + r")"
DATE_RE = re.compile(r"\b(" + MONTH_RE + r")\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.I)


def iso_date(text):
    match = DATE_RE.search(text)
    if not match:
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        if not match:
            return None
        value = match[1]
    else:
        month = next(i for i, m in enumerate(MONTHS, 1) if m.lower().startswith(match[1].lower()))
        value = f"{match[3]}-{month:02d}-{int(match[2]):02d}"
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def mission_id(text):
    number = re.search(r"\b(?:flight(?:\s+test)?|test\s+flight)\s*(\d+)\b", text, re.I)
    if number:
        return f"starship-flight-{int(number[1])}"
    words = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split()
    for index, word in enumerate(words, 1):
        if re.search(rf"\b{word}\s+(?:test\s+)?flight\b", text, re.I):
            return f"starship-flight-{index}"
    return None


def page_date(soup):
    # Only explicit publication metadata, never copyright/footer or HTTP Date.
    for name in ("article:modified_time", "article:published_time"):
        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            value = tag["content"]
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo:
                    return parsed.isoformat()
            except ValueError:
                pass
    time = soup.find("time", attrs={"datetime": True})
    if time:
        try:
            value = datetime.fromisoformat(time["datetime"].replace("Z", "+00:00"))
            if value.tzinfo:
                return value.isoformat()
        except ValueError:
            pass
    return None


def claim(source, text, at, published, mission, kind, **extra):
    digest = hashlib.sha256(f"{source['url']}|{published}|{text}".encode()).hexdigest()[:20]
    return dict(id=f"{source['id']}-{digest}", sourceId=source["id"], sourceUrl=source["url"],
                sourceType=source["type"], publishedAt=published, observedAt=at,
                missionId=mission, claimType=kind, excerpt=text[:900], verification="verified",
                claimConfidence="confirmed", originId=f"{source['url']}#{published}",
                supersedes=[], parserVersion=PARSER_VERSION, **extra)


def parse_source(source, html, at, max_age_days=45):
    soup = BeautifulSoup(html, "html.parser")
    published = page_date(soup)
    for element in soup(["script", "style", "footer", "nav", "noscript", "header"]):
        element.decompose()
    main = soup.find("main") or soup.find("article") or soup
    text = main.get_text(" ", strip=True)
    claims = []
    dated = []
    now = datetime.fromisoformat(at.replace("Z", "+00:00"))
    def fresh(value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return 0 <= (now - dt).total_seconds() <= max_age_days * 86400
    if source["id"] == "cameron":
        for row in main.find_all("tr"):
            line = row.get_text(" ", strip=True)
            date = iso_date(line)
            if not date:
                continue
            dated.append(date)
            # A closure date is not its publication date. Unknown publication
            # stays null; these notices never advance a mission forecast.
            if abs((now.date() - datetime.strptime(date, "%Y-%m-%d").date()).days) <= 14:
                claims.append(claim(source, line, at, None, None, "notice", noticeDate=date))
    elif source["id"] == "faa":
        # Preserve dated regulatory text as background. An environmental review
        # page is not mission-specific authorization, even if it says "complete".
        for paragraph in main.find_all("p"):
            line = paragraph.get_text(" ", strip=True)
            date = iso_date(line)
            if date:
                dated.append(date)
            if date and any(word in line.lower() for word in ("starship", "faa", "assessment", "comment period")):
                claims.append(claim(source, line, at, None, mission_id(line), "regulatory", authorization=False, noticeDate=date))
    elif source["id"] == "spacex":
        # A mission detail page must associate a flight with a dated statement.
        # Schedule dates do not masquerade as publication timestamps.
        mission = mission_id(text) if "starship" in text.lower() else None
        if published:
            dated.append(published[:10])
        for paragraph in main.find_all("p"):
            line = paragraph.get_text(" ", strip=True)
            if not mission or not published or not fresh(published):
                continue
            if re.search(r"\b(?:not|no longer|previously|had been)\s+target", line, re.I):
                continue
            date = iso_date(line)
            if date and re.search(r"\b(?:is targeting|are targeting|targeted for|scheduled for|no earlier than)\b", line, re.I):
                claims.append(claim(source, line, at, published, mission, "target", target=dict(
                    precision="day", label=line[:400], lower=date, upper=None,
                    net=bool(re.search(r"no earlier than|\bNET\b", line, re.I)), timeZone="America/Chicago")))
            # Free prose outcomes, scrubs, times and milestones require review.
            # They are never inferred from "complete" or a missing target.
    state = "ok" if claims else "stale" if dated and max(dated) < now.date().isoformat() else "no-dated-evidence"
    if dated and not any(abs((now.date() - datetime.strptime(d[:10], "%Y-%m-%d").date()).days) <= max_age_days for d in dated):
        state = "stale"
    return claims, {"id": source["id"], "url": source["url"], "state": state, "lastAttemptAt": at,
                    "lastSuccessAt": at if state == "ok" else None, "parserVersion": PARSER_VERSION,
                    "contentHash": hashlib.sha256(html.encode()).hexdigest(),
                    "detail": f"{len(claims)} dated/background claims; {len(text)} extractable characters. Publication dates required for mission targets."}


def fetch_html(url):
    req = Request(url, headers={"User-Agent": "DailyBrief-Starship/1.0 (+https://phurley.github.io/daily-brief/)"})
    with urlopen(req, timeout=25) as response:
        return response.read(3_000_001).decode("utf-8", errors="replace")


async def spacex_pages(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=45000)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            links = []
            for anchor in soup.find_all("a", href=True):
                link = urljoin(url, anchor["href"])
                label = anchor.get_text(" ", strip=True) + " " + link
                if urlparse(link).hostname in ("www.spacex.com", "spacex.com") and re.search(r"starship|flight-test", label, re.I) and link not in links and link != url:
                    links.append(link)
            pages = [(url, html)]
            for link in links[:3]:
                await page.goto(link, wait_until="networkidle", timeout=45000)
                pages.append((link, await page.content()))
            return pages
        finally:
            await browser.close()


def collect(config, at):
    evidence, health = [], []
    for source in config["sources"]:
        try:
            if source["id"] == "reddit-spacex":
                from reddit import collect_reddit
                claims, result = collect_reddit(source)
                evidence.extend(claims)
                health.append(result)
                continue
            pages = asyncio.run(spacex_pages(source["url"])) if source["id"] == "spacex" and config.get("renderSpaceX") else [(source["url"], fetch_html(source["url"]))]
            parsed = []
            for url, html in pages:
                observed_at = datetime.now(timezone.utc).isoformat()
                claims, result = parse_source({**source, "url": url}, html, observed_at, config["maxSourceAgeDays"])
                evidence.extend(claims)
                parsed.append(result)
            best = next((r for r in parsed if r["state"] == "ok"), parsed[0])
            health.append({**best, "url": source["url"], "detail": best["detail"] + f" Checked {len(pages)} page(s)."})
        except Exception as error:
            health.append(dict(id=source["id"], url=source["url"], state="error", lastAttemptAt=at,
                               lastSuccessAt=None, parserVersion=PARSER_VERSION, contentHash=None, detail=str(error)[:400]))
    return dict(evidence=evidence, sourceHealth=health)


if __name__ == "__main__":
    config = json.load(open(sys.argv[1]))
    print(json.dumps(collect(config, sys.argv[2])))
