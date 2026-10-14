import copy
from datetime import datetime, timedelta
import json
import unittest
from review_stories import (TZ, DEFAULT_POLICY, publication, source_page, validate_review,
                            run_review, choose_days, input_facts)


class StoryEditorTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, 12, tzinfo=TZ)
        self.story = {'id': 'library', 'title': 'Plymouth library hours',
                      'url': 'https://example.org/library', 'publishedAt': '2026-10-14T00:00:00-04:00',
                      'summary': 'Unverified feed summary', 'localityIndex': 0, 'source': {'name': 'Library'}}
        self.html = '''<html><head><meta property="article:published_time" content="2026-10-13T10:00:00-04:00"></head>
            <body><article><h1>Plymouth library hours</h1><p>The Plymouth library will extend evening opening hours to give residents more time to visit after work.</p>
            <p>The additional hours will improve access to books, study spaces and public computers for people with daytime commitments.</p></article></body></html>'''
        self.decision = {'include': True, 'relevance': 5, 'novelty': 4,
            'summary': 'The Plymouth library will extend its evening opening hours, giving residents more time to visit after work. The change improves access to books, study spaces and public computers for people with daytime commitments.',
            'reason': 'A practical change to nearby public services.', 'storyKey': 'plymouth-library-hours',
            'localConnection': 'Plymouth public library', 'factEvidence': ['E0', 'E1'], 'localEvidence': 'E0'}
        self.page = source_page(self.html, self.story['url'], self.now)

    def run_editor(self, previous=None, getter=None, generate=None, **kwargs):
        return run_review([self.story], previous or {}, 'news', self.now,
                          getter or (lambda url: self.html), generate or (lambda *args: self.decision),
                          DEFAULT_POLICY, **kwargs)

    def test_official_date_corrects_event_date_and_keeps_input_for_invalidation(self):
        reviewed = self.run_editor()['stories'][0]
        self.assertEqual(reviewed['publishedAt'], '2026-10-13T10:00:00-04:00')
        self.assertEqual(reviewed['editorial']['inputFacts'], input_facts(self.story))
        self.assertEqual(reviewed['editorial']['publicationEvidence'], '2026-10-13T10:00:00-04:00')

    def test_related_story_cards_do_not_replace_the_main_article_body(self):
        raw = self.html.replace('<article>', '<article><h2>Unrelated police drone story</h2></article><article itemprop="articleBody">', 1)
        page = source_page(raw, self.story['url'], self.now, title=self.story['title'])
        self.assertIn('Plymouth library', page['text'])
        self.assertNotIn('police drone', page['text'])

    def test_redirected_or_wrong_article_cannot_supply_evidence(self):
        with self.assertRaisesRegex(ValueError, 'headline'):
            source_page(self.html, self.story['url'], self.now, title='Drone assists police with a Dearborn arrest')

    def test_related_article_dates_and_modification_dates_cannot_supply_publication(self):
        other = {'@type': 'NewsArticle', 'url': 'https://example.org/other', 'datePublished': '2026-10-14T12:00:00-04:00'}
        raw = '<meta property="article:modified_time" content="2026-10-14"><script type="application/ld+json">' + json.dumps(other) + '</script>'
        with self.assertRaisesRegex(ValueError, 'publication date'):
            publication(raw, self.story['url'], self.now)

    def test_conflicting_and_future_dates_are_rejected(self):
        for date in ['2026-10-12T10:00:00-04:00', '2026-10-15T10:00:00-04:00']:
            html = self.html + f'<meta property="article:published_time" content="{date}">'
            with self.subTest(date=date), self.assertRaises(ValueError):
                publication(html, self.story['url'], self.now)
        with self.assertRaisesRegex(ValueError, 'future'):
            publication(self.html.replace('2026-10-13', '2026-10-15'), self.story['url'], self.now)

    def test_visible_official_publication_date_and_date_only_precision(self):
        raw = '<article><p>Published: October 7, 2026 at 1:16 PM</p></article>'
        date, _, precision = publication(raw, self.story['url'], self.now)
        self.assertEqual(date.isoformat(), '2026-10-07T13:16:00-04:00')
        self.assertEqual(precision, 'time')
        date, _, precision = publication('<meta property="article:published_time" content="2026-10-13">', self.story['url'], self.now)
        self.assertEqual(precision, 'date')

    def test_invented_evidence_unsupported_numbers_and_missing_local_connection_fail(self):
        for update in [{'factEvidence': ['E999']}, {'localEvidence': ''}, {'relevance': 2},
                       {'summary': self.decision['summary'] + ' This adds 17 new hours.'}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_review(self.story, {**self.decision, **update}, self.page, 'news', self.now, 'policy')

    def test_rejection_does_not_fill_empty_slots(self):
        doc = self.run_editor(generate=lambda *args: {**self.decision, 'include': False, 'reason': 'Remote road closure.'})
        self.assertEqual(doc['stories'], [])
        self.assertEqual(doc['days'][1]['selectedIds'], [])
        self.assertIn('Remote road closure', doc['rejections'][0]['reason'])

    def test_grounding_rejects_a_grant_without_retrying_to_force_inclusion(self):
        calls = []
        doc = self.run_editor(generate=lambda *args: calls.append('edit') or self.decision,
            verify=lambda *args: {'supported': True, 'substantive': False, 'reason': 'Only a grant, no new results.'})
        self.assertEqual(doc['stories'], [])
        self.assertEqual(calls, ['edit'])

    def test_grounding_copy_repair_is_bounded_and_success_is_recorded(self):
        calls = []
        def check(*args):
            calls.append('check')
            return {'supported': len(calls) > 1, 'substantive': True, 'reason': 'All claims supported.'}
        doc = self.run_editor(verify=check)
        self.assertEqual(len(calls), 2)
        self.assertTrue(doc['stories'][0]['editorial']['grounded'])
        doc = self.run_editor(verify=lambda *args: {'supported': False, 'substantive': True, 'reason': 'Target presented as measured result.'})
        self.assertEqual(doc['stories'], [])
        self.assertIn('Target presented as measured result', doc['rejections'][0]['reason'])

    def test_cached_acceptance_and_rejection_avoid_network_and_model_calls(self):
        def forbidden(*args): raise AssertionError('Cached decision made a paid call')
        for include in [True, False]:
            doc = self.run_editor(generate=lambda *args: {**self.decision, 'include': include})
            refreshed = self.run_editor(doc, getter=forbidden, generate=forbidden)
            self.assertEqual(len(refreshed['stories']), int(include))
            self.assertEqual(len(refreshed['rejections']), int(not include))

    def test_changed_source_and_policy_require_new_review(self):
        doc = self.run_editor()
        self.story['summary'] = 'Materially changed feed content'
        changed = self.run_editor(doc, generate=lambda *args: {**self.decision, 'include': False})
        self.assertEqual(changed['stories'], [])
        changed = run_review([self.story], doc, 'news', self.now, lambda url: self.html,
                             lambda *args: {**self.decision, 'include': False}, {'newsFocus': ['Different policy']})
        self.assertEqual(changed['stories'], [])

    def test_daily_order_stays_stable_and_duplicates_do_not_fill_quota(self):
        first = self.run_editor()['stories'][0]
        second = copy.deepcopy(first); second['id'] = 'second'; second['editorial']['storyKey'] = 'another-development'
        second['editorial']['novelty'] = 5
        prior = {'days': [{'date': '2026-10-14', 'selectedIds': ['library']}]}
        days = choose_days([first, second, {**first, 'id': 'duplicate'}], prior, 'news', self.now)
        self.assertEqual(days[1]['selectedIds'], ['library', 'second'])
        self.assertEqual(days[2]['selectedIds'][0], 'second')
        self.assertEqual(len(days[2]['selectedIds']), 2)

    def test_shortlist_is_bounded_and_future_feed_dates_are_not_paid_for(self):
        stories = [{**self.story, 'id': f'story-{i}', 'url': f'https://example.org/{i}', 'source': {'name': str(i)}} for i in range(12)]
        stories += [{**self.story, 'id': 'future', 'publishedAt': '2031-09-30T00:00:00-04:00'}]
        calls = []
        doc = run_review(stories, {}, 'news', self.now, lambda url: self.html,
                         lambda story, *args: calls.append(story['id']) or self.decision, DEFAULT_POLICY, limit=4)
        self.assertEqual(len(calls), 4)
        self.assertNotIn('future', calls)


if __name__ == '__main__':
    unittest.main()
