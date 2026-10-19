# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline Docker cleanup checker tests; `gh` and `git` are stubbed, no token or network."""
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
SCRIPT = ROOT / ".github/scripts/verify-docker-cleanup.py"
FIXTURES = Path(__file__).parent / "fixtures/docker-cleanup"
spec = importlib.util.spec_from_file_location("checker", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

PRUNE, BULK = checker.PRUNE_REASON, checker.BULK_REASON


def d(text):
    return f"docker {text}"


def kinds(relative, text):
    return [PRUNE if reason == PRUNE else BULK for _, reason in checker.scan_text(relative, text)]


def scan(case, repo="Fixture", excluded=()):
    result = checker.Scan()
    result.add_directory(FIXTURES / case, repo, excluded)
    return result


def run(*arguments):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, text=True, timeout=60)


class PruneTests(unittest.TestCase):
    def test_every_prune_kind_fails(self):
        for kind in ["system", "container", "image", "volume", "network", "builder", "buildx"]:
            self.assertEqual(kinds("a.sh", d(f"{kind} prune -f")), [PRUNE], kind)

    def test_sudo_extra_spaces_and_case_are_tolerated(self):
        self.assertEqual(kinds("a.sh", "sudo   " + d("SYSTEM    Prune --all")), [PRUNE])

    def test_label_filter_scopes_the_prune(self):
        for text in [d('system prune -f --filter "label=run=1"'), d("image prune --filter=label=run=1"),
                     d("volume prune --filter 'label=run=1' -f")]:
            self.assertEqual(kinds("a.sh", text), [], text)

    def test_negated_label_and_until_do_not_scope(self):
        self.assertEqual(kinds("a.sh", d('system prune --filter "label!=keep"')), [PRUNE])
        self.assertEqual(kinds("a.sh", d("system prune --filter until=24h")), [PRUNE])

    def test_filter_on_another_command_does_not_scope(self):
        self.assertEqual(kinds("a.sh", d("system prune -f && ") + d("ps --filter label=run=1")), [PRUNE])

    def test_continuation_lines_are_joined(self):
        scoped = d("system prune -f \\\n  --filter label=run=1\n")
        unscoped = d("system prune \\\n  --all\n")
        self.assertEqual(kinds("a.sh", scoped), [])
        self.assertEqual(checker.scan_text("a.sh", "echo hi\n" + unscoped), [(2, PRUNE)])


class BulkRemovalTests(unittest.TestCase):
    def test_substitution_fails_for_each_removal_verb(self):
        for verb, listing in [("rm -f", "ps -aq"), ("kill", "ps -q"), ("stop", "container ls -q"),
                              ("rmi", "images -q"), ("volume rm", "volume ls -q"), ("network rm", "network ls -q"),
                              ("container rm -f", "ps -aq")]:
            self.assertEqual(kinds("a.sh", d(f"{verb} $({d(listing)})")), [BULK], verb)

    def test_backticks_fail(self):
        self.assertEqual(kinds("a.sh", d("kill `" + d("ps -q") + "`")), [BULK])

    def test_xargs_fails_with_flags(self):
        self.assertEqual(kinds("a.sh", d("ps -aq | xargs -r ") + d("rm -f")), [BULK])

    def test_label_filter_on_the_listing_scopes_the_removal(self):
        self.assertEqual(kinds("a.sh", d("rm -f $(") + d('ps -aq --filter "label=run=1")')), [])
        self.assertEqual(kinds("a.sh", d('ps -aq --filter label=run=1 | xargs ') + d("rm -f")), [])

    def test_negated_label_does_not_scope(self):
        self.assertEqual(kinds("a.sh", d("rm -f $(") + d('ps -aq --filter "label!=keep")')), [BULK])

    def test_sudo_in_the_listing_fails(self):
        self.assertEqual(kinds("a.sh", "sudo " + d("rm -f $(sudo -n ") + d("ps -aq)")), [BULK])
        self.assertEqual(kinds("a.sh", d("rm -f `sudo ") + d("ps -aq`")), [BULK])

    def test_sudo_listing_with_label_filter_passes(self):
        self.assertEqual(kinds("a.sh", d("rm -f $(sudo ") + d("ps -aq --filter label=run=1)")), [])

    def test_variable_before_the_substitution_does_not_hide_it(self):
        self.assertEqual(kinds("a.sh", d("rm -f $FLAGS $(") + d("ps -aq)")), [BULK])
        self.assertEqual(kinds("a.yml", d("rm -f ${{ env.X }} $(") + d("ps -aq)")), [BULK])
        self.assertEqual(kinds("a.sh", d("rm -f ${FLAGS} $(") + d("ps -aq --filter label=run=1)")), [])

    def test_removal_by_name_is_not_bulk(self):
        self.assertEqual(kinds("a.sh", d("rm -f my-container")), [])


