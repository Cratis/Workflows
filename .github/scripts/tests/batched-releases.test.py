# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks for batched-release opt-in: template, cron helper and staleness decision."""
import importlib.util
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / ".github/templates/publish.template.yml"
CRON = ROOT / ".github/scripts/batched-release-cron.sh"
spec = importlib.util.spec_from_file_location("staleness", ROOT / ".github/scripts/check-batched-releases.py")
staleness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staleness)


def active_lines():
    return [line for line in TEMPLATE.read_text().splitlines() if not line.lstrip().startswith("#")]


def cron(repo):
    result = subprocess.run(["bash", str(CRON), repo], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class PublishTemplateOptIn(unittest.TestCase):
    def test_keeps_queue_max(self):
        self.assertIn("  queue: max", active_lines())

    def test_has_no_active_schedule_or_window(self):
        active = "\n".join(active_lines())
        self.assertNotRegex(active, r"(?m)^\s*schedule:")
        self.assertNotIn("release-window", active)

    def test_contains_commented_opt_in_block(self):
        lines = TEMPLATE.read_text().splitlines()
        self.assertIn("  # schedule:", lines)
        self.assertTrue(any(re.fullmatch(r'  #   - cron: ".*"', line) for line in lines))
        self.assertIn("          # release-window: 60", lines)


class BatchedReleaseCron(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(cron("Chronicle"), cron("Chronicle"))

    def test_offsets_are_zero_to_fourteen_with_four_ticks_per_hour(self):
        offsets = set()
        for repo in ("Arc", "Chronicle", "Stage", "Fundamentals", "Components", "Workflows", "AI", "Studio"):
            match = re.fullmatch(r"(\d+),(\d+),(\d+),(\d+) \* \* \* \*", cron(repo))
            self.assertIsNotNone(match, cron(repo))
            minutes = [int(value) for value in match.groups()]
            self.assertTrue(0 <= minutes[0] <= 14)
            self.assertEqual(minutes, [minutes[0] + step for step in (0, 15, 30, 45)])
            offsets.add(minutes[0])
        self.assertGreater(len(offsets), 1)

    def test_requires_a_repository_name(self):
        result = subprocess.run(["bash", str(CRON)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


NOW = staleness.parse_time("2026-01-01T12:00:00Z")
RELEASE = {"created_at": "2026-01-01T08:00:00Z", "draft": False}


def pull(number, merged_at, *labels):
    return {"number": number, "merged_at": merged_at, "labels": list(labels)}


def decide(pulls, releases=(RELEASE,), window=60):
    return staleness.evaluate("Repo", window, list(releases), pulls, NOW)


class StalenessDecision(unittest.TestCase):
    def test_nothing_pending_passes(self):
        verdict = decide([])
        self.assertFalse(verdict.stale)
        self.assertEqual(verdict.pending, [])

    def test_merge_before_the_release_is_not_pending(self):
        self.assertEqual(decide([pull(1, "2026-01-01T07:00:00Z", "patch")]).pending, [])

    def test_pending_inside_window_plus_sixty_passes(self):
        verdict = decide([pull(1, "2026-01-01T10:30:00Z", "minor")])
        self.assertEqual(verdict.age_minutes, 90)
        self.assertFalse(verdict.stale)

    def test_pending_beyond_window_plus_sixty_fails(self):
        verdict = decide([pull(2, "2026-01-01T09:00:00Z", "patch"), pull(3, "2026-01-01T11:00:00Z", "major")])
        self.assertTrue(verdict.stale)
        self.assertEqual([number for number, _ in verdict.pending], [2, 3])
        self.assertIn("::error::Repo", verdict.error())
        self.assertIn("#2, #3", verdict.error())

    def test_draft_release_is_ignored(self):
        draft = {"created_at": "2026-01-01T11:00:00Z", "draft": True}
        verdict = decide([pull(1, "2026-01-01T09:00:00Z", "patch")], releases=(RELEASE, draft))
        self.assertTrue(verdict.stale)

    def test_no_release_and_unlabelled_pulls_are_ignored(self):
        verdict = decide([pull(1, "2026-01-01T09:00:00Z", "no-release"), pull(2, "2026-01-01T09:00:00Z")])
        self.assertEqual(verdict.pending, [])
        self.assertFalse(verdict.stale)

    def test_window_zero_is_not_opted_in(self):
        verdict = decide([pull(1, "2026-01-01T01:00:00Z", "patch")], window=0)
        self.assertFalse(verdict.opted_in)
        self.assertFalse(verdict.stale)

    def test_window_is_read_from_active_lines_only(self):
        self.assertEqual(staleness.parse_window("          release-window: 60\n"), 60)
        self.assertEqual(staleness.parse_window("          # release-window: 60\n"), 0)
        self.assertEqual(staleness.parse_window("          release-window: 0\n"), 0)


if __name__ == "__main__":
    unittest.main()
