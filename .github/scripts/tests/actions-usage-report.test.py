# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline usage-report fixtures; no token or live GitHub calls."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("report", Path(__file__).parents[1] / "actions-usage-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)
SINCE = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
PRIVATE = {"full_name": "Cratis/Private", "visibility": "private"}
PUBLIC = {"full_name": "Cratis/Public", "visibility": "public"}


def job(identity=1, group="GitHub Actions", labels=None, seconds=61, queue=120):
    start = SINCE + dt.timedelta(hours=1)
    return {"id": identity, "runner_group_name": group, "labels": labels or ["ubuntu-latest"],
            "started_at": start.isoformat(), "completed_at": (start + dt.timedelta(seconds=seconds)).isoformat(),
            "created_at": (start - dt.timedelta(seconds=queue)).isoformat()}


def render(jobs=(), repos=None, total=1, errors=()):
    return report.render(repos if repos is not None else [PRIVATE], total, [], jobs, errors, SINCE, UNTIL)


class UsageReportTests(unittest.TestCase):
    def test_rounding_and_os_multipliers_apply_per_private_job(self):
        jobs = [("Cratis/Private", "private", "Build", job(i, labels=[os]))
                for i, os in enumerate(["ubuntu-latest", "windows-latest", "macos-15"], 1)]
        self.assertIn("| Cratis/Private | 3 | 26 | 0.0 | 0 |", render(jobs))

    def test_public_hosted_jobs_are_not_billed(self):
        output = render([("Cratis/Public", "public", "Build", job())], repos=[PUBLIC], total=0)
        self.assertIn("Private repository coverage unavailable", output)
        self.assertIn("This is not evidence of zero private usage", output)
        self.assertIn("Not observable with PAT_WORKFLOWS", output)
        self.assertNotIn("billed wall-clock", output)

    def test_scale_set_execution_short_jobs_and_queue_percentile(self):
        jobs = [("Cratis/Private", "private", "Build", job(i, "Custom runners", ["cratis-arc"], 30, i * 60))
                for i in range(1, 21)]
        output = render(jobs)
        self.assertIn("| Cratis/Private | 20 | 0 | 10.0 | 0 |", output)
        self.assertIn("execution under one minute: **20**", output)
        self.assertIn("Job queue p95: **19.0 minutes** (20 samples)", output)

    def test_unknown_runner_group_is_not_silently_self_hosted(self):
        output = render([("Cratis/Private", "private", "Build", job(group=None))])
        self.assertIn("| Cratis/Private | 1 | 0 | 0.0 | 1 |", output)
        self.assertIn("not counted as zero billing", output)

    def test_unknown_hosted_os_is_unclassified(self):
        output = render([("Cratis/Private", "private", "Build", job(labels=["unrecognized"]))])
        self.assertIn("| Cratis/Private | 1 | 0 | 0.0 | 1 |", output)

    def test_unfinished_jobs_are_not_counted_as_final_billing(self):
        unfinished = job()
        unfinished["completed_at"] = None
        self.assertIn("Jobs with unavailable final duration: **1**", render([("Cratis/Private", "private", "Build", unfinished)]))

    def test_missing_queue_timestamp_does_not_use_run_creation(self):
        data = job(group="Custom", labels=["cratis-arc"])
        data.pop("created_at")
        output = render([("Cratis/Private", "private", "Build", data)])
        self.assertIn("Job queue p95: **unavailable**", output)
        self.assertIn("Missing queue timestamps: **1**", output)

    def test_coverage_and_api_failures_are_explicit(self):
        self.assertIn("Partial private coverage", render(total=2))
        self.assertIn("Private coverage unconfirmed", render(total=None))
        self.assertIn("Incomplete API coverage", render(errors=["Run inventory incomplete for Cratis/Private."]))

    def test_weekly_hosted_guardrail(self):
        data = job(labels=["macos-latest"], seconds=1801)
        output = render([("Cratis/Private", "private", "Build", data)])
        self.assertIn("Alert: observed private hosted usage is 310", output)

    def test_collect_includes_all_attempts_and_deduplicates_ids(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if "/jobs?" in endpoint:
                return {"jobs": [job(), job(), job(2)]}
            return {"workflow_runs": [{"id": 42, "name": "Build"}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[2]), 1)
        self.assertEqual(len(data[3]), 2)
        self.assertTrue(any("jobs?filter=all&per_page=100&page=1" in call for call in calls))

    def test_collection_failure_is_reported_not_zero(self):
        def get(endpoint):
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            raise report.APIError("denied")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[4]), 8)
        self.assertTrue(any("Run inventory incomplete for Cratis/Private" in error for error in data[4]))

    def test_filtered_search_cap_is_not_silent_truncation(self):
        with self.assertRaises(report.APIError):
            list(report.pages(lambda _: {"total_count": 1001, "workflow_runs": []}, "runs", "workflow_runs"))

    def test_pagination(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            return list(range(100)) if endpoint.endswith("page=1") else [100]
        self.assertEqual(len(list(report.pages(get, "fixture"))), 101)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
