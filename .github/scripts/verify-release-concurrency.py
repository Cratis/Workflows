#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Fail when a publish, release or deploy workflow can cancel a release or drop a pending one.

cratis/release-action releases one merged pull request per run. GitHub's default concurrency keeps a
single pending run per group and cancels any older pending one, so a release workflow without
`queue: max` loses releases when merges cluster, and `cancel-in-progress` stops a release half-way.

Exit codes: 0 ran clean, 1 found violations, 2 could not run.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # Reported as "could not run" by main().
    yaml = None

OWNER = "Cratis"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
SUBJECT_PATTERN = re.compile(r"publish|release|deploy", re.IGNORECASE)

# (repository, workflow file, reason). The exemption covers only the missing `queue: max`; cancelling
# is never exempt. The organization sweep fails an entry once its file no longer needs it.
ALLOWLIST = (
    ("Studio", "publish.yml",
     "aggregates every unreleased pull request per run and keeps a single pending slot on purpose (plan §5)"),
    ("Arc", "chronicle-published.yml",
     "dependency bump per Chronicle version, one group per version; not a release of Arc"),
    *(("Chronicle", f"publish-{client}-client.yml",
       "reusable workflow; concurrency inherited from caller publish.yml, and a group of its own "
       "could deadlock against the calling run (manual dispatch is the only other trigger)")
      for client in ("elixir", "kotlin", "python", "typescript")),
)
ALLOWED = {(repo, name) for repo, name, _ in ALLOWLIST}

CANCEL_REASON = "cancel-in-progress is not literally false ({where}); a cancelled release is lost"
DROPS = "GitHub keeps one pending run and cancels older pending releases"
QUEUE_REASON = f"no 'queue: max' concurrency at workflow or job level; {DROPS}"
WORKFLOW_QUEUE_REASON = ("workflow-level concurrency group is shared between runs but has no 'queue: max' "
                         f"(it applies before any job group); {DROPS}")
JOB_QUEUE_REASON = ("workflow-level concurrency group is unique per run or commit, but no job has "
                    f"'queue: max'; {DROPS}")
UNIQUE_GROUP_MARKERS = ("github.run_id", "github.sha")


class CouldNotRun(Exception):
    pass


class NotFound(Exception):
    pass


def is_subject_name(name):
    lower = name.lower()
    if not lower.endswith(WORKFLOW_SUFFIXES) or not SUBJECT_PATTERN.search(lower):
        return False
    # A documentation workflow only triggers a site build, where latest-wins is correct.
    excluded = (lower.startswith(("verify-", "auto-approve"))
                or any(part in lower for part in ("release-notes", "release-intent", "documentation")))
    return not excluded


def is_deploy(name):
    return name.lower().startswith("deploy")


def parse(repo, name, text):
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CouldNotRun(f"{repo}/{name}: could not parse workflow YAML: {error}") from error
    if not isinstance(document, dict):
        raise CouldNotRun(f"{repo}/{name}: workflow is not a YAML mapping")
    return document


def triggers(document):
    # PyYAML follows YAML 1.1, where an unquoted `on:` key parses as the boolean True.
    on = document.get(True, document.get("on"))
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(trigger) for trigger in on}
    return set()


def concurrency_blocks(document):
    """Yield (location, concurrency) for the workflow and for every job that declares one."""
    if "concurrency" in document:
        yield "workflow level", document["concurrency"]
    jobs = document.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if isinstance(job, dict) and "concurrency" in job:
                yield f"job '{job_id}'", job["concurrency"]


def cancels(concurrency):
    return (isinstance(concurrency, dict) and "cancel-in-progress" in concurrency
            and concurrency["cancel-in-progress"] is not False)


def queues(concurrency):
    return isinstance(concurrency, dict) and concurrency.get("queue") == "max"


def unique_per_run(concurrency):
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    return isinstance(group, str) and any(marker in group for marker in UNIQUE_GROUP_MARKERS)


def queue_problem(document):
    """Return why pending releases can be dropped, or None.

    GitHub applies the workflow-level group before any job group, so a shared workflow group must queue
    itself. Only a group unique per run or commit (Ante's per-sha shape) hands the queue to a job.
    """
    jobs_queue = any(queues(concurrency) for where, concurrency in concurrency_blocks(document)
                     if where != "workflow level")
    if "concurrency" not in document:
        return None if jobs_queue else QUEUE_REASON
    workflow = document["concurrency"]
    if queues(workflow):
        return None
    if not unique_per_run(workflow):
        return WORKFLOW_QUEUE_REASON
    return None if jobs_queue else JOB_QUEUE_REASON


