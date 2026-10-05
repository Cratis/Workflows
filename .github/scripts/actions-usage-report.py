#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Report job execution, estimated hosted billing, and scale-set queue pressure."""
import datetime as dt
import heapq
import json
import math
import os
import subprocess
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class APIError(Exception):
    pass


class CollectionStopped(APIError):
    pass


class RunWindowTooLarge(APIError):
    pass


class RequestBudget:
    """Reserve each request (including pagination) atomically across workers."""
    def __init__(self, get, remaining, max_seconds):
        self.get = get
        self.remaining = max(0, remaining - 100)  # Leave capacity to publish the issue.
        self.deadline = time.monotonic() + max_seconds
        self.lock = threading.Lock()
        self.reason = None

    def reserve(self):
        with self.lock:
            if time.monotonic() >= self.deadline:
                self.reason = "Collection time budget exhausted"
            if self.remaining <= 0:
                self.reason = "API request budget exhausted (100 requests reserved for publication)"
            if self.reason:
                raise CollectionStopped(self.reason)
            self.remaining -= 1

    def __call__(self, endpoint):
        self.reserve()
        return self.fetch(endpoint)

    def fetch(self, endpoint):
        """Fetch a request whose budget was already reserved."""
        try:
            return self.get(endpoint)
        except CollectionStopped as error:
            with self.lock:
                self.reason = str(error)
            raise


def api(endpoint):
    try:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
        if result.returncode:
            if "rate limit" in result.stderr.lower():
                raise CollectionStopped("GitHub API rate limit reached")
            raise APIError("GitHub API request failed")
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError) as error:
        raise APIError("GitHub API request timed out or returned invalid JSON") from error


def pages(get, endpoint, key=None):
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 1001):
        response = get(f"{endpoint}{separator}per_page=100&page={page}")
        if key == "workflow_runs" and response.get("total_count", 0) > 1000:
            raise RunWindowTooLarge("Filtered run search exceeds GitHub's 1000-result limit")
        items = response[key] if key else response
        if not isinstance(items, list):
            raise APIError("Invalid API listing")
        yield from items
        if len(items) < 100:
            return
    raise APIError("API pagination limit exceeded")


def window_runs(get, full, start, end):
    """Bisect capped searches; callers deduplicate inclusive range boundaries."""
    start_query = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_query = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    endpoint = f"repos/{full}/actions/runs?created={start_query}..{end_query}"
    try:
        yield from pages(get, endpoint, "workflow_runs")
    except RunWindowTooLarge:
        middle = (start + (end - start) / 2).replace(microsecond=0)
        if middle <= start or middle >= end:
            raise  # Even a one-second window is capped: report the gap explicitly.
        yield from window_runs(get, full, middle, end)
        yield from window_runs(get, full, start, middle)


