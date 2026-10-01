# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline usage-report fixtures; no token or live GitHub calls."""
import base64
import datetime as dt
import importlib.util
from pathlib import Path
import threading
import time
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
    return {"id": identity, "runner_id": identity, "runner_name": "runner", "runner_group_name": group, "labels": labels or ["ubuntu-latest"],
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
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
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
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            raise report.APIError("denied")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[4]), 1)
        self.assertIn("7 daily windows", data[4][0])
        self.assertTrue(any("Run inventory incomplete for Cratis/Private" in error for error in data[4]))

    def test_skipped_and_never_assigned_jobs_do_not_distort_statistics(self):
        skipped = job(1, None, ["cratis-arc"], seconds=-1, queue=0)
        skipped.update(conclusion="skipped", runner_name=None, runner_id=None)
        cancelled = job(2, "", ["cratis-arc"], seconds=0, queue=0)
        cancelled.update(conclusion="cancelled", runner_name="", runner_id=0)
        executed = job(3, "Custom", ["cratis-arc"], seconds=30, queue=120)
        executed["conclusion"] = "cancelled"
        output = render([("Cratis/Private", "private", "Build", data)
                         for data in (skipped, cancelled, executed)])
        self.assertIn("| Cratis/Private | 1 | 0 | 0.5 | 0 |", output)
        self.assertIn("Completed jobs: **1**; execution under one minute: **1**", output)
        self.assertIn("Job queue p95: **2.0 minutes** (1 samples)", output)
        self.assertIn("statistics: **2**", output)
        self.assertIn("Jobs with unavailable final duration: **0**", output)
        self.assertIn("unknown runner group or OS: **0**", output)

    def test_archived_private_visibility_counts_but_usage_does_not(self):
        archived = {"full_name": "Cratis/Archived", "private": True, "archived": True}
        output = render(repos=[PRIVATE, archived], total=2)
        self.assertNotIn("Partial private coverage", output)
        self.assertIn("private: **1**", output)
        self.assertNotIn("| Cratis/Archived |", output)

    def test_public_ranking_uses_run_elapsed_not_job_execution(self):
        run = {"name": "Build", "status": "completed", "run_started_at": SINCE.isoformat(),
               "updated_at": (SINCE + dt.timedelta(minutes=10)).isoformat()}
        output = report.render([PUBLIC], 0, [("Cratis/Public", "public", run)], [], [], SINCE, UNTIL)
        self.assertIn("| Cratis/Public / Build | 1 | 10.0 |", output)
        self.assertIn("not job execution or billing", output)

    def test_public_job_inventory_only_for_arc_workflows_and_definition_is_cached(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 0}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PUBLIC]
            if "/git/trees/" in endpoint:
                return {"tree": [{"path": ".github/workflows/hosted.yml", "type": "blob", "sha": "hosted"},
                                 {"path": ".github/workflows/arc.yml", "type": "blob", "sha": "arc"}]}
            if "/git/blobs/" in endpoint:
                text = "runs-on: cratis-arc" if endpoint.endswith("/arc") else "runs-on: ubuntu-latest"
                return {"content": base64.b64encode(text.encode()).decode()}
            if "/jobs?" in endpoint:
                return {"jobs": [job(group="Custom", labels=["cratis-arc"]), job(2)]}
            return {"workflow_runs": [{"id": i, "path": path, "head_sha": "abc", "name": "Build"}
                                      for i, path in ((1, ".github/workflows/hosted.yml"),
                                                      (2, ".github/workflows/arc.yml"),
                                                      (3, ".github/workflows/arc.yml"))]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[2]), 3)
        self.assertEqual(len(data[3]), 1)
        self.assertEqual(sum("/git/trees/" in call for call in calls), 1)
        self.assertEqual(sum("/git/blobs/" in call for call in calls), 2)
        self.assertEqual(sum("/jobs?" in call for call in calls), 2)
        self.assertFalse(data[4])

    def test_budget_exhaustion_is_explicit_and_a_fresh_collection_can_resume(self):
        calls = []
        remaining = 105
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": remaining}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if "/jobs?" in endpoint:
                return {"jobs": [job(1), job(2)]}
            return {"workflow_runs": [{"id": 1, "name": "Build"}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(calls[0], "rate_limit")
        self.assertEqual(len(calls), 6)  # Preflight plus exactly five reserved requests.
        self.assertIn("API request budget exhausted", render(errors=data[4]))
        self.assertEqual(len(data[3]), 2)  # Successful data retained, not silently zeroed.
        remaining = 5000
        self.assertFalse(report.collect(get, "Cratis", SINCE, UNTIL)[4])

    def test_shared_budget_cannot_be_overspent_by_parallel_workers(self):
        calls = []
        lock = threading.Lock()
        def get(endpoint):
            with lock:
                calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 110}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [{"full_name": f"Cratis/P{i}", "private": True} for i in range(20)]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 20}
            time.sleep(.001)
            return {"workflow_runs": []}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(calls), 11)
        self.assertTrue(any("API request budget exhausted" in error for error in data[4]))

    def test_server_rate_limit_stops_further_requests(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            raise report.CollectionStopped("GitHub API rate limit reached")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(calls), 4)
        self.assertTrue(any("rate limit reached" in error for error in data[4]))

    def test_rate_preflight_failure_does_not_start_collection(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            raise report.APIError("denied")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(calls, ["rate_limit"])
        self.assertIn("collection not started", data[4][0])

    def test_time_budget_stops_with_partial_report(self):
        def get(endpoint):
            return {"resources": {"core": {"remaining": 5000}}}
        data = report.collect(get, "Cratis", SINCE, UNTIL, max_seconds=0)
        self.assertIn("Collection time budget exhausted", data[4][0])

    def test_job_failures_are_collapsed_per_repository(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if "/jobs?" in endpoint:
                raise report.APIError("denied")
            return {"workflow_runs": [{"id": i} for i in range(99)]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[4]), 1)
        self.assertIn("99 runs", data[4][0])

    def test_observed_org_volume_fits_budget_with_bounded_parallelism(self):
        repos = [{"full_name": f"Cratis/R{i}", "visibility": "private" if i < 3 else "public"}
                 for i in range(20)]
        lock = threading.Lock()
        active = peak = calls = 0
        def get(endpoint):
            nonlocal active, peak, calls
            with lock:
                active += 1
                peak = max(peak, active)
                calls += 1
            try:
                time.sleep(.0001)
                if endpoint == "rate_limit":
                    return {"resources": {"core": {"remaining": 5000}}}
                if endpoint.startswith("orgs/Cratis/repos?"):
                    return repos
                if endpoint == "orgs/Cratis":
                    return {"total_private_repos": 3}
                if "/git/trees/" in endpoint:
                    return {"tree": [{"path": ".github/workflows/build.yml", "type": "blob", "sha": "hosted"}]}
                if "/git/blobs/" in endpoint:
                    return {"content": base64.b64encode(b"runs-on: ubuntu-latest").decode()}
                if "/jobs?" in endpoint:
                    return {"jobs": []}
                page = int(endpoint.rsplit("=", 1)[1])
                if page > 1:
                    return {"workflow_runs": []}
                day = int(endpoint.split("created=2026-09-")[1][:2])
                return {"workflow_runs": [{"id": day * 100 + i, "path": ".github/workflows/build.yml",
                                           "head_sha": "abc"} for i in range(100)]}
            finally:
                with lock:
                    active -= 1
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[2]), 14000)
        self.assertFalse(data[4])
        self.assertLess(calls, 5000)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 8)

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