class Scan:
    """Accumulates counts and violations across repositories."""

    def __init__(self):
        self.repositories = 0
        self.files = 0
        self.subjects = 0
        self.violations = []
        self.allowlist_used = set()

    def add_repository(self, repo, loaders):
        """Check one repository; `loaders` maps workflow file names to callables returning their text."""
        self.repositories += 1
        for name in sorted(loaders):
            self.files += 1
            if is_subject_name(name):
                self.check(repo, name, loaders[name]())

    def check(self, repo, name, text):
        document = parse(repo, name, text)
        if triggers(document) == {"workflow_call"}:
            return  # Only reachable as a reusable piece, so it runs inside its caller's concurrency group.
        self.subjects += 1
        for where, concurrency in concurrency_blocks(document):
            if cancels(concurrency):
                self.violations.append((repo, name, CANCEL_REASON.format(where=where)))
        problem = None if is_deploy(name) else queue_problem(document)
        if problem is None:
            return  # Latest-wins is correct for a deploy: the newest commit covers the ones before it.
        if (repo, name) in ALLOWED:
            self.allowlist_used.add((repo, name))
            return
        self.violations.append((repo, name, problem))

    def stale_allowlist(self):
        return [(repo, name, f"allowlist entry no longer needed ({reason})")
                for repo, name, reason in ALLOWLIST if (repo, name) not in self.allowlist_used]


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CouldNotRun(f"{path}: {error}") from error


def scan_directory(path, repo):
    directory = Path(path)
    if not directory.is_dir():
        raise CouldNotRun(f"{path}: not a directory")
    workflows = sorted(item for item in directory.iterdir() if item.is_file() and item.name.lower().endswith(WORKFLOW_SUFFIXES))
    if not workflows:
        raise CouldNotRun(f"{path}: no workflow files found")
    scan = Scan()
    scan.add_repository(repo, {item.name: (lambda item=item: read_text(item)) for item in workflows})
    return scan


