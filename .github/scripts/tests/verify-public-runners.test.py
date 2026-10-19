# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline public-runner checker fixtures; no token or live GitHub calls."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/verify-public-runners.py"
BOOTSTRAP = (ROOT / ".github/scripts/bootstrap-common-workflows.sh").read_text()
FIXTURES = Path(__file__).parent / "fixtures/public-runners"
spec = importlib.util.spec_from_file_location("checker", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def scan(case, **options):
    options.setdefault("local_repos", {"Workflows": str(FIXTURES / case / "_workflows")})
    return checker.scan_directory(FIXTURES / case, "Fixture", **options)


def run(*arguments):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, text=True, timeout=60)


def files_of(result):
    return sorted({name for _, name, _ in result.findings})


class FakeAPI:
    """Serves canned `gh api` responses keyed by endpoint; unknown endpoints fail like an API error."""

    def __init__(self, repositories, workflows, variables=None, remote=None):
        self.responses = {"orgs/Cratis/repos?type=all&per_page=100&page=1": repositories}
        for repo, files in workflows.items():
            base = f"repos/Cratis/{repo}/contents/.github/workflows"
            self.responses[base] = [{"name": name, "path": f".github/workflows/{name}", "type": "file"} for name in files]
            for name, text in files.items():
                self.responses[f"{base}/{name}"] = self.encoded(text)
        for repo, values in (variables or {}).items():
            self.responses[f"repos/Cratis/{repo}/actions/organization-variables?per_page=30&page=1"] = {"variables": []}
            self.responses[f"repos/Cratis/{repo}/actions/variables?per_page=30&page=1"] = {
                "variables": [{"name": key, "value": value} for key, value in values.items()]}
        for key, text in (remote or {}).items():
            self.responses[key] = self.encoded(text)

    @staticmethod
    def encoded(text):
        return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

    def __call__(self, endpoint):
        if endpoint not in self.responses:
            if "/contents/" in endpoint:
                raise checker.NotFound(endpoint)
            raise checker.CouldNotRun(f"unexpected endpoint {endpoint}")
        return self.responses[endpoint]


class LiteralTests(unittest.TestCase):
    def test_self_hosted_labels_fail(self):
        for case in ["literal-self-hosted", "literal-small", "self-hosted-label", "list-self-hosted", "labels-object"]:
            result = scan(case)
            self.assertEqual(files_of(result), ["ci.yml"], case)
            self.assertIn("self-hosted label", result.findings[0][2], case)

    def test_runner_group_fails(self):
        self.assertIn("runner group 'org-runners'", scan("group-object").findings[0][2])

    def test_pull_request_target_is_checked(self):
        self.assertEqual(files_of(scan("pull-request-target")), ["ci.yml"])

    def test_hosted_runners_pass_and_are_counted(self):
        for case in ["hosted", "hosted-fallback", "matrix-hosted"]:
            result = scan(case)
            self.assertEqual((result.subjects, result.findings), (1, []), case)

    def test_push_only_workflow_is_not_checked(self):
        result = scan("push-only")
        self.assertEqual((result.files, result.subjects, result.findings), (1, 0, []))

    def test_matrix_include_can_select_self_hosted(self):
        self.assertEqual(files_of(scan("matrix-self-hosted")), ["ci.yml"])

    def test_private_repository_is_never_reported(self):
        result = scan("literal-self-hosted", public=False)
        self.assertEqual((result.repositories, result.private, result.subjects, result.findings), (1, 1, 0, []))


class ExpressionTests(unittest.TestCase):
    def test_vars_and_inputs_fallback_to_self_hosted_fail(self):
        self.assertEqual(files_of(scan("vars-fallback")), ["ci.yml"])
        self.assertEqual(files_of(scan("inputs-fallback")), ["ci.yml"])

    def test_set_variable_replaces_the_fallback(self):
        self.assertEqual(scan("vars-fallback", variables={"RUNNER_GATE": "ubuntu-latest"}).findings, [])
        hosted = scan("hosted-fallback", variables={"runner_gate": "cratis-arc"})
        self.assertEqual(files_of(hosted), ["ci.yml"])
        self.assertIn("'cratis-arc'", hosted.findings[0][2])

    def test_unresolvable_expression_fails_as_unresolved(self):
        for case in ["unresolved", "unresolved-mixed"]:
            result = scan(case)
            self.assertEqual(files_of(result), ["ci.yml"], case)
            self.assertIn("unresolved runner", result.findings[0][2], case)


