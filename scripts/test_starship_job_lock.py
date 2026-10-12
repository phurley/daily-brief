"""Verify publication returns through the job's lock cleanup on success/failure."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StarshipJobLockTests(unittest.TestCase):
    def test_publishing_cleans_lock_for_both_exit_statuses(self):
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'starship').mkdir()
                lock = root / 'job.lock'
                shlock = root / 'shlock'
                shlock.write_text('#!/bin/sh\ntouch "$2"\n')
                shlock.chmod(0o755)
                script = (ROOT / 'starship/run.sh').read_text()
                script = script.replace('LOCK=/tmp/dailybrief-starship.lock', f'LOCK="{lock}"')
                script = script.replace('/usr/bin/shlock', str(shlock))
                script = script.replace('/usr/bin/caffeinate -i "$NODE" starship/collect.mjs', 'true')
                script = script.replace('/usr/bin/python3 scripts/publish_brief.py starship.json starship/history', f'/bin/sh -c "exit {status}"')
                runner = root / 'starship/run.sh'
                runner.write_text(script)
                result = subprocess.run(['/bin/sh', str(runner)], capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertFalse(lock.exists(), 'Publisher must return through the shell EXIT trap')


if __name__ == '__main__':
    unittest.main()
