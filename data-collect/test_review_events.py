import copy
from datetime import datetime, timedelta
import unittest
from review_events import validate_review, page_evidence, review_candidates, input_facts, TZ


class FeaturedReviewTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 12, tzinfo=TZ)
        self.candidate = {'id': 'stories', 'title': 'String of Stories',
            'url': 'https://events.example.org/event/stories', 'start': '2026-10-13T17:00:00-04:00',
            'venue': '202 S. Thayer', 'city': 'Ann Arbor', 'status': 'scheduled', 'score': 83}
        self.html = '''<main><h1>String of Stories</h1><p>October 13, 2026</p>
            <p>5:00pm - 7:00pm</p><p>202 S. Thayer, Ann Arbor</p><p>Free</p>
            <p>Register to attend</p><a href="https://events.example.org/register">Register</a>
            <p>Learn about African history through beads and string your own selection.</p></main>'''
        self.result = {'isUpcomingEvent': True, 'title': 'String of Stories',
            'start': '2026-10-13T17:00:00-04:00', 'end': '2026-10-13T19:00:00-04:00',
            'venue': '202 S. Thayer', 'city': 'Ann Arbor', 'price': 'Free',
            'registration': 'Register to attend', 'bookingUrl': 'https://events.example.org/register',
            'summary': 'Explore African history through beads in a hands-on storytelling workshop. Participants learn how visual materials preserve cultural history, then string their own selection to take home. It combines an unusual art encounter with the pleasure of making something personal.',
            'quotes': {'date': 'October 13, 2026', 'time': '5:00pm', 'end': '7:00pm',
                'venue': '202 S. Thayer', 'admission': 'Free', 'registration': 'Register to attend',
                'description': 'Learn about African history through beads and string your own selection.'}}
        self.page = page_evidence(self.html, self.candidate['url'])

    def valid(self, result=None):
        return validate_review(self.candidate, result or self.result, self.page, self.now)

    def test_verified_facts_replace_incomplete_fields_and_keep_evidence(self):
        event = self.valid()
        self.assertEqual(event['priceMin'], 0)
        self.assertEqual(event['editorial']['inputFacts'], input_facts(self.candidate))
        self.assertEqual(event['timePrecision'], 'time')
        self.assertEqual(event['editorial']['sourceUrl'], self.candidate['url'])

    def test_fabricated_quotes_wrong_address_and_wrong_time_are_rejected(self):
        for update in [{'quotes': {**self.result['quotes'], 'admission': 'Free admission for everyone'}},
                       {'venue': '412 Maynard St.'}, {'start': '2026-10-13T18:00:00-04:00'},
                       {'start': '2026-10-12T17:00:00-04:00'}, {'price': '$20'}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.valid({**self.result, **update})

    def test_registration_cannot_be_omitted_or_link_invented(self):
        for update in [{'registration': ''}, {'bookingUrl': ''}, {'bookingUrl': 'https://evil.example/register'}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.valid({**self.result, **update})

    def test_unknown_end_is_removed_and_elapsed_unknown_end_is_rejected(self):
        result = {**self.result, 'end': None, 'quotes': {**self.result['quotes'], 'end': ''}}
        self.assertNotIn('end', self.valid(result))
        with self.assertRaisesRegex(ValueError, 'actionable'):
            validate_review(self.candidate, result, self.page, datetime(2026, 10, 13, 18, tzinfo=TZ))

    def test_completed_retrospective_and_long_raw_copy_are_rejected(self):
        with self.assertRaises(ValueError): self.valid({**self.result, 'isUpcomingEvent': False})
        with self.assertRaises(ValueError): self.valid({**self.result, 'summary': ' '.join(['word'] * 100)})
        with self.assertRaises(ValueError): self.valid({**self.result, 'summary': self.result['summary'] + ' Check the listing.'})

    def test_recent_reviews_reuse_evidence_without_network_or_model(self):
        old = self.valid()
        def forbidden(*args): raise AssertionError('Unnecessary external call')
        doc = review_candidates([self.candidate], {'events': [old]}, self.now + timedelta(hours=1), forbidden, forbidden)
        self.assertEqual(doc['events'], [old])
        changed = {**self.candidate, 'start': '2026-10-13T18:00:00-04:00'}
        doc = review_candidates([changed], {'events': [old]}, self.now + timedelta(hours=1), forbidden, forbidden)
        self.assertEqual(doc['events'], [])

    def test_transient_failure_retains_recent_review_but_not_expired_review(self):
        old = self.valid()
        def unavailable(*args): raise OSError('temporarily unavailable')
        doc = review_candidates([self.candidate], {'events': [old]}, self.now + timedelta(hours=22), unavailable, unavailable)
        self.assertEqual(doc['events'], [old])
        doc = review_candidates([self.candidate], {'events': [old]}, self.now + timedelta(hours=25), unavailable, unavailable)
        self.assertEqual(doc['events'], [])


if __name__ == '__main__': unittest.main()
