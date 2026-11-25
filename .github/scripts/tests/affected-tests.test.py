# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the affected-tests workflow, its composite action and its two scripts.

The map write-back runs against real local git remotes, including a push that loses a race, because that
is where a bot loses work or rewrites history.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/affected-tests.yml"
ACTION = ROOT / ".github/actions/affected-cli/action.yml"
OUTPUTS_SCRIPT = ROOT / ".github/scripts/affected-select-outputs.py"
COMMIT_SCRIPT = ROOT / ".github/scripts/commit-affected-maps.sh"
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
BASH = shutil.which("bash")
MAPS = ".cratis/affected/maps"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(document):
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return document.get("on", document.get(True))


def all_steps(document):
    for name, job in document["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


class WorkflowContract(unittest.TestCase):
    def test_is_reusable_and_requires_a_pinned_cli_version(self):
        call = triggers(workflow())["workflow_call"]
        self.assertTrue(call["inputs"]["cli-version"]["required"])
        self.assertNotIn("default", call["inputs"]["cli-version"])

    def test_runner_is_an_input_defaulting_to_a_hosted_runner(self):
        self.assertEqual(triggers(workflow())["workflow_call"]["inputs"]["runs-on"]["default"], "ubuntu-latest")

    def test_every_job_has_a_timeout(self):
        for name, job in workflow()["jobs"].items():
            self.assertIn("timeout-minutes", job, name)

    def test_pull_request_runs_are_cancelled_but_merge_runs_are_not(self):
        concurrency = workflow()["concurrency"]
        self.assertEqual(concurrency["cancel-in-progress"], "${{ github.event_name == 'pull_request' }}")
        self.assertIn("github.ref", concurrency["group"])

    def test_permissions_are_minimal(self):
        document = workflow()
        self.assertEqual(document["permissions"], {"contents": "read"})
        writers = [name for name, job in document["jobs"].items() if job.get("permissions", {}).get("contents") == "write"]
        self.assertEqual(writers, ["merge"])

    def test_third_party_actions_are_pinned_by_full_sha(self):
        for name, step in all_steps(workflow()):
            uses = step.get("uses")
            if uses and not uses.startswith(("Cratis/", "./")):
                self.assertRegex(uses, PINNED, f"{name}: {uses}")

    def test_one_aggregator_always_runs_and_waits_for_every_other_job(self):
        document = workflow()
        aggregators = [job for job in document["jobs"].values() if job.get("if") == "always()"]
        self.assertEqual(len(aggregators), 1)
        others = set(document["jobs"]) - {name for name, job in document["jobs"].items() if job.get("if") == "always()"}
        self.assertEqual(set(aggregators[0]["needs"]), others)
        script = aggregators[0]["steps"][0]["run"]
        self.assertIn("needs.*.result", aggregators[0]["steps"][0]["env"]["RESULTS"])
        self.assertIn("skipped", script)

    def test_only_a_pull_request_can_skip_work(self):
        select = next(step for name, step in all_steps(workflow()) if step.get("id") == "select")
        self.assertIn('if [ "$MODE" != pr ]', select["run"])
        self.assertIn('echo "run-tests=true"', select["run"])

    def test_failed_selection_still_runs_the_tests(self):
        select = next(step for name, step in all_steps(workflow()) if step.get("id") == "select")
        self.assertRegex(select["run"], r"if ! cratis affected select[\s\S]*run-tests=true")

    def test_self_hosted_setup_uses_a_writable_dotnet_directory(self):
        for name, step in all_steps(workflow()):
            if step.get("uses", "").startswith("actions/setup-dotnet@"):
                self.assertEqual(step["env"]["DOTNET_INSTALL_DIR"], "${{ runner.temp }}/dotnet", name)

    def test_no_inputs_are_interpolated_into_shell_scripts(self):
        for name, step in all_steps(workflow()):
            self.assertNotRegex(step.get("run", ""), r"\$\{\{\s*(inputs|github)\.", f"{name}: {step.get('name')}")

    def test_map_commits_do_not_skip_ci(self):
        code = "\n".join(line for line in COMMIT_SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ["[skip ci]", "--force", "rebase", "--amend", "reset --hard"]:
            self.assertNotIn(forbidden, code)

    def test_maps_are_only_written_from_the_merge_job(self):
        document = workflow()
        self.assertEqual(document["jobs"]["merge"]["if"], "needs.detect.outputs.mode == 'merge'")
        self.assertEqual(document["jobs"]["test"]["if"], "needs.detect.outputs.run-tests == 'true' && needs.detect.outputs.mode != 'merge'")


class ActionContract(unittest.TestCase):
    def test_requires_an_exact_version(self):
        action = yaml.safe_load(ACTION.read_text())
        self.assertTrue(action["inputs"]["version"]["required"])
        run = action["runs"]["steps"][0]["run"]
        self.assertIn("exact version", run)
        self.assertIn("--tool-path", run)

    def test_rejects_ranges_and_latest(self):
        script = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]["run"]
        pattern = re.search(r'=~ (\^.*\$)\ \]\]', script).group(1)
        for good in ["3.150.0", "3.150.0-preview.1"]:
            self.assertRegex(good, pattern.replace("[0-9A-Za-z.-]", "[0-9A-Za-z.-]"))
        for bad in ["latest", "3.*", "3.150", "[3.0,4.0)", ""]:
            self.assertNotRegex(bad, pattern)


def run_outputs(report):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "selection.json"
        output = Path(folder) / "github-output"
        if report is not None:
            path.write_text(report if isinstance(report, str) else json.dumps(report))
        result = subprocess.run(
            ["python3", "-B", str(OUTPUTS_SCRIPT), str(path)],
            env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True, check=True)
        return dict(line.split("=", 1) for line in output.read_text().splitlines()), result


def project(mode):
    return {"selection": {"project": "p", "mode": mode}}


class SelectOutputs(unittest.TestCase):
    def test_run_tests_when_any_project_runs(self):
        values, _ = run_outputs({"runEverything": False, "projects": [project("skip"), project("filtered")]})
        self.assertEqual(values, {"run-tests": "true", "run-everything": "false", "test-projects": "1"})

    def test_skip_the_test_job_when_every_project_is_skipped(self):
        values, _ = run_outputs({"runEverything": False, "projects": [project("skip"), project("skip")]})
        self.assertEqual(values["run-tests"], "false")

    def test_skip_the_test_job_when_there_are_no_test_projects(self):
        values, _ = run_outputs({"runEverything": False, "projects": []})
        self.assertEqual(values["run-tests"], "false")

    def test_run_everything_is_reported(self):
        values, _ = run_outputs({"runEverything": True, "projects": [project("all")]})
        self.assertEqual((values["run-tests"], values["run-everything"]), ("true", "true"))

    def test_unreadable_or_corrupted_reports_run_the_tests(self):
        for report in [None, "not json", {"projects": "x"}, {"runEverything": False}]:
            values, result = run_outputs(report)
            self.assertEqual(values["run-tests"], "true", report)
            self.assertIn("running the tests", result.stderr)


class Remote:
    """A bare remote, with a working clone that did the tested merge and a second clone that races it."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.remote = self.folder / "remote.git"
        self.git(self.folder, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        self.clone = self.make_clone("clone")
        self.write(self.clone, "src.txt", "one")
        self.tested = self.commit(self.clone, "tested", push=True)
        self.other = self.make_clone("other")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()

    def make_clone(self, name):
        path = self.folder / name
        subprocess.run(["git", "clone", "--quiet", str(self.remote), str(path)], capture_output=True, check=True)
        for key, value in [("user.name", "tester"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")]:
            self.git(path, "config", key, value)
        return path

    def write(self, repo, relative, text):
        path = Path(repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, repo, message, push=False):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "--quiet", "-m", message)
        if push:
            self.git(repo, "push", "--quiet", "origin", "HEAD:main")
        return self.git(repo, "rev-parse", "HEAD")

    def tip(self):
        return self.git(self.remote, "rev-parse", "main")

    def count(self):
        return int(self.git(self.remote, "rev-list", "--count", "main"))

    def commit_maps(self, **extra):
        env = {**os.environ, "BRANCH": "main", "TESTED_SHA": self.tested, "GIT_TERMINAL_PROMPT": "0", **extra}
        return subprocess.run([BASH, str(COMMIT_SCRIPT)], cwd=self.clone, env=env, capture_output=True, text=True)


@unittest.skipIf(BASH is None, "bash is required")
class CommitMaps(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.remote = Remote(self._folder.name)

    def test_nothing_changed_commits_nothing(self):
        result = self.remote.commit_maps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote.tip(), self.remote.tested)

    def test_changed_maps_are_committed_and_pushed_on_top_of_the_tested_commit(self):
        self.remote.write(self.remote.clone, f"{MAPS}/Source/A.Specs/A.Specs.csproj.json", "{}\n")
        result = self.remote.commit_maps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote.git(self.remote.remote, "rev-parse", "main~1"), self.remote.tested)
        self.assertEqual(self.remote.git(self.remote.remote, "log", "-1", "--format=%an"), "github-actions[bot]")
        self.assertNotIn("[skip ci]", self.remote.git(self.remote.remote, "log", "-1", "--format=%B"))

    def test_only_the_maps_are_committed(self):
        self.remote.write(self.remote.clone, f"{MAPS}/a.json", "{}\n")
        self.remote.write(self.remote.clone, "unrelated.txt", "build output")
        self.assertEqual(self.remote.commit_maps().returncode, 0)
        files = self.remote.git(self.remote.remote, "show", "--name-only", "--format=", "main").split()
        self.assertEqual(files, [f"{MAPS}/a.json"])

    def test_deleted_maps_are_committed(self):
        self.remote.write(self.remote.clone, f"{MAPS}/old.json", "{}\n")
        self.remote.tested = self.remote.commit(self.remote.clone, "map", push=True)
        (self.remote.clone / MAPS / "old.json").unlink()
        self.assertEqual(self.remote.commit_maps().returncode, 0)
        self.assertEqual(self.remote.git(self.remote.remote, "ls-tree", "-r", "--name-only", "main").split(), ["src.txt"])

    def test_branch_that_moved_past_the_tested_commit_is_left_alone(self):
        self.remote.write(self.remote.other, "newer.txt", "newer")
        newer = self.remote.commit(self.remote.other, "newer", push=True)
        self.remote.write(self.remote.clone, f"{MAPS}/a.json", "{}\n")
        result = self.remote.commit_maps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote.tip(), newer)
        self.assertIn("newer run will update the maps", result.stdout)

    def test_push_that_loses_a_race_is_merged_and_retried_without_rewriting_history(self):
        self.remote.write(self.remote.clone, f"{MAPS}/a.json", "{}\n")
        # The hook moves the remote once, just before the first push, so that push is rejected as non-fast-forward.
        marker = self.remote.folder / "raced"
        racer = self.remote.other
        self.remote.write(racer, "raced.txt", "raced")
        raced = self.remote.commit(racer, "raced")
        hook = self.remote.clone / ".git/hooks/pre-push"
        hook.write_text(f"#!/bin/sh\nif [ ! -e '{marker}' ]; then touch '{marker}'; git -C '{racer}' push --quiet origin HEAD:main; fi\nexit 0\n")
        hook.chmod(0o755)
        before = self.remote.count()
        result = self.remote.commit_maps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Push rejected on attempt 1", result.stdout)
        self.assertEqual(self.remote.git(self.remote.remote, "merge-base", "--is-ancestor", raced, "main") or "ok", "ok")
        files = self.remote.git(self.remote.remote, "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn(f"{MAPS}/a.json", files)
        self.assertIn("raced.txt", files)
        self.assertGreater(self.remote.count(), before + 1)  # the racing commit, our map commit and the merge

    def test_a_push_that_never_succeeds_is_not_fatal(self):
        self.remote.write(self.remote.clone, f"{MAPS}/a.json", "{}\n")
        hook = self.remote.clone / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.remote.commit_maps(MAX_ATTEMPTS="2")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Could not push the coverage maps after 2 attempts", result.stdout + result.stderr)
        self.assertEqual(self.remote.tip(), self.remote.tested)


if __name__ == "__main__":
    unittest.main()
