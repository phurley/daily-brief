#!/usr/bin/env python3
"""Start a scheduled job from a clean origin/main in the deploy checkout.

On kitchen, ~/daily-brief is a deploy directory: launchd runs the jobs from it
and nobody edits there (develop in a `git worktree` instead). It is marked by
the git-ignored file `.deploy-checkout` at the repo root. Without that marker
this guard does nothing but run the job, so a developer's worktree is never
reset.

Each entry script (run_collect.sh, run_science.sh, run_calendar.sh,
starship/run.sh, run_edition.sh, vibe-check/run.sh, run_health.sh) begins with

    DEPLOY_GUARD=<root>/scripts/deploy_guard.py
    if [ -z "${DAILYBRIEF_DEPLOY_GUARD:-}" ] && [ -f "$DEPLOY_GUARD" ]; then exec /usr/bin/python3 "$DEPLOY_GUARD" "$0" "$@"; fi

In the deploy checkout the guard then:

1. Every running job holds a shared lock, inherited by its children and
   released when they all exit. Nested jobs (collect running science and
   vibe-check) inherit DAILYBRIEF_DEPLOY_GUARD and skip the guard.
2. If no other job is running (the exclusive lock is free), it fetches
   origin/main and forces the checkout to it. Any local edits, local commits,
   another branch or untracked files in the way are first saved under
   ~/daily-brief-discarded/<time>/ and reported in the job log. Git-ignored
   state (.env, .venv, processed/, crawl/ ...) is never touched.
   If another job is running, the reset is skipped, so it never discards that
   job's unpublished output, and this job runs on the current checkout without
   waiting. Collect can run for hours; the next quiet job catches up.
3. Execs the job's current script.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = ".deploy-checkout"
LOCK_PATH = Path(os.getenv("DAILYBRIEF_DEPLOY_LOCK", "/tmp/dailybrief-deploy.lock"))
DISCARD_ROOT = Path(os.getenv("DAILYBRIEF_DISCARD_DIR", str(Path.home() / "daily-brief-discarded")))


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [deploy] {message}", file=sys.stderr, flush=True)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def lock_and_sync() -> None:
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exclusive = True
    except BlockingIOError:
        exclusive = False
    if exclusive:
        try:
            sync()
        except Exception as exc:  # never skip the job because of the guard
            log(f"WARNING: sync failed, running the current checkout: {exc}")
    else:
        log("another Daily Brief job is running; skipping the reset to origin/main")
    # Shared from here on: other jobs may start, but none resets under us.
    # Blocks only while another job is mid-reset (seconds).
    fcntl.flock(fd, fcntl.LOCK_SH)
    os.set_inheritable(fd, True)  # held by the job and its children until they exit


def untracked_in_the_way() -> list[str]:
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    incoming = set(git("ls-tree", "-r", "--name-only", "-z", "origin/main").split("\0"))
    return sorted(path for path in untracked if path and path in incoming)


def save_discarded(head: str, branch: str, dirty: str, ahead: str, untracked: list[str]) -> Path:
    target = DISCARD_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    (target / "changes.patch").write_text(git("diff", "--binary", "HEAD"))
    for path in untracked:
        destination = target / "untracked" / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    (target / "README.txt").write_text(
        f"Discarded from {ROOT} by scripts/deploy_guard.py before a scheduled job.\n"
        f"HEAD was {head} on {branch or '(detached)'}; recover commits with `git branch rescue {head}`.\n\n"
        f"Changed files:\n{dirty}\nCommits not on origin/main:\n{ahead}\n"
        f"Untracked files replaced by origin/main:\n" + "".join(f"{p}\n" for p in untracked)
    )
    return target


def sync() -> None:
    if subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT).returncode:
        log("WARNING: git fetch failed; syncing to the last fetched origin/main")
    head = git("rev-parse", "HEAD").strip()
    target = git("rev-parse", "origin/main").strip()
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    dirty = git("status", "--porcelain", "--untracked-files=no")
    ahead = git("log", "--oneline", "origin/main..HEAD")
    untracked = untracked_in_the_way()
    if dirty or ahead or untracked or branch != "main":
        saved = save_discarded(head, branch, dirty, ahead, untracked)
        log(f"WARNING: deploy checkout had local changes; saved to {saved} and discarded")
        for line in (dirty + ahead).splitlines()[:20]:
            log(f"  {line}")
    git("checkout", "--force", "-B", "main", "origin/main")
    git("worktree", "prune")
    if head != target:
        log(f"updated {head[:7]} -> {target[:7]}")


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        sys.exit("usage: deploy_guard.py SCRIPT [ARGS...]")
    script = os.path.abspath(argv[1])
    os.environ["DAILYBRIEF_DEPLOY_GUARD"] = "1"
    if (ROOT / MARKER).exists():
        lock_and_sync()
    os.execv(script, [script, *argv[2:]])


if __name__ == "__main__":
    main(sys.argv)
