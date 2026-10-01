# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the bootstrap's repository-specific package-update wrapper."""
import base64
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / ".github/scripts/bootstrap-common-workflows.sh").read_text()
TEMPLATE = re.search(
    r'BOOTSTRAPPED_FILES\[".github/workflows/update-packages.yml"\]="([^"]+)"', SOURCE)[1]
CRON_FUNCTION = re.search(r"^package_update_cron\(\) \{.*?^\}", SOURCE, re.M | re.S)[0]
RENDER = re.search(
    r'^    if \[ "\$file_path" = "\.github/workflows/update-packages.yml" \]; then\n.*?^    fi',
    SOURCE, re.M | re.S)[0]


def render(repo):
    # Execute the production rendering block without invoking the live bootstrap,
    # its PAT probe, or any GitHub writes.
    return subprocess.check_output([
        "bash", "-c", 'set -euo pipefail\n' + CRON_FUNCTION + '\n'
        'repo="$1"\nfile_b64="$2"\nfile_path=".github/workflows/update-packages.yml"\n'
        + RENDER + '\nprintf "%s" "$file_b64"', "bootstrap-test", repo, TEMPLATE,
    ], text=True)


class BootstrapSchedulesTests(unittest.TestCase):
    def test_only_the_schedule_changes(self):
        template = base64.b64decode(TEMPLATE).decode()
        rendered = base64.b64decode(render("Arc")).decode()
        cron = re.search(r"cron: '([^']+)'", rendered)[1]
        self.assertEqual(rendered, template.replace("__PACKAGE_UPDATE_CRON__", cron))
        self.assertIn("  workflow_dispatch:", rendered)
        self.assertNotIn("__PACKAGE_UPDATE_CRON__", rendered)

    def test_weekly_offsets_are_stable_and_off_the_hour(self):
        crons = set()
        for repo in ["Arc", "Chronicle", "Fundamentals", "cli", "Arc.TypeScript", "Cratis-Example"]:
            first = render(repo)
            self.assertEqual(first, render(repo))
            cron = re.search(r"cron: '([^']+)'", base64.b64decode(first).decode())[1]
            minute, hour, day, month, weekday = cron.split()
            self.assertTrue(1 <= int(minute) <= 59)
            self.assertTrue(3 <= int(hour) <= 7)
            self.assertEqual((day, month, weekday), ("*", "*", "1"))
            crons.add(cron)
        self.assertGreater(len(crons), 1)

    def test_private_callers_use_the_same_deterministic_offsets(self):
        expected = {
            "Direct": "37 5 * * 1", "Studio": "48 5 * * 1",
            "Infrastructure": "5 3 * * 1", "Ensemble": "32 4 * * 1",
            "Experiments": "57 5 * * 1",
        }
        for repo, cron in expected.items():
            with self.subTest(repo=repo):
                self.assertIn("cron: '" + cron + "'", base64.b64decode(render(repo)).decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
