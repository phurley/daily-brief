"""Offline identity contract, migration, cancellation and negative controls."""
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import dedupe, feeds, identity, pipeline, publish


def event(**changes):
    r = dict(kind="event", id="old-event", title="Example screening", url="https://venue.test/event/123",
             start="2026-10-23T13:00:00-04:00", venue="Downtown Library", city="Ann Arbor",
             category="Film", summary="A screening.", sourceId="library", sourceAuthority="organizer",
             observedAt="2026-10-09T12:00:00-04:00", timePrecision="time")
    r.update(changes)
    return r


class IdentityTests(unittest.TestCase):
    def resolve(self, *records, registry=None, fuzzy=False):
        return identity.resolve(list(records), registry, fuzzy)

    def test_replay_preserves_ids_and_input(self):
        a, b = event(), event(title="CANCELED Example screening")
        before = copy.deepcopy([a, b])
        out, registry, _ = self.resolve(a, b)
        replay, again, _ = self.resolve(b, a, registry=registry)
        self.assertEqual([a, b], before)
        self.assertEqual(out[0]['id'], replay[0]['id'])
        self.assertEqual(registry, again)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['status'], 'canceled')

    def test_title_correction_retains_native_identity(self):
        a = event(title='Goodbye Politicans', sourceEventId='123')
        out, registry, _ = self.resolve(a)
        b = event(title='Goodbye Politicians', sourceEventId='123')
        new, _, _ = self.resolve(b, registry=registry)
        self.assertEqual(out[0]['id'], new[0]['id'])

    def test_typo_is_review_until_fuzzy_enabled(self):
        a, b = event(title='Goodbye Politicans: A film'), event(title='Goodbye Politicians: A film')
        out, _, report = self.resolve(a, b)
        self.assertEqual(len(out), 2)
        self.assertTrue(report['reviewCandidates'])
        self.assertEqual(len(self.resolve(a, b, fuzzy=True)[0]), 1)

    def test_same_title_distinct_time_venue_city_date_recurrence(self):
        for changes in ({'start':'2026-10-23T15:00:00-04:00'}, {'venue':'West Branch'},
                        {'city':'Detroit'}, {'start':'2026-10-24T13:00:00-04:00'},
                        {'recurrenceId':'instance-b'}, {'sourceEventId':'different'}):
            with self.subTest(changes=changes):
                a = event(sourceEventId='123', recurrenceId='instance-a')
                b = dict(a, **changes)
                self.assertEqual(len(self.resolve(a, b)[0]), 2)

    def test_unknown_time_not_equality(self):
        for time in (None, '2026-10-23T00:00:00-04:00'):
            a, b = event(start=time, timePrecision='unknown'), event(summary='Other text', start=time, timePrecision='unknown')
            self.assertEqual(len(self.resolve(a, b)[0]), 2)

    def test_listing_url_does_not_merge_activities(self):
        a = event(url='https://venue.test/events', title='Family movie')
        b = dict(a, title='Family movies')
        self.assertEqual(len(self.resolve(a, b, fuzzy=True)[0]), 2)

    def test_conflicting_performers_prevent_merge(self):
        a, b = event(performers=['Red Leather', 'Band A']), event(performers=['Red Leather', 'Band B'])
        self.assertEqual(len(self.resolve(a, b)[0]), 2)

    def test_cancellation_beats_richness_and_implicit_later_active(self):
        canceled = event(title='CANCELED Example screening')
        active = event(summary='A much richer active summary', imageUrl='https://venue.test/image.jpg',
                       observedAt='2026-10-10T12:00:00-04:00')
        out, _, _ = self.resolve(canceled, active)
        self.assertEqual(out[0]['status'], 'canceled')
        self.assertIn('imageUrl', out[0])
        self.assertEqual(len(out[0]['sources']), 2)
        self.assertIn('status', out[0]['identity']['fields'])

    def test_explicit_reinstatement_and_authority(self):
        a = event(status='canceled', statusEvidence='Event canceled', sourceAuthority='ticket')
        b = event(status='scheduled', statusEvidence='Event reinstated', observedAt='2026-10-10T12:00:00-04:00')
        self.assertEqual(self.resolve(a,b)[0][0]['status'], 'scheduled')

    def test_reschedule_requires_native_and_previous_start(self):
        a = event(sourceEventId='123')
        b = event(sourceEventId='123', start='2026-10-24T13:00:00-04:00', status='rescheduled',
                  statusEvidence='Moved to Saturday', observedAt='2026-10-10T12:00:00-04:00')
        out, registry, _ = self.resolve(a)
        self.assertEqual(len(self.resolve(a,b)[0]), 2)
        b['previousStart'] = a['start']
        moved, _, _ = self.resolve(a,b, registry=registry)
        self.assertEqual(len(moved), 1)
        self.assertEqual(out[0]['id'], moved[0]['id'])
        self.assertEqual(moved[0]['start'], b['start'])

    def test_preserve_meaningful_url_parameters(self):
        url='https://Venue.test/event?id=123&utm_source=x&date=2026-10-23#top'
        self.assertEqual(identity.canonical_url(url), 'https://venue.test/event?date=2026-10-23&id=123')
        self.assertNotEqual(identity.canonical_url(url), identity.canonical_url(url.replace('123','124')))

    def test_unicode_punctuation_equivalence(self):
        self.assertEqual(identity.normalize('CAFÉ — Night!'), identity.normalize('Café Night'))

    def test_pipeline_ids_no_longer_collide(self):
        origin={'source_slug':'library', 'url':'https://venue.test/events', 'text':'Example screening', 'chunk_id':'one'}
        a=pipeline.finalize(event(), origin)
        b=pipeline.finalize(event(start='2026-10-23T15:00:00-04:00'), origin)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(a['id'], pipeline.finalize(event(), origin)['id'])
        self.assertTrue(a['aliases'])

    def test_feed_native_fields_survive(self):
        body='# Calendar feed (ICS): https://venue.test/calendar.ics\n\n- 2026-10-23 17:00Z: Example screening @ Library\n  uid: abc\n  recurrence_id: 20261023T170000Z\n  status: CANCELLED\n  last_modified: 20261009T120000Z\n'
        parsed=feeds.parse_ics_markdown(body)[0]
        self.assertEqual(parsed['sourceEventId'], 'abc')
        self.assertEqual(parsed['recurrenceId'], '20261023T170000Z')
        out=pipeline.finalize(event(), dict(parsed, source_slug='library'))
        self.assertEqual(out['sourceEventId'], 'abc')
        self.assertEqual(out['status'], 'canceled')

    def test_pre_extraction_does_not_suppress_recurrence(self):
        records=[dict(candidate_hint='event', heading='A long weekly event title for families', text='Identical description',
                      start=day, source_slug='library', item_id=day)
                 for day in ('2026-10-23','2026-10-24')]
        dedupe._annotate(records, threshold=3, max_group_size=15)
        self.assertTrue(all(r['is_canonical'] for r in records))
        records[1]=dict(records[0], item_id='copy')
        dedupe._annotate(records, threshold=3, max_group_size=15)
        self.assertEqual(sum(r['is_canonical'] for r in records), 1)

    def test_canonical_schema_and_shadow_equivalence(self):
        records=[event(),event(title='CANCELED Example screening')]
        args=('2026-10-09','2026-10-09T16:00:00Z')
        canonical, _, stats=publish.build(records,*args,identity_mode='canonical')
        self.assertTrue(publish._doc_validator('events.schema.json').is_valid(canonical))
        self.assertEqual(len(canonical['events']), 1)
        legacy=publish.build(records,*args)
        shadow=publish.build(records,*args,identity_mode='shadow')
        self.assertEqual(legacy[:2],shadow[:2])
        self.assertEqual(len(stats['identity']['mappings']), 2)

    def test_performer_list_variants_cross_url(self):
        a = event(title='Red Leather with The Romance and Northern Lights', category='Music')
        b = event(title='Red Leather , The Romance, Northern Lights', category='Music',
                  url='https://tickets.test/event/77', city='Ann Arbor, MI')
        self.assertEqual(len(self.resolve(a,b)[0]), 1)

    def test_feed_update_fingerprint_changes_without_full_version_bump(self):
        a = dict(_kind='feed', source_slug='library', item_id='one', summary='Event scheduled')
        b = dict(a, summary='Event canceled')
        self.assertNotEqual(pipeline._fingerprint(a), pipeline._fingerprint(b))
        self.assertEqual(pipeline._fingerprint(a), pipeline._fingerprint(dict(a, age_days=2)))

    def test_no_fabricated_end(self):
        out, _, _=self.resolve(event())
        self.assertNotIn('end',out[0])

if __name__ == '__main__':
    unittest.main()