class CommentTests(unittest.TestCase):
    def test_hash_comments(self):
        for name in ["a.sh", "a.py", "a.ps1", "Dockerfile", "Makefile", "justfile"]:
            self.assertEqual(kinds(name, "# " + d("system prune -af")), [], name)
        self.assertEqual(kinds("a.sh", "echo ok # " + d("rm -f $(") + d("ps -aq)")), [])

    def test_slash_comments_only_for_script_languages(self):
        for name in ["a.js", "a.mjs", "a.ts", "a.cs"]:
            self.assertEqual(kinds(name, "// " + d("system prune -af")), [], name)
        self.assertEqual(kinds("a.sh", "// " + d("system prune -af")), [PRUNE])

    def test_code_before_a_trailing_comment_still_counts(self):
        self.assertEqual(kinds("a.sh", d("system prune -af # reclaim space")), [PRUNE])


class DirectoryTests(unittest.TestCase):
    def test_violations_are_reported_by_file_and_line_in_every_subject_kind(self):
        result = scan("violations")
        self.assertEqual(sorted((path, number) for _, path, number, _ in result.violations), [
            (".github/actions/cleanup/action.yml", 6), (".github/workflows/ci.yml", 9),
            ("Dockerfile", 2), ("Makefile", 2), ("scripts/continued.sh", 2), ("scripts/xargs.sh", 2)])
        self.assertEqual((result.repositories, result.files), (1, 6))

    def test_scripts_under_actions_are_subjects(self):
        for path in [".github/actions/x/action.yml", ".github/actions/x/cleanup.sh", ".github/actions/x/Dockerfile"]:
            self.assertTrue(checker.is_subject(path), path)
        self.assertFalse(checker.is_subject(".github/actions/x/readme.md"))
        self.assertFalse(checker.is_subject(".github/actions/x/other.yml"))

    def test_only_this_checkers_fixtures_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            for relative in [".github/scripts/tests/fixtures/docker-cleanup/a.sh", ".github/scripts/tests/fixtures/other/b.sh"]:
                target = Path(directory) / relative
                target.parent.mkdir(parents=True)
                target.write_text("echo\n")
            self.assertEqual([path for path, _ in checker.subject_files(directory)],
                             [".github/scripts/tests/fixtures/other/b.sh"])

    def test_clean_fixture_passes(self):
        result = scan("clean")
        self.assertEqual((result.files, result.violations), (2, []))

    def test_excluded_directory_is_skipped(self):
        result = scan("violations", excluded=["scripts"])
        self.assertEqual(result.files, 4)

    def test_default_skips_planted_fixture_directory(self):
        self.assertEqual(sorted(path for path, _ in checker.subject_files(ROOT)
                                if "fixtures/docker-cleanup" in path), [])

    def test_this_repository_is_clean(self):
        result = checker.Scan()
        result.add_directory(ROOT, "Workflows")
        self.assertEqual(result.violations, [])
        self.assertGreater(result.files, 0)

    def test_missing_directory_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            checker.Scan().add_directory(FIXTURES / "missing", "Fixture")


