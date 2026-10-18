# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline release-concurrency checker fixtures; no token or live GitHub calls."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/verify-release-concurrency.py"
FIXTURES = Path(__file__).parent / "fixtures/release-concurrency"
spec = importlib.util.spec_from_file_location("checker", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def scan(case, repo="Fixture"):
    return checker.scan_directory(FIXTURES / case, repo)


def kinds(result):
    return sorted((name, checker.kind(reason)) for _, name, reason in result.violations)


def run(*arguments):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, text=True, timeout=60)


class FakeAPI:
    """Serves canned `gh api` responses keyed by endpoint; unknown endpoints fail like an API error."""

    def __init__(self, repositories, workflows):
        self.responses = {f"orgs/Cratis/repos?type=all&per_page=100&page=1": repositories}
        for repo, files in workflows.items():
            base = f"repos/Cratis/{repo}/contents/.github/workflows"
            self.responses[base] = [{"name": name, "path": f".github/workflows/{name}", "type": "file"} for name in files]
            for name, text in files.items():
                self.responses[f"{base}/{name}"] = {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

    def __call__(self, endpoint):
        if endpoint not in self.responses:
            if "/contents/.github/workflows" in endpoint:
                raise checker.NotFound(endpoint)
            raise checker.CouldNotRun(f"unexpected endpoint {endpoint}")
        return self.responses[endpoint]


QUEUED = (FIXTURES / "push-queue-max/publish.yml").read_text()
UNQUEUED = (FIXTURES / "no-concurrency/publish.yml").read_text()


class FixtureTests(unittest.TestCase):
    def test_push_with_queue_max_passes(self):
        result = scan("push-queue-max")
        self.assertEqual((result.subjects, result.violations), (1, []))

    def test_pull_request_closed_pattern_passes(self):
        result = scan("pull-request-closed")
        self.assertEqual((result.subjects, result.violations), (1, []))

    def test_cancel_in_progress_true_fails(self):
        self.assertIn(("publish.yml", "cancel"), kinds(scan("cancel-true")))

    def test_cancel_in_progress_expression_fails_even_with_queue_max(self):
        self.assertEqual(kinds(scan("cancel-expression")), [("release.yml", "cancel")])

    def test_missing_concurrency_fails(self):
        self.assertEqual(kinds(scan("no-concurrency")), [("publish.yml", "queue")])

    def test_job_level_queue_max_passes(self):
        result = scan("job-queue-max")
        self.assertEqual((result.subjects, result.violations), (1, []))

    def test_deploy_needs_no_queue_when_it_does_not_cancel(self):
        result = scan("deploy-latest-wins")
        self.assertEqual((result.subjects, result.violations), (1, []))

    def test_workflow_call_only_is_exempt(self):
        result = scan("reusable-only")
        self.assertEqual((result.files, result.subjects, result.violations), (1, 0, []))

    def test_verify_release_notes_is_not_a_subject(self):
        result = scan("not-a-subject")
        self.assertEqual((result.files, result.subjects, result.violations), (1, 0, []))

    def test_allowlisted_file_passes_only_for_its_repository(self):
        allowed = scan("allowlisted", repo="Studio")
        self.assertEqual((allowed.violations, allowed.allowlist_used), ([], {("Studio", "publish.yml")}))
        self.assertEqual(kinds(scan("allowlisted", repo="Other")), [("publish.yml", "queue")])

    def test_publish_template_passes(self):
        result = checker.scan_directory(ROOT / ".github/templates", "Workflows")
        self.assertEqual((result.subjects, result.violations), (1, []))


class SubjectNameTests(unittest.TestCase):
    def test_names(self):
        for name in ["publish.yml", "Release.yaml", "deploy-staging.yml", "preview-publish.yml"]:
            self.assertTrue(checker.is_subject_name(name), name)
        for name in ["verify-publish.yml", "verify-release-notes.yml", "release-notes.yml",
                     "release-intent.yml", "auto-approve-publish-deployments.yml", "build.yml", "publish.txt"]:
            self.assertFalse(checker.is_subject_name(name), name)


class OrganizationTests(unittest.TestCase):
    def test_sweep_skips_archived_repositories_and_counts_ones_without_workflows(self):
        api = FakeAPI([{"name": "Arc"}, {"name": "Empty"}, {"name": "Old", "archived": True}, {"name": "Studio"}],
                      {"Arc": {"publish.yml": QUEUED, "build.yml": UNQUEUED}, "Studio": {"publish.yml": UNQUEUED}})
        result = checker.scan_organization(api)
        self.assertEqual((result.repositories, result.files, result.subjects, result.violations), (3, 3, 2, []))

    def test_sweep_reports_violations_by_repository_and_file(self):
        api = FakeAPI([{"name": "Arc"}, {"name": "Studio"}], {"Arc": {"publish.yml": UNQUEUED}, "Studio": {"publish.yml": UNQUEUED}})
        self.assertEqual([(repo, name) for repo, name, _ in checker.scan_organization(api).violations], [("Arc", "publish.yml")])

    def test_allowlist_entry_that_is_no_longer_needed_fails(self):
        api = FakeAPI([{"name": "Studio"}], {"Studio": {"publish.yml": QUEUED}})
        violations = checker.scan_organization(api).violations
        self.assertEqual(len(violations), 1)
        self.assertIn("allowlist entry no longer needed", violations[0][2])

    def test_allowlist_entry_for_a_missing_file_fails(self):
        api = FakeAPI([{"name": "Arc"}], {"Arc": {"publish.yml": QUEUED}})
        self.assertEqual([(repo, name) for repo, name, _ in checker.scan_organization(api).violations], [("Studio", "publish.yml")])

    def test_zero_repositories_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(FakeAPI([{"name": "Old", "archived": True}], {}))

    def test_api_error_could_not_run(self):
        def failing(endpoint):
            raise checker.CouldNotRun("gh api failed")
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(failing)

    def test_zero_subjects_could_not_run(self):
        result = checker.scan_organization(FakeAPI([{"name": "Arc"}], {"Arc": {"build.yml": UNQUEUED}}))
        with self.assertRaises(checker.CouldNotRun):
            checker.report(result)

    def test_unparsable_subject_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(FakeAPI([{"name": "Arc"}], {"Arc": {"publish.yml": "on: [push\n"}}))


class CommandLineTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = run("--self-test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_self_test_fails_when_detection_is_broken(self):
        output = io.StringIO()
        with patch.object(checker, "cancels", lambda concurrency: False), redirect_stdout(output):
            self.assertEqual(checker.self_test(), 1)
        self.assertIn("planted publish-cancel-true.yml (cancel) was not found", output.getvalue())

    def test_clean_directory_exits_zero_and_prints_counts(self):
        result = run("--dir", str(FIXTURES / "push-queue-max"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked 1 repositories, 1 workflow files, 1 release subjects, 0 violations", result.stdout)

    def test_violation_exits_one_naming_the_file(self):
        result = run("--dir", str(FIXTURES / "no-concurrency"), "--repo", "Components")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Components/publish.yml: no 'queue: max'", result.stdout)

    def test_empty_directory_exits_two(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run("--dir", directory).returncode, 2)

    def test_directory_without_subjects_exits_two(self):
        self.assertEqual(run("--dir", str(FIXTURES / "not-a-subject")).returncode, 2)

    def test_missing_directory_exits_two(self):
        self.assertEqual(run("--dir", str(FIXTURES / "does-not-exist")).returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
