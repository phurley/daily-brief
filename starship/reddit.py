"""Read public r/SpaceX Atom feeds as unverified community leads, not facts."""
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from urllib.error import HTTPError
import time
from bs4 import BeautifulSoup
from sources import fetch_html, mission_id

PARSER_VERSION = "reddit-1.1.0"
NS = {"a": "http://www.w3.org/2005/Atom"}
RELEVANT = re.compile(r"\bstarship\b|\bstarbase\b|\bsuper\s*heavy\b|\b(?:ship|booster)\s+\d+", re.I)
FUTURE = re.compile(r"\b(?:next|upcoming|future|target(?:ing|ed)?|expect(?:ed)?|estimate|predict|guess|NET|no earlier than|could|might|likely|development)\b", re.I)
REDDIT_HOSTS = {"www.reddit.com", "reddit.com", "old.reddit.com"}

def clean_url(value):
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")])
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))
    except ValueError:
        return None


def parse_feed(xml, observed_at, *, max_age_days=30, thread_url=None):
    root = ET.fromstring(xml)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("Reddit did not return an Atom feed")
    now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    evidence, threads = [], []
    for entry in root.findall("a:entry", NS):
        title = entry.findtext("a:title", "", NS)
        link = entry.find("a:link", NS)
        permalink = clean_url(link.get("href", "")) if link is not None else None
        if not permalink or urlparse(permalink).hostname not in REDDIT_HOSTS or not urlparse(permalink).path.lower().startswith("/r/spacex/comments/"):
            continue
        if not thread_url and re.search(r"Starship Development Thread", title, re.I):
            threads.append(permalink)
        published = entry.findtext("a:published", "", NS)
        try:
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if not published_dt.tzinfo or not 0 <= (now - published_dt).total_seconds() <= max_age_days * 86400:
                continue
        except ValueError:
            continue
        content = BeautifulSoup(entry.findtext("a:content", "", NS), "html.parser")
        for el in content(["script", "style"]):
            el.decompose()
        text = content.get_text(" ", strip=True)
        text = re.split(r"\bsubmitted by\b", text, flags=re.I)[0].strip()
        if text in ("[deleted]", "[removed]") or title in ("[deleted]", "[removed]"):
            continue
        combined = f"{title} — {text}" if text else title
        if not RELEVANT.search(title) and not thread_url:
            continue
        # Discussion comments are useful when they actually concern outlook or
        # readiness; skip votes, applause and unrelated megathread chatter.
        if thread_url and not (FUTURE.search(text) or re.search(r"static fire|readiness|license|closure|rollout|stack", text, re.I)):
            continue
        links = []
        for anchor in content.find_all("a", href=True):
            url = clean_url(anchor["href"])
            if not url:
                continue
            host = urlparse(url).hostname
            if host in REDDIT_HOSTS or host.endswith(".redd.it") or host == "redd.it":
                continue
            if url not in links:
                links.append(url)
        links = links[:5]
        # Only record the post/comment as a lead. Even flair saying "Official"
        # does not authenticate its linked material; verification happens later.
        kind = "speculation" if FUTURE.search(combined) else "linked-report" if links else "discussion"
        excerpt = combined[:900]
        digest = hashlib.sha256(f"{permalink}|{excerpt}|{links}".encode()).hexdigest()[:20]
        evidence.append(dict(id=f"reddit-{digest}", sourceId="reddit-spacex", sourceUrl=permalink,
                             sourceType="community", publishedAt=published_dt.isoformat(), observedAt=observed_at,
                             missionId=mission_id(combined), claimType="discussion", excerpt=excerpt,
                             verification="unverified", claimConfidence="reported",
                             originId=urlparse(links[0])._replace(fragment="").geturl() if links else permalink, supersedes=[], parserVersion=PARSER_VERSION,
                             communityKind=kind, linkedSourceUrls=links))
    return evidence, threads


def collect_reddit(source):
    feeds = ["https://www.reddit.com/r/spacex/new/.rss?limit=50", "https://www.reddit.com/r/spacex/hot/.rss?limit=25"]
    evidence, threads, failures, hashes = [], [], [], []
    def read(url, thread_url=None):
        xml = fetch_html(url)
        at = datetime.now(timezone.utc).isoformat()
        claims, discovered = parse_feed(xml, at, thread_url=thread_url)
        return claims, discovered, hashlib.sha256(xml.encode()).hexdigest()
    # Bounded requests: two listings and at most two development-thread feeds.
    # Public-feed refusal is reported, not bypassed with another identity/host.
    requested = 0
    rate_limited = False
    for url in feeds[:]:
        if requested:
            time.sleep(2)
        requested += 1
        try:
            claims, discovered, digest = read(url)
            evidence.extend(claims); threads.extend(discovered); hashes.append(digest)
        except Exception as error:
            failures.append(str(error)[:120])
            if isinstance(error, HTTPError) and error.code == 429:
                rate_limited = True
                break  # Do not make more requests after Reddit asks us to stop.
    if not rate_limited:
        for thread in list(dict.fromkeys(threads))[:2]:
            time.sleep(2)
            requested += 1
            try:
                claims, _, digest = read(thread.rstrip("/") + "/.rss?sort=new&limit=50", thread)
                evidence.extend(claims); hashes.append(digest)
            except Exception as error:
                failures.append(str(error)[:120])
                if isinstance(error, HTTPError) and error.code == 429:
                    break
    unique = {e["id"]: e for e in evidence}
    at = datetime.now(timezone.utc).isoformat()
    # Partial fetches are explicitly degraded, even when some leads are usable.
    state = "error" if failures else "ok" if unique else "no-dated-evidence"
    health = dict(id=source["id"], url=source["url"], state=state, lastAttemptAt=at,
                  lastSuccessAt=at if state == "ok" else None, parserVersion=PARSER_VERSION,
                  contentHash=hashlib.sha256("".join(hashes).encode()).hexdigest() if hashes else None,
                  detail=f"{len(unique)} unverified community leads from {requested} requested feeds; post age limited to 30 days. " + ("Fetch failures: " + "; ".join(failures) if failures else "No targets, authorizations or outcomes verified by Reddit."))
    return list(unique.values()), health