class ReusableWorkflowTests(unittest.TestCase):
    def test_callee_input_default_to_self_hosted_fails(self):
        result = scan("reusable-default")
        self.assertEqual(files_of(result), ["caller.yml"])
        self.assertIn("gate.yml > job verify", result.findings[0][2])

    def test_callee_input_default_that_is_hosted_passes(self):
        self.assertEqual(scan("reusable-hosted-default").findings, [])

    def test_caller_with_override_to_self_hosted_fails_despite_hosted_default(self):
        self.assertEqual(files_of(scan("reusable-override")), ["caller.yml"])

    def test_caller_with_hosted_value_overrides_self_hosted_default(self):
        self.assertEqual(scan("reusable-override-hosted").findings, [])

    def test_input_without_default_or_value_is_unresolved(self):
        self.assertIn("resolves to no label", scan("reusable-no-default").findings[0][2])

    def test_missing_local_callee_is_unresolved(self):
        self.assertIn("was not found", scan("reusable-missing").findings[0][2])

    def test_callee_is_followed_only_from_a_pull_request_caller(self):
        self.assertEqual(scan("reusable-not-pull-request").findings, [])

    def test_cratis_callee_is_followed_through_a_local_repository_mapping(self):
        self.assertEqual(files_of(scan("remote-callee")), ["caller.yml"])
        self.assertEqual(scan("remote-callee-hosted").findings, [])

    def test_cratis_callee_without_a_source_is_unresolved(self):
        result = scan("remote-callee", local_repos={})
        self.assertIn("is not available offline", result.findings[0][2])

    def test_external_callee_is_not_followed_but_counted(self):
        result = scan("external-callee")
        self.assertEqual((result.external, result.findings), (1, []))

    def test_callee_that_calls_itself_is_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "loop.yml").write_text("on: pull_request\njobs:\n  again:\n    uses: ./.github/workflows/loop.yml\n")
            result = checker.scan_directory(directory, "Fixture")
        self.assertIn("calls itself", result.findings[0][2])


WORKFLOWS = {"ci.yml": "on: pull_request\njobs:\n  build:\n    runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}\n"}
LISTING = [{"name": "Public", "visibility": "public", "private": False},
           {"name": "Hidden", "visibility": "private", "private": True},
           {"name": "Old", "visibility": "public", "private": False, "archived": True}]


class OrganizationTests(unittest.TestCase):
    def test_sweep_checks_public_repositories_and_skips_private_and_archived(self):
        api = FakeAPI(LISTING, {"Public": WORKFLOWS, "Hidden": {"ci.yml": "on: pull_request\njobs:\n  b:\n    runs-on: cratis-arc\n"}},
                      {"Public": {}})
        result = checker.scan_organization(api)
        self.assertEqual((result.repositories, result.private, result.files, result.subjects, result.findings),
                         (2, 1, 1, 1, []))

    def test_variable_pointing_at_self_hosted_is_a_finding(self):
        api = FakeAPI(LISTING[:1], {"Public": WORKFLOWS}, {"Public": {"RUNNER_GATE": "cratis-arc"}})
        result = checker.scan_organization(api)
        self.assertEqual([(repo, name) for repo, name, _ in result.findings], [("Public", "ci.yml")])

    def test_unreadable_variables_use_the_fallback_and_are_noted(self):
        output = io.StringIO()
        result = checker.scan_organization(FakeAPI(LISTING[:1], {"Public": WORKFLOWS}))
        with redirect_stdout(output):
            self.assertEqual(checker.report(result, require_subjects=True), 0)
        self.assertEqual(result.unreadable_variables, ["Public"])
        self.assertIn("variables were not readable for 1 public repositories", output.getvalue())

    def test_remote_cratis_callee_is_fetched_with_its_ref(self):
        caller = "on: pull_request\njobs:\n  g:\n    uses: Cratis/Workflows/.github/workflows/gate.yml@main\n"
        gate = (FIXTURES / "remote-callee/_workflows/gate.yml").read_text()
        api = FakeAPI(LISTING[:1], {"Public": {"ci.yml": caller}}, {"Public": {}},
                      {"repos/Cratis/Workflows/contents/.github/workflows/gate.yml?ref=main": gate})
        self.assertEqual(files_of(checker.scan_organization(api)), ["ci.yml"])

    def test_zero_repositories_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(FakeAPI([{"name": "Old", "archived": True}], {}))

    def test_api_error_could_not_run(self):
        def failing(endpoint):
            raise checker.CouldNotRun("gh api failed")
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(failing)

    def test_no_pull_request_jobs_in_the_organization_could_not_run(self):
        result = checker.scan_organization(FakeAPI(LISTING[:1], {"Public": {"ci.yml": "on: push\njobs: {}\n"}}, {"Public": {}}))
        with self.assertRaises(checker.CouldNotRun):
            checker.report(result, require_subjects=True)

    def test_unparsable_workflow_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            checker.scan_organization(FakeAPI(LISTING[:1], {"Public": {"ci.yml": "on: [push\n"}}, {"Public": {}}))


