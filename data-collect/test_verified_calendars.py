"""Offline regressions using the shapes exposed by the public organizer feeds."""
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

import verified_calendars as calendars
import refresh_event_changes as refresh
import calendar_health
from extract import evidence, feeds, funnel, identity, pipeline, publish

NOW = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo('America/Detroit'))


def source(adapter, **config):
    return dict(name='Test venue', url='https://venue.test/events', category='Theatre',
                verified_calendar=dict(adapter=adapter, venue='Test venue', city='Ann Arbor', **config))


def collect(adapter, data, **config):
    return calendars.collect(source(adapter, **config), reference_day='2026-10-12',
        getter=lambda *args: json.dumps(data) if not isinstance(data,str) else data)


def event(**extra):
    return dict(kind='event', id='test-event', title='Test concert', summary='A concert.',
                start='2026-11-01T18:30:00-05:00', venue='Test venue', city='Ann Arbor', category='Music',
                url='https://venue.test/event/test', sourceId='test-venue', **extra)


class CalendarTests(unittest.TestCase):
    def test_tribe_pagination_prices_sets_and_announcement(self):
        item = dict(id=1, title='Alicia Witt', description='This show has been postponed to Friday, April 30, 2027 at 8pm.',
                    start_date='2026-11-01 19:30:00',end_date='2026-11-01 23:00:00',cost='$25',
                    url='https://venue.test/event/alicia',categories=[{'name':'Postponed'}],venue={})
        calls=[]
        def get(url,*args):
            calls.append(url)
            return json.dumps({'events':[item], 'next_rest_url': 'https://venue.test/page2' if len(calls)==1 else None})
        result=calendars.collect(source('tribe'),reference_day='2026-10-12',getter=get)
        self.assertEqual(len(calls),2)
        self.assertTrue(result.coverage['complete'])
        self.assertEqual(len(result.events),1)
        self.assertTrue(result.events[0]['eventStatus'].endswith('EventPostponed'))
        self.assertIn('April 30',result.events[0]['statusEvidence'])
        item.update(description='Sets 7pm & 8:30pm',categories=[])
        result=collect('tribe',{'events':[item]})
        self.assertEqual([e['startDate'][11:16] for e in result.events],['19:00','20:30'])
        self.assertTrue(all('endDate' not in e for e in result.events))

    def test_failed_page_keeps_discovered_slots_and_flags_gap(self):
        item=dict(id=1,title='Show',description='Music',start_date='2026-11-01 19:00:00',url='https://venue.test/e/1')
        calls=[]
        def get(url,*args):
            calls.append(url)
            if len(calls)>1: raise TimeoutError('offline')
            return json.dumps({'events':[item],'next_rest_url':'https://venue.test/page2'})
        result=calendars.collect(source('tribe'),reference_day='2026-10-12',getter=get)
        self.assertEqual(result.coverage['status'],'partial')
        self.assertFalse(result.coverage['complete'])
        self.assertEqual(len(result.events),1)

    def test_crowdwork_individual_dates_fee_inclusive_price(self):
        result=collect('crowdwork',{'status':200,'data':[dict(id=42,name='Joe Greer',url='https://www.crowdwork.com/e/joe',
            dates=['2026-11-01T18:30:00-05:00','2026-11-08T18:30:00-05:00'],venue='Black Box',
            description={'body':'Comedy'},cost_tiers=[{'cost':1000,'cost_after_fees':1184}],status='active')]})
        self.assertEqual(len(result.events),2)
        self.assertEqual(result.events[0]['priceText'],'$11.84 including fees')
        self.assertNotIn('endDate',result.events[0])

    def test_season_current_heading_beats_stale_title_and_conflicting_year_is_held(self):
        body='<title>2025-2026 Season</title><h2>CET’s 2026-2027 Season</h2><strong>Cyrano</strong><p>Performances: November 12-15, 2026</p><strong>Alice</strong><p>Performances: March 11-14, 2026</p>'
        result=collect('season',body)
        self.assertEqual(len(result.events),4)
        self.assertEqual(result.events[0]['name'],'Cyrano')
        self.assertEqual(result.events[0]['startDate'],'2026-11-12')
        self.assertIn('conflicts',result.coverage['unresolved'][0]['reason'])

    def test_park_multiple_months_and_school_names(self):
        s=source('season'); s['name']='The Park Players Theatre Company'
        body='<title>2026-2027 Season</title><h2>"Game Show"</h2><h2>February 26, 27, March 5, 6</h2><p>Canton High School</p>'
        result=calendars.collect(s,reference_day='2027-02-01',getter=lambda *args:body)
        self.assertEqual([e['startDate'] for e in result.events],['2027-02-26','2027-02-27','2027-03-05','2027-03-06'])
        self.assertEqual({e['name'] for e in result.events},{'Game Show'})
        self.assertEqual(result.events[0]['location']['name'],'Canton High School')

    def test_northville_ordinal_days_keep_title_and_unknown_time(self):
        s=source('season');s['name']='Northville High School Drama Club'
        body='<h2>2026 - 2027 Season</h2><h2>NHS Theatre Fall Play<br>Arsenic and Old Lace</h2><p>November 12th @ 7:00</p><p>November 13th @ 2:00</p><p>November 14th @ 2:00 &amp; @7:00</p>'
        result=calendars.collect(s,reference_day='2026-10-12',getter=lambda *args:body)
        self.assertEqual(len(result.events),3)
        self.assertEqual({e['name'] for e in result.events},{'Arsenic and Old Lace'})
        self.assertEqual(len(result.coverage['unresolved']),1)
        self.assertTrue(all('T' not in e['startDate'] for e in result.events))

    def test_ovation_production_summary_does_not_become_continuous_event(self):
        production={'id':1,'productionName':'Play','description':'A play','venue':{'name':'Village Theater','address':{'city':'Canton'}},'performanceSummary':{'next':'2026-11-01','last':'2026-11-07'}}
        calendar=[{'productions':[{'productionId':1,'name':'Play','showtimes':[
            {'performanceId':2,'performanceStartTime':'2026-11-01T14:00:00-05:00'},
            {'performanceId':3,'performanceStartTime':'2026-11-07T19:00:00-05:00','isSoldOut':True,'remainingTix':0}]}]}]
        result=calendars.collect(source('ovationtix',clientId='36533'),reference_day='2026-10-12',
            getter=lambda url,*args:json.dumps(calendar if url.endswith('CalendarProductions') else [production]))
        self.assertEqual(len(result.events),2)
        self.assertTrue(all('endDate' not in e for e in result.events))
        self.assertTrue(result.events[1]['offers']['availability'].endswith('SoldOut'))

    def test_semja_month_uses_keyed_popup_and_explicit_end(self):
        body='<table class="main"><td class="hasevents"><a class="dayofmonth" href="day.php?date=20261101">1</a><a class="entry" id="pop1-0" href="view_entry.php?id=1&amp;date=20261101">6:00pm» Hayden Cusmano Trio</a></td></table><dl id="eventinfo-pop1-0"><dt>Time:</dt><dd>6:00pm - 9:00pm</dd><dt>Location:</dt><dd>Ebenezer Cocktail Lounge | Plymouth</dd><dt>Description:</dt><dd>Jazz trio.</dd></dl>'
        result=calendars.collect(source('semja'),reference_day='2026-11-01',horizon_days=30,getter=lambda *args:body)
        self.assertEqual(result.coverage['requests'],1)
        self.assertEqual(result.events[0]['endDate'],'2026-11-01T21:00:00-05:00')
        self.assertEqual(result.events[0]['location']['address']['addressLocality'],'Plymouth')
        broken=collect('semja','<table class="main"></table>')
        self.assertEqual(broken.coverage['status'],'failed')

    def test_dso_zero_unavailable_zones_do_not_become_free(self):
        result=collect('dso',[dict(perf_no=1,prod_season_no=2,title='Enigma',perf_date='2026-11-01T15:00:00-05:00',
            facility='Orchestra Hall',min_price=20.95,max_price=114.66,event_url='https://dso.test/event/enigma',summary='Music',prices=[{'price':'0','available':'N'}])])
        self.assertEqual(result.events[0]['priceText'],'$20.95–$114.66')

    def test_nature_walk_markup_punctuation_and_free_policy(self):
        result=collect('nature','<p>There is no cost or registration for our public nature or mushroom walks</p><p>Nature Society <strong>“Fall Color</strong> <strong>Walk”</strong> on Sunday, <strong>November 1</strong>, 2026, at Tenhave Woods beginning at <strong>2:00 pm</strong>.</p>')
        self.assertEqual(result.events[0]['startDate'],'2026-11-01T14:00:00-05:00')
        self.assertEqual(result.events[0]['priceText'],'Free')
        self.assertEqual(result.events[0]['registration'],'No registration required')

    def test_atg_expands_advertised_schedule_across_dst(self):
        obj={'@type':'TheaterEvent','name':'Maybe Happy Ending','startDate':'2026-10-20T19:30:00-04:00','description':'A musical','image':['https://venue.test/i.png']}
        detail='<script type="application/ld+json">'+json.dumps(obj)+'</script><p>Schedule Tue, Oct 20 - Sun, Nov 1, 2026 Tues - Sat, 7:30 PM ET Sat, 2:00 PM ET Sun, 1:00 PM &amp; 6:30 PM ET Run time 100 minutes</p>'
        s=source('atg')
        result=calendars.collect(s,reference_day='2026-10-12',getter=lambda url,*args:'<a href="/events/maybe/fisher-theatre/">Tickets</a>' if url==s['url'] else detail)
        nov1=[e for e in result.events if e['startDate'].startswith('2026-11-01')]
        self.assertEqual([e['startDate'] for e in nov1],['2026-11-01T13:00:00-05:00','2026-11-01T18:30:00-05:00'])
        self.assertTrue(all('endDate' not in e for e in result.events))

    def test_sub_events_and_free_offers_survive_to_public_contract(self):
        obj={'@type':'TheaterEvent','name':'Play','description':'A play','url':'https://venue.test/event/play','startDate':'2026-11-01','endDate':'2026-11-07',
            'location':{'name':'Stage','address':{'addressLocality':'Canton'}},'offers':{'price':0,'priceCurrency':'USD'},
            'subEvent':[{'startDate':'2026-11-01T14:00:00-05:00'},{'startDate':'2026-11-07T19:00:00-05:00'}]}
        items=feeds.parse_jsonld_markdown('# Structured data (JSON-LD): https://venue.test/events\n```json\n'+json.dumps(obj)+'\n```')
        self.assertEqual(len(items),2)
        self.assertTrue(all(not i['end'] for i in items))
        record=pipeline.finalize(pipeline.structured_event(items[0]),dict(items[0],source_slug='test-venue'))
        self.assertTrue(pipeline.valid_record(record))
        self.assertEqual(publish.to_event(record)['priceMin'],0)

    def test_verified_events_bypass_gate_generation_caps_and_recurrence_guess(self):
        result=collect('crowdwork',{'status':200,'data':[dict(id=1,name='Improv',url='https://venue.test/e/improv',venue='Black Box',dates=['2026-11-01T19:00:00-05:00'],description={'body':'Every Sunday night, comedy!'},status='active')]})
        item=feeds.parse_jsonld_markdown(result.markdown())[0]
        item.update(source_slug='test-venue',source_name='Venue',_kind='feed')
        specs={'test-venue':{'verified_calendar':True}}
        with patch.object(pipeline.jev,'make_client',side_effect=AssertionError('No gate')):
            rows,stats,outcomes=pipeline.run([item],workers=1,extract_limit=0,model=None,source_specs=specs,mechanical_only=True)
        self.assertEqual(stats['completed'],1)
        self.assertNotIn('schedule',rows[0])
        self.assertEqual(outcomes[0][1],'extracted')
        picked,_,_=pipeline.select_work([item],{}, {},[],NOW,max_items=0,retry_limit=0,max_attempts=3,replay_days=14,stale_days=2,source_specs=specs)
        self.assertEqual(len(picked),1)

    def test_local_calendar_biography_cannot_move_the_event_to_another_state(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);crawl=root/'crawl/test-venue';crawl.mkdir(parents=True)
            result=collect('crowdwork',{'status':200,'data':[dict(id=1,name='Touring artist',venue='Stage',
                url='https://venue.test/event/artist',dates=['2026-11-01T19:00:00-05:00'],description={'body':'An artist from Virginia.'})]})
            (crawl/'content.md').write_text('<!-- jsonld 1 -->\n'+result.markdown())
            (crawl/'meta.json').write_text(json.dumps({'crawl':{'pages':[{'kind':'jsonld','index':1,'url':result.url,'status_code':200}]}}))
            opts=funnel.build_parser().parse_args(['--reference-date','2026-10-12'])
            funnel.process_source('test-venue',dict(source('crowdwork'),locality_index=2,location='Ann Arbor, MI'),root/'crawl',root/'processed',opts)
            items=pipeline.load_candidates(root/'processed')
            self.assertEqual(len(items),1)
            self.assertFalse(items[0]['signals']['locality']['out_of_area'])

    def test_future_budget_and_unchanged_check_fingerprint(self):
        def item(d):return dict(_kind='feed',source_slug='test-venue',item_id=d,start=d+'T18:00:00-04:00')
        items=[item('2026-10-12') for _ in range(100)]+[item('2026-10-25'),item('2026-11-30')]
        picked=pipeline.select(items,10,NOW)
        self.assertIn('2026-11-30', {r['start'][:10] for r in picked})
        a=item('2026-11-01');a.update(verifiedCalendar=True,price='$20',lastCheckedAt=NOW.isoformat())
        b=dict(a,lastCheckedAt='2026-10-13T12:00:00-04:00')
        self.assertEqual(pipeline._fingerprint(a),pipeline._fingerprint(b))
        self.assertNotEqual(pipeline._fingerprint(a),pipeline._fingerprint(dict(a,price='$25')))

    def test_status_announcement_survives_newer_generic_scheduled(self):
        negative=evidence.apply(event(sourceAuthority='organizer'), 'This show has been postponed to Friday, April 30, 2027 at 8pm.')
        active=event(sourceAuthority='organizer',status='scheduled',statusEvidence='https://schema.org/EventScheduled',statusEvidenceKind='structured',observedAt='2026-10-13T12:00:00-04:00')
        merged=identity.resolve([negative,active],{})[0][0]
        self.assertEqual(merged['status'],'postponed')
        self.assertEqual(negative['replacementStart'],'2027-04-30T20:00:00-04:00')
        self.assertEqual(evidence.apply(event(),'This show has been postponed to Friday, April 30, 2027 at 8 p.m. Tickets remain valid.')['replacementStart'],'2027-04-30T20:00:00-04:00')
        reinstated=evidence.apply(event(sourceAuthority='organizer',observedAt='2026-10-14T12:00:00-04:00'),'This show has been reinstated.')
        self.assertEqual(identity.resolve([negative,reinstated],{})[0][0]['status'],'scheduled')
        self.assertFalse(evidence.apply(event(),'Our play explores cancellation culture.').get('status'))

    def test_detail_refresh_failure_retains_facts_and_success_updates_price(self):
        rows=[identity.prepare(event(status='scheduled',statusEvidence='https://schema.org/EventScheduled',statusEvidenceKind='structured'))]
        state={}
        updated,report=refresh.refresh(rows,[rows[0]['url']],NOW,state,getter=lambda url:'<h1>Test concert</h1><p>This show is canceled.</p><span class="tribe-events-event-cost">$15</span>')
        self.assertEqual(updated[0]['status'],'canceled')
        self.assertEqual(updated[0]['priceMin'],15)
        self.assertNotEqual(updated[0]['sourceRecordId'],rows[0]['sourceRecordId'])
        self.assertEqual(len(report['changes']),1)
        def fail(url):raise TimeoutError('offline')
        failed,report=refresh.refresh(updated,[rows[0]['url']],NOW,state,getter=fail)
        self.assertEqual(failed,updated)
        self.assertIn('lastSuccess',state[rows[0]['url']])
        self.assertEqual(len(report['failures']),1)

    def test_health_distinguishes_no_events_from_failed_calendar(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);crawl=root/'crawl/test-venue';crawl.mkdir(parents=True)
            (crawl/'calendar_attempt.json').write_text(json.dumps({'status':'failed','complete':False,'errors':['blocked']}))
            processed=root/'processed';processed.mkdir()
            report=calendar_health.build({'sources':[source('tribe')]},root/'crawl',processed,root/'events.json')
            self.assertEqual(report['sources'][0]['attempt']['status'],'failed')
            self.assertEqual(report['sources'][0]['publishedOccurrences'],0)

    def test_calendar_only_publication_preserves_news(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);records=root/'records.jsonl';out=root/'public';out.mkdir()
            records.write_text(json.dumps(event())+'\n')
            news=out/'news.json';news.write_text('existing news\n')
            code=publish.main(['--events-only','--records',str(records),'--out',str(out),'--edition-date','2026-10-12'])
            self.assertEqual(code,0)
            self.assertEqual(news.read_text(),'existing news\n')
            self.assertEqual(len(json.loads((out/'events.json').read_text())['events']),1)


if __name__=='__main__':
    unittest.main()