def timestamp(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect(get, owner, since, until, workers=8, max_seconds=480):
    # Do not start a collection whose request budget cannot be established.
    try:
        remaining = get("rate_limit")["resources"]["core"]["remaining"]
    except (APIError, KeyError, TypeError) as error:
        raise APIError("API rate budget unavailable; collection not started.") from error
    request = RequestBudget(get, remaining, max_seconds)
    repos, private_total, errors = [], None, []
    try:
        # Retain archived metadata for the visibility check, but never collect its usage.
        repos = list(pages(request, f"orgs/{owner}/repos"))
    except APIError as error:
        raise APIError("Unable to enumerate repositories; collection not started.") from error
    try:
        private_total = request(f"orgs/{owner}").get("total_private_repos")
    except CollectionStopped as error:
        return repos, private_total, [], [], [str(error)]
    except APIError:
        errors.append("Organization private-repository count unavailable; coverage cannot be confirmed.")

    def inventory(repo):
        full = repo["full_name"]
        visibility = repo.get("visibility", "private" if repo.get("private") else "public")
        runs, candidates, seen_runs, failures = [], [], set(), Counter()
        inventory_errors = []
        cursor = until
        completed_window = False
        try:
            while cursor > since:
                window_start = max(cursor - dt.timedelta(days=1), since)
                try:
                    for run in window_runs(request, full, window_start, cursor):
                        if run["id"] in seen_runs:
                            continue  # GitHub's range endpoints are inclusive.
                        seen_runs.add(run["id"])
                        runs.append((full, visibility, run))
                        # Public repositories use hosted runners and need run-level data only.
                        if visibility == "private":
                            candidates.append((full, visibility, run))
                    completed_window = True
                except CollectionStopped:
                    raise
                except APIError:
                    failures["Run inventory incomplete"] += 1
                cursor = window_start
        except CollectionStopped:
            if not completed_window:
                inventory_errors.append(f"Run inventory not started for {full}; all runs are missing.")
            else:
                inventory_errors.append(f"Run inventory stopped before completing {full}; "
                                        f"runs created before {cursor.strftime('%Y-%m-%dT%H:%M:%SZ')} are missing.")
        inventory_errors.extend(f"{kind} for {full}: {count} daily windows." for kind, count in failures.items())
        return runs, candidates, inventory_errors

    def job_inventory(candidate, page):
        full, visibility, run = candidate
        found = []
        try:
            # Fetch only the page reserved by the scheduler; continuation pages
            # compete with other runs for the same newest-first request budget.
            response = request.fetch(f"repos/{full}/actions/runs/{run['id']}/jobs?filter=all&per_page=100&page={page}")
            items = response["jobs"]
            if not isinstance(items, list):
                raise APIError("Invalid API listing")
            for job in items:
                found.append((full, visibility, run.get("name") or "Unnamed workflow", job))
            if len(items) >= 100:
                if page == 1000:
                    raise APIError("API pagination limit exceeded")
                return found, None, page + 1
        except CollectionStopped:
            return found, "Job inventory stopped before completing listed private runs", None
        except (APIError, KeyError, ValueError, UnicodeError):
            return found, "Job or runner inventory incomplete", None
        return found, None, None

    active = [repo for repo in repos if not repo.get("archived")]
    active.sort(key=lambda repo: not (repo.get("private") or repo.get("visibility") == "private"))
    runs, jobs, seen_jobs, job_failures = [], [], set(), Counter()
    private_repos = deque(repo for repo in active if repo.get("private") or repo.get("visibility") == "private")
    public_repos = deque(repo for repo in active if not (repo.get("private") or repo.get("visibility") == "private"))
    candidates, pending, private_inventories = [], {}, set()

    def queue_jobs(candidate, page):
        created = timestamp(candidate[2].get("created_at")) or since
        heapq.heappush(candidates, (-created.timestamp(), candidate[0], candidate[2]["id"], page, candidate))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while private_repos or public_repos or candidates or pending:
            # Inventory every private repository before jobs so a budget cut drops
            # the globally oldest listed runs. Start public inventories next, but
            # do not wait for them to finish before collecting private jobs.
            while len(pending) < workers:
                if private_repos:
                    future = pool.submit(inventory, private_repos.popleft())
                    private_inventories.add(future)
                    pending[future] = None
                elif private_inventories:
                    break
                elif public_repos:
                    pending[pool.submit(inventory, public_repos.popleft())] = None
                elif candidates:
                    page, candidate = heapq.heappop(candidates)[3:]
                    try:
                        request.reserve()
                    except CollectionStopped:
                        job_failures[(candidate[0], "Job inventory stopped before completing listed private runs")] += 1
                        continue
                    pending[pool.submit(job_inventory, candidate, page)] = (candidate, page)
                else:
                    break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                task = pending.pop(future)
                if task is None:
                    private_inventories.discard(future)
                    repo_runs, repo_candidates, repo_errors = future.result()
                    runs.extend(repo_runs)
                    for entry in repo_candidates:
                        queue_jobs(entry, 1)
                    errors.extend(repo_errors)
                    continue
                candidate, _ = task
                found, failure, continuation = future.result()
                if continuation is not None:
                    queue_jobs(candidate, continuation)
                if failure:
                    job_failures[(candidate[0], failure)] += 1
                for entry in found:
                    key = (entry[0], entry[3]["id"])
                    if key not in seen_jobs:
                        seen_jobs.add(key)
                        jobs.append(entry)
    errors.extend(f"{kind} for {full}: {count} runs." for (full, kind), count in job_failures.items())
    if request.reason:
        errors.append(request.reason + "; remaining repositories/runs were not collected. Missing usage is unknown.")
    return repos, private_total, runs, jobs, errors


def os_multiplier(labels):
    labels = [label.lower() for label in labels]
    if any(label.startswith(("macos", "mac-")) for label in labels):
        return 10
    if any(label.startswith("windows") for label in labels):
        return 2
    if any(label.startswith("ubuntu") or label == "linux" for label in labels):
        return 1
    return None


def percentile95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None


def render(repos, private_total, runs, jobs, errors, since, until):
    private_visible = sum(repo.get("visibility") == "private" or repo.get("private", False) for repo in repos)
    repos = [repo for repo in repos if not repo.get("archived")]
    private_active = sum(repo.get("visibility") == "private" or repo.get("private", False) for repo in repos)
    private = defaultdict(lambda: [0, 0.0, 0.0, 0])
    workflows = defaultdict(lambda: [0, 0.0, 0])
    public_workflows = defaultdict(lambda: [0, 0.0])
    for full, visibility, run in runs:
        if visibility != "public" or run.get("status") != "completed":
            continue
        start, end = timestamp(run.get("run_started_at")), timestamp(run.get("updated_at"))
        if start is None or end is None or end < start or not since <= start < until:
            continue
        row = public_workflows[f"{full} / {run.get('name') or 'Unnamed workflow'}"]
        row[0] += 1
        row[1] += (end - start).total_seconds() / 60
    arc_queue, arc_jobs, arc_short, queue_unknown, duration_unknown = [], 0, 0, 0, 0
    unknown_runner = 0
    hosted_private = 0
    excluded = 0
    for full, visibility, workflow, job in jobs:
        if job.get("conclusion") == "skipped" or not job.get("runner_name") or not job.get("runner_id"):
            excluded += 1
            continue
        start, end = timestamp(job.get("started_at")), timestamp(job.get("completed_at"))
        if start is None or end is None or end < start:
            # In-progress/never-started jobs have no final execution or billing verdict.
            duration_unknown += 1
            continue
        if not since <= start < until:
            continue
        seconds = (end - start).total_seconds()
        minutes = seconds / 60
        if visibility == "private":
            row = workflows[f"{full} / {workflow}"]
            row[0] += 1
            row[1] += minutes
            row[2] += seconds < 60
        hosted = job.get("runner_group_name") == "GitHub Actions"
        labels = job.get("labels") or []
        arc = not hosted and "cratis-arc" in labels
        if arc:
            arc_jobs += 1
            arc_short += seconds < 60
            created = timestamp(job.get("created_at"))
            # Do not substitute run creation: that includes dependency waits, not just job queueing.
            if created and created <= start:
                arc_queue.append((start - created).total_seconds() / 60)
            else:
                queue_unknown += 1
        if visibility == "private":
            p = private[full]
            p[0] += 1
            multiplier = os_multiplier(labels) if hosted else None
            if hosted and multiplier is not None:
                billed = math.ceil(seconds / 60) * multiplier
                p[1] += billed
                hosted_private += billed
            elif hosted or not job.get("runner_group_name"):
                p[3] += 1
                unknown_runner += 1
            else:
                p[2] += minutes
    lines = [f"Actions usage from {since.isoformat()} to {until.isoformat()} (UTC).", "",
             "Hosted private minutes are estimates: each completed job is rounded up to a minute, "
             "then multiplied by Linux 1×, Windows 2× or macOS 10×. Public hosted jobs are free. "
             "Self-hosted execution is not billed by GitHub; this is not an invoice. "
             "The inventory covers runs created in the period and their completed jobs that started in the period.", "",
             f"Visible non-archived repositories: **{len(repos)}**, private: **{private_active}**. "
             f"Inventoried runs: **{len(runs)}**, jobs (all attempts): **{len(jobs)}**.", ""]
    dynamic_runs = sum(visibility != "private" and run.get("path", "").startswith("dynamic/")
                       for _, visibility, run in runs)
    lines.extend([f"GitHub-managed dynamic runs (included in public run-level totals): **{dynamic_runs}**.", ""])
    if private_visible == 0:
        lines.append("**Private repository coverage unavailable:** PAT_WORKFLOWS exposes no private repositories. "
                     "This is not evidence of zero private usage. Give the reporting token read access to private "
                     "repository metadata and Actions; on plans without private organization-secret support, use "
                     "a repository secret in Workflows.")
    elif private_total is None:
        lines.append("**Private coverage unconfirmed:** the token-filtered repository list may omit private "
                     "repositories. The organization private-repository count is unavailable.")
    elif private_visible < private_total:
        lines.append(f"**Partial private coverage:** {private_visible} private repositories visible (including archived) "
                     f"out of {private_total} organization private repositories (including archived repositories). "
                     "Check PAT_WORKFLOWS repository access; missing usage is not zero.")
    else:
        lines.append("Private repository visibility matches the organization private-repository count.")
    lines.extend(["", "### Private repositories", "",
                  "| Repository | completed jobs | estimated hosted weighted minutes | self-hosted execution minutes | unclassified jobs |",
                  "|---|---:|---:|---:|---:|"])
    for repo in sorted((r["full_name"] for r in repos if r.get("visibility") == "private" or r.get("private")),
                       key=lambda name: -private[name][1]):
        n, billed, self_hosted, unknown = private[repo]
        lines.append(f"| {repo} | {n} | {billed:,.0f} | {self_hosted:,.1f} | {unknown} |")
    if not private_active:
        lines.append("| Not observable with PAT_WORKFLOWS | — | unknown | unknown | — |")
    if hosted_private > 300:
        lines.extend(["", f"**Alert: observed private hosted usage is {hosted_private:,.0f} weighted minutes, "
                      "above the 300-minute weekly guardrail.**"])
    lines.extend(["", "### cratis-arc queue pressure", "",
                  f"Completed jobs: **{arc_jobs}**; execution under one minute: **{arc_short}**."])
    p95 = percentile95(arc_queue)
    lines.append(f"Job queue p95: **{p95:.1f} minutes** ({len(arc_queue)} samples)." if p95 is not None
                 else "Job queue p95: **unavailable** (no valid job creation/start timestamps).")
    lines.append(f"Missing queue timestamps: **{queue_unknown}**. Run creation is not substituted for job creation.")
    lines.extend(["", "### Top 20 private workflows by job execution minutes", "",
                  "| Workflow | completed jobs | execution minutes | jobs under 1 minute |",
                  "|---|---:|---:|---:|"])
    for name, (n, minutes, short) in sorted(workflows.items(), key=lambda item: -item[1][1])[:20]:
        lines.append(f"| {name.replace('|', '/')} | {n} | {minutes:,.1f} | {short} |")
    lines.extend(["", "### Top 20 public workflows by run elapsed minutes", "",
                  "Run-level elapsed time includes dependency waits and is not job execution or billing. "
                  "Public repositories use GitHub-hosted runners and are reported from run-level data only. "
                  "Job-level billing and cratis-arc queue statistics cover private repositories only.", "",
                  "| Workflow | completed runs | elapsed minutes |", "|---|---:|---:|"])
    for name, (n, minutes) in sorted(public_workflows.items(), key=lambda item: -item[1][1])[:20]:
        lines.append(f"| {name.replace('|', '/')} | {n} | {minutes:,.1f} |")
    lines.extend(["", f"Skipped or never-assigned jobs excluded from execution and queue statistics: **{excluded}**.",
                  f"Jobs with unavailable final duration: **{duration_unknown}**. "
                  f"Private jobs with unknown runner group or OS: **{unknown_runner}**; these are not counted as zero billing."])
    if errors:
        lines.extend(["", "### Incomplete API coverage", "", "Missing data must not be treated as zero usage.", ""])
        lines.extend(f"- {error}" for error in sorted(set(errors)))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    until = dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(days=7)
    try:
        data = collect(api, os.environ["GITHUB_REPOSITORY_OWNER"], since, until)
        print(render(*data, since, until), end="")
    except APIError:
        raise SystemExit("Unable to enumerate repositories; no usage report published.")
