# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline usage-report fixtures; no token or live GitHub calls."""
from concurrent.futures import Future
import datetime as dt
import importlib.util
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

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

    def test_public_collection_uses_only_run_level_data_for_all_workflows(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 0}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PUBLIC]
            if "/actions/runs?" in endpoint:
                return {"workflow_runs": [{"id": i, "path": path, "head_sha": str(i), "name": "Build"}
                                         for i, path in enumerate((".github/workflows/hosted.yml",
                                                                   ".github/workflows/arc.yml",
                                                                   "dynamic/dependabot/dependabot-updates"))]}
            self.fail(f"Public workflows must not request definitions or jobs: {endpoint}")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[2]), 3)
        self.assertFalse(data[3])
        self.assertFalse(data[4])
        output = report.render(*data, SINCE, UNTIL)
        self.assertIn("reported from run-level data only", output)
        self.assertIn("queue statistics cover private repositories only", output)

    def test_budget_exhaustion_is_explicit_and_a_fresh_collection_can_resume(self):
        calls = []
        remaining = 111
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
            day = int(endpoint.split("created=2026-09-")[1][:2])
            return {"workflow_runs": [{"id": day, "name": "Build"}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(calls[0], "rate_limit")
        self.assertEqual(len(calls), 12)  # Preflight plus exactly eleven reserved requests.
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
        with self.assertRaisesRegex(report.APIError, "collection not started"):
            report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(calls, ["rate_limit"])

    def test_time_budget_stops_with_partial_report(self):
        def get(endpoint):
            return {"resources": {"core": {"remaining": 5000}}}
        with self.assertRaisesRegex(report.APIError, "Unable to enumerate repositories"):
            report.collect(get, "Cratis", SINCE, UNTIL, max_seconds=0)

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

    def test_dynamic_workflows_are_included_in_public_run_level_totals(self):
        calls = []
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 0}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PUBLIC]
            if "/actions/runs?" in endpoint:
                return {"workflow_runs": [{"id": i, "path": path, "head_sha": "abc"}
                                         for i, path in enumerate(("dynamic/github-code-quality/codeql",
                                                                   "dynamic/dependabot/dependabot-updates"))]}
            self.fail(f"Dynamic workflows must not request definitions or jobs: {endpoint}")
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        output = report.render(*data, SINCE, UNTIL)
        self.assertFalse(data[4])
        self.assertIn("GitHub-managed dynamic runs (included in public run-level totals): **2**", output)
        self.assertNotIn("Incomplete API coverage", output)

    def test_over_cap_window_is_recursively_split_and_inclusive_boundaries_are_deduplicated(self):
        calls = []
        end = SINCE + dt.timedelta(days=1)
        middle = SINCE + dt.timedelta(hours=12)
        created = [SINCE + dt.timedelta(seconds=i * 60) for i in range(1201)]
        def get(endpoint):
            calls.append(endpoint)
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if "/jobs?" in endpoint:
                return {"jobs": [job(int(endpoint.split("/runs/")[1].split("/")[0]))]}
            query = endpoint.split("created=")[1].split("&")[0]
            start, stop = (report.timestamp(value) for value in query.split(".."))
            entries = [{"id": i, "name": "Build"} for i, date in enumerate(created) if start <= date <= stop]
            page = int(endpoint.rsplit("=", 1)[1])
            return {"total_count": len(entries), "workflow_runs": entries[(page - 1) * 100:page * 100]}
        data = report.collect(get, "Cratis", SINCE, end)
        self.assertFalse(data[4])
        self.assertEqual(len(data[2]), 1201)
        self.assertEqual(len(data[3]), 1201)
        self.assertTrue(any(f"..{middle.strftime('%Y-%m-%dT%H:%M:%SZ')}" in call for call in calls))

    def test_even_a_capped_one_second_window_reports_a_gap(self):
        with self.assertRaises(report.RunWindowTooLarge):
            list(report.window_runs(lambda _: {"total_count": 1001, "workflow_runs": []},
                                    "Cratis/Private", SINCE, SINCE + dt.timedelta(seconds=1)))

    def test_repository_listing_failure_aborts_instead_of_publishing_a_token_diagnosis(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            raise report.APIError("transient repository listing failure")
        with self.assertRaisesRegex(report.APIError, "Unable to enumerate repositories"):
            report.collect(get, "Cratis", SINCE, UNTIL)

    def test_private_count_failure_retains_enumerated_repository_usage(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                raise report.APIError("denied")
            if "/jobs?" in endpoint:
                return {"jobs": [job()]}
            return {"workflow_runs": [{"id": 1}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL)
        self.assertEqual(len(data[3]), 1)
        self.assertIn("Private coverage unconfirmed", report.render(*data, SINCE, UNTIL))

    def test_busy_private_and_300_sha_public_repositories_finish_with_latency_and_shared_limits(self):
        volumes = {"Studio": 1428, "Direct": 509, "Strategy": 474,
                   "Infrastructure": 191, "Chronicle.Wolverine": 129, "Other": 397}
        repos = [PUBLIC] + [{"full_name": f"Cratis/{name}", "private": True} for name in volumes]
        lock = threading.Lock()
        calls = active = peak = 0
        # Compress API latency, with extra deadline margin for shared runners.
        # Studio alone would take >14 seconds serially, beyond this 10-second budget.
        latency, budget = .01, 10
        def get(endpoint):
            nonlocal calls, active, peak
            with lock:
                calls += 1
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(latency)
                if endpoint == "rate_limit":
                    return {"resources": {"core": {"remaining": 5000}}}
                if endpoint.startswith("orgs/Cratis/repos?"):
                    return repos
                if endpoint == "orgs/Cratis":
                    return {"total_private_repos": len(volumes)}
                if "/git/" in endpoint or "repos/Cratis/Public/actions/runs/" in endpoint:
                    self.fail(f"Public collection must not inspect definitions or jobs: {endpoint}")
                if "/jobs?" in endpoint:
                    return {"jobs": [job(int(endpoint.split("/runs/")[1].split("/")[0]))]}
                name = endpoint.split("repos/Cratis/")[1].split("/")[0]
                day = int(endpoint.split("created=2026-09-")[1][:2]) - 24
                volume = 2100 if name == "Public" else volumes[name]
                entries = [{"id": i, "name": "Build", "head_sha": f"sha-{i % 300}"}
                           for i in range(volume) if i % 7 == day]
                page = int(endpoint.rsplit("=", 1)[1])
                return {"total_count": len(entries), "workflow_runs": entries[(page - 1) * 100:page * 100]}
            finally:
                with lock:
                    active -= 1
        start = time.monotonic()
        data = report.collect(get, "Cratis", SINCE, UNTIL, max_seconds=budget)
        self.assertFalse(data[4])
        self.assertEqual(len(data[3]), sum(volumes.values()))
        self.assertEqual(len(data[2]), sum(volumes.values()) + 2100)
        self.assertEqual(len({run[2]["head_sha"] for run in data[2] if run[1] == "public"}), 300)
        self.assertLess(time.monotonic() - start, budget)
        self.assertLess(calls, 4900)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 8)

    def test_private_jobs_start_before_public_run_inventory_finishes(self):
        private_job_started = threading.Event()
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 5000}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PUBLIC, PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if "/jobs?" in endpoint:
                private_job_started.set()
                return {"jobs": [job()]}
            if "repos/Cratis/Public/" in endpoint:
                self.assertTrue(private_job_started.wait(1), "Private jobs waited for public inventory")
                return {"workflow_runs": []}
            return {"workflow_runs": [{"id": 1}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL, workers=2)
        self.assertEqual(len(data[3]), 1)
        self.assertFalse(data[4])

    def test_budget_cut_keeps_newest_private_days_and_names_incomplete_listed_runs(self):
        listing_days, job_days = [], []
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 111}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if "/jobs?" in endpoint:
                day = int(endpoint.split("/runs/")[1].split("/")[0])
                job_days.append(day)
                return {"jobs": [job(day)]}
            day = int(endpoint.split("created=2026-09-")[1][:2])
            listing_days.append(day)
            return {"workflow_runs": [{"id": day, "created_at": f"2026-09-{day}T12:00:00Z"}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL, workers=1)
        self.assertEqual(listing_days, list(range(30, 23, -1)))
        self.assertEqual(job_days, [30, 29])
        self.assertEqual([entry[3]["id"] for entry in data[3]], [30, 29])
        self.assertIn("Job inventory stopped before completing listed private runs for Cratis/Private: 5 runs.", data[4])
        self.assertNotIn("older private runs may be missing", "\n".join(data[4]))

    def test_multi_repository_budget_cut_drops_globally_oldest_listed_private_runs(self):
        repos = [{"full_name": f"Cratis/P{i}", "private": True} for i in range(9)]
        for workers in (1, 8):
            with self.subTest(workers=workers):
                first_inventory_finished = threading.Event()
                finished_inventories, collected = set(), []
                lock = threading.Lock()
                def get(endpoint):
                    if endpoint == "rate_limit":
                        # Org metadata, nine weekly inventories, then eighteen jobs.
                        return {"resources": {"core": {"remaining": 100 + 2 + 9 * 7 + 18}}}
                    if endpoint.startswith("orgs/Cratis/repos?"):
                        return repos
                    if endpoint == "orgs/Cratis":
                        return {"total_private_repos": 9}
                    full = endpoint.split("repos/")[1].split("/actions/")[0]
                    if "/jobs?" in endpoint:
                        identity = int(endpoint.split("/runs/")[1].split("/")[0])
                        with lock:
                            self.assertEqual(finished_inventories, {repo["full_name"] for repo in repos})
                            collected.append((full, identity % 100))
                        return {"jobs": [job(identity)]}
                    day = int(endpoint.split("created=2026-09-")[1][:2])
                    if full == "Cratis/P8":
                        self.assertTrue(first_inventory_finished.wait(1))
                    if day == 24:
                        with lock:
                            finished_inventories.add(full)
                        if full == "Cratis/P0":
                            first_inventory_finished.set()
                    identity = int(full.rsplit("P", 1)[1]) * 100 + day
                    return {"workflow_runs": [{"id": identity, "created_at": f"2026-09-{day}T12:00:00Z"}]}
                data = report.collect(get, "Cratis", SINCE, UNTIL, workers=workers)
                self.assertEqual(set(collected), {(repo["full_name"], day) for repo in repos for day in (30, 29)})
                self.assertEqual(len(data[3]), 18)
                self.assertEqual(len(data[2]), 63)
                for repo in repos:
                    self.assertIn("Job inventory stopped before completing listed private runs "
                                  f"for {repo['full_name']}: 5 runs.", data[4])

    def test_job_continuations_keep_run_priority_with_immediate_page_arrivals(self):
        # Finish submitted pages immediately so both first-page results reach
        # the scheduler together, without relying on worker timing.
        class ImmediateExecutor:
            def __init__(self, max_workers):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, function, *args):
                future = Future()
                future.set_result(function(*args))
                return future

        calls = []
        def get(endpoint):
            if endpoint == "rate_limit":
                # Metadata, one daily window, two first pages, one continuation.
                return {"resources": {"core": {"remaining": 106}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            if "/jobs?" in endpoint:
                identity = int(endpoint.split("/runs/")[1].split("/")[0])
                page = int(endpoint.rsplit("=", 1)[1])
                calls.append((identity, page))
                if page == 1:
                    return {"jobs": [job(identity * 1000 + i) for i in range(100)]}
                return {"jobs": [job(identity * 1000 + 100)]}
            return {"workflow_runs": [{"id": day, "created_at": f"2026-09-{day}T12:00:00Z"}
                                      for day in (29, 30, 28)]}

        def completed_newest_last(pending, return_when):
            # Hand back the older continuation first; arrival order must not
            # decide which continuation gets the final budget reservation.
            return list(reversed(pending)), set()

        with patch.object(report, "ThreadPoolExecutor", ImmediateExecutor), \
                patch.object(report, "wait", completed_newest_last):
            data = report.collect(get, "Cratis", UNTIL - dt.timedelta(days=1), UNTIL, workers=2)
        self.assertEqual(calls, [(30, 1), (29, 1), (30, 2)])
        self.assertEqual(len(data[3]), 201)
        self.assertIn("Job inventory stopped before completing listed private runs for Cratis/Private: 2 runs.", data[4])

    def test_first_window_budget_cut_distinguishes_unstarted_and_partial_inventories(self):
        for partial_page in (False, True):
            with self.subTest(partial_page=partial_page):
                calls = []
                def get(endpoint):
                    calls.append(endpoint)
                    if endpoint == "rate_limit":
                        return {"resources": {"core": {"remaining": 103 if partial_page else 102}}}
                    if endpoint.startswith("orgs/Cratis/repos?"):
                        return [PRIVATE]
                    if endpoint == "orgs/Cratis":
                        return {"total_private_repos": 1}
                    return {"workflow_runs": [{"id": i, "created_at": "2026-09-30T00:00:00Z"}
                                             for i in range(100)]}
                data = report.collect(get, "Cratis", SINCE, UNTIL, workers=1)
                if partial_page:
                    self.assertIn("Run inventory stopped before completing Cratis/Private; "
                                  "runs created before 2026-09-30T00:00:00Z may be missing.", data[4])
                    self.assertFalse(any("all runs are missing" in error for error in data[4]))
                else:
                    self.assertIn("Run inventory not started for Cratis/Private; all runs are missing.", data[4])
                self.assertNotIn("older runs may be missing", "\n".join(data[4]))
                self.assertEqual(len(data[2]), 100 if partial_page else 0)
                self.assertEqual(len(calls), 4 if partial_page else 3)

    def test_failed_run_windows_then_budget_cut_reports_partial_inventory_consistently(self):
        for partial_page in (False, True):
            with self.subTest(partial_page=partial_page):
                def get(endpoint):
                    if endpoint == "rate_limit":
                        # Metadata, then two failing daily windows; no completed windows.
                        return {"resources": {"core": {"remaining": 106 if partial_page else 104}}}
                    if endpoint.startswith("orgs/Cratis/repos?"):
                        return [PUBLIC]
                    if endpoint == "orgs/Cratis":
                        return {"total_private_repos": 0}
                    if partial_page and endpoint.endswith("page=1"):
                        day = int(endpoint.split("created=2026-09-")[1][:2])
                        return {"workflow_runs": [{"id": day * 100 + i,
                                                  "created_at": f"2026-09-{day}T12:00:00Z"}
                                                 for i in range(100)]}
                    raise report.APIError("denied")
                data = report.collect(get, "Cratis", SINCE, UNTIL, workers=1)
                self.assertEqual(len(data[2]), 200 if partial_page else 0)
                self.assertIn("Run inventory stopped before completing Cratis/Public; "
                              "runs created before 2026-09-29T00:00:00Z may be missing.", data[4])
                self.assertIn("Run inventory incomplete for Cratis/Public: 2 daily windows.", data[4])
                self.assertFalse(any("all runs are missing" in error for error in data[4]))

    def test_empty_completed_run_window_reports_the_precise_missing_cursor(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 103}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            return {"workflow_runs": []}
        data = report.collect(get, "Cratis", SINCE, UNTIL, workers=1)
        self.assertIn("Run inventory stopped before completing Cratis/Private; "
                      "runs created before 2026-09-30T00:00:00Z may be missing.", data[4])
        self.assertFalse(any("all runs are missing" in error for error in data[4]))

    def test_run_inventory_stop_is_not_misreported_as_one_missing_run(self):
        def get(endpoint):
            if endpoint == "rate_limit":
                return {"resources": {"core": {"remaining": 104}}}
            if endpoint.startswith("orgs/Cratis/repos?"):
                return [PRIVATE]
            if endpoint == "orgs/Cratis":
                return {"total_private_repos": 1}
            day = int(endpoint.split("created=2026-09-")[1][:2])
            return {"workflow_runs": [{"id": day}]}
        data = report.collect(get, "Cratis", SINCE, UNTIL, workers=1)
        self.assertIn("Run inventory stopped before completing Cratis/Private; "
                      "runs created before 2026-09-29T00:00:00Z may be missing.", data[4])
        self.assertTrue(any("Job inventory stopped" in error and "2 runs" in error for error in data[4]))
        self.assertFalse(any("1 runs" in error for error in data[4]))

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
