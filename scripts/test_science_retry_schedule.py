"""Exercise real shell wrappers with isolated locks and stubbed paid/Git work."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ScienceScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ('data-collect/.venv/bin', 'scripts', 'vibe-check'):
            (self.root / folder).mkdir(parents=True)
        self.trace = self.root / 'trace'
        self.env = {**os.environ, 'BRIEF_TEST_ROOT': str(self.root),
                    'SCIENCE_CAFFEINATED': '1', 'DAILYBRIEF_CAFFEINATED': '1'}
        for name in ('geeknews.json', 'science-health.json'):
            (self.root / name).write_text('{}')
        self.executable('lock', '#!/bin/sh\nexit 0\n')

    def executable(self, name, text):
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def wrapper(self, name):
        text = (ROOT / name).read_text().replace('/usr/bin/shlock', str(self.root / 'lock'))
        text = text.replace('/tmp/dailybrief-science.lock', str(self.root / 'science.lock'))
        text = text.replace('/tmp/dailybrief-collect.lock', str(self.root / 'collect.lock'))
        return self.executable(name, text)

    def run_wrapper(self, path, *args, **env):
        return subprocess.run(['/bin/sh', str(path), *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=10)

    def test_skipped_retry_does_not_publish_or_refresh_editorial(self):
        runner = self.wrapper('data-collect/run_science.sh')
        self.executable('data-collect/.venv/bin/python', '#!/bin/sh\necho skipped >> "$BRIEF_TEST_ROOT/trace"\n')
        self.executable('scripts/push_generated.sh', '#!/bin/sh\necho push >> "$BRIEF_TEST_ROOT/trace"\n')
        self.executable('vibe-check/run.sh', '#!/bin/sh\necho vibe >> "$BRIEF_TEST_ROOT/trace"\n')
        result = self.run_wrapper(runner, '--retry-failed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), ['skipped'])

    def test_partial_publication_runs_publisher_and_editorial(self):
        runner = self.wrapper('data-collect/run_science.sh')
        self.executable('data-collect/.venv/bin/python', '''#!/bin/sh
echo '{"stories":["verified"]}' > "$BRIEF_TEST_ROOT/geeknews.json"
echo '{"status":"partial"}' > "$BRIEF_TEST_ROOT/science-health.json"
''')
        self.executable('scripts/push_generated.sh', '#!/bin/sh\necho push >> "$BRIEF_TEST_ROOT/trace"\n')
        self.executable('vibe-check/run.sh', '#!/bin/sh\necho vibe >> "$BRIEF_TEST_ROOT/trace"\n')
        result = self.run_wrapper(runner, '--retry-failed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), ['push', 'vibe'])

    def test_hourly_retry_runs_before_fallible_crawler_and_skips_dry_runs(self):
        runner = self.wrapper('data-collect/run_collect.sh')
        self.executable('data-collect/run_science.sh', '#!/bin/sh\necho retry:$*:vibe=$SCIENCE_VIBE_ENABLED >> "$BRIEF_TEST_ROOT/trace"\nexit 1\n')
        self.executable('data-collect/.venv/bin/python', '#!/bin/sh\necho "$1" >> "$BRIEF_TEST_ROOT/trace"\nexit 1\n')
        result = self.run_wrapper(runner)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace.read_text().splitlines(), ['retry:--retry-failed:vibe=0', 'crawl_sources.py'])
        for env in ({'COLLECT_NO_PUSH':'1'}, {'COLLECT_OUT_DIR':str(self.root/'other')}, {'SCIENCE_RETRY_ENABLED':'0'}):
            self.trace.write_text('')
            result = self.run_wrapper(runner, **env)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(self.trace.read_text().splitlines(), ['crawl_sources.py'])


if __name__ == '__main__':
    unittest.main()
