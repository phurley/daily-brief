#!/usr/bin/env python3
"""Find every public still photo taken on a calendar day in a previous year.

Uses SmugMug's date-filtered API, follows every page, and verifies the original
capture date. No login, model calls, upload dates, or filename date guesses.
Without --date, refresh yesterday, today and tomorrow in America/Detroit.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DETROIT = ZoneInfo('America/Detroit')
CACHE_VERSION = 1


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', delete=False) as out:
        temporary = Path(out.name)
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write('\n')
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def plain(value):
    return ' '.join(unescape(re.sub(r'<[^>]+>', '', str(value or ''))).split())


def capture_date(image, metadata):
    # SmugMug can normalize DateTimeOriginal to UTC using an upload timezone.
    # The embedded camera date preserves the original local calendar day.
    for source, value in [('ImageMetadata.DateTimeCreated', metadata.get('DateTimeCreated')),
                          ('ImageMetadata.DateCreated', metadata.get('DateCreated')),
                          ('Image.DateTimeOriginal', image.get('DateTimeOriginal'))]:
        try:
            return date.fromisoformat(str(value)[:10]), source
        except ValueError:
            continue
    return None, None


def photo_candidate(image, expansions, target, site):
    if image.get('IsVideo') or image.get('Hidden') or image.get('Processing'):
        return None
    uris = image.get('Uris', {})
    def expanded(name):
        uri = uris.get(name)
        if isinstance(uri, dict):
            uri = uri.get('Uri')
        return expansions.get(uri, {}).get(name, {})
    metadata = expanded('ImageMetadata')
    taken, source = capture_date(image, metadata)
    if not taken or taken.year >= target.year or (taken.month, taken.day) != (target.month, target.day):
        return None
    sizes = expanded('ImageSizes')
    image_url = next((sizes.get(k) for k in ['X2LargeImageUrl', 'XLargeImageUrl', 'LargeImageUrl', 'MediumImageUrl'] if sizes.get(k)), None)
    image_key = image.get('ImageKey')
    if not image_key or not image_url or urlsplit(image_url).scheme != 'https':
        raise ValueError('A matching photo lacks an image ID or public display-size URL')
    # Only explicit image location metadata is published. Creator contact info,
    # GPS coordinates, filenames, album-name guesses and API keys stay out.
    location = ', '.join(dict.fromkeys(plain(metadata.get(k)) for k in ['City', 'State', 'Country'] if plain(metadata.get(k))))
    description = plain(image.get('Title') or image.get('Caption') or metadata.get('Caption'))
    return {'id': f'smugmug-{image_key}', 'imageKey': image_key,
            'imageUrl': image_url, 'takenDate': taken.isoformat(),
            'takenDateSource': source, 'location': location,
            'description': description or f'Photograph taken on {taken.strftime("%B %-d, %Y")}.',
            'sourceUrl': image.get('WebUri') or f'{site}/i-{image_key}'}


class SmugMug:
    def __init__(self, site):
        self.site = site.rstrip('/')
        if urlsplit(self.site).scheme != 'https' or urlsplit(self.site).path:
            raise ValueError('--site must be an HTTPS site origin')
        with urlopen(Request(self.site, headers={'User-Agent': 'DailyBriefPhotos/1.0'}), timeout=30) as response:
            page = response.read().decode('utf-8')
        # These are the public visitor API key and account nickname embedded in
        # the archive's own page. Do not persist either key or an OAuth token.
        key = re.search(r'\bvar\s+SM\s*=\s*\{\s*env\s*:\s*\{\s*"apiKey"\s*:\s*("[^"\n]+")', page)
        nickname = re.search(r'"nickName"\s*:\s*("[^"\n]+")', page)
        if not key or not nickname:
            raise ValueError('Public SmugMug visitor API configuration was not found')
        self.api_key = json.loads(key.group(1))
        self.scope = '/api/v2/user/' + json.loads(nickname.group(1))

    def get(self, uri, parameters=None):
        parts = urlsplit(urljoin(self.site + '/', uri))
        if parts.scheme != 'https' or parts.hostname not in {urlsplit(self.site).hostname, 'api.smugmug.com'} or not parts.path.startswith('/api/v2/'):
            raise ValueError('Unexpected SmugMug pagination URL')
        query = dict(parse_qsl(parts.query))
        query.update(parameters or {})
        query.update(APIKey=self.api_key, _verbosity=1)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
        for attempt in range(3):
            try:
                request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'DailyBriefPhotos/1.0'})
                with urlopen(request, timeout=30) as response:
                    result = json.load(response)
                if result.get('Code') != 200:
                    raise ValueError(f'SmugMug response code {result.get("Code")}')
                return result
            except HTTPError as error:
                if error.code not in [429, 500, 502, 503, 504] or attempt == 2:
                    raise RuntimeError(f'SmugMug HTTP {error.code} at {parts.path}') from None
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError(f'SmugMug request failed at {parts.path}') from None
            time.sleep(2 ** attempt)

    def first_year(self):
        result = self.get('/api/v2/image!search', {'Scope': self.scope, 'Type': 'Image',
            'SortMethod': 'DateTaken', 'SortDirection': 'Ascending',
            'DateTakenStart': '1800-01-01T00:00:00Z', 'count': 1})
        images = result.get('Response', {}).get('Image', [])
        if not images:
            return None
        taken, _ = capture_date(images[0], {})
        if not taken:
            raise ValueError('Oldest searchable capture date could not be established')
        return max(1, taken.year - 1)  # include a possible local-date year boundary

    def year_candidates(self, target, year):
        try:
            anniversary = target.replace(year=year)
        except ValueError:  # February 29 has no anniversary in non-leap years
            return []
        # Include timezone-shifted API dates around the target, then enforce
        # the exact camera date. Never substitute the photo's upload date.
        params = {'Scope': self.scope, 'Type': 'Image', 'SortMethod': 'DateTaken',
                  'SortDirection': 'Ascending', 'count': 100,
                  'DateTakenStart': (anniversary - timedelta(days=1)).isoformat() + 'T00:00:00Z',
                  'DateTakenEnd': (anniversary + timedelta(days=2)).isoformat() + 'T00:00:00Z',
                  '_expand': 'ImageMetadata,ImageSizes'}
        uri = '/api/v2/image!search'
        seen = set()
        photos = []
        received = 0
        while uri:
            if uri in seen:
                raise ValueError('Repeated SmugMug pagination page')
            seen.add(uri)
            result = self.get(uri, params)
            response = result.get('Response', {})
            pages = response.get('Pages', {})
            if not isinstance(pages.get('Total'), int):
                raise ValueError('SmugMug did not provide a result-count receipt')
            if pages.get('Total', 0) >= 10000:
                raise ValueError('SmugMug search cap reached; refusing an incomplete candidate list')
            for image in response.get('Image', []):
                photo = photo_candidate(image, result.get('Expansions', {}), target, self.site)
                if photo:
                    photos.append(photo)
            received += len(response.get('Image', []))
            uri = pages.get('NextPage')
            if uri and not response.get('Image'):
                raise ValueError('Empty SmugMug page has more results')
            if not uri and received < pages['Total']:
                raise ValueError('SmugMug results ended before the advertised total')
            # NextPage retains the date filters; expansions must be reapplied.
        return photos


def collect_dates(client, targets, first_year, workers=4):
    candidates = {target: {} for target in targets}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = {executor.submit(client.year_candidates, target, year): target
                 for target in targets for year in range(first_year or target.year, target.year)}
        for finished, task in enumerate(as_completed(tasks), 1):
            target = tasks[task]
            for photo in task.result():
                candidates[target][photo['id']] = photo
            if finished % 10 == 0 or finished == len(tasks):
                print(f'Checked {finished}/{len(tasks)} archive date queries', flush=True)
    return {target: sorted(photos.values(), key=lambda p: (p['takenDate'], p['id']))
            for target, photos in candidates.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, help='one edition date, YYYY-MM-DD')
    parser.add_argument('--site', default='https://photos.hurleyhome.com')
    parser.add_argument('--out', type=Path, default=ROOT / 'photos.json')
    parser.add_argument('--cache-dir', type=Path, default=ROOT / 'data-collect/photo-cache')
    parser.add_argument('--force', action='store_true', help='ignore the 24-hour candidate cache')
    parser.add_argument('--first-year', type=int, help='override discovery of the archive\'s earliest capture year')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8 or (args.first_year is not None and args.first_year < 1):
        parser.error('workers must be 1–8 and first-year must be positive')
    now = datetime.now(timezone.utc)
    today = now.astimezone(DETROIT).date()
    targets = [args.date] if args.date else [today + timedelta(days=i) for i in [-1, 0, 1]]
    checked = now.isoformat().replace('+00:00', 'Z')
    candidates, missing = {}, []
    for target in targets:
        cached = read_json(args.cache_dir / (target.isoformat() + '.json'))
        if (not args.force and cached and cached.get('version') == CACHE_VERSION
                and cached.get('site') == args.site.rstrip('/') and cached.get('firstYearOverride') == args.first_year
                and 0 <= now.timestamp() - cached.get('checkedEpoch', 0) < 86400):
            candidates[target] = cached['photos']
        else:
            missing.append(target)
    if missing:
        client = SmugMug(args.site)
        first_year = args.first_year if args.first_year is not None else client.first_year()
        print(f'Searching public capture dates {first_year or "none"}–{max(d.year for d in missing) - 1}', flush=True)
        fresh = collect_dates(client, missing, first_year, args.workers)
        for target, photos in fresh.items():
            atomic_json(args.cache_dir / (target.isoformat() + '.json'),
                        {'version': CACHE_VERSION, 'site': client.site, 'firstYearOverride': args.first_year,
                         'checkedEpoch': now.timestamp(), 'checkedAt': checked, 'photos': photos})
        candidates.update(fresh)
    previous = read_json(args.out) or {}
    days = {d['date']: d for d in previous.get('days', [])}
    for target, photos in candidates.items():
        # Cache hits retain the successful API check time, not this run's time.
        receipt = read_json(args.cache_dir / (target.isoformat() + '.json')) or {}
        days[target.isoformat()] = {'date': target.isoformat(), 'checkedAt': receipt.get('checkedAt', checked), 'photos': photos}
        print(f'{target}: {len(photos)} candidates' + (' (cached)' if target not in missing else ''), flush=True)
    if not args.date:
        days = {k: v for k, v in days.items() if k in {d.isoformat() for d in targets}}
    ordered = sorted(days.values(), key=lambda d: d['date'])[-62:]
    document = {'schemaVersion': '1.0.0', 'generatedAt': checked, 'timeZone': 'America/Detroit',
                'collectionStart': ordered[0]['date'], 'collectionEnd': ordered[-1]['date'], 'days': ordered}
    if all(previous.get(k) == v for k, v in document.items() if k != 'generatedAt'):
        print('Photo candidates unchanged; retaining existing publication timestamp.')
    else:
        atomic_json(args.out, document)
        print(f'Wrote {args.out}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f'Photo refresh failed; existing photos.json preserved: {error}', file=sys.stderr)
        sys.exit(1)
