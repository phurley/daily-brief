#!/usr/bin/env python3
"""Serialize local publishers, then publish source + compact edition in one commit.

--no-push only builds artifacts (no git changes or network). All source paths are
explicit so unrelated staged/unstaged work is never included in a bot commit.
"""
import argparse
from contextlib import contextmanager
import time
import tempfile
import fcntl
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {'events.json', 'news.json', 'weather.json', 'calendar.json', 'vibe.json', 'geeknews.json', 'photos.json', 'starship.json', 'starship/history', 'science-health.json', 'recommendations.json'}

def run(*args):
    return subprocess.run(args, cwd=ROOT, check=True)

def build_snapshot(node, sources):
    # Another collector may be writing a different source. Publish that source's
    # committed version until its own job submits it, never uncommitted data.
    inputs = ['weather.json', 'events.json', 'news.json', 'geeknews.json', 'vibe.json',
              'starship.json', 'science-health.json', 'brief-preferences.json', 'scoring-weights.json']
    with tempfile.TemporaryDirectory(prefix='daily-brief-edition-') as directory:
        snapshot = Path(directory)
        for name in inputs:
            if name in sources:
                shutil.copyfile(ROOT / name, snapshot / name)
            else:
                data = subprocess.check_output(['git', 'show', 'HEAD:' + name], cwd=ROOT)
                (snapshot / name).write_bytes(data)
        if (ROOT / 'brief-manifest.json').exists():
            shutil.copyfile(ROOT / 'brief-manifest.json', snapshot / 'brief-manifest.json')
        if (ROOT / 'editions').exists():
            shutil.copytree(ROOT / 'editions', snapshot / 'editions')
        run(node, 'scripts/build_edition.mjs', str(snapshot))
        (ROOT / 'editions').mkdir(exist_ok=True)
        files = [*sorted((snapshot / 'editions').glob('*.json')),
                 snapshot / 'recommendations.json', snapshot / 'widget-events.json', snapshot / 'brief-manifest.json']
        for file in files:
            destination = ROOT / file.relative_to(snapshot)
            temporary = destination.with_name(destination.name + '.tmp-' + str(os.getpid()))
            shutil.copyfile(file, temporary)
            os.replace(temporary, destination)
        keep = {file.name for file in (snapshot / 'editions').glob('*.json')}
        for file in (ROOT / 'editions').glob('*.json'):
            if file.name not in keep:
                file.unlink()

@contextmanager
def publication_lock(gitdir):
    # Interoperate with the existing macOS collectors' shlock mutex. Linux CI
    # uses flock plus its workflow concurrency group.
    shlock = Path('/usr/bin/shlock')
    lock_path = Path('/tmp/dailybrief-publish.lock')
    if shlock.exists():
        for attempt in range(120):
            if subprocess.run([str(shlock), '-f', str(lock_path), '-p', str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Timed out waiting for Daily Brief publication lock')
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)
    else:
        with open(Path(gitdir) / 'daily-brief-publish.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-push', action='store_true')
    parser.add_argument('sources', nargs='*', metavar='SOURCE.json')
    args = parser.parse_args()
    if not set(args.sources) <= ALLOWED:
        parser.error('Only public source JSON filenames may be published')
    node = os.environ.get('BRIEF_NODE') or shutil.which('node') or '/opt/homebrew/bin/node'
    build = lambda: run(node, 'scripts/build_edition.mjs')
    if args.no_push:
        build()
        return
    gitdir = subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], cwd=ROOT, text=True).strip()
    with publication_lock(gitdir):
        paths = list(dict.fromkeys([*args.sources, 'brief-manifest.json', 'widget-events.json', 'recommendations.json', 'editions']))
        for attempt in range(3):
            # Other machines (including GitHub Actions) can publish too. Rebuild
            # after a rebase so the manifest describes the resulting source tree.
            run('git', 'pull', '--rebase', '--autostash', 'origin', 'main')
            build_snapshot(node, args.sources)
            run('git', 'add', '-A', '--', *paths)
            changed = subprocess.run(['git', 'diff', '--cached', '--quiet', '--', *paths], cwd=ROOT).returncode
            if changed:
                run('git', '-c', 'user.name=daily-brief bot', '-c', 'user.email=phurley@gmail.com',
                    'commit', '--only', '-m', 'Publish current brief edition' + (': ' + ', '.join(args.sources) if args.sources else ''), '--', *paths)
            # Push also retries an earlier local commit even if data is unchanged.
            if subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=ROOT).returncode == 0:
                return
        raise SystemExit('Push failed after three attempts; commits preserved locally')

if __name__ == '__main__':
    main()
