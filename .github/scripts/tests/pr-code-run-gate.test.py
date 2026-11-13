# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the pr-code-run-gate script against a stubbed GitHub API (curl)."""
from pathlib import Path
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / ".github/actions/pr-code-run-gate"
SCRIPT = ACTION / "wait-for-code-run.sh"
BASH = shutil.which("bash")
JQ = shutil.which("jq")


def run_script(*responses):
    """Run the script with curl answering each poll with the next response."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for index, response in enumerate(responses):
            (tmp / f"r{index}.json").write_text(json.dumps({"workflow_runs": response}))
        curl = tmp / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            f"n=$(cat '{tmp}/n' 2>/dev/null || echo 0)\n"
            f"last=$(ls '{tmp}'/r*.json | wc -l)\n"
            "i=$n; [ \"$i\" -ge \"$last\" ] && i=$((last-1))\n"
            f"echo $((n+1)) > '{tmp}/n'\n"
            f"cat '{tmp}'/r$i.json\n"
        )
        curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "GH_TOKEN": "x", "HEAD_SHA": "abc", "WORKFLOW_FILE": "pull-requests.yml",
            "MARKER": "[meta]", "GITHUB_REPOSITORY": "Cratis/Example",
            "TIMEOUT_SECONDS": "3", "POLL_SECONDS": "1",
        }
        return subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True)


def run(title, status, conclusion, created="2026-01-01T00:00:00Z"):
    return {"display_title": title, "status": status, "conclusion": conclusion, "created_at": created, "html_url": "https://example/run"}


@unittest.skipUnless(JQ, "jq is required")
class WaitForCodeRun(unittest.TestCase):
    def test_passes_when_the_code_run_succeeded(self):
        self.assertEqual(run_script([run("Add thing", "completed", "success")]).returncode, 0)

    def test_fails_when_the_code_run_failed_or_was_cancelled(self):
        for conclusion in ("failure", "cancelled"):
            self.assertEqual(run_script([run("Add thing", "completed", conclusion)]).returncode, 1)

    def test_fails_without_a_code_run(self):
        self.assertEqual(run_script([]).returncode, 1)

    def test_ignores_label_runs_even_when_they_succeeded(self):
        result = run_script([run("Add thing [meta]", "completed", "success")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("No code run", result.stdout + result.stderr)

    def test_uses_the_latest_code_run(self):
        runs = [run("Add thing", "completed", "failure", "2026-01-01T00:00:00Z"),
                run("Add thing", "completed", "success", "2026-01-01T01:00:00Z")]
        self.assertEqual(run_script(runs).returncode, 0)

    def test_waits_for_an_unfinished_code_run(self):
        result = run_script([run("Add thing", "in_progress", None)], [run("Add thing", "completed", "success")])
        self.assertEqual(result.returncode, 0)
        self.assertIn("Waiting for", result.stdout)

    def test_times_out_when_the_code_run_never_finishes(self):
        self.assertEqual(run_script([run("Add thing", "in_progress", None)]).returncode, 1)


class ActionDefinition(unittest.TestCase):
    def test_runs_the_script_and_requires_only_the_head_sha(self):
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        required = [name for name, spec in action["inputs"].items() if spec.get("required")]
        self.assertEqual(required, ["head-sha"])
        self.assertIn("wait-for-code-run.sh", action["runs"]["steps"][0]["run"])


if __name__ == "__main__":
    unittest.main()
