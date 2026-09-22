# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline structural guards for the package-update rollout's narrow write boundary."""
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_PATH = ".github/workflows/bootstrap-common-workflows.yml"
VERIFY_PATH = ".github/workflows/verify-package-updates.yml"
TEST_PATH = ".github/scripts/tests/bootstrap-package-update-safety.test.py"
BOOTSTRAP = (ROOT / BOOTSTRAP_PATH).read_text()
VERIFY = (ROOT / VERIFY_PATH).read_text()


def event_block(source, event):
    # These native tests deliberately accept only the workflows' simple block
    # trigger format, not a partial YAML interpretation of arbitrary new syntax.
    on = re.search(r"^on:\n(.*?)(?=^[^ #\n]|\Z)", source, re.M | re.S)
    if not on:
        raise AssertionError("Expected block-form on trigger")
    match = re.search(r"^  " + re.escape(event) + r":\n(.*?)(?=^  [a-z_]+:|\Z)",
                      on[1], re.M | re.S)
    if not match:
        raise AssertionError(f"Missing {event} trigger")
    return match[1]


def event_paths(source, event):
    block = event_block(source, event)
    match = re.search(r"^    paths:\n((?:      - [^\n]+\n)+)", block, re.M)
    if not match:
        raise AssertionError(f"Expected explicit {event} paths")
    return [line.split("- ", 1)[1].strip().strip('"\'') for line in match[1].splitlines()]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


class BootstrapPackageUpdateSafetyTests(unittest.TestCase):
    def test_only_package_update_implementation_trigger_is_excluded(self):
        self.assertEqual(event_paths(BOOTSTRAP, "push"), [
            ".github/workflows/cleanup-pr-artifacts.yml",
            ".github/workflows/auto-approve-publish-deployments.yml",
            ".github/codeql/codeql-config.yml",
            ".github/scripts/bootstrap-common-workflows.sh",
        ])
        self.assertIn('    branches: ["main"]', event_block(BOOTSTRAP, "push"))
        self.assertIn("  workflow_dispatch:", BOOTSTRAP)
        self.assertNotIn(BOOTSTRAP_PATH, event_paths(BOOTSTRAP, "push"))

    def test_stage_ownership_is_explicit_and_other_exclusions_are_preserved(self):
        ignored = re.search(r"^  REPOS_TO_IGNORE: '([^']+)'$", BOOTSTRAP, re.M)
        self.assertIsNotNone(ignored)
        self.assertEqual(json.loads(ignored[1]), [
            "Workflows", "cratis.github.io", "StudioIssues", "Documentation", "cratis.studio",
            "Automation", ".github", "Dockerfiles", "AI", "Templates", "Dockerfiles",
            "release-action", "Chronicle.Dapr", "Chronicle.Wolverine", "Stage",
        ])
        self.assertIn("# Stage - owns its reviewed package-update/kernel guard workflow and pin;", BOOTSTRAP)
        self.assertIn("#   bootstrap must not overwrite it", BOOTSTRAP)
        self.assertIn("REPOS_IGNORE: ${{ env.REPOS_TO_IGNORE }}", BOOTSTRAP)
        self.assertIn("bash .github/scripts/bootstrap-common-workflows.sh", BOOTSTRAP)

    def test_other_organization_write_template_triggers_are_preserved(self):
        templates = (ROOT / ".github/workflows/propagate-pr-templates.yml").read_text()
        self.assertEqual(event_paths(templates, "push"), [
            ".github/ISSUE_TEMPLATE/**", ".github/pull_request_template.md",
        ])
        self.assertIn('    branches: ["main"]', event_block(templates, "push"))
        self.assertIn("  workflow_dispatch:", templates)

    def test_effective_changed_files_do_not_trigger_organization_writes(self):
        # CI supplies the PR base or pre-push SHA and fetches history. Local runs
        # include all staged/unstaged changes and untracked, nonignored files.
        # This intentionally fails if a rollout is bundled with a watched write
        # trigger: that separate deployment effect needs explicit review.
        base = os.environ.get("PACKAGE_UPDATE_DIFF_BASE") or "HEAD"
        self.assertRegex(base, r"^(?:HEAD|[0-9a-f]{40})$")
        self.assertNotEqual(base, "0" * 40, "A real comparison base is required")
        changed = set(filter(None, git("diff", "--name-only", "--no-renames", "-z", base, "--").split("\0")))
        changed.update(filter(None, git("ls-files", "--others", "--exclude-standard", "-z").split("\0")))
        patterns = event_paths(BOOTSTRAP, "push") + event_paths(
            (ROOT / ".github/workflows/propagate-pr-templates.yml").read_text(), "push")
        self.assertEqual(sorted(path for path in changed if any(
            fnmatch.fnmatchcase(path, pattern) for pattern in patterns)), [],
            "Changed files would trigger organization-wide writes")
        # Also keep a non-vacuous guard when run on a clean checkout without a CI base.
        rollout = {".github/workflows/update-packages.yml", BOOTSTRAP_PATH, VERIFY_PATH,
                   TEST_PATH, ".github/scripts/tests/update-packages.test.py", "README.md"}
        self.assertFalse(any(fnmatch.fnmatchcase(path, pattern) for path in rollout for pattern in patterns))

    def test_ci_runs_safety_suite_on_both_relevant_events(self):
        required = {".github/workflows/update-packages.yml", BOOTSTRAP_PATH, VERIFY_PATH,
                    TEST_PATH, ".github/scripts/tests/update-packages.test.py"}
        for event in ["pull_request", "push"]:
            self.assertTrue(required.issubset(event_paths(VERIFY, event)))
        self.assertIn("python3 " + TEST_PATH, VERIFY)
        self.assertIn("python3 .github/scripts/tests/update-packages.test.py", VERIFY)
        self.assertIn("fetch-depth: 0", VERIFY)
        self.assertIn("PACKAGE_UPDATE_DIFF_BASE: ${{ github.event.pull_request.base.sha || github.event.before }}", VERIFY)
        self.assertNotIn("continue-on-error:", VERIFY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
