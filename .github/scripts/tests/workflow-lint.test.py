# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline actionlint/zizmor red controls and reusable-workflow smoke coverage."""
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
    "close-superseded-bump-pulls.yml": "comments on and closes pull requests and deletes head branches; requires writes",
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


def workflow_step(name):
    workflow = document(WORKFLOWS / "workflow-lint.yml")
    return next(step for step in workflow["jobs"]["lint"]["steps"] if step.get("name") == name)


def audit(root, mode):
    return subprocess.run(["bash", "-c", workflow_step("Audit workflow security")["run"]],
                          cwd=root, env=dict(os.environ, ZIZMOR_MODE=mode),
                          capture_output=True, text=True, timeout=30)


class WorkflowLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ["actionlint", "zizmor"]:
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} is required; run .github/scripts/install-{tool}.sh in CI")

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
        script = workflow_step("Lint workflows and additional files")["run"]
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

    def test_zizmor_blocking_fails_and_report_prints_the_same_findings(self):
        for mode in ["blocking", "report"]:
            with self.subTest(mode=mode):
                result = audit(FIXTURES / "security", mode)
                if mode == "blocking":
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for rule in ["unpinned-uses", "template-injection"]:
                    self.assertIn(f"[{rule}]", result.stdout + result.stderr)

    def test_zizmor_off_skips_install_and_audit_and_invalid_modes_fail(self):
        workflow = document(WORKFLOWS / "workflow-lint.yml")
        inputs = workflow.get("on", workflow.get(True))["workflow_call"]["inputs"]
        self.assertEqual(inputs["zizmor"]["default"], "report")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow_step("Install zizmor")["if"], "inputs.zizmor != 'off'")
        with tempfile.TemporaryDirectory() as directory:
            # No workflows here: an attempted audit would fail.
            result = audit(directory, "off")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = audit(directory, "typo")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid zizmor mode", result.stderr)

    def test_zizmor_discovers_caller_config_and_report_still_fails_on_tool_errors(self):
        for config_name in [".github/zizmor.yml", "zizmor.yml"]:
            with self.subTest(config=config_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(FIXTURES / "security/.github", root / ".github")
                config = root / config_name
                config.write_text("rules:\n  template-injection:\n    ignore: [unsafe.yml]\n")
                result = audit(root, "blocking")
                output = result.stdout + result.stderr
                self.assertIn("[unpinned-uses]", output)
                self.assertNotIn("[template-injection]", output)
                config.write_text("rules: [invalid config]\n")
                result = audit(root, "report")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


class InstallZizmorTests(unittest.TestCase):
    """The pinned archive checksum must reject a substituted download before extraction."""

    def run_installer(self, machine):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs, runner, github_path = root / "stubs", root / "runner", root / "github_path"
            stubs.mkdir()
            runner.mkdir()
            github_path.write_text("")
            # Offline: curl writes a substituted archive; uname selects the architecture branch.
            (stubs / "curl").write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                                        'printf substituted > "$out"\n')
            (stubs / "uname").write_text(f"#!/bin/sh\necho {machine}\n")
            # Fails if the installer reaches extraction.
            (stubs / "tar").write_text(f'#!/bin/sh\necho "$@" > "{root}/tar-called"\nexit 0\n')
            probe = subprocess.run(["sha256sum", "--check", "--strict"], input="", capture_output=True, text=True)
            if "usage" in probe.stderr.lower():
                # macOS portability shim; CI (Linux) uses the real sha256sum.
                (stubs / "sha256sum").write_text('#!/bin/sh\nexec shasum -a 256 -c -\n')
            for stub in stubs.iterdir():
                stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", RUNNER_TEMP=str(runner),
                       GITHUB_PATH=str(github_path))
            result = subprocess.run(["bash", str(ROOT / ".github/scripts/install-zizmor.sh")], env=env,
                                    capture_output=True, text=True, timeout=30)
            return (result, (root / "tar-called").exists(), (runner / "bin" / "zizmor").exists(),
                    github_path.read_text())

    def test_substituted_archive_is_rejected_on_both_architectures(self):
        for machine in ["x86_64", "aarch64"]:
            with self.subTest(machine=machine):
                result, extracted, installed, github_path = self.run_installer(machine)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(extracted)
                self.assertFalse(installed)
                self.assertEqual(github_path, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
