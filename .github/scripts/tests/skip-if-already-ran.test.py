# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Run the production composite script offline, with a fake gh and real jq."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / ".github/actions/skip-if-already-ran/action.yml"
BASH = shutil.which("bash")
JQ = shutil.which("jq")
SHA = "a" * 40
URL = "https://github.com/Cratis/Workflows/actions/runs/41"
FAKE_GH = '''
import json, os, subprocess, sys
from pathlib import Path
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
is_lookup = "/actions/runs/" in sys.argv[2]
if os.environ["FAIL_AT"] == ("lookup" if is_lookup else "runs"):
    sys.exit(1)
data = {"workflow_id": 7} if is_lookup else json.loads(Path(os.environ["RUNS"]).read_text())
result = subprocess.run([os.environ["JQ"], "-r", sys.argv[4]], input=json.dumps(data), text=True)
sys.exit(result.returncode)
'''


def load():
    return yaml.safe_load(ACTION.read_text())


def check_step():
    return next(s for s in load()["runs"]["steps"] if s.get("id") == "check")


def prior_run():
    return dict(id=41, event="schedule", head_sha=SHA, conclusion="success", html_url=URL)


def execute(runs=None, *, missing_gh=False, response=None, **overrides):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        for name in ("output", "summary", "calls"):
            (directory / name).write_text("")
        (directory / "runs").write_text(json.dumps(
            response if response is not None else {"workflow_runs": runs or []}))
        if not missing_gh:
            fake = directory / "gh"
            fake.write_text(f"#!{sys.executable}\n" + FAKE_GH)
            fake.chmod(0o755)
        env = dict(PATH=temp, GITHUB_OUTPUT=str(directory / "output"),
                   GITHUB_STEP_SUMMARY=str(directory / "summary"), GH_TOKEN="test-token",
                   EVENT_NAME="schedule", EVENTS="schedule", SHA=SHA, GITHUB_SHA=SHA,
                   REPO="Cratis/Workflows", RUN_ID="42", RUN_ATTEMPT="1",
                   FAIL_AT="", JQ=JQ, RUNS=str(directory / "runs"), CALLS=str(directory / "calls"))
        env.update(overrides)
        env["GITHUB_RUN_ATTEMPT"] = env["RUN_ATTEMPT"]
        result = subprocess.run([BASH, "-c", check_step()["run"]], env=env,
                                cwd=ROOT, capture_output=True, text=True)
        outputs = dict(line.split("=", 1) for line in (directory / "output").read_text().splitlines())
        calls = [json.loads(line) for line in (directory / "calls").read_text().splitlines()]
        return result, outputs, calls, (directory / "summary").read_text()


class ActionShapeTests(unittest.TestCase):
    def test_composite_contract(self):
        action = load()
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertEqual(set(action["outputs"]), {"skip", "previous-run-url"})
        for name in action["outputs"]:
            self.assertEqual(action["outputs"][name]["value"], "${{ steps.check.outputs." + name + " }}")
        self.assertEqual({key: value["default"] for key, value in action["inputs"].items()},
                         dict(events="schedule", sha="${{ github.sha }}", token="${{ github.token }}"))
        self.assertEqual(check_step()["shell"], "bash")
        self.assertTrue(check_step()["run"].startswith("set -euo pipefail\n"))
        self.assertNotIn("${{", check_step()["run"])
        expected = dict(EVENT_NAME="github.event_name", EVENTS="inputs.events", SHA="inputs.sha",
                        GH_TOKEN="inputs.token", REPO="github.repository", RUN_ID="github.run_id",
                        RUN_ATTEMPT="github.run_attempt")
        self.assertEqual(check_step()["env"], {k: "${{ " + v + " }}" for k, v in expected.items()})
        for step in action["runs"]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], re.compile(r"^[^@\s]+@[0-9a-f]{40}$"))

    def test_bash_syntax_and_shellcheck(self):
        script = check_step()["run"]
        subprocess.run([BASH, "-n"], input=script, text=True, check=True)
        if shutil.which("shellcheck"):
            subprocess.run(["shellcheck", "--shell=bash", "-"], input=script, text=True, check=True)


class ActionBehaviorTests(unittest.TestCase):
    def assert_result(self, execution, skip):
        result, outputs, calls, summary = execution
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs, {"skip": str(skip).lower(), "previous-run-url": URL if skip else ""})
        return result.stdout, calls, summary

    def test_manual_dispatch_always_runs_even_when_listed(self):
        _, calls, _ = self.assert_result(execute(EVENT_NAME="workflow_dispatch", EVENTS="workflow_dispatch"), False)
        self.assertEqual(calls, [])

    def test_unlisted_event_does_not_query(self):
        _, calls, _ = self.assert_result(execute(EVENT_NAME="pull_request"), False)
        self.assertEqual(calls, [])

    def test_event_matching_is_not_a_substring(self):
        _, calls, _ = self.assert_result(execute(EVENTS="schedule_other"), False)
        self.assertEqual(calls, [])

    def test_manual_rerun_does_not_query(self):
        _, calls, _ = self.assert_result(execute(RUN_ATTEMPT="2"), False)
        self.assertEqual(calls, [])

    def test_prior_success_skips_with_notice_and_summary_link(self):
        stdout, calls, summary = self.assert_result(execute(runs=[prior_run()]), True)
        self.assertIn("::notice title=skip-if-already-ran::", stdout)
        self.assertIn(URL, stdout)
        self.assertIn(URL, summary)
        self.assertEqual(calls[0], ["api", "repos/Cratis/Workflows/actions/runs/42", "--jq", ".workflow_id"])
        self.assertEqual(calls[1][1],
                         f"repos/Cratis/Workflows/actions/workflows/7/runs?head_sha={SHA}&status=success&per_page=100")

    def test_current_run_is_not_a_prior_success(self):
        run = prior_run()
        run["id"] = 42
        self.assert_result(execute(runs=[run]), False)

    def test_events_control_which_prior_runs_count(self):
        run = prior_run()
        run["event"] = "push"
        self.assert_result(execute(runs=[run]), False)
        self.assert_result(execute(runs=[run], EVENTS="schedule,push"), True)
        self.assert_result(execute(runs=[run], EVENTS="schedule, push"), True)

    def test_no_success_runs_again(self):
        self.assert_result(execute(), False)

    def test_sha_override_is_used_in_query(self):
        _, calls, _ = self.assert_result(execute(SHA="b" * 40), False)
        self.assertIn("head_sha=" + "b" * 40, calls[1][1])

    def test_api_errors_fail_open_at_either_request(self):
        for fail_at in ("lookup", "runs"):
            with self.subTest(fail_at=fail_at):
                stdout, _, _ = self.assert_result(execute(FAIL_AT=fail_at), False)
                self.assertIn("::warning::", stdout)

    def test_missing_gh_fails_open(self):
        stdout, calls, _ = self.assert_result(execute(missing_gh=True), False)
        self.assertIn("::warning::", stdout)
        self.assertEqual(calls, [])

    def test_malformed_api_response_fails_open(self):
        stdout, _, _ = self.assert_result(execute(response={"message": "not run data"}), False)
        self.assertIn("::warning::", stdout)


if __name__ == "__main__":
    unittest.main()
