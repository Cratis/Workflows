# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline actionlint red controls and reusable-workflow smoke coverage."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
CONFIG = ROOT / ".github/actionlint.yaml"
FIXTURES = Path(__file__).parent / "fixtures/workflow-lint"
NOT_SMOKEABLE = {
    "update-packages.yml": "requires secrets and writes package updates",
    "cleanup-pr-artifacts.yml": "requires secrets and deletes repository artifacts",
    "trigger-documentation-build.yml": "dispatches to another repository using a secret",
    "normalize-dependabot-labels.yml": "writes pull request labels",
    "verify-pull-request-policy.yml": "holds Workflows PRs to consumer release policy; decide separately",
    "verify-release-notes.yml": "holds Workflows PRs to consumer release policy; decide separately",
    "verify-release-intent.yml": "holds Workflows PRs to consumer release policy; decide separately",
}


def document(path):
    return yaml.safe_load(path.read_text())


def triggers(workflow):
    # PyYAML's YAML 1.1 loader reads the unquoted `on` key as True.
    value = workflow.get("on", workflow.get(True, {}))
    return {value} if isinstance(value, str) else set(value)


def lint(case, config=True):
    args = ["actionlint", "-pyflakes=", "-shellcheck="]
    # Always specify a config: discovery would otherwise find the repository's shared config.
    args += ["-config-file", str(CONFIG if config else FIXTURES / "no-ignores.yaml")]
    files = sorted((FIXTURES / case / ".github/workflows").glob("*.yml"))
    return subprocess.run(args + [str(path) for path in files], cwd=FIXTURES / case,
                          capture_output=True, text=True, timeout=30)


class WorkflowLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("actionlint"):
            raise RuntimeError("actionlint is required; run .github/scripts/install-actionlint.sh in CI")

    def test_broken_fixture_fails_with_expected_rule_ids(self):
        result = lint("broken")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(set(re.findall(r"\[([a-z-]+)\]", result.stdout)),
                         {"expression", "job-needs", "syntax-check"}, result.stdout)
        for diagnostic in ['property "nope"', '"missing-job"', 'unexpected key "invalid-key"']:
            self.assertIn(diagnostic, result.stdout)

    def test_queue_and_shared_runner_labels_pass_with_shared_config(self):
        result = lint("good")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # A red control proves the fixture actually needs the narrowly scoped exception.
        without_config = lint("good", config=False)
        self.assertEqual(without_config.returncode, 1, without_config.stdout + without_config.stderr)
        self.assertIn('unexpected key "queue" for "concurrency" section', without_config.stdout)

    def test_every_reusable_is_smoked_or_has_an_explicit_reason(self):
        reusables = {path.name for path in WORKFLOWS.iterdir()
                     if path.suffix in {".yml", ".yaml"} and "workflow_call" in triggers(document(path))}
        smoke = document(WORKFLOWS / "smoke-workflows.yml")
        called = {job["uses"].removeprefix("./.github/workflows/")
                  for job in smoke["jobs"].values() if "uses" in job}
        self.assertTrue(reusables, "No reusable workflows were discovered")
        self.assertTrue(called, "No reusable workflows are smoke-called")
        self.assertEqual(called & NOT_SMOKEABLE.keys(), set(), "Remove obsolete exemptions")
        self.assertEqual(reusables, called | NOT_SMOKEABLE.keys(),
                         "Smoke-call new reusables or give a concrete NOT_SMOKEABLE reason")
        self.assertTrue(all(reason.strip() for reason in NOT_SMOKEABLE.values()))
        self.assertEqual(triggers(smoke), {"pull_request"})
        self.assertEqual(smoke["permissions"], {"contents": "read"})
        for job in smoke["jobs"].values():
            if "uses" in job:
                self.assertTrue(job["uses"].startswith("./.github/workflows/"))
                self.assertNotIn("secrets", job)

    def test_lint_step_uses_repo_config_or_shared_default_and_keeps_workflows_with_extras(self):
        workflow = document(WORKFLOWS / "workflow-lint.yml")
        script = workflow["jobs"]["lint"]["steps"][-1]["run"]
        for config_name in ["actionlint.yaml", "actionlint.yml", None]:
            with self.subTest(config=config_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(FIXTURES / "good/.github", root / ".github")
                shared = root / ".cratis-workflows/.github"
                shared.mkdir(parents=True)
                shutil.copy(CONFIG, shared / "actionlint.yaml")
                if config_name:
                    shutil.copy(CONFIG, root / ".github" / config_name)
                    (shared / "actionlint.yaml").write_text("invalid config: true\n")
                extra = root / ".github/templates/extra.yml"
                extra.parent.mkdir()
                shutil.copy(FIXTURES / "good/.github/workflows/queue.yml", extra)
                env = dict(os.environ, SHELLCHECK="false", EXTRA_FILES=str(extra.relative_to(root)))
                result = subprocess.run(["bash", "-c", script], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # A valid extra file must not hide a broken workflow from discovery.
                shutil.copy(FIXTURES / "broken/.github/workflows/broken.yml", root / ".github/workflows")
                result = subprocess.run(["bash", "-c", script], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("[job-needs]", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
