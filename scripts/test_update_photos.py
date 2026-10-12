import importlib.util
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('photos', Path(__file__).with_name('update_photos.py'))
photos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(photos)


def fixture(key='one', original='2015-10-13T01:00:00Z', capture='2015-10-12T18:00:00'):
    image = {'ImageKey': key, 'DateTimeOriginal': original,
             'OriginalWidth': 2400, 'OriginalHeight': 1600,
             'DateTimeUploaded': '2020-10-12T00:00:00Z',
             'Uris': {'ImageMetadata': f'/metadata/{key}', 'ImageSizes': f'/sizes/{key}',
                      'ImageSizeDetails': f'/details/{key}'}}
    expansions = {f'/metadata/{key}': {'ImageMetadata': {'DateTimeCreated': capture,
                  'CreatorContactInfo': {'CiAdrExtadr': 'private contact'}, 'Latitude': 42}},
                  f'/sizes/{key}': {'ImageSizes': {'X2LargeImageUrl': f'https://photos.smugmug.com/{key}.jpg'}},
                  f'/details/{key}': {'ImageSizeDetails': {'ImageSizeX2Large': {
                      'Url': f'https://photos.smugmug.com/{key}.jpg', 'Width': 1280, 'Height': 853}}}}
    return image, expansions


class PhotosTests(unittest.TestCase):
    def test_original_camera_day_overrides_normalized_api_day(self):
        image, expansions = fixture()
        result = photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com')
        self.assertEqual(result['takenDate'], '2015-10-12')
        self.assertEqual(result['takenDateSource'], 'ImageMetadata.DateTimeCreated')
        self.assertEqual(result['id'], 'smugmug-one')
        self.assertEqual(result['location'], '')
        self.assertNotIn('private contact', json.dumps(result))
        self.assertNotIn('Latitude', result)

    def test_reject_wrong_dates_current_year_videos_and_hidden_images(self):
        for change in [{'DateTimeOriginal': None}, {'IsVideo': True}, {'Hidden': True}, {'Processing': True}]:
            image, expansions = fixture(capture='')
            image.update(change)
            self.assertIsNone(photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com'))
        for capture in ['2015-10-11T20:00:00', '2026-10-12T12:00:00']:
            image, expansions = fixture(capture=capture)
            self.assertIsNone(photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com'))

    def test_upload_date_never_becomes_capture_date(self):
        image, expansions = fixture(original=None, capture='')
        self.assertIsNone(photos.capture_date(image, {} )[0])

    def test_uses_served_dimensions_after_source_rotation_and_keeps_portraits(self):
        image, expansions = fixture()
        expansions['/details/one']['ImageSizeDetails']['ImageSizeX2Large'].update(Width=853, Height=1280)
        result = photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com')
        self.assertEqual((result['width'], result['height']), (853, 1280))

    def test_falls_back_to_original_dimensions_and_rejects_unknown_dimensions(self):
        image, expansions = fixture()
        expansions.pop('/details/one')
        result = photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com')
        self.assertEqual((result['width'], result['height']), (2400, 1600))
        for width in [None, 0, -1, True, '2400']:
            image['OriginalWidth'] = width
            with self.assertRaises(ValueError):
                photos.photo_candidate(image, expansions, date(2026, 10, 12), 'https://photos.hurleyhome.com')

    def test_all_pages_and_more_than_five_candidates(self):
        client = object.__new__(photos.SmugMug)
        client.site = 'https://photos.hurleyhome.com'
        client.scope = '/api/v2/user/hurleyhome'
        results = []
        for n in [0, 1]:
            images, expansions = [], {}
            for k in range(n * 6, (n + 1) * 6):
                image, extra = fixture(str(k)); images.append(image); expansions.update(extra)
            pages = {'Total': 12}
            if n == 0:
                pages['NextPage'] = '/api/v2/image!search?start=7'
            results.append({'Response': {'Image': images, 'Pages': pages}, 'Expansions': expansions})
        calls = []
        def get(uri, params):
            calls.append((uri, params)); return results.pop(0)
        client.get = get
        found = client.year_candidates(date(2026, 10, 12), 2015)
        self.assertEqual(len(found), 12)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]['_expand'], 'ImageMetadata,ImageSizes,ImageSizeDetails')
        self.assertEqual(calls[0][1]['DateTakenStart'], '2015-10-11T00:00:00Z')

    def test_incomplete_pagination_and_search_cap_fail(self):
        client = object.__new__(photos.SmugMug)
        client.site, client.scope = 'https://photos.hurleyhome.com', '/api/v2/user/hurleyhome'
        for total in [3, 10000]:
            client.get = lambda *args: {'Response': {'Pages': {'Total': total}, 'Image': []}}
            with self.assertRaises(ValueError):
                client.year_candidates(date(2026, 10, 12), 2015)

    def test_leap_day_only_checks_leap_years(self):
        client = object.__new__(photos.SmugMug)
        client.get = lambda *args: self.fail('non-leap year must not call API')
        self.assertEqual(client.year_candidates(date(2028, 2, 29), 2027), [])

    def test_overlap_deduplicates_by_stable_id(self):
        class Client:
            def year_candidates(self, target, year):
                return [{'id': 'same', 'takenDate': '2015-10-12'}]
        result = photos.collect_dates(Client(), [date(2026, 10, 12)], 2020)
        self.assertEqual(len(result[date(2026, 10, 12)]), 1)

    def test_failed_collection_keeps_previous_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'photos.json'
            output.write_text('{"original":"preserve"}')
            with patch('sys.argv', ['update_photos.py', '--date', '2026-10-12', '--out', str(output),
                                    '--cache-dir', str(Path(directory) / 'cache')]), \
                 patch.object(photos, 'SmugMug', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    photos.main()
            self.assertEqual(output.read_text(), '{"original":"preserve"}')


if __name__ == '__main__':
    unittest.main()
