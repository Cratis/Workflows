# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline regressions for the extracted work-record guard."""
import os
import hashlib
from pathlib import Path
import subprocess
import shutil
import re
import textwrap
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/verify-no-work-records.mjs"
SOURCE = SCRIPT.read_text()
NODE = shutil.which("node")

CLEAN = [
    "README.md",
    "DECISIONS.md",
    "decisions/D-0001-example.md",
    "decisions/index.md",
    "Documentation/decisions/0003-kernel-boundary.md",
    "Documentation/client-snippets/design-for-async.md",
    "Documentation/Design/overview.md",
    "Source/Design/overview.md",
    "Source/Reporting.md",
    "Source/Planning.md",
    "Documentation/adrs/0006-planning-roadmap-handover-authority.md",
    ".pi/settings.json",
    ".pi/extensions/lookup/index.ts",
    ".github/PROMPT-x.md",
    ".cratis/ai/Plan-notes.md",
]


class Guard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.script = SCRIPT

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_script(self, cwd, *arguments, **environment):
        env = {**os.environ, "EXTRA_ALLOWED": "", "EXTRA_ALLOWED_PATHS": "",
               "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent), **environment}
        return subprocess.run([NODE, str(self.script), *arguments], cwd=cwd, env=env,
                              capture_output=True, text=True, check=False)

    def fixture(self, files):
        repository = tempfile.TemporaryDirectory()
        self.addCleanup(repository.cleanup)
        root = Path(repository.name) / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        subprocess.run(["git", "add", "-f", "-A"], cwd=root, check=True)
        return root

    def test_clean_repository_passes_and_prints_counts(self):
        result = self.run_script(self.fixture(CLEAN))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"scanned: {len(CLEAN)} tracked files, rules: 5, violations: 0", result.stdout)

    def test_each_dirty_path_fails_and_is_named(self):
        dirty = [
            "PLAN-foo.md", "Plan-bar.md", "REPORT-weekly.md", "notes/DESIGN-bar.md",
            "notes/pLaN-mixed.md", "notes/dEsIgN-mixed.MD", "notes/rEpOrT-mixed.md",
            "notes/sTaTuS-mixed.md", "docs/hAnDoVeR.md", "notes/pRoMpT-next.md",
            "Notes/Plan-something.md", "Source/Report-weekly.md", "Source/Status-board.md",
            "Source/STATUS.md", "decisions/HANDOVER.md", "docs/Handover.md", "docs/Session-notes.md",
            "Notes/Next-Session.md", "IMPLEMENTATION_STATUS.md", ".ai-work/anything.md",
            ".pi/fusion/x/prompt.md", ".pi/delegate/run.json", ".pi/tasks/t.md",
            ".pi/review-session-1/state.json",
        ]
        for name in dirty:
            with self.subTest(path=name):
                result = self.run_script(self.fixture(CLEAN + [name]))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(name, result.stdout)
                self.assertIn("violations: 1", result.stdout)

    def test_mutation_proof_adds_one_violation(self):
        result = self.run_script(self.fixture(CLEAN + ["Notes/PLAN-something.md"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Notes/PLAN-something.md", result.stdout)

    def test_documentation_directories_exempt_shape_but_not_session_rule(self):
        self.assertEqual(self.run_script(self.fixture(CLEAN + ["docs/Plan-roadmap.md"])).returncode, 0)
        self.assertEqual(self.run_script(self.fixture(CLEAN + ["decisions/HANDOVER.md"])).returncode, 1)

    def test_extra_allowed_inputs(self):
        files = CLEAN + ["LIFECYCLE.md", "governance/plan-coverage-audit.md", "governance/Plan-coverage.md"]
        self.assertEqual(self.run_script(self.fixture(files)).returncode, 1)
        result = self.run_script(self.fixture(files), EXTRA_ALLOWED="LIFECYCLE.md",
                                 EXTRA_ALLOWED_PATHS="governance/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_not_a_checkout_exits_two_with_reason(self):
        with tempfile.TemporaryDirectory() as empty:
            result = self.run_script(empty)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not run", result.stderr)

    def test_zero_tracked_files_exits_two(self):
        repository = tempfile.TemporaryDirectory()
        self.addCleanup(repository.cleanup)
        root = Path(repository.name) / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        result = self.run_script(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no tracked files", result.stderr)

    def test_failing_listing_exits_two_not_zero(self):
        mutated = SOURCE.replace("spawnSync('git', ['ls-files', '-z']", "spawnSync('false', ['ls-files', '-z']")
        self.assertNotEqual(mutated, SOURCE)
        script = Path(self.directory.name) / "mutated.mjs"
        script.write_text(mutated)
        root = self.fixture(CLEAN)
        result = subprocess.run([NODE, str(script)], cwd=root, capture_output=True, text=True,
                                env={**os.environ, "EXTRA_ALLOWED": "", "EXTRA_ALLOWED_PATHS": ""})
        self.assertEqual(result.returncode, 2)

    def test_listing_preserves_unusual_filenames(self):
        result = self.run_script(self.fixture(CLEAN + ["notes/Plan-with space.md", "notes/Plan-with\nnewline.md"]))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("violations: 2", result.stdout)
        self.assertIn("Plan-with\\nnewline.md", result.stdout)

    def test_missing_git_exits_two(self):
        with tempfile.TemporaryDirectory() as empty:
            result = self.run_script(empty, PATH="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not run", result.stderr)

    def test_unknown_argument_exits_two(self):
        result = self.run_script(self.fixture(CLEAN), "--unknown")
        self.assertEqual(result.returncode, 2)

    def test_workflow_pin_matches_the_tested_script(self):
        workflow = (ROOT / ".github/workflows/verify-no-work-records.yml").read_text()
        pin = re.search(r"raw.githubusercontent.com/Cratis/Workflows/([a-f0-9]{40})/", workflow)
        self.assertIsNotNone(pin)
        digest = re.search(r"script_sha256='([a-f0-9]{64})'", workflow)
        self.assertIsNotNone(digest)
        self.assertEqual(digest[1], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())

    def test_workflow_runs_against_the_caller_and_propagates_fetch_failure(self):
        workflow = (ROOT / ".github/workflows/verify-no-work-records.yml").read_text()
        shell = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            curl = tools / "curl"
            curl.write_text('#!/bin/sh\nfor arg; do output="$arg"; done\ncp "$GUARD_SOURCE" "$output"\n')
            curl.chmod(0o755)
            caller = self.fixture(CLEAN + ["Notes/pLaN-caller.md"])
            env = {**os.environ, "RUNNER_TEMP": temporary, "GUARD_SOURCE": str(SCRIPT),
                   "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "EXTRA_ALLOWED": "", "EXTRA_ALLOWED_PATHS": ""}
            result = subprocess.run(["/bin/bash", "-c", shell], cwd=caller, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Notes/pLaN-caller.md", result.stdout)
            curl.write_text('#!/bin/sh\nfor arg; do output="$arg"; done\nprintf "wrong script" > "$output"\n')
            result = subprocess.run(["/bin/bash", "-c", shell], cwd=caller, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("checksum mismatch", result.stderr)
            curl.write_text('#!/bin/sh\nexit 1\n')
            result = subprocess.run(["/bin/bash", "-c", shell], cwd=caller, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("downloading the pinned", result.stderr)

    def test_self_test_passes_and_breaks_on_demand(self):
        with tempfile.TemporaryDirectory() as cwd:
            passing = self.run_script(cwd, "--self-test")
            self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
            self.assertIn("self-test: 5 planted, 5 red, control 0, broken-listing 2", passing.stdout)
            broken = self.run_script(cwd, "--self-test", VERIFY_SELF_TEST_BREAK="1")
            self.assertNotEqual(broken.returncode, 0)


if __name__ == "__main__":
    unittest.main()
