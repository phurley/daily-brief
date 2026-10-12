"""Bounded, read-only adapters for verified public calendars.

Adapters return individual schema.org occurrences into the existing funnel.
Coverage means queried dates/pagination, not merely dates of discovered events.
No model is used to infer missing dates, showtimes, prices, or cancellations.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urlsplit, parse_qsl, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from extract import evidence

DETROIT = ZoneInfo("America/Detroit")
MONTHS = "January February March April May June July August September October November December".split()
MONTH_RE = "(?:" + "|".join(MONTHS) + ")"


def stamp(value, tz="America/Detroit"):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(ZoneInfo(tz)).replace(microsecond=0).isoformat()
    raw = str(value).strip()
    if re.fullmatch(r"\d{8}T\d{6}", raw):
        raw = datetime.strptime(raw, "%Y%m%dT%H%M%S").isoformat()
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    dt = dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz))
    return dt.isoformat(timespec="seconds")


def day(value):
    return datetime.fromisoformat(stamp(value)).astimezone(DETROIT).date()


def query(url, **params):
    u = urlsplit(url)
    values = dict(parse_qsl(u.query))
    values.update(params)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(values), ""))


def fetch(url, timeout=20, headers=None):
    if urlsplit(url).scheme not in {"https", "http"}:
        raise ValueError("Public HTTP(S) URLs only")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyBriefCalendar/1.0)", **(headers or {})}
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        status, body = response.status, response.read(12_000_001)
    except Exception:
        # Same TLS-compatible HTTP fallback already used by the general crawler.
        from curl_cffi import requests
        response = requests.get(url, headers=headers, impersonate="chrome", timeout=timeout)
        status, body = response.status_code, response.content
    if status != 200:
        raise ValueError(f"HTTP {status}: {url}")
    if len(body) > 12_000_000:
        raise ValueError("Calendar response exceeds 12 MB")
    return body.decode("utf-8", "replace")


@dataclass
class Result:
    url: str
    events: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    def markdown(self):
        return "# Structured data (JSON-LD): " + self.url + "\n\n" + "\n\n".join(
            "```json\n" + json.dumps(e, ensure_ascii=False) + "\n```" for e in self.events)


class Collector:
    def __init__(self, source, first, last, timeout, getter):
        self.source, self.config = source, source["verified_calendar"]
        self.first, self.last, self.timeout, self.getter = first, last, timeout, getter
        self.checked = datetime.now(DETROIT).isoformat(timespec="seconds")
        self.url = self.config.get("url") or source.get("events_url") or source["url"]
        self.events, self.errors, self.unresolved, self.pages = [], [], [], []
        self.complete = False
        self.max_requests = self.config.get("maxRequests", 100)

    def get(self, url, headers=None):
        if len(self.pages) >= self.max_requests:
            raise ValueError("Calendar request budget exhausted; coverage incomplete")
        self.pages.append(url)
        return self.getter(url, self.timeout, headers)

    def json(self, url, headers=None):
        return json.loads(self.get(url, headers))

    def problem(self, title, reason, url=None):
        item = {"title": title, "reason": reason, "url": url or self.url}
        if item not in self.unresolved:
            self.unresolved.append(item)

    def add(self, title, start, *, end=None, url=None, native_id=None, venue=None, city=None,
            description=None, price=None, category=None, image=None, status=None, **extra):
        if not title or not start:
            self.problem(title, "Missing title or verified date", url)
            return
        try:
            start_day = day(start)
            if not self.first <= start_day <= self.last:
                return
            if end and datetime.fromisoformat(stamp(end)) < datetime.fromisoformat(stamp(start)):
                self.problem(title, "End precedes start", url)
                return
        except (ValueError, TypeError, OverflowError):
            self.problem(title, "Invalid source timestamp", url)
            return
        venue = venue or self.config.get("venue")
        city = city or self.config.get("city")
        if not venue or not city:
            self.problem(title, "Venue/city unresolved", url)
            return
        price = evidence.price_fields(price)
        record = evidence.apply({"title": evidence.text(title), "start": stamp(start),
                                 "price": price.get("price"), "status": status}, description or "",
                                checked_at=self.checked)
        event = {"@context": "https://schema.org", "@type": "Event", "name": record["title"],
                 "startDate": str(start) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(start)) else stamp(start),
                 "url": url or self.url, "identifier": str(native_id or url or self.url),
                 "location": {"@type": "Place", "name": venue, "address": {"addressLocality": city}},
                 "description": record["title"] if evidence.text(description).lower() in {'', 'tbd', 'n/a', 'none'} else evidence.text(description),
                 "eventCategory": category or self.config.get("category") or self.source.get("category") or "Event",
                 "verifiedCalendar": True, "lastCheckedAt": self.checked,
                 **extra}
        if end:
            event["endDate"] = stamp(end)
        if price:
            event["priceText"] = price["price"]
        if image:
            event["image"] = image
        if record.get("status"):
            statuses = {"scheduled": "EventScheduled", "canceled": "EventCancelled",
                        "postponed": "EventPostponed", "rescheduled": "EventRescheduled"}
            event["eventStatus"] = "https://schema.org/" + statuses[record["status"]]
        if record.get("statusEvidenceKind") == "announcement":
            event.update(statusEvidence=record["statusEvidence"], statusEvidenceKind="announcement")
        booking = re.search(r"[^.!?]*(?:reservations? required|registration required|pre-registration|limited to \d+|RSVP by|register by|registration closes|registration deadline)[^.!?]*", evidence.text(description), re.I)
        age = re.search(r"\b(?:ages?\s+\d+\s*(?:(?:and|&)\s+(?:up|older|over)|\+)|\d+\+\s*(?:only|event))", evidence.text(description), re.I)
        if booking and not extra.get("registration"):
            event["registration"] = booking[0].strip()
        if age:
            event["typicalAgeRange"] = age[0]
        self.events.append(event)
        if record.get("replacementStart"):
            # The old date remains a notice. The replacement has its own slot.
            self.add(title, record["replacementStart"], url=url, native_id=native_id,
                     venue=venue, city=city, description=title, price=price.get("price"), category=category,
                     status="rescheduled", previousStartDate=stamp(start),
                     statusEvidence=record["statusEvidence"], statusEvidenceKind="announcement")

    def jsonld(self, body, url):
        soup = BeautifulSoup(body, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                objects = list(evidence.event_objects(json.loads(script.string or script.get_text())))
            except (ValueError, TypeError):
                continue
            for obj in objects:
                location = obj.get("location") or {}
                location = location[0] if isinstance(location, list) and location else location
                address = location.get("address") or {} if isinstance(location, dict) else {}
                offers = evidence.offer_fields(obj.get("offers"))
                self.add(obj.get("name"), obj.get("startDate"), end=obj.get("endDate"),
                         url=obj.get("url") or url, native_id=obj.get("identifier") or obj.get("@id") or url,
                         venue=location.get("name") if isinstance(location, dict) else location,
                         city=address.get("addressLocality") if isinstance(address, dict) else None,
                         description=obj.get("description"), price=offers.get("price"), offers=obj.get("offers"),
                         status={"EventScheduled": "scheduled", "EventCancelled": "canceled", "EventPostponed": "postponed",
                                 "EventRescheduled": "rescheduled"}.get(str(obj.get("eventStatus")).rsplit("/", 1)[-1]),
                         image=obj.get("image"))
        return soup


def umich(c):
    page = 1
    while True:
        rows = c.json(query(c.url, filter="all", range=f"{c.first}to{c.last}", v=2, **{"max-results": 200, "page": page}))
        if not isinstance(rows, list):
            raise ValueError("U-M feed changed shape")
        for r in rows:
            # Virtual/unspecified locations do not acquire a fabricated venue.
            venue = r.get("location_name") or r.get("building_name")
            city = "Ann Arbor" if r.get("building_id") and r.get("building_name") not in {"Off Campus Location", "Virtual"} else None
            known = {"ARK Reserved": "The Ark", "GA - Hill": "Hill Auditorium", "Mendelssohn": "Lydia Mendelssohn Theatre"}
            if venue in known:
                venue, city = known[venue], "Ann Arbor"
            if not city or not venue or venue.lower() in {"virtual", "off campus location"}:
                c.problem(r.get("event_title"), "Location requires verification", r.get("permalink"))
                continue
            c.add(r.get("event_title"), stamp(r.get("datetime_start"), r.get("time_zone") or "America/Detroit"),
                  end=stamp(r.get("datetime_end"), r.get("time_zone") or "America/Detroit") if r.get("has_end_time") else None,
                  url=str(r.get("permalink", "")).replace("http://", "https://"), native_id=r.get("id"),
                  venue=venue, city=city, description=r.get("description"), price=r.get("cost"),
                  category=r.get("event_type"), dateModified=stamp(r.get("datetime_modified")), image=r.get("image_url"))
        if len(rows) < 200:
            c.complete = True
            return
        page += 1


def crowdwork(c):
    data = c.json(query(c.url, start=str(c.first), end=str(c.last)))
    if data.get("status") != 200 or not isinstance(data.get("data"), list):
        raise ValueError("Crowdwork calendar changed shape")
    for show in data["data"]:
        for start in show.get("dates", []):
            override = show.get("date_overrides", {}).get(start, {})
            tiers = override.get("cost_tiers") or show.get("cost_tiers") or []
            prices = [t["cost_after_fees"] / 100 for t in tiers if isinstance(t.get("cost_after_fees"), (int, float))]
            price = (f"${min(prices):g}" + (f"–${max(prices):g}" if max(prices) != min(prices) else "") + " including fees") if prices else None
            desc = override.get("description") or show.get("description") or {}
            desc = desc.get("body") if isinstance(desc, dict) else desc
            status = override.get("status") or show.get("status")
            status = {"cancelled": "canceled", "canceled": "canceled", "postponed": "postponed", "active": "scheduled"}.get(status)
            c.add(override.get("name") or show.get("name"), start, url=query(show["url"], date=start),
                  native_id=f"{show['id']}:{start}", venue=override.get("venue") or show.get("venue"),
                  description=desc, price=price, status=status,
                  dateModified=(show.get("description") or {}).get("updated_at"),
                  image=(show.get("img") or {}).get("url") if isinstance(show.get("img"), dict) else show.get("img"))
    c.complete = True


def squarespace(c):
    data = c.json(query(c.url, format="json"))
    if not isinstance(data.get("upcoming"), list):
        raise ValueError("Squarespace upcoming calendar changed shape")
    for r in data["upcoming"]:
        location = r.get("location") or {}
        desc = evidence.text(r.get("body"))
        price_match = re.search(r"\$[\d,.]+\s*(?:cover|per\s+person|admission)?|\bfree\s+(?:happy hour concert|admission)\b", desc, re.I)
        price = price_match[0] if price_match else None
        if price and price.lower().startswith("free"):
            price = "Free"
        c.add(r.get("title"), r.get("startDate"), end=r.get("endDate"),
              native_id=r.get("id"), url=urljoin(c.url, r.get("fullUrl") or ""),
              venue=location.get("addressTitle"), description=desc, price=price,
              dateModified=stamp(r.get("updatedOn")), image=r.get("assetUrl"))
    # Squarespace returns all upcoming events; pagination here is the past feed.
    c.complete = True


def tribe(c):
    url = query(c.url, start_date=str(c.first), end_date=str(c.last), per_page=50)
    seen = set()
    while url:
        if url in seen or urlsplit(url).netloc != urlsplit(c.url).netloc:
            raise ValueError("Invalid calendar pagination")
        seen.add(url)
        data = c.json(url)
        if not isinstance(data.get("events"), list):
            raise ValueError("Events Calendar API changed shape")
        for r in data["events"]:
            venue = r.get("venue") or {}
            if isinstance(venue, list):
                venue = venue[0] if venue else {}
            desc = r.get("description") or ""
            categories = [x.get("name", "") for x in r.get("categories", [])]
            if "postponed" in [s.lower() for s in categories] and not evidence._STATUS.search(evidence.text(desc)):
                desc = "This show is postponed. " + desc
            start = stamp(r.get("start_date"), r.get("timezone") or "America/Detroit")
            end = stamp(r.get("end_date"), r.get("timezone") or "America/Detroit")
            # Blue LLama sells separate sets. The explicit set times replace its
            # generic evening span; ends stay unknown unless each set supplies one.
            sets = re.search(r"\bSets\s+(\d{1,2}(?::\d{2})?\s*[ap]m)\s*&\s*(\d{1,2}(?::\d{2})?\s*[ap]m)", evidence.text(desc), re.I)
            starts = [start]
            if sets:
                from dateutil.parser import parse
                starts = [parse(f"{day(start)} {clock}").replace(tzinfo=DETROIT).isoformat() for clock in sets.groups()]
                end = None
            for slot in starts:
                c.add(r.get("title"), slot, end=end, url=r.get("url"),
                      native_id=f"{r['id']}:{slot}", venue=venue.get("venue"), city=venue.get("city"),
                      description=desc, price=r.get("cost"), category=c.config.get("category"),
                      dateModified=stamp(r.get("modified_utc"), "UTC"), image=(r.get("image") or {}).get("url"))
        url = data.get("next_rest_url")
        if url and data.get("total_pages") and len(seen) >= data["total_pages"]:
            url = None
    c.complete = True


def ovationtix(c):
    headers = {"clientId": c.config["clientId"], "Accept": "application/json"}
    productions = c.json(c.url + "/Production?expandPerformances=summary", headers)
    calendar = c.json(c.url + "/CalendarProductions", headers)
    by_id = {r["id"]: r for r in productions}
    for row in calendar:
        for p in row.get("productions", []):
            if p.get("hidden"):
                continue
            details = by_id.get(p["productionId"], {})
            venue = details.get("venue") or {}
            for slot in p.get("showtimes", []):
                if not slot.get("isVisible", True):
                    continue
                c.add(p.get("name"), stamp(slot.get("performanceStartTime")), end=stamp(slot.get("performanceEndTime")),
                      native_id=slot.get("performanceId"),
                      url=f"https://ci.ovationtix.com/{c.config['clientId']}/production/{p['productionId']}?performanceId={slot['performanceId']}",
                      venue=venue.get("name"), city=(venue.get("address") or {}).get("city"),
                      description=details.get("description"), status="canceled" if slot.get("isCancelled") else "scheduled",
                      offers={"availability": "https://schema.org/SoldOut" if slot.get("isSoldOut") else "https://schema.org/InStock"} if slot.get("remainingTix") is not None else None,
                      image=details.get("logoUrl"))
    c.complete = True


def semja(c):
    # Public month views contain every dated slot and keyed venue/time popups.
    # Four monthly requests cover a 90-day window, including days with no events.
    cursor = c.first.replace(day=1)
    while cursor <= c.last:
        url = query(c.url, year=cursor.year, month=cursor.month)
        soup = BeautifulSoup(c.get(url), "html.parser")
        if not soup.select("table.main"):
            raise ValueError("SEMJA calendar changed shape")
        if not any(cursor.strftime('%Y%m') in a.get('href','') for a in soup.select('a.dayofmonth')):
            raise ValueError('SEMJA did not return the requested month')
        for card in soup.select("td.hasevents"):
            for link in card.select("a.entry"):
                m = re.match(r"([^»]+)»\s*(.+)", link.get_text(" ", strip=True))
                if not m:
                    continue
                popup = soup.find(id='eventinfo-' + link.get('id', ''))
                if popup is None:
                    c.problem(m[2], "Missing venue description", url)
                    continue
                labels = {dt.get_text(' ',strip=True).rstrip(':'): dt.find_next_sibling('dd').get_text(' ',strip=True)
                          for dt in popup.select('dt') if dt.find_next_sibling('dd')}
                location = labels.get('Location','').rsplit('|',1)
                venue, city = (v.strip() for v in location) if len(location) == 2 else (None,None)
                from dateutil.parser import parse
                params = dict(parse_qsl(urlsplit(link['href']).query))
                event_day = datetime.strptime(params['date'],'%Y%m%d').date()
                if not c.first <= event_day <= c.last:
                    continue
                clocks = re.split(r'\s*[-–]\s*',labels.get('Time',''))
                start = parse(f"{event_day} {m[1]}").replace(tzinfo=DETROIT).isoformat()
                end = parse(f'{event_day} {clocks[1]}').replace(tzinfo=DETROIT) if len(clocks)==2 else None
                if end and end < datetime.fromisoformat(start):
                    end += timedelta(days=1)
                if not city or not venue:
                    c.problem(m[2],'Venue/city requires verification',urljoin(url,link['href']))
                    continue
                c.add(m[2], start, end=end.isoformat() if end else None, native_id=params.get('id'), url=urljoin(url, link.get("href")),
                      venue=venue, city=city, description=labels.get('Description'), category="Jazz")
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    c.complete = True


def dated_season(c):
    """Sparse season pages retain verified days without inventing showtimes."""
    soup = BeautifulSoup(c.get(c.url), "html.parser")
    page_title = soup.title.get_text(' ',strip=True) if soup.title else ''
    for x in soup.select("nav,header,footer,script,style,title"):
        x.decompose()
    body = soup.get_text("\n", strip=True)
    year_pattern = r"(20\d{2})\s*[-–]\s*(20\d{2})\s+Season"
    year_match = re.search(year_pattern, body) or re.search(year_pattern, page_title)
    year = int(year_match[1]) if year_match else c.config.get("seasonYear")
    if not year:
        c.problem(c.source["name"], "Season year not explicitly verified")
        return
    lines = [x.replace('\u200b', '').strip() for x in body.splitlines() if x.replace('\u200b', '').strip()]
    named = {x.get_text(' ', strip=True) for x in soup.select('strong')
             if not re.match(r"(?:By|In a |Music |Book |Lyrics |- NHS)", x.get_text(' ', strip=True), re.I)}
    if 'northville' in c.source['name'].lower():
        named.update(x.get_text('\n',strip=True).splitlines()[-1].replace('\u200b','').strip()
                     for x in soup.select('h2') if not re.search(r'season',x.get_text(),re.I))
    title, venue = None, c.config.get("venue")
    for i, line in enumerate(lines):
        if (line.startswith('"') and line.endswith('"')) or line in named:
            title = line.strip('"')
        if re.search(r"High School$", line):
            venue = line
        cleaned_line = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", line)
        performance = re.match(r"(?:Performances:\s*)?(" + MONTH_RE + r")\s+\d", cleaned_line, re.I)
        if not performance:
            continue
        # Northville's production name immediately precedes its dates.
        if not title:
            title = next((v for v in reversed(lines[:i]) if v and not re.search(r"theatre|play|season|^NHS|^Purchase|@", v, re.I)), None)
        # Park Players supplies each production's school after its dates.
        following_venue = next((v for v in lines[i + 1:i + 4] if re.search(r"High School$", v)), venue)
        if '@' in line and not re.search(r'[ap]\.?m\.?',line,re.I):
            c.problem(title, 'Showtimes omit AM/PM; verified days retained')
        for piece in re.finditer(r'(' + MONTH_RE + r')\s+([\d, –-]+)', cleaned_line.split('@')[0],re.I):
            month = next(n + 1 for n, v in enumerate(MONTHS) if v.lower() == piece[1].lower())
            stated_year = re.search(r"\b(20\d{2})\b", piece[2])
            expected_year = year if month >= 7 else year + 1
            actual_year = int(stated_year[1]) if stated_year else expected_year
            if actual_year != expected_year:
                c.problem(title, f"Date year {actual_year} conflicts with season {year}–{year + 1}")
                continue
            numbers = re.sub(r",?\s*20\d{2}\b", "", piece[2]).strip(' ,')
            for part in numbers.split(','):
                match = re.fullmatch(r"\s*(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*", part)
                if not match:
                    continue
                for d in range(int(match[1]),int(match[2] or match[1])+1):
                    c.add(title, date(actual_year, month, d).isoformat(), venue=following_venue,
                          native_id=title, description=f"{title}. {line}", category="Theatre")
    c.complete = True


def kempf(c):
    from dateutil.parser import parse
    soup = BeautifulSoup(c.get(c.url), 'html.parser')
    cards = soup.select('.decm-events-details')
    if not cards:
        raise ValueError('Kempf upcoming events changed shape')
    for card in cards:
        heading, date_label, time_label = card.select_one('h2 a'), card.select_one('.ecs-eventDate .decm_date'), card.select_one('.ecs-eventTime .decm_date')
        if not heading or not date_label:
            c.problem('Kempf event', 'Missing title/date')
            continue
        event_day = parse(date_label.get_text(' ', strip=True)).date()
        start, end = str(event_day), None
        if time_label:
            clocks = re.split(r'\s*[-–]\s*', time_label.get_text(' ', strip=True))
            start = parse(f'{event_day} {clocks[0]}').replace(tzinfo=DETROIT).isoformat()
            if len(clocks) == 2:
                end = parse(f'{event_day} {clocks[1]}').replace(tzinfo=DETROIT).isoformat()
        desc = card.get_text(' ', strip=True)
        c.add(heading.get_text(' ', strip=True), start, end=end, url=heading['href'],
              native_id=heading['href'], description=desc, price='Free' if 'admission free' in desc.lower() else None)
    c.complete = True


def dso(c):
    # Exact public endpoint and parameters used by DSO's BlocksOffice calendar.
    rows = c.json(c.url)
    if not isinstance(rows, list):
        raise ValueError('DSO performance feed changed shape')
    venues = {'Orchestra Hall', 'DSO Orchestra Hall', 'The Cube', 'Allesee Hall'}
    for r in rows:
        if r.get('disable_in_listings') or not c.first <= day(r['perf_date']) <= c.last:
            continue
        if r.get('facility') not in venues:
            c.problem(r.get('title'), 'Community venue city requires verification', r.get('event_url'))
            continue
        low, high = r.get('min_price'), r.get('max_price')
        price = f'${low:g}' + (f'–${high:g}' if high is not None and high != low else '') if isinstance(low, (int,float)) else None
        c.add(r['title'], r['perf_date'], url=query(r['event_url'], performance=r['perf_no']),
              native_id=r['prod_season_no'], venue=r['facility'], city='Detroit',
              description=r.get('summary'), price=price, image=r.get('image_url'),
              status='canceled' if str(r.get('perf_status')).lower() in {'canceled','cancelled'} else 'scheduled',
              offers={'availability': 'https://schema.org/SoldOut'} if r.get('tickets_available') == 0 and r.get('is_on_sale') == 'Y' else None)
    c.complete = True


def five15(c):
    rows = c.json(query(c.url, start=str(c.first), end=str(c.last + timedelta(days=1))))
    if not isinstance(rows, list):
        raise ValueError('Five15 calendar feed changed shape')
    for r in rows:
        props = r.get('extendedProps') or {}
        if props.get('category') == 'Pronto! Event':
            c.problem(r.get('title'), 'Partner event venue requires verification', r.get('url'))
            continue
        price = props.get('ticketPrice')
        c.add(r.get('title'), r.get('start'), end=r.get('end'), url=r.get('url'), native_id=r.get('id'),
              description=r.get('title'), price=f'${price:g}' if isinstance(price,(int,float)) else None,
              registration='Reservation required; check remaining seats on the booking page.')
    c.complete = True


def nature(c):
    soup = BeautifulSoup(c.get(c.url), "html.parser")
    from dateutil.parser import parse
    free = 'no cost or registration for our public nature' in soup.get_text(' ',strip=True).lower()
    for paragraph in soup.select("p"):
        body = paragraph.get_text(" ", strip=True)
        body = re.sub(r'\s+,', ',', body)
        match = re.search(r'(?:“|")([^”"]+)(?:”|").*?(Sunday|Saturday|Monday|Tuesday|Wednesday|Thursday|Friday),?\s*('
                          + MONTH_RE + r'\s+\d{1,2},?\s+20\d{2}).*?at\s+(Tenhave Woods|Cummingston Park).*?(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)', body, re.I)
        if match:
            dt = parse(match[3] + " " + match[5]).replace(tzinfo=DETROIT)
            c.add(match[1], dt.isoformat(), native_id=f"{match[1]}:{dt.isoformat()}", venue=match[4],
                  description=body, price="Free" if free or "no cost" in body.lower() else None, category="Nature",
                  registration='No registration required' if free else None)
    c.complete = True


def etix(c):
    # The favorite venue's homepage is the source when Etix blocks its public
    # calendar. Keep date-only occurrences; never apply generic hours as times.
    soup = BeautifulSoup(c.get(c.url), "html.parser")
    age_policy = '18 and over' if re.search(r'18\s*(?:&|and)\s*Over Venue',soup.get_text(' ',strip=True),re.I) else None
    found = 0
    for heading in soup.select("h3"):
        parent = heading.find_parent(class_="eventSlide") or heading
        for _ in range(1 if parent is not heading else 7):
            if parent.parent is None:
                break
            if "eventSlide" not in (parent.get("class") or []):
                parent = parent.parent
            links = parent.select('a[href*="etix.com/ticket/"]')
            content = parent.get_text(" ", strip=True)
            label = parent.select_one('.eventTime')
            dated = label.get_text(' ', strip=True) if label else content
            match = re.search(r"(" + MONTH_RE + r")\s+(\d{1,2}(?:st|nd|rd|th)?(?:\s*(?:,|&|and)?\s+\d{1,2}(?:st|nd|rd|th)?)*)(?:\s*[, ]\s*(20\d{2}))?", dated, re.I)
            if links and match and len(content) < 6500:
                month = next(n + 1 for n, m in enumerate(MONTHS) if m.lower() == match[1].lower())
                year = int(match[3]) if match[3] else c.first.year + (month < c.first.month and c.first.month >= 10 and month <= 3)
                for d in re.findall(r"\d{1,2}", match[2]):
                    c.add(heading.get_text(" ", strip=True), date(year, month, int(d)).isoformat(),
                          url=links[0]["href"], native_id=f"{links[0]['href']}:{year}-{month}-{d}", description=content,
                          category="Comedy", typicalAgeRange=age_policy)
                    found += 1
                break
    if not found:
        raise ValueError("No dated comedy cards found")
    workshop_link = soup.select_one('a[href$="/comedy-workshop"]')
    if workshop_link:
        from dateutil.parser import parse
        workshop_url = urljoin(c.url,workshop_link['href'])
        workshop = BeautifulSoup(c.get(workshop_url),'html.parser')
        for n in workshop.select('script,style,nav,header,footer'):
            n.decompose()
        content = re.sub(r'(\d)\s*(?:st|nd|rd|th)\b',r'\1',workshop.get_text(' ',strip=True))
        for section in re.finditer(r'Dates:\s*Sundays,\s*(' + MONTH_RE + r'\s+\d{1,2})\s+to\s+(' + MONTH_RE + r'\s+\d{1,2})(.*?)Time:\s*(\d{1,2}(?::\d{2})?\s*[ap]m)\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*[ap]m)(.*?)(?=Dates:|REGISTER HERE|Class Itinerary:|$)',content,re.I):
            begin = parse(section[1],default=datetime(c.first.year,1,1)).date()
            if begin.month < c.first.month and c.first.month >=10 and begin.month <=3:
                begin = begin.replace(year=begin.year+1)
            finish = parse(section[2],default=datetime(begin.year,1,1)).date()
            if finish < begin:
                finish = finish.replace(year=finish.year+1)
            if begin.weekday()!=6 or finish.weekday()!=6:
                c.problem('Comedy workshop','Course dates and stated Sundays conflict',workshop_url)
                continue
            skipped = re.search(r'no class\s+([A-Za-z]+\s+\d{1,2})',section[3],re.I)
            excluded = parse(skipped[1],default=datetime(begin.year,1,1)).date() if skipped else None
            cost = re.search(r'Price:\s*(\$[\d,.]+)',section[6])
            cursor = begin
            while cursor <= finish:
                if cursor!=excluded:
                    start,end = [parse(f'{cursor} {clock}').replace(tzinfo=DETROIT).isoformat() for clock in (section[4],section[5])]
                    c.add('Stand-Up Comedy Writing & Performance Workshop with Brandi Alexander',start,end=end,
                          url=workshop_url,native_id=f'workshop:{begin}',description=section[0],
                          price=(cost[1]+' for the full course') if cost else None,category='Comedy Workshop',typicalAgeRange=age_policy)
                cursor += timedelta(days=7)
            graduation = re.search(r'Graduation Show:\s*(?:Wednesday,)?\s*(' + MONTH_RE + r'\s+\d{1,2})\s*@\s*(\d{1,2}:\d{2}\s*[ap]m)',section[6],re.I)
            if graduation:
                start = parse(graduation[1]+' '+graduation[2],default=datetime(finish.year,1,1)).replace(tzinfo=DETROIT)
                c.add('Comedy Workshop Graduation Performance',start.isoformat(),url=workshop_url,
                      native_id=f'graduation:{start.date()}',description=graduation[0],category='Comedy',typicalAgeRange=age_policy)
    c.problem(c.source["name"], "Homepage dates collected; public ticket calendar coverage/showtimes require verification")
    c.complete = False


def atg(c):
    from dateutil.parser import parse
    soup = BeautifulSoup(c.get(c.url), 'html.parser')
    links = sorted({urljoin(c.url, a['href']) for a in soup.select('a[href]')
                    if re.search(r'^/events/[^/]+/fisher-theatre/?$', urlsplit(a['href']).path)})
    if not links:
        raise ValueError('No Fisher production detail links found')
    for url in links:
        body = c.get(url)
        detail = BeautifulSoup(body, 'html.parser')
        obj = None
        for script in detail.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
                if isinstance(data,dict) and data.get('@type') == 'TheaterEvent':
                    obj = data
                    break
            except (ValueError,TypeError):
                pass
        if not obj:
            c.problem(url, 'Production structured metadata missing', url)
            continue
        if obj.get('startDate') and day(obj['startDate']) > c.last:
            continue
        visible = detail.get_text(' ',strip=True)
        schedule = re.search(r'\bSchedule\s+(.*?)\s+(?:Run time|Age guidance|Access performances)', visible, re.I)
        pattern = r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s*([A-Z][a-z]+\s+\d{1,2})\s*[-–]\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s*([A-Z][a-z]+\s+\d{1,2}),\s*(20\d{2})'
        span = re.match(pattern, schedule[1]) if schedule else None
        if not span:
            c.jsonld(body,url)
            if schedule and re.search(r'\d{1,2}\s*[-–]\s*',schedule[1]):
                c.problem(obj['name'], 'Only upcoming previews verified; full performance schedule unresolved', url)
            continue
        first, last = [parse(v+' '+span[3]).date() for v in (span[1],span[2])]
        if last < first:
            first = first.replace(year=first.year-1)
        weekdays = {'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6}
        rules = []
        for rule in re.finditer(r'\b(Mon\w*|Tue\w*|Wed\w*|Thu\w*|Fri\w*|Sat\w*|Sun\w*)(?:\s*[-–]\s*(Mon\w*|Tue\w*|Wed\w*|Thu\w*|Fri\w*|Sat\w*|Sun\w*))?,\s*(\d{1,2}(?::\d{2})?\s*[AP]M(?:\s*&\s*\d{1,2}(?::\d{2})?\s*[AP]M)?)\s*(?:ET|EST|EDT)?', schedule[1][span.end():], re.I):
            begin = weekdays[rule[1][:3].lower()]
            finish = weekdays[(rule[2] or rule[1])[:3].lower()]
            days = {(begin+i)%7 for i in range((finish-begin)%7+1)}
            clocks = re.split(r'\s*&\s*',rule[3])
            rules.append((days,clocks))
        if not rules or re.search(r'\b(?:except|excluding|additional|no show)\b', schedule[1],re.I):
            c.jsonld(body,url)
            c.problem(obj['name'],'Unrecognized explicit performance schedule',url)
            continue
        cursor = max(first,c.first)
        while cursor <= min(last,c.last):
            for days, clocks in rules:
                if cursor.weekday() not in days:
                    continue
                for clock in clocks:
                    start = parse(f'{cursor} {clock}').replace(tzinfo=DETROIT).isoformat()
                    c.add(obj['name'],start,url=url,native_id=url,description=obj.get('description'),
                          image=obj.get('image',[None])[0],status='scheduled',
                          scheduleEvidence=schedule[0])
            cursor += timedelta(days=1)
    c.complete = True


ADAPTERS = {"umich": umich, "crowdwork": crowdwork, "squarespace": squarespace,
            "tribe": tribe, "ovationtix": ovationtix, "semja": semja, "season": dated_season,
            "nature": nature, "etix": etix, "atg": atg, "dso": dso,
            "kempf": kempf, "five15": five15}


def collect(source, reference_day=None, horizon_days=90, timeout=20, getter=fetch):
    first = date.fromisoformat(reference_day) if reference_day else datetime.now(DETROIT).date()
    last = first + timedelta(days=horizon_days - 1)
    c = Collector(source, first, last, timeout, getter)
    try:
        ADAPTERS[c.config["adapter"]](c)
    except Exception as exc:
        c.errors.append(f"{type(exc).__name__}: {exc}")
    # Detail/listing copies of the same native slot do not multiply occurrences.
    unique = {(e["url"], e["startDate"], e["name"]): e for e in c.events}
    precise = {(e['name'],day(e['startDate']),e['location']['name']) for e in unique.values() if 'T' in e['startDate']}
    unique = {key:e for key,e in unique.items() if 'T' in e['startDate'] or
              (e['name'],day(e['startDate']),e['location']['name']) not in precise}
    status = "ok" if c.complete and not c.errors and not c.unresolved else "partial" if c.events or c.complete else "failed"
    coverage = {"source": source["name"], "adapter": c.config["adapter"], "status": status,
                "checkedAt": c.checked, "requestedStart": str(first), "requestedEnd": str(last),
                "complete": c.complete and not c.errors, "requests": len(c.pages), "pages": c.pages,
                "discovered": len(unique), "datesWithEvents": sorted({day(e["startDate"]).isoformat() for e in unique.values()}),
                "errors": c.errors, "unresolved": c.unresolved}
    return Result(c.url, list(unique.values()), coverage)
