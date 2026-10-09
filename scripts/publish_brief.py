#!/usr/bin/env python3
"""Publish captured source output in a disposable checkout of the latest remote.

Each retry rebuilds derived files, without rebasing generated commits or touching
unrelated work in the collector checkout. --no-push builds local artifacts only.
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
DERIVED = ['brief-manifest.json', 'widget-events.json', 'recommendations.json', 'editions']


def run(*args, cwd=ROOT, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def output(*args, cwd=ROOT):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


@contextmanager
def publication_lock(gitdir):
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


def sync_checkout(commit, captured):
    """Fast-forward when safe. Never stash, rebase or overwrite newer source work."""
    index = Path(output('git', 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    original_index = index.read_bytes()
    # Git permits a fast-forward over staged content identical to the new tree.
    # Stage only our captured files that are still identical to the published copy.
    for name, data in captured.items():
        path = ROOT / name
        if not path.is_file() or path.read_bytes() != data:
            continue
        blob = subprocess.run(['git', 'rev-parse', f'{commit}:{name}'], cwd=ROOT, capture_output=True, text=True)
        if blob.returncode == 0 and output('git', 'hash-object', '--', name) == blob.stdout.strip():
            run('git', 'add', '--', name)
    result = subprocess.run(['git', 'merge', '--ff-only', '--no-autostash', commit], cwd=ROOT)
    if result.returncode:
        temporary = index.with_name('index.dailybrief-tmp')
        temporary.write_bytes(original_index)
        os.replace(temporary, index)
        print('Publication succeeded. Local edits prevent a safe fast-forward; checkout and index preserved. Future publications still start from origin/main.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-push', action='store_true')
    parser.add_argument('sources', nargs='*', metavar='SOURCE.json')
    args = parser.parse_args()
    if not set(args.sources) <= ALLOWED:
        parser.error('Only public source JSON filenames may be published')
    node = os.environ.get('BRIEF_NODE') or shutil.which('node') or '/opt/homebrew/bin/node'
    if args.no_push:
        run(node, 'scripts/build_edition.mjs')
        return
    gitdir = output('git', 'rev-parse', '--absolute-git-dir')
    with publication_lock(gitdir):
        # Capture once: a retry must neither lose this writer's output nor pick up
        # an unrelated writer's unsubmitted data. History directories are additive.
        captured = {}
        for name in args.sources:
            path = ROOT / name
            for file in sorted(path.rglob('*')) if path.is_dir() else [path]:
                if file.is_file():
                    captured[str(file.relative_to(ROOT))] = file.read_bytes()
                elif not file.exists():
                    raise FileNotFoundError(file)
        paths = list(dict.fromkeys([*args.sources, *DERIVED]))
        last_commit = None
        for attempt in range(3):
            run('git', 'fetch', 'origin', 'main')
            with tempfile.TemporaryDirectory(prefix='daily-brief-publication-') as directory:
                checkout = Path(directory) / 'checkout'
                run('git', 'worktree', 'add', '--detach', str(checkout), 'origin/main')
                try:
                    for name, data in captured.items():
                        path = checkout / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                    run(node, 'scripts/build_edition.mjs', cwd=checkout)
                    run('git', 'add', '-A', '--', *paths, cwd=checkout)
                    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=checkout).returncode:
                        run('git', '-c', 'user.name=daily-brief bot', '-c', 'user.email=phurley@gmail.com',
                            'commit', '-m', 'Publish current brief edition' + (': ' + ', '.join(args.sources) if args.sources else ''), cwd=checkout)
                    last_commit = output('git', 'rev-parse', 'HEAD', cwd=checkout)
                    if subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=checkout).returncode == 0:
                        sync_checkout(last_commit, captured)
                        return
                finally:
                    run('git', 'worktree', 'remove', '--force', str(checkout))
        recovery = f'refs/dailybrief/failed/{int(time.time())}-{os.getpid()}'
        run('git', 'update-ref', recovery, last_commit)
        raise SystemExit(f'Push failed after three fresh rebuilds; source output remains local and the last candidate is preserved at {recovery}')


if __name__ == '__main__':
    main()