def gh_api(endpoint):
    try:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CouldNotRun(f"gh api {endpoint}: {error}") from error
    if result.returncode:
        if "HTTP 404" in result.stderr:
            raise NotFound(endpoint)
        raise CouldNotRun(f"gh api {endpoint} failed: {result.stderr.strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise CouldNotRun(f"gh api {endpoint}: invalid JSON response") from error


def list_repositories(get, owner):
    repositories = []
    for page in range(1, 101):
        batch = get(f"orgs/{owner}/repos?type=all&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise CouldNotRun(f"orgs/{owner}/repos: invalid repository listing")
        repositories.extend(batch)
        if len(batch) < 100:
            return sorted(repo["name"] for repo in repositories if not repo.get("archived"))
    raise CouldNotRun(f"orgs/{owner}/repos: pagination limit exceeded")


def fetch_text(get, owner, repo, path):
    try:
        content = get(f"repos/{owner}/{repo}/contents/{path}")
    except NotFound as error:
        raise CouldNotRun(f"{repo}/{path}: listed but not found") from error
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        raise CouldNotRun(f"{repo}/{path}: unexpected contents response")
    try:
        return base64.b64decode(content["content"]).decode("utf-8")
    except (KeyError, ValueError) as error:
        raise CouldNotRun(f"{repo}/{path}: could not decode contents") from error


def workflow_loaders(get, owner, repo):
    """Map each workflow file on the default branch to a loader; a repository without workflows has none."""
    try:
        entries = get(f"repos/{owner}/{repo}/contents/.github/workflows")
    except NotFound:
        return {}
    if not isinstance(entries, list):
        raise CouldNotRun(f"{repo}/.github/workflows: not a directory listing")
    return {entry["name"]: (lambda path=entry["path"]: fetch_text(get, owner, repo, path))
            for entry in entries
            if entry.get("type") == "file" and entry.get("name", "").lower().endswith(WORKFLOW_SUFFIXES)}


def scan_organization(get=gh_api, owner=OWNER):
    try:
        names = list_repositories(get, owner)
    except NotFound as error:
        raise CouldNotRun(f"organization {owner} not found or not visible to the token") from error
    if not names:
        raise CouldNotRun(f"no non-archived repositories found in {owner}")
    scan = Scan()
    for name in names:
        scan.add_repository(name, workflow_loaders(get, owner, name))
    scan.violations.extend(scan.stale_allowlist())
    return scan


def report(scan):
    if scan.subjects == 0:
        raise CouldNotRun(f"no release subjects among {scan.files} workflow files; nothing was checked")
    for repo, name, reason in scan.violations:
        print(f"{repo}/{name}: {reason}")
    print(f"checked {scan.repositories} repositories, {scan.files} workflow files, "
          f"{scan.subjects} release subjects, {len(scan.violations)} violations")
    return 1 if scan.violations else 0


QUEUED = "concurrency:\n  group: publish\n  queue: max\n"
PER_SHA = "concurrency:\n  group: publish-${{ github.sha }}\n"
JOB_QUEUED = "jobs:\n  release:\n    concurrency:\n      group: release\n      queue: max\n"
PLANTED = {
    "publish-cancel-true.yml": ("on: push\nconcurrency:\n  group: publish\n  cancel-in-progress: true\n  queue: max\n", "cancel"),
    "release-cancel-expression.yml": ("on: push\nconcurrency:\n  group: release\n  queue: max\n"
                                      "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n", "cancel"),
    "publish-job-cancel.yml": ("on: push\n" + QUEUED + "jobs:\n  release:\n    concurrency:\n"
                               "      group: release\n      cancel-in-progress: true\n", "cancel"),
    "deploy-cancel.yml": ("on: push\nconcurrency:\n  group: deploy\n  cancel-in-progress: true\n", "cancel"),
    "publish-no-concurrency.yml": ("on: push\njobs: {}\n", "queue"),
    "release-group-only.yml": ("on: push\nconcurrency: release\n", "queue"),
    "publish-single-slot.yml": ("on: push\nconcurrency:\n  group: publish\n  cancel-in-progress: false\n", "queue"),
    "publish-shared-group-job-queue.yml": ("on: push\nconcurrency:\n  group: publish\n" + JOB_QUEUED, "queue"),
    "release-per-sha-no-job-queue.yml": ("on: push\n" + PER_SHA + "jobs: {}\n", "queue"),
}
CLEAN = {
    "publish-queued.yml": "on: push\n" + QUEUED,
    "deploy-latest-wins.yml": "on: push\nconcurrency:\n  group: deploy\n  cancel-in-progress: false\n",
    "release-reusable.yml": "on: workflow_call\nconcurrency:\n  group: release\n  cancel-in-progress: true\n",
    "verify-release-notes.yml": "on: pull_request\nconcurrency:\n  group: notes\n  cancel-in-progress: true\n",
    "publish-documentation.yml": "on: push\njobs: {}\n",
    "publish-per-sha-job-queue.yml": "on: push\n" + PER_SHA + JOB_QUEUED,
}


def kind(reason):
    return "cancel" if reason.startswith("cancel-in-progress") else "queue"


def self_test_planted():
    with tempfile.TemporaryDirectory() as directory:
        for name, text in [*((name, text) for name, (text, _) in PLANTED.items()), *CLEAN.items()]:
            (Path(directory) / name).write_text(text, encoding="utf-8")
        scan = scan_directory(directory, "SelfTest")
    expected = {(name, expected_kind) for name, (_, expected_kind) in PLANTED.items()}
    found = {(name, kind(reason)) for _, name, reason in scan.violations}
    return [f"planted {name} ({wanted}) was not found" for name, wanted in sorted(expected - found)] + \
           [f"clean {name} was reported ({got})" for name, got in sorted(found - expected)]


def self_test_allowlist():
    repo, name, _ = ALLOWLIST[0]
    listing = [{"name": name, "path": f".github/workflows/{name}", "type": "file"}]

    def fake(text):
        responses = {
            f"orgs/{OWNER}/repos?type=all&per_page=100&page=1": [{"name": repo}],
            f"repos/{OWNER}/{repo}/contents/.github/workflows": listing,
            f"repos/{OWNER}/{repo}/contents/.github/workflows/{name}":
                {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()},
        }
        return responses.__getitem__

    def violations(text):
        # Other allowlist entries are absent from the fake organization and correctly reported as stale.
        return [violation for violation in scan_organization(fake(text)).violations if violation[:2] == (repo, name)]

    still_needed = violations("on: push\njobs: {}\n")
    no_longer_needed = violations("on: push\n" + QUEUED)
    problems = [] if not still_needed else [f"allowlisted {repo}/{name} was reported: {still_needed}"]
    if not any("allowlist entry no longer needed" in reason for _, _, reason in no_longer_needed):
        problems.append(f"stale allowlist entry {repo}/{name} was not reported")
    return problems


def self_test():
    problems = self_test_planted() + self_test_allowlist()
    for problem in problems:
        print(f"self-test: {problem}")
    if problems:
        return 1
    print(f"self-test: found all {len(PLANTED)} planted violations and the stale allowlist entry, "
          f"{len(CLEAN)} clean files passed")
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dir", help="scan the *.yml/*.yaml files in this workflows directory")
    mode.add_argument("--org", action="store_true", help=f"scan every non-archived {OWNER} repository (needs GH_TOKEN)")
    mode.add_argument("--self-test", action="store_true", help="plant violations and fail unless every one is found")
    parser.add_argument("--repo", help="repository name reported for --dir (default: the directory name)")
    arguments = parser.parse_args(argv)
    if arguments.repo and not arguments.dir:
        parser.error("--repo applies only to --dir")
    return arguments


def main(argv=None):
    arguments = parse_arguments(argv)
    if yaml is None:
        print("could not run: PyYAML is not installed", file=sys.stderr)
        return 2
    try:
        if arguments.self_test:
            return self_test()
        if arguments.org:
            return report(scan_organization())
        return report(scan_directory(arguments.dir, arguments.repo or Path(arguments.dir).resolve().name))
    except (CouldNotRun, NotFound) as error:
        print(f"could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
