#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Fail when a repository that releases in batches has releasable merges waiting too long.

A repository opts in to batched releases (Cratis/Workflows#189) with `release-window: <minutes>` on its
cratis/release-action step. GitHub cron is best effort and can skip or lag, so this sweep checks that the
oldest merged pull request carrying `major`, `minor` or `patch` since the latest non-draft release is not
older than the window plus 60 minutes. All GitHub calls are read only and live in `fetch_*`; `evaluate` is
a pure function.

Exit codes: 0 ran clean, 1 found stale repositories, 2 could not run.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

OWNER = "Cratis"
PUBLISH_PATH = ".github/workflows/publish.yml"
RELEASE_LABELS = {"major", "minor", "patch"}
GRACE_MINUTES = 60
WINDOW_PATTERN = re.compile(r"^\s*release-window:\s*['\"]?(\d+)", re.MULTILINE)


class CouldNotRun(Exception):
    pass


@dataclass
class Verdict:
    repo: str
    window: int
    opted_in: bool
    pending: list = field(default_factory=list)  # (number, merged_at) oldest first
    age_minutes: int = 0
    stale: bool = False

    def summary(self):
        if not self.opted_in:
            return f"{self.repo}: not opted in"
        if not self.pending:
            return f"{self.repo}: window {self.window}m, nothing pending"
        state = "STALE" if self.stale else "ok"
        return (f"{self.repo}: window {self.window}m, {len(self.pending)} pending, "
                f"oldest {self.age_minutes}m old, {state}")

    def error(self):
        numbers = ", ".join(f"#{number}" for number, _ in self.pending)
        return (f"::error::{self.repo}: release window is {self.window} minutes but {numbers} "
                f"merged {self.age_minutes} minutes ago and has not been released "
                f"(limit {self.window + GRACE_MINUTES} minutes); the schedule may have been skipped")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_window(publish_yml):
    """The release-window in minutes from a publish workflow, ignoring comments; 0 when absent."""
    active = "\n".join(line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                       for line in publish_yml.splitlines())
    match = WINDOW_PATTERN.search(active)
    return int(match.group(1)) if match else 0


def evaluate(repo, window, releases, pulls, now):
    """Decide whether releasable merges are waiting too long.

    releases: dicts with `created_at` and `draft`. pulls: dicts with `number`, `merged_at` and `labels`
    (names) for pull requests merged into the default branch.
    """
    if window <= 0:
        return Verdict(repo, window, opted_in=False)
    created = [parse_time(r["created_at"]) for r in releases if not r.get("draft")]
    since = max(created) if created else None
    pending = sorted(
        (parse_time(p["merged_at"]), p["number"])
        for p in pulls
        if p.get("merged_at")
        and RELEASE_LABELS & set(p.get("labels", []))
        and (since is None or parse_time(p["merged_at"]) > since))
    verdict = Verdict(repo, window, opted_in=True, pending=[(n, t) for t, n in pending])
    if pending:
        verdict.age_minutes = int((now - pending[0][0]).total_seconds() // 60)
        verdict.stale = verdict.age_minutes > window + GRACE_MINUTES
    return verdict


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise CouldNotRun(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def fetch_repositories():
    out = gh("api", f"orgs/{OWNER}/repos", "--paginate", "--jq",
             '.[] | select(.archived | not) | [.name, .default_branch] | @tsv')
    return [tuple(line.split("\t")) for line in out.splitlines() if line]


def fetch_publish_window(repo, branch):
    result = subprocess.run(
        ["gh", "api", f"repos/{OWNER}/{repo}/contents/{PUBLISH_PATH}?ref={branch}", "--jq", ".content"],
        capture_output=True, text=True)
    if result.returncode != 0:
        if "404" in result.stderr:
            return 0
        raise CouldNotRun(f"reading {repo} publish.yml failed: {result.stderr.strip()}")
    return parse_window(base64.b64decode(result.stdout).decode("utf-8", "replace"))


def fetch_releases(repo):
    out = gh("api", f"repos/{OWNER}/{repo}/releases?per_page=30", "--jq",
             "[.[] | {created_at, draft}]")
    return json.loads(out or "[]")


def fetch_merged_pulls(repo, branch, releases, now):
    created = [parse_time(r["created_at"]) for r in releases if not r.get("draft")]
    since = (max(created) if created else now - timedelta(days=90)).date().isoformat()
    query = f"repo:{OWNER}/{repo} is:pr is:merged base:{branch} merged:>={since}"
    out = gh("api", "-X", "GET", "search/issues", "-f", f"q={query}", "-F", "per_page=100", "--paginate",
             "--jq", ".items[] | {number, labels: [.labels[].name], merged_at: .pull_request.merged_at}")
    return [json.loads(line) for line in out.splitlines() if line]


def sweep(now):
    verdicts = []
    for repo, branch in fetch_repositories():
        window = fetch_publish_window(repo, branch)
        if window <= 0:
            continue
        releases = fetch_releases(repo)
        pulls = fetch_merged_pulls(repo, branch, releases, now)
        verdicts.append(evaluate(repo, window, releases, pulls, now))
    return verdicts


def self_test():
    """Plant a stale and a fresh repository and fail unless the decision tells them apart."""
    now = parse_time("2026-01-01T12:00:00Z")
    releases = [{"created_at": "2026-01-01T08:00:00Z", "draft": False}]
    stale = evaluate("Planted", 60, releases, [{"number": 1, "merged_at": "2026-01-01T09:00:00Z", "labels": ["patch"]}], now)
    fresh = evaluate("Planted", 60, releases, [{"number": 2, "merged_at": "2026-01-01T11:00:00Z", "labels": ["patch"]}], now)
    if not stale.stale or fresh.stale or parse_window("release-window: 60") != 60:
        print("::error::self-test failed: the staleness decision no longer separates stale from fresh")
        return 1
    print("self-test ok: planted stale repository found, fresh one passed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--self-test", action="store_true", help="plant a stale repository and prove it is found")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        verdicts = sweep(datetime.now(timezone.utc))
    except CouldNotRun as error:
        print(f"::error::could not run: {error}")
        return 2
    if not verdicts:
        print("::error::could not run: no repository opts in to batched releases, so nothing was checked")
        return 2
    for verdict in verdicts:
        print(verdict.summary())
    stale = [v for v in verdicts if v.stale]
    for verdict in stale:
        print(verdict.error())
    print(f"checked {len(verdicts)} batched repositories, {len(stale)} stale")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
