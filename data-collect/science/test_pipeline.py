import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from science.pipeline import run, parse_feed, verify_date, article_content, canonical, atomic_json, ROOT

FIX = ROOT / 'scripts/fixtures/science'
NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)
SOURCE = {'id':'fixture','name':'Fixture Science','group':'Fixture','url':'https://science.example/feed','articleDomains':['science.example'],'intervalHours':1,'staleAfterHours':6}
RESULT = {'relevant':True,'finding':'An effect was observed in laboratory samples.','significance':'The observation offers a starting point for further work.','caveat':'This preprint has not been peer reviewed; human effects remain unknown.','supportingQuote':'Researchers observed the effect in laboratory samples.','caveatQuote':'The preprint has not been peer reviewed. Human effects remain unknown.','evidenceQuote':None,'evidenceType':'preprint','paperUrl':'https://arxiv.org/abs/example','topics':['biology'],'entities':[],'milestoneStatus':'unknown'}

class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)/'public'; self.out.mkdir()
        self.state = Path(self.tmp.name)/'private'
        self.config = {'sources':[SOURCE], 'maxArticlesPerSource':4}
        self.calls = 0
        self.feed = (FIX/'feed.xml').read_bytes()
    def fetch(self, url):
        return self.feed if url.endswith('/feed') else (FIX/'article.html').read_bytes()
    def extract(self, *args):
        self.calls += 1
        return copy.deepcopy(RESULT)
    def run_pipeline(self, **kw):
        return run(self.config, self.out, self.state, now=kw.pop('now',NOW), force=True,
                   fetcher=kw.pop('fetcher',self.fetch), extractor=kw.pop('extractor',self.extract),
                   selector=kw.pop('selector', lambda d: {'date':d['editionDate'],'selectedIds':[s['id'] for s in d['stories']],'reasons':{}}), **kw)
    def doc(self): return json.loads((self.out/'geeknews.json').read_text())
    def test_ingest_replay_missing_date_and_quiet_success(self):
        health = self.run_pipeline()
        self.assertEqual(health['status'],'ok')
        story = self.doc()['stories'][0]
        self.assertEqual(story['evidenceType'],'preprint')
        self.assertIn('not been peer reviewed',story['caveat'])
        self.assertEqual(story['publishedAt'],'2026-10-08T12:00:00Z')
        self.assertEqual(health['sources']['fixture']['rejectedCount'],1)
        self.run_pipeline(now=NOW+timedelta(hours=1))
        self.assertEqual(self.calls,1)
        self.assertEqual(self.doc()['stories'][0],story)
        self.feed=b'<rss><channel/></rss>'
        health=self.run_pipeline(now=NOW+timedelta(hours=2))
        self.assertEqual(health['status'],'ok')
        self.assertEqual(health['sources']['fixture']['candidateCount'],0)
        self.assertEqual(health['sources']['fixture']['lastNewStoryAt'],'2026-10-09T18:00:00Z')
    def test_source_and_partial_failure_preserve_exact_document(self):
        self.run_pipeline(); before=(self.out/'geeknews.json').read_bytes()
        self.config['sources'].append({**SOURCE,'id':'broken','url':'https://bad.example/feed'})
        def fetch(url):
            if 'bad.example' in url: raise OSError('source offline')
            return self.fetch(url)
        health=self.run_pipeline(fetcher=fetch)
        self.assertEqual(health['status'],'error')
        self.assertEqual((self.out/'geeknews.json').read_bytes(),before)
        self.assertIn('source offline',health['sources']['broken']['errors'][0])
    def test_invalid_extraction_does_not_publish_or_poison_cache(self):
        result={**RESULT,'paperUrl':'https://invented.example/paper'}
        health=self.run_pipeline(extractor=lambda *a:result)
        self.assertEqual(health['status'],'error')
        self.assertFalse((self.out/'geeknews.json').exists())
        self.assertEqual(json.loads((self.state/'state.json').read_text())['records'],{})
    def test_malformed_output_preserves_edition(self):
        self.run_pipeline(); before=(self.out/'geeknews.json').read_bytes()
        health=self.run_pipeline(selector=lambda d:{'date':'bad','selectedIds':['not-a-story'],'reasons':{}})
        self.assertEqual(health['status'],'error')
        self.assertEqual((self.out/'geeknews.json').read_bytes(),before)
    def test_article_date_must_match_feed(self):
        entry=parse_feed(self.feed)[0]
        article=article_content((FIX/'article.html').read_bytes(),entry['url'])
        entry['publishedAt']='Thu, 08 Oct 2026 12:00:00' # no timezone
        with self.assertRaises(ValueError):verify_date(entry,article,NOW)
        entry['publishedAt']='Wed, 07 Oct 2026 12:00:00 +0000'
        with self.assertRaises(ValueError):verify_date(entry,article,NOW)
    def test_recheck_does_not_redate_or_reextract_unchanged_article(self):
        self.run_pipeline(); original=self.doc()['stories'][0]
        self.run_pipeline(now=NOW+timedelta(days=2))
        latest=self.doc()['stories'][0]
        self.assertEqual(self.calls,1)
        self.assertEqual(latest['publishedAt'],original['publishedAt'])
        self.assertNotEqual(latest['checkedAt'],original['checkedAt'])
    def test_atom_and_html_response(self):
        entries=parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><link href="https://science.example/atom"/><published>2026-10-08T00:00:00Z</published><updated>2026-10-09T00:00:00Z</updated></entry></feed>')
        self.assertEqual(entries[0]['publishedAt'],'2026-10-08T00:00:00Z')
        with self.assertRaises(ValueError):parse_feed(b'<html><body>blocked</body></html>')
    def test_atomic_replace_failure(self):
        path=self.out/'example.json';path.write_text('{"old":true}')
        with patch('science.pipeline.os.replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):atomic_json(path,{'new':True})
        self.assertEqual(path.read_text(),'{"old":true}')
        self.assertEqual(list(self.out.iterdir()),[path])
    def test_real_shared_selector_publishes_fixture_end_to_end(self):
        health = self.run_pipeline(selector=None)
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(self.doc()['selection']['selectedIds'], [self.doc()['stories'][0]['id']])
    def test_unsupported_specificity_is_rejected(self):
        result={**RESULT, 'finding':'Researchers found isotopes in the samples.'}
        health=self.run_pipeline(extractor=lambda *a:result)
        self.assertEqual(health['status'],'error')
        self.assertIn('unsupported technical specificity',health['sources']['fixture']['errors'][0])
    def test_caveat_must_have_support_and_absence_is_explicit(self):
        result={**RESULT, 'caveatQuote':'An invented claim.'}
        health=self.run_pipeline(extractor=lambda *a:result)
        self.assertEqual(health['status'],'error')
        result={**RESULT, 'caveat':'Invented preliminary results.', 'caveatQuote':None}
        self.run_pipeline(extractor=lambda *a:result)
        self.assertNotIn('Invented',self.doc()['stories'][0]['caveat'])
        self.assertIn('does not specify',self.doc()['stories'][0]['caveat'])
    def test_stable_existing_id_and_date(self):
        old={'schemaVersion':'1.0.0','generatedAt':'2026-10-08T12:00:00Z','editionDate':'2026-10-08','stories':[{'id':'original-id','title':'Original','summary':'Original summary','url':'https://science.example/study','source':{'name':'Fixture Science'},'publishedAt':'2026-10-08T12:00:00Z','topics':['biology']}]}
        atomic_json(self.out/'geeknews.json',old)
        self.run_pipeline()
        self.assertEqual(self.doc()['stories'][0]['id'],'original-id')

if __name__=='__main__': unittest.main()
