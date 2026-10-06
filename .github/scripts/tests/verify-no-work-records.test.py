# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline regressions for the exact inline program of verify-no-work-records.yml."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/verify-no-work-records.yml"
SOURCE = WORKFLOW.read_text()
SHELL = textwrap.dedent(SOURCE.split("        run: |\n", 1)[1])

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
        cls.script = Path(cls.directory.name) / "guard.sh"
        cls.script.write_text(SHELL)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_script(self, cwd, *arguments, **environment):
        env = {**os.environ, "EXTRA_ALLOWED": "", "EXTRA_ALLOWED_PATHS": "",
               "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent), **environment}
        return subprocess.run(["bash", str(self.script), *arguments], cwd=cwd, env=env,
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
        self.assertIn("could not run", result.stdout)

    def test_zero_tracked_files_exits_two(self):
        repository = tempfile.TemporaryDirectory()
        self.addCleanup(repository.cleanup)
        root = Path(repository.name) / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        result = self.run_script(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no tracked files", result.stdout)

    def test_failing_listing_exits_two_not_zero(self):
        mutated = SHELL.replace("git ls-files 2>&1", "false")
        self.assertNotEqual(mutated, SHELL)
        script = Path(self.directory.name) / "mutated.sh"
        script.write_text(mutated)
        root = self.fixture(CLEAN)
        result = subprocess.run(["bash", str(script)], cwd=root, capture_output=True, text=True,
                                env={**os.environ, "EXTRA_ALLOWED": "", "EXTRA_ALLOWED_PATHS": ""})
        self.assertEqual(result.returncode, 2)

    def test_self_test_passes_and_breaks_on_demand(self):
        with tempfile.TemporaryDirectory() as cwd:
            passing = self.run_script(cwd, "--self-test")
            self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
            self.assertIn("self-test: 5 planted, 5 red, control 0, broken-listing 2", passing.stdout)
            broken = self.run_script(cwd, "--self-test", VERIFY_SELF_TEST_BREAK="1")
            self.assertNotEqual(broken.returncode, 0)


if __name__ == "__main__":
    unittest.main()
