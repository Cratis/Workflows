# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline regressions for the reusable discoverability check, on a tiny good and bad fixture site."""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/verify-discoverability.mjs"
WORKFLOW = ROOT / ".github/workflows/verify-discoverability.yml"
FIXTURES = ROOT / ".github/scripts/tests/fixtures/discoverability"
NODE = shutil.which("node")
BASE = "https://example.cratis.io"


class Discoverability(unittest.TestCase):
    def run_script(self, site, *arguments, **environment):
        env = {**os.environ, **environment}
        env.pop("GITHUB_STEP_SUMMARY", None) if "GITHUB_STEP_SUMMARY" not in environment else None
        return subprocess.run([NODE, str(SCRIPT), "--root", str(site), "--base-url", BASE, *arguments],
                              env=env, capture_output=True, text=True, check=False)

    def test_good_site_is_clean_even_when_failing_on_errors(self):
        result = self.run_script(FIXTURES / "good", "--fail-on", "error")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 error(s)", result.stdout)
        self.assertIn("0 warning(s)", result.stdout)
        self.assertIn("2 indexable page(s)", result.stdout)

    def test_bad_site_reports_every_planted_defect_and_is_warn_only_by_default(self):
        result = self.run_script(FIXTURES / "bad", "--ignore", "ignored.html")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in [
            "::error file=index.html::missing <html lang>",
            "::error file=index.html::2 <title> elements",
            "::warning file=docs.html::title is ",
            "::warning file=docs.html::missing meta description",
            "::warning file=index.html::meta description is 173 characters",
            "::error file=docs.html::missing <link rel=\"canonical\">",
            "::error file=index.html::canonical points at elsewhere.example.org",
            "::error file=index.html::JSON-LD does not parse",
            "::warning file=docs.html::missing og:image",
            "::warning file=index.html::og:image without og:image:alt",
            "::error file=robots.txt::robots.txt has no Sitemap line",
            "::error file=sitemap.xml::sitemap URL https://example.cratis.io/missing/ does not map",
            "::warning file=llms.txt::missing llms.txt",
        ]:
            self.assertIn(expected, result.stdout)
        self.assertNotIn("ignored.html", result.stdout)
        self.assertIn("7 error(s)", result.stdout)

    def test_fail_on_error_exits_one_and_ignore_globs_apply(self):
        failing = self.run_script(FIXTURES / "bad", "--fail-on", "error")
        self.assertEqual(failing.returncode, 1)
        self.assertIn("file=ignored.html", failing.stdout)
        ignored = self.run_script(FIXTURES / "bad", "--fail-on", "error", "--ignore", "*.html")
        self.assertNotIn("file=index.html", ignored.stdout)
        self.assertEqual(self.run_script(FIXTURES / "bad", "--ignore", "**/*.html,docs.*").returncode, 0)

    def test_minified_html_with_unquoted_and_single_quoted_attributes_is_clean(self):
        # Unquoted lang/description/canonical, single-quoted og:image:alt and ld+json type, an inline <svg><title>,
        # and sitemap URLs without a trailing slash that map to <path>.html and <path>/index.html while <path>/ is a directory.
        result = self.run_script(FIXTURES / "minified", "--fail-on", "error")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 indexable page(s), 4 sitemap URL(s): **0 error(s)**, **0 warning(s)**", result.stdout)

    def test_only_head_titles_count(self):
        with tempfile.TemporaryDirectory() as site:
            (Path(site) / "index.html").write_text(
                "<!doctype html><html lang=en><head></head><body><svg><title>Icon</title></svg></body></html>")
            result = self.run_script(site)
            self.assertIn("::error file=index.html::missing or empty <title>", result.stdout)

    def test_malformed_sitemap_url_and_unreadable_files_are_findings_not_crashes(self):
        with tempfile.TemporaryDirectory() as site:
            root = Path(site)
            (root / "sitemap.xml").write_text(
                "<urlset><url><loc>https://example.cratis.io/%E0%A4%A</loc></url></urlset>")
            (root / "robots.txt").write_text("Sitemap: https://example.cratis.io/sitemap.xml\n")
            (root / "locked.html").write_text("<html lang=en></html>")
            (root / "locked.html").chmod(0)
            result = self.run_script(site, "--fail-on", "error")
            (root / "locked.html").chmod(0o644)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("is not a valid URL path", result.stdout)
            if os.geteuid() != 0:
                self.assertIn("::error file=locked.html::cannot read file", result.stdout)

    def test_missing_files_are_errors(self):
        with tempfile.TemporaryDirectory() as empty:
            result = self.run_script(empty, "--fail-on", "error")
            self.assertEqual(result.returncode, 1)
            self.assertIn("missing robots.txt", result.stdout)
            self.assertIn("no sitemap.xml", result.stdout)
            self.assertIn("missing llms.txt", result.stdout)

    def test_summary_is_appended_to_the_step_summary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            self.run_script(FIXTURES / "bad", GITHUB_STEP_SUMMARY=str(summary))
            text = summary.read_text()
            self.assertTrue(text.startswith("## Discoverability check"))
            self.assertIn("| error | `index.html` | missing &lt;html lang> |", text)

    def test_bad_arguments_exit_two(self):
        self.assertEqual(self.run_script(FIXTURES / "nowhere").returncode, 2)
        self.assertEqual(self.run_script(FIXTURES / "good", "--fail-on", "warning").returncode, 2)
        self.assertEqual(self.run_script(FIXTURES / "good", "--unknown", "x").returncode, 2)
        bad_url = subprocess.run([NODE, str(SCRIPT), "--root", str(FIXTURES / "good"), "--base-url", "not a url"],
                                 capture_output=True, text=True)
        self.assertEqual(bad_url.returncode, 2)
        self.assertIn("could not run", bad_url.stderr)

    def test_workflow_pin_matches_the_tested_script(self):
        workflow = WORKFLOW.read_text()
        pin = re.search(r"raw.githubusercontent.com/Cratis/Workflows/([a-f0-9]{40})/", workflow)
        self.assertIsNotNone(pin)
        digest = re.search(r"script_sha256='([a-f0-9]{64})'", workflow)
        self.assertIsNotNone(digest)
        self.assertEqual(digest[1], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        pinned = subprocess.run(["git", "cat-file", "blob", f"{pin[1]}:.github/scripts/verify-discoverability.mjs"],
                                cwd=ROOT, capture_output=True)
        if pinned.returncode == 0:
            self.assertEqual(digest[1], hashlib.sha256(pinned.stdout).hexdigest(),
                             "The pinned commit's script does not match script_sha256; re-pin the URL")

    def test_workflow_download_step_runs_the_script_and_propagates_failures(self):
        workflow = WORKFLOW.read_text()
        shell = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary) / "tools"
            tools.mkdir()
            curl = tools / "curl"
            curl.write_text('#!/bin/sh\nfor arg; do output="$arg"; done\ncp "$GUARD_SOURCE" "$output"\n')
            curl.chmod(0o755)
            env = {**os.environ, "RUNNER_TEMP": temporary, "GUARD_SOURCE": str(SCRIPT),
                   "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "SITE_PATH": str(FIXTURES / "bad"),
                   "BASE_URL": BASE, "FAIL_ON": "error", "IGNORE": "ignored.html"}
            result = subprocess.run(["/bin/bash", "-c", shell], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("7 error(s)", result.stdout)
            result = subprocess.run(["/bin/bash", "-c", shell], env={**env, "FAIL_ON": "none"}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            curl.write_text('#!/bin/sh\nfor arg; do output="$arg"; done\nprintf "wrong script" > "$output"\n')
            result = subprocess.run(["/bin/bash", "-c", shell], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("checksum mismatch", result.stderr)
            curl.write_text('#!/bin/sh\nexit 1\n')
            result = subprocess.run(["/bin/bash", "-c", shell], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("downloading the pinned", result.stderr)


if __name__ == "__main__":
    unittest.main()
