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

if __name__ == '__main__':
    unittest.main()
