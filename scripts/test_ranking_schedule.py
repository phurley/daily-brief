#!/usr/bin/env python3
"""Offline macOS smoke checks for ranking's scheduled collection integration.
No network, extraction/model calls, Git operations, or production locks are used.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(Path('/usr/bin/shlock').exists(), 'launchd job smoke test requires macOS shlock')
class RankingScheduleTest(unittest.TestCase):
    def test_no_push_collection_still_generates_shortlist(self):
        with tempfile.TemporaryDirectory(prefix='brief-schedule-') as folder:
            root = Path(folder)
            collect = root / 'data-collect'
            (collect / '.venv/bin').mkdir(parents=True)
            (collect / 'processed').mkdir()
            (collect / 'processed/extracted_records.jsonl').write_text('{}\n')
            (root / 'scripts').mkdir()
            for name in ('preferences.mjs', 'ranking.mjs', 'scoring.mjs', 'scoring-weights.json', 'brief-preferences.json'):
                shutil.copyfile(ROOT / name, root / name)
            shutil.copyfile(ROOT / 'scripts/select-best-bets.mjs', root / 'scripts/select-best-bets.mjs')
            # The fixture replaces paid collection/extraction, not selection.
            event = dict(id='fixture', title='Test event', venue='Test venue', city='Canton', category='Music', start='2026-10-09T18:00:00-04:00', score=75)
            (root / 'events.json').write_text(json.dumps(dict(generatedAt='2026-10-09T12:00:00-04:00', events=[event])))
            (root / 'news.json').write_text('{"stories": []}')
            python = collect / '.venv/bin/python'
            python.write_text('#!/bin/sh\nexit 0\n')
            python.chmod(0o755)
            script = (ROOT / 'data-collect/run_collect.sh').read_text().replace('/tmp/dailybrief-collect.lock', str(root / 'collect.lock'))
            runner = collect / 'run_collect.sh'
            runner.write_text(script)
            env = {**os.environ, 'DAILYBRIEF_CAFFEINATED': '1', 'COLLECT_NO_PUSH': '1', 'VIBE_ENABLED': '0', 'COLLECT_OUT_DIR': str(root)}
            subprocess.run(['/bin/sh', str(runner)], env=env, check=True, timeout=20)
            feed = json.loads((root / 'recommendations.json').read_text())
            self.assertEqual(feed['days'][0]['events'][0]['id'], 'fixture')
            self.assertFalse((root / 'collect.lock').exists())
            self.assertIn('push skipped', (collect / 'cron.log').read_text())

    def test_chained_editorial_can_acquire_lock_and_parent_job_lock_is_cleaned(self):
        with tempfile.TemporaryDirectory(prefix='brief-locks-') as folder:
            root = Path(folder)
            helper = root / 'helper.sh'
            helper.write_text((ROOT / 'scripts/git-publish-lock.sh').read_text().replace('/tmp/dailybrief-publish.lock', str(root / 'publish.lock')))
            (root / 'vibe-check').mkdir()
            child = root / 'vibe-check/run.sh'
            child.write_text('''#!/bin/sh
LOCK="$ROOT/child.lock"
. "$ROOT/helper.sh"
acquire_publish_lock
[ -f "$ROOT/collect.lock" ] || exit 2
printf acquired > "$ROOT/child-acquired"
''')
            child.chmod(0o755)
            source = (ROOT / 'data-collect/run_collect.sh').read_text()
            start = source.index('run_vibe() {')
            end = source.index('\n# Re-exec', start)
            script = '''set -e
export ROOT="$1"
LOCK="$ROOT/collect.lock"
/usr/bin/shlock -f "$LOCK" -p $$
. "$ROOT/helper.sh"
acquire_publish_lock
''' + source[start:end] + '''
run_vibe
[ -f "$ROOT/collect.lock" ]
[ -f "$ROOT/child-acquired" ]
'''
            subprocess.run(['/bin/sh', '-c', script, 'lock-test', str(root)], check=True, timeout=10)
            self.assertFalse((root / 'collect.lock').exists())
            self.assertFalse((root / 'publish.lock').exists())

if __name__ == '__main__':
    unittest.main()