def render_policy_callers(releases, runner):
    """Render the bootstrap's pull-request policy caller with the script's own render_policy_wrapper."""
    function = re.search(r"^render_policy_wrapper\(\) \{.*?^\}$", BOOTSTRAP, re.S | re.M).group(0)
    payload = re.search(r'BOOTSTRAPPED_FILES\["\.github/workflows/verify-pull-request-policy\.yml"\]="([^"]+)"', BOOTSTRAP).group(1)
    template = base64.b64decode(payload).decode()
    rendered = subprocess.run(["bash", "-c", f'{function}\nrender_policy_wrapper "$1" "$2" "$3"', "bash", template, releases, runner],
                              capture_output=True, text=True, timeout=30, check=True).stdout
    return rendered


def bootstrapped_workflows():
    """Every plain workflow the bootstrap installs, decoded, except the policy caller that is rendered."""
    payloads = re.findall(r'^BOOTSTRAPPED_FILES\["(\.github/workflows/[^"]+)"\]="([^"]+)"', BOOTSTRAP, re.M)
    return {Path(path).name: base64.b64decode(payload).decode() for path, payload in payloads
            if "verify-pull-request-policy" not in path}


class BootstrapRenderingTests(unittest.TestCase):
    def check(self, rendered, public=True):
        with tempfile.TemporaryDirectory() as directory:
            for name, text in {**bootstrapped_workflows(), "verify-pull-request-policy.yml": rendered}.items():
                (Path(directory) / name).write_text(text, encoding="utf-8")
            return checker.scan_directory(directory, "Rendered", public=public,
                                          local_repos={"Workflows": str(ROOT / ".github/workflows")})

    def test_public_callers_never_resolve_to_a_self_hosted_runner(self):
        for releases in ("true", "false"):
            result = self.check(render_policy_callers(releases, "ubuntu-latest"))
            self.assertEqual(result.findings, [], releases)
            self.assertGreaterEqual(result.subjects, 1)

    def test_private_callers_would_be_findings_if_rendered_into_a_public_repository(self):
        result = self.check(render_policy_callers("true", "cratis-arc-small"))
        self.assertEqual(files_of(result), ["verify-pull-request-policy.yml"])

    def test_private_callers_are_not_reported_for_a_private_repository(self):
        result = self.check(render_policy_callers("true", "cratis-arc-small"), public=False)
        self.assertEqual(result.findings, [])

    def test_workflows_own_workflows_have_no_public_findings(self):
        result = checker.scan_directory(ROOT / ".github/workflows", "Workflows",
                                        local_repos={"Workflows": str(ROOT / ".github/workflows")})
        self.assertEqual(result.findings, [])
        self.assertGreaterEqual(result.subjects, 1)


class CommandLineTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = run("--self-test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_self_test_fails_when_detection_is_broken(self):
        output = io.StringIO()
        with patch.object(checker, "is_self_hosted", lambda label: False), redirect_stdout(output):
            self.assertEqual(checker.self_test(), 1)
        self.assertIn("planted literal.yml (self-hosted) was not found", output.getvalue())

    def test_clean_directory_exits_zero_and_prints_counts(self):
        result = run("--dir", str(FIXTURES / "hosted"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked 1 repositories (0 private skipped), 1 workflow files, 1 pull_request jobs", result.stdout)

    def test_violation_exits_one_naming_the_file(self):
        result = run("--dir", str(FIXTURES / "literal-self-hosted"), "--repo", "Components")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Components/ci.yml: job build: runs on self-hosted label 'cratis-arc'", result.stdout)

    def test_private_visibility_exits_zero(self):
        result = run("--dir", str(FIXTURES / "literal-self-hosted"), "--visibility", "private")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("1 private skipped", result.stdout)

    def test_local_repo_mapping_reaches_the_callee(self):
        result = run("--dir", str(FIXTURES / "remote-callee"), "--local-repo", f"Workflows={FIXTURES / 'remote-callee/_workflows'}")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_bad_local_repo_exits_two(self):
        self.assertEqual(run("--dir", str(FIXTURES / "hosted"), "--local-repo", "Workflows").returncode, 2)

    def test_empty_directory_exits_two(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run("--dir", directory).returncode, 2)

    def test_missing_directory_exits_two(self):
        self.assertEqual(run("--dir", str(FIXTURES / "does-not-exist")).returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
