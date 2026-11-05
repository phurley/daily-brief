#!/usr/bin/env python3
"""deploy_guard resets only a marked deploy checkout and always runs the job."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "deploy_guard.py"
JOB = """#!/bin/sh
DEPLOY_GUARD="$(cd "$(dirname "$0")/.." && pwd)/scripts/deploy_guard.py"
if [ -z "${DAILYBRIEF_DEPLOY_GUARD:-}" ] && [ -f "$DEPLOY_GUARD" ]; then exec "$PYTHON" "$DEPLOY_GUARD" "$0" "$@"; fi
echo "ran $(cat version.txt) args=$*"
"""


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class DeployGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        seed = self.tmp / "seed"
        (seed / "scripts").mkdir(parents=True)
        shutil.copy2(GUARD, seed / "scripts" / "deploy_guard.py")
        job = seed / "scripts" / "job.sh"
        job.write_text(JOB)
        job.chmod(0o755)
        (seed / "version.txt").write_text("v1\n")
        (seed / ".gitignore").write_text(".deploy-checkout\nstate/\n")
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "v1")
        git(self.tmp, "clone", "-q", "--bare", str(seed), "origin.git")
        git(self.tmp, "clone", "-q", "origin.git", "deploy")
        self.deploy = self.tmp / "deploy"
        self.seed = seed
        git(seed, "remote", "add", "origin", str(self.tmp / "origin.git"))

    def publish(self, version):
        (self.seed / "version.txt").write_text(version + "\n")
        git(self.seed, "commit", "-qam", version)
        git(self.seed, "push", "-q", "origin", "main")

    def run_job(self, *args):
        env = {**os.environ, "PYTHON": sys.executable,
               "DAILYBRIEF_DEPLOY_LOCK": str(self.tmp / "deploy.lock"),
               "DAILYBRIEF_DISCARD_DIR": str(self.tmp / "discarded")}
        env.pop("DAILYBRIEF_DEPLOY_GUARD", None)
        return subprocess.run([str(self.deploy / "scripts" / "job.sh"), *args], cwd=self.deploy, env=env,
                              capture_output=True, text=True, check=True)

    def test_marked_checkout_is_reset_to_origin_and_edits_are_saved(self):
        (self.deploy / ".deploy-checkout").touch()
        (self.deploy / "state").mkdir()
        (self.deploy / "state" / "keep").write_text("ignored state")
        (self.deploy / "version.txt").write_text("hand edit\n")
        self.publish("v2")
        result = self.run_job("--force")
        self.assertEqual(result.stdout.strip(), "ran v2 args=--force")
        self.assertIn("saved to", result.stderr)
        self.assertEqual(git(self.deploy, "status", "--porcelain"), "")
        self.assertEqual((self.deploy / "state" / "keep").read_text(), "ignored state")
        [saved] = (self.tmp / "discarded").iterdir()
        self.assertIn("hand edit", (saved / "changes.patch").read_text())

    def test_local_commit_and_other_branch_are_replaced(self):
        (self.deploy / ".deploy-checkout").touch()
        git(self.deploy, "checkout", "-qb", "experiment")
        (self.deploy / "version.txt").write_text("local\n")
        git(self.deploy, "commit", "-qam", "local only")
        self.assertEqual(self.run_job().stdout.strip(), "ran v1 args=")
        self.assertEqual(git(self.deploy, "symbolic-ref", "--short", "HEAD"), "main")
        [saved] = (self.tmp / "discarded").iterdir()
        self.assertIn("local only", (saved / "README.txt").read_text())

    def test_running_job_blocks_the_reset_but_not_the_next_job(self):
        import fcntl
        (self.deploy / ".deploy-checkout").touch()
        (self.deploy / "version.txt").write_text("unpublished output\n")
        self.publish("v2")
        with open(self.tmp / "deploy.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_SH)  # another job is mid-run
            result = self.run_job()
        self.assertEqual(result.stdout.strip(), "ran unpublished output args=")
        self.assertIn("skipping the reset", result.stderr)
        self.assertFalse((self.tmp / "discarded").exists())
        # Once that job is done, the next one resets as usual.
        self.assertEqual(self.run_job().stdout.strip(), "ran v2 args=")

    def test_unmarked_checkout_is_never_touched(self):
        (self.deploy / "version.txt").write_text("developer work\n")
        self.publish("v2")
        result = self.run_job()
        self.assertEqual(result.stdout.strip(), "ran developer work args=")
        self.assertIn("version.txt", git(self.deploy, "status", "--porcelain"))
        self.assertFalse((self.tmp / "discarded").exists())


if __name__ == "__main__":
    unittest.main()
