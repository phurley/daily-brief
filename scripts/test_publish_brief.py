"""Isolated Git integration test; never contacts the production remote."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
NODE = os.environ.get('BRIEF_NODE') or shutil.which('node') or '/opt/homebrew/bin/node'

class PublicationTest(unittest.TestCase):
    def test_source_and_edition_publish_without_unrelated_staged_work(self):
        with tempfile.TemporaryDirectory(prefix='brief-publish-') as directory:
            temp = Path(directory)
            remote, repo = temp / 'origin.git', temp / 'checkout'
            def command(*args, cwd=None):
                return subprocess.check_output(args, cwd=cwd or repo, stderr=subprocess.STDOUT, text=True)
            command('git', 'init', '--bare', '--initial-branch=main', str(remote), cwd=temp)
            command('git', 'clone', str(remote), str(repo), cwd=temp)
            command('git', 'config', 'user.name', 'Test')
            command('git', 'config', 'user.email', 'test@example.com')
            (repo / 'scripts').mkdir()
            for name in ['scripts/build_edition.mjs', 'scripts/publish_brief.py', 'brief-selection.mjs', 'story-order.mjs', 'ranking.mjs', 'preferences.mjs', 'scoring.mjs', 'news-ranking.mjs', 'science.mjs', 'starship.json', 'science-health.json', 'brief-preferences.json', 'scoring-weights.json', 'weather.json', 'events.json', 'news.json', 'geeknews.json', 'vibe.json']:
                shutil.copy(ROOT / name, repo / name)
            publisher=repo / 'scripts/publish_brief.py'
            publisher.write_text(publisher.read_text().replace('/tmp/dailybrief-publish.lock', str(temp / 'publish.lock')))
            (repo / 'notes.txt').write_text('original')
            command('git', 'add', '.')
            command('git', 'commit', '-m', 'fixture')
            command('git', 'push', '-u', 'origin', 'main')
            (repo / 'notes.txt').write_text('unrelated user edit')
            command('git', 'add', 'notes.txt')
            news = json.loads((repo / 'news.json').read_text())
            original_news_time = news['generatedAt']
            news['generatedAt'] = '2026-10-08T01:00:00Z'
            (repo / 'news.json').write_text(json.dumps(news))
            weather = json.loads((repo / 'weather.json').read_text())
            weather['generatedAt'] = '2026-10-09T18:00:00Z'
            (repo / 'weather.json').write_text(json.dumps(weather))
            command(sys.executable, 'scripts/publish_brief.py', 'weather.json')
            committed = command('git', 'show', '--pretty=', '--name-only', 'HEAD').splitlines()
            self.assertIn('weather.json', committed)
            self.assertIn('brief-manifest.json', committed)
            self.assertIn('widget-events.json', committed)
            self.assertNotIn('notes.txt', committed)
            manifest = json.loads((repo / 'brief-manifest.json').read_text())
            self.assertEqual(manifest['sections']['news']['generatedAt'], original_news_time)
            self.assertNotIn('news.json', committed)
            self.assertEqual(command('git', 'show', 'HEAD:notes.txt').strip(), 'original')
            self.assertEqual((repo / 'notes.txt').read_text(), 'unrelated user edit')
            self.assertEqual(command('git', 'rev-parse', 'HEAD'), command('git', 'rev-parse', 'origin/main'))
            # No-source date rollover works on the system Python used by launchd.
            command(sys.executable, 'scripts/publish_brief.py')
            command(sys.executable, 'scripts/publish_brief.py', '--no-push')
            self.assertIn('notes.txt', command('git', 'status', '--short'))



class PublicationRaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='brief-race-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.remote, self.other = [self.root / n for n in ('writer', 'origin.git', 'other')]
        self.git = shutil.which('git')
        self.env = {**os.environ, 'BRIEF_NOW':'2026-10-09T12:00:00Z'}
        self.command('init', '--bare', '--initial-branch=main', str(self.remote), cwd=self.root)
        self.command('clone', str(self.remote), str(self.repo), cwd=self.root)
        self.command('config', 'user.name', 'Test')
        self.command('config', 'user.email', 'test@example.com')
        (self.repo/'scripts').mkdir()
        for name in ['scripts/publish_brief.py','scripts/build_edition.mjs','brief-selection.mjs','ranking.mjs','preferences.mjs','scoring.mjs','news-ranking.mjs','science.mjs']:
            shutil.copyfile(ROOT/name,self.repo/name)
        publisher = self.repo/'scripts/publish_brief.py'
        publisher.write_text(publisher.read_text().replace('/tmp/dailybrief-publish.lock',str(self.root/'publish.lock')))
        for name, fields in {'weather':{'daily':[],'location':{}},'events':{'events':[]},'news':{'stories':[]},'geeknews':{'stories':[]},'vibe':{'messages':[]},'starship':{'forecast':{}},'science-health':{'sources':{}}}.items():
            data={'schemaVersion':{'events':'2.0.0','news':'1.1.0'}.get(name,'1.0.0'),'generatedAt':'2026-10-09T10:00:00Z','editionDate':'2026-10-09',**fields}
            (self.repo/(name+'.json')).write_text(json.dumps(data))
        for name in ['brief-preferences.json','scoring-weights.json']:(self.repo/name).write_text('{}')
        (self.repo/'notes.txt').write_text('original')
        self.build(self.repo)
        self.command('add','.')
        self.command('commit','-m','fixture')
        self.command('push','-u','origin','main')
        self.command('clone',str(self.remote),str(self.other),cwd=self.root)
        self.command('config','user.name','Other',cwd=self.other)
        self.command('config','user.email','other@example.com',cwd=self.other)
        self.base=self.command('rev-parse','HEAD').strip()
        p=self.repo/'weather.json';d=json.loads(p.read_text());d['generatedAt']='2026-10-09T11:00:00Z';p.write_text(json.dumps(d))
        (self.repo/'notes.txt').write_text('unrelated staged edit')
        self.command('add','notes.txt')

    def command(self,*args,cwd=None):
        return subprocess.check_output([self.git,*args],cwd=cwd or self.repo,text=True,stderr=subprocess.STDOUT)

    def build(self,where):
        subprocess.run([NODE,'scripts/build_edition.mjs'],cwd=where,env=self.env,check=True,stdout=subprocess.DEVNULL)

    def publish(self,mode,sources=None):
        fake=self.root/'bin';fake.mkdir()
        marker=self.root/'raced'
        body=f"""#!{sys.executable}