class AllowlistTests(unittest.TestCase):
    def test_allowlisted_file_is_suppressed_and_used(self):
        entry = ("Fixture", ".github/workflows/ci.yml", "reason")
        with patch.object(checker, "ALLOWLIST", (entry,)):
            result = scan("violations")
        self.assertNotIn(".github/workflows/ci.yml", [path for _, path, _, _ in result.violations])
        self.assertEqual(result.allowlist_used, {entry[:2]})

    def test_entry_without_a_violation_fails(self):
        entry = ("Fixture", "scripts/cleanup.sh", "reason")
        with patch.object(checker, "ALLOWLIST", (entry,)):
            result = scan("clean")
            stale = result.stale_allowlist()
        self.assertEqual(len(stale), 1)
        self.assertIn("allowlist entry no longer needed", stale[0][3])


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class OrganizationTests(unittest.TestCase):
    def sweep(self, names, clone_files, failing=()):
        """Run the sweep with `gh` and `git` stubbed; `clone_files` maps a relative path to its text."""
        self.calls = []

        def fake(arguments, environment=None, timeout=300):
            self.calls.append(list(arguments))
            if arguments[0] == "gh":
                return Completed(stdout=str([{"name": name} for name in names]).replace("'", '"'))
            repo = arguments[-1].rsplit("/", 1)[-1]
            if repo in failing:
                return Completed(128, stderr="fatal: repository not found")
            for relative, text in clone_files.items():
                target = Path(arguments[-1]) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return Completed()
        patcher = patch.object(checker, "run_command", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict("os.environ", {"GH_TOKEN": "secret-token"})
        environment.start()
        self.addCleanup(environment.stop)
        return checker.scan_organization()

    def test_sweep_counts_files_and_repositories(self):
        result = self.sweep(["Arc", "Studio"], {"build.sh": d("rm -f $(") + d("ps -aq)\n"), "ok.sh": "echo ok\n"})
        self.assertEqual((result.repositories, result.files, len(result.violations)), (2, 4, 2))
        self.assertEqual({repo for repo, *_ in result.violations}, {"Arc", "Studio"})

    def test_clone_failure_could_not_run_and_never_leaks_the_token(self):
        with self.assertRaises(checker.CouldNotRun) as raised:
            self.sweep(["Arc", "Broken"], {"ok.sh": "echo ok\n"}, failing=("Broken",))
        self.assertIn("Broken: could not clone", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_token_never_appears_in_command_lines(self):
        import base64
        encoded = base64.b64encode(b"x-access-token:secret-token").decode()
        self.sweep(["Arc"], {"ok.sh": "echo ok\n"})
        self.assertTrue(any(call[0] == "git" for call in self.calls))
        self.assertTrue(any(call[0] == "gh" for call in self.calls))
        for arguments in self.calls:
            joined = " ".join(arguments)
            self.assertNotIn("secret-token", joined)
            self.assertNotIn(encoded, joined)

    def test_no_repositories_could_not_run(self):
        with self.assertRaises(checker.CouldNotRun):
            self.sweep([], {})

    def test_zero_files_could_not_run(self):
        result = self.sweep(["Arc"], {"readme.md": "text\n"})
        with self.assertRaises(checker.CouldNotRun):
            checker.report(result)

    def test_stale_allowlist_entry_fails_the_sweep(self):
        with patch.object(checker, "ALLOWLIST", (("Arc", "ok.sh", "reason"),)):
            result = self.sweep(["Arc"], {"ok.sh": "echo ok\n"})
        self.assertEqual(len(result.violations), 1)
        self.assertIn("allowlist entry no longer needed", result.violations[0][3])

    def test_report_prints_counts_and_exit_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = checker.report(self.sweep(["Arc"], {"ok.sh": "echo ok\n"}))
        self.assertEqual(status, 0)
        self.assertIn("Scanned 1 files in 1 repositories, found 0 violations", output.getvalue())


class CommandLineTests(unittest.TestCase):
    def test_self_test_passes(self):
        result = run("--self-test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_self_test_fails_when_detection_is_broken(self):
        output = io.StringIO()
        with patch.object(checker, "PRUNE_FILTER", checker.re.compile(r".")), redirect_stdout(output):
            self.assertEqual(checker.self_test(), 1)
        self.assertIn("planted prune-system.sh (prune) was not found", output.getvalue())
        output = io.StringIO()
        with patch.object(checker, "line_violations", lambda line: []), redirect_stdout(output):
            self.assertEqual(checker.self_test(), 1)
        self.assertIn("planted xargs.sh (bulk) was not found", output.getvalue())

    def test_clean_directory_exits_zero_and_prints_counts(self):
        result = run("--root", str(FIXTURES / "clean"), "--repo", "Fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scanned 2 files in 1 repositories, found 0 violations", result.stdout)

    def test_violation_exits_one_naming_repository_file_and_line(self):
        result = run("--root", str(FIXTURES / "violations"), "--repo", "Components")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Components:.github/workflows/ci.yml:9: unscoped docker prune", result.stdout)

    def test_exclude_option(self):
        result = run("--root", str(FIXTURES / "violations"), "--exclude", "scripts", "--exclude", ".github")
        self.assertEqual(result.returncode, 1)
        self.assertIn("found 2 violations", result.stdout)

    def test_directory_without_subjects_exits_two(self):
        self.assertEqual(run("--root", str(FIXTURES / "empty")).returncode, 2)

    def test_empty_and_missing_directories_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run("--root", directory).returncode, 2)
        self.assertEqual(run("--root", str(FIXTURES / "does-not-exist")).returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