import subprocess,sys,os,json
from pathlib import Path
if sys.argv[1:2]==['push']:
 if {mode!r}=='reject': sys.exit(1)
 if not Path({str(marker)!r}).exists():
  Path({str(marker)!r}).touch()
  p=Path({str(self.other/'science-health.json')!r});d=json.loads(p.read_text());d['generatedAt']='2026-10-09T11:30:00Z';p.write_text(json.dumps(d))
  for args in [[{NODE!r},'scripts/build_edition.mjs'],[{self.git!r},'add','.'],[{self.git!r},'commit','-m','competing publication'],[{self.git!r},'push','origin','main']]:
   subprocess.run(args,cwd={str(self.other)!r},check=True,stdout=subprocess.DEVNULL)
os.execv({self.git!r},[{self.git!r},*sys.argv[1:]])
"""
        (fake/'git').write_text(body);(fake/'git').chmod(0o755)
        return subprocess.run([sys.executable,'scripts/publish_brief.py',*(sources or ['weather.json'])],cwd=self.repo,env={**self.env,'PATH':str(fake)+os.pathsep+os.environ['PATH']},stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)

    def test_competing_writer_rebuilds_preserving_both_sources_and_local_edits(self):
        result=self.publish('race')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(self.command('rev-parse','HEAD'),self.command('rev-parse','origin/main'))
        for name,stamp in [('weather','2026-10-09T11:00:00Z'),('science-health','2026-10-09T11:30:00Z')]:
            self.assertEqual(json.loads((self.repo/(name+'.json')).read_text())['generatedAt'],stamp)
        manifest=json.loads((self.repo/'brief-manifest.json').read_text())
        self.assertEqual(manifest['sections']['scienceHealth']['generatedAt'],'2026-10-09T11:30:00Z')
        self.assertEqual((self.repo/'notes.txt').read_text(),'unrelated staged edit')
        self.assertEqual(self.command('diff','--cached','--name-only').strip(),'notes.txt')
        self.assertNotIn('UU ',self.command('status','--short'))

    def test_collector_preliminary_recommendations_are_rebuilt_and_sync_cleanly(self):
        p=self.repo/'recommendations.json'
        document=json.loads(p.read_text());document['generatedAt']='2026-10-09T11:00:00Z'
        p.write_text(json.dumps(document))
        result=self.publish('race',['weather.json','recommendations.json'])
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(self.command('rev-parse','HEAD'),self.command('rev-parse','origin/main'))
        self.assertEqual(json.loads(p.read_text())['generatedAt'],'2026-10-09T12:00:00.000Z')
        self.assertEqual(self.command('diff','--cached','--name-only').strip(),'notes.txt')
        self.assertEqual(self.command('diff','--name-only','--','recommendations.json').strip(),'')

    def test_exhausted_retries_preserve_checkout_and_recovery_commit(self):
        result=self.publish('reject')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.command('rev-parse','HEAD').strip(),self.base)
        self.assertIn('refs/dailybrief/failed/',self.command('for-each-ref','--format=%(refname)','refs/dailybrief/failed/'))
        self.assertEqual(self.command('diff','--cached','--name-only').strip(),'notes.txt')
        self.assertNotIn('UU ',self.command('status','--short'))
        self.assertEqual(len(self.command('worktree','list','--porcelain').split('worktree '))-1,1)

if __name__ == '__main__':
    unittest.main()
