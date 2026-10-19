#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Fail on Docker cleanup that is not scoped to a run: an unscoped prune or a bulk removal.

Each self-hosted job has its own Docker daemon, but an unscoped prune or `rm -f $(ps -aq)` inside a
job still removes containers that parallel steps or Testcontainers in the same job depend on
(Cratis/StudioIssues#905: containers vanished mid-run and specs failed with `No such container`).
A cleanup must carry `--filter label=...` so it only touches what the run created.

The scan is textual (by logical line, after joining `\\` continuations), not a YAML or shell parse.

Exit codes: 0 ran clean, 1 found violations, 2 could not run.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

OWNER = "Cratis"
SCRIPT_SUFFIXES = (".sh", ".bash", ".ps1", ".py", ".mjs", ".js", ".ts", ".cs")
SLASH_COMMENT_SUFFIXES = (".mjs", ".js", ".ts", ".cs")
SPECIAL_NAMES = ("makefile", "justfile")
SKIPPED_DIRECTORIES = {"node_modules", "bin", "obj", "dist", ".git", "vendor", "vendored", "third_party"}
# Relative directories (from the scan root) that hold planted violations on purpose.
SKIPPED_PATHS = (".github/scripts/tests/fixtures",)

# (repository, path, reason). The sweep fails an entry once the file no longer has a violation.
# Empty: the 2026-10-18 search of every Cratis repository found no unscoped cleanup.
ALLOWLIST = ()

D = r"\bdocker\s+"
PRUNE = re.compile(D + r"(?:system|container|image|volume|network|builder|buildx)\s+prune\b", re.IGNORECASE)
PRUNE_FILTER = re.compile(r"--filter(?:\s+|=)[\"']?label=", re.IGNORECASE)
REMOVE = r"(?:(?:container|volume|network|image)\s+)?(?:rm|kill|stop|rmi)\b"
LISTING = r"docker\s+(?:\w+\s+)*?(?:ls|ps|images)\b"
SUBSTITUTION = re.compile(D + REMOVE + r"[^$`]*(?:\$\(|`)\s*(" + LISTING + r"[^)`]*)", re.IGNORECASE)
XARGS = re.compile(r"(" + LISTING + r"[^|]*)\|\s*xargs\b[^|]*" + D + REMOVE, re.IGNORECASE)
LISTING_FILTER = re.compile(r"(?:--filter|-f)(?:\s+|=)[\"']?label=", re.IGNORECASE)
SEPARATORS = re.compile(r"&&|\|\||;")

PRUNE_REASON = "unscoped docker prune; add --filter label=... for the run's own resources"
BULK_REASON = "bulk docker removal of an unscoped listing; add --filter label=... to the listing"


class CouldNotRun(Exception):
    pass


def is_subject(relative):
    """Whether a path (posix, relative to the scan root) is a file this checker reads."""
    parts = relative.split("/")
    name = parts[-1].lower()
    if relative.startswith(".github/workflows/") and len(parts) == 3:
        return name.endswith((".yml", ".yaml"))
    if relative.startswith(".github/actions/"):
        return name in ("action.yml", "action.yaml")
    return name.endswith(SCRIPT_SUFFIXES) or name.startswith("dockerfile") or name in SPECIAL_NAMES


def uses_slash_comments(relative):
    return relative.lower().endswith(SLASH_COMMENT_SUFFIXES)


def strip_comment(line, slash):
    marker = re.compile(r"(?:^|\s)//" if slash else r"(?:^|\s)#")
    found = marker.search(line)
    return line[:found.start()] if found else line


def logical_lines(text, slash):
    """Yield (line number, text) with `\\` continuations joined and comments removed."""
    pending, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw, slash).rstrip()
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        yield start, pending + line
        pending = ""
    if pending:
        yield start, pending


def line_violations(line):
    reasons = []
    for segment in SEPARATORS.split(line):
        for found in PRUNE.finditer(segment):
            if not PRUNE_FILTER.search(segment, found.end()):
                reasons.append(PRUNE_REASON)
        for pattern in (SUBSTITUTION, XARGS):
            for found in pattern.finditer(segment):
                if not LISTING_FILTER.search(found.group(1)):
                    reasons.append(BULK_REASON)
    return reasons


def scan_text(relative, text):
    return [(number, reason) for number, line in logical_lines(text, uses_slash_comments(relative))
            for reason in line_violations(line)]


def subject_files(root, excluded=()):
    root = Path(root)
    skipped = tuple(excluded) + SKIPPED_PATHS
    for directory, names, files in os.walk(root):
        relative_directory = Path(directory).relative_to(root).as_posix()
        prefix = "" if relative_directory == "." else relative_directory + "/"
        names[:] = sorted(name for name in names
                          if name not in SKIPPED_DIRECTORIES
                          and (prefix + name) not in {path.strip("/") for path in skipped})
        for name in sorted(files):
            relative = prefix + name
            if is_subject(relative):
                yield relative, Path(directory) / name


class Scan:
    """Accumulates counts and violations across repositories."""

    def __init__(self):
        self.repositories = 0
        self.files = 0
        self.violations = []  # (repo, path, line, reason)
        self.allowlist_used = set()

    def add_directory(self, root, repo, excluded=()):
        if not Path(root).is_dir():
            raise CouldNotRun(f"{root}: not a directory")
        self.repositories += 1
        allowed = {(entry_repo, path) for entry_repo, path, _ in ALLOWLIST}
        for relative, path in subject_files(root, excluded):
            self.files += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                raise CouldNotRun(f"{repo}:{relative}: {error}") from error
            found = scan_text(relative, text)
            if found and (repo, relative) in allowed:
                self.allowlist_used.add((repo, relative))
                continue
            self.violations.extend((repo, relative, number, reason) for number, reason in found)

    def stale_allowlist(self):
        return [(repo, path, 0, f"allowlist entry no longer needed ({reason})")
                for repo, path, reason in ALLOWLIST if (repo, path) not in self.allowlist_used]


def run_command(arguments, environment=None, timeout=300):
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(environment or {})})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CouldNotRun(f"{arguments[0]} {arguments[1] if len(arguments) > 1 else ''}: {error}") from error


def list_repositories(owner=OWNER):
    result = run_command(["gh", "repo", "list", owner, "--no-archived", "--limit", "1000", "--json", "name"], timeout=120)
    if result.returncode:
        raise CouldNotRun(f"gh repo list {owner} failed: {result.stderr.strip()[:300]}")
    try:
        names = sorted(item["name"] for item in json.loads(result.stdout))
    except (ValueError, KeyError, TypeError) as error:
        raise CouldNotRun(f"gh repo list {owner}: invalid response") from error
    if not names:
        raise CouldNotRun(f"no non-archived repositories found in {owner}")
    return names


def access_token():
    token = os.environ.get("GH_TOKEN")
    if token:
        return token
    result = run_command(["gh", "auth", "token"], timeout=30)
    if result.returncode or not result.stdout.strip():
        raise CouldNotRun("no GH_TOKEN and `gh auth token` failed")
    return result.stdout.strip()


def clone(owner, repo, destination, token):
    import base64
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # The token travels in the environment, never in the command line or the clone URL.
    environment = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": header,
                   "GIT_TERMINAL_PROMPT": "0"}
    result = run_command(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:limit=1m",
                          f"https://github.com/{owner}/{repo}.git", str(destination)], environment)
    if result.returncode:
        raise CouldNotRun(f"{repo}: could not clone: {result.stderr.replace(token, '***').strip()[:300]}")


def scan_organization(owner=OWNER):
    names = list_repositories(owner)
    token = access_token()
    scan = Scan()
    with tempfile.TemporaryDirectory(prefix="docker-cleanup-") as workspace:
        for name in names:
            destination = Path(workspace) / name
            clone(owner, name, destination, token)
            scan.add_directory(destination, name)
    scan.violations.extend(scan.stale_allowlist())
    return scan


def report(scan):
    if scan.repositories == 0 or scan.files == 0:
        raise CouldNotRun(f"scanned {scan.files} files in {scan.repositories} repositories; nothing was checked")
    for repo, path, number, reason in scan.violations:
        print(f"{repo}:{path}:{number}: {reason}")
    print(f"Scanned {scan.files} files in {scan.repositories} repositories, found {len(scan.violations)} violations")
    return 1 if scan.violations else 0


def docker(text):
    return f"docker {text}"


PLANTED = {
    "prune-system.sh": ("prune", docker("system prune -af\n")),
    "prune-kinds.sh": ("prune", docker("image prune -f\n") + docker("volume prune -f\n")),
    "prune-buildx.sh": ("prune", "sudo  " + docker("buildx   prune --all --force\n")),
    "prune-neq-filter.sh": ("prune", docker("container prune -f --filter \"label!=keep\"\n")),
    "prune-until-only.ps1": ("prune", docker("system prune --filter until=24h\n")),
    "prune-continued.sh": ("prune", docker("system prune \\\n  --all\n")),
    "prune-uppercase.sh": ("prune", docker("system prune\n").upper()),
    "rm-substitution.sh": ("bulk", docker("rm -f $(docker ps -aq)\n")),
    "kill-backticks.py": ("bulk", "run(\"" + docker("kill `docker ps -q`") + "\")\n"),
    "volume-rm.sh": ("bulk", docker("volume rm $(docker volume ls -q)\n")),
    "xargs.sh": ("bulk", docker("ps -aq | xargs -r docker rm -f\n")),
    "rmi.mjs": ("bulk", "exec(`" + docker("rmi $(docker images -q)") + "`)\n"),
}
CLEAN = {
    "scoped-prune.sh": docker("system prune -f --filter \"label=run=$RUN\"\n"),
    "scoped-prune-equals.sh": docker("image prune --filter=label=run=$RUN\n"),
    "scoped-rm.sh": docker("rm -f $(docker ps -aq --filter label=run=$RUN)\n"),
    "scoped-xargs.sh": docker("ps -aq --filter \"label=run=$RUN\" | xargs docker rm -f\n"),
    "scoped-continued.sh": docker("rm -f $(docker ps -aq \\\n  --filter label=run=$RUN)\n"),
    "comment.sh": "# " + docker("system prune -af\n") + "echo ok # " + docker("rm -f $(docker ps -aq)\n"),
    "comment.cs": "// " + docker("system prune -af\n"),
    "named-rm.sh": docker("rm -f my-container\n"),
    "unrelated.txt": docker("system prune -af\n"),
}


def self_test():
    problems = []
    with tempfile.TemporaryDirectory() as directory:
        for name, (_, text) in PLANTED.items():
            (Path(directory) / name).write_text(text, encoding="utf-8")
        for name, text in CLEAN.items():
            (Path(directory) / name).write_text(text, encoding="utf-8")
        scan = Scan()
        scan.add_directory(directory, "SelfTest")
    found = {}
    for _, path, _, reason in scan.violations:
        found.setdefault(path, []).append("prune" if reason == PRUNE_REASON else "bulk")
    for name, (kind, _) in sorted(PLANTED.items()):
        expected = 2 if name == "prune-kinds.sh" else 1
        if found.get(name, []).count(kind) != expected:
            problems.append(f"planted {name} ({kind}) was not found")
    for name in sorted(set(found) - set(PLANTED)):
        problems.append(f"clean {name} was reported")
    for problem in problems:
        print(f"self-test: {problem}")
    if problems:
        return 1
    print(f"self-test: found all {len(PLANTED)} planted violations, {len(CLEAN)} scoped or commented files passed")
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--org", action="store_true", help=f"scan every non-archived {OWNER} repository (needs GH_TOKEN)")
    mode.add_argument("--self-test", action="store_true", help="plant violations and fail unless every one is found")
    parser.add_argument("--root", default=".", help="directory to scan in local mode (default: .)")
    parser.add_argument("--repo", help="repository name reported in local mode (default: the root directory name)")
    parser.add_argument("--exclude", action="append", default=[], help="directory relative to --root to skip (repeatable)")
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)
    try:
        if arguments.self_test:
            return self_test()
        if arguments.org:
            return report(scan_organization())
        scan = Scan()
        scan.add_directory(arguments.root, arguments.repo or Path(arguments.root).resolve().name, arguments.exclude)
        return report(scan)
    except CouldNotRun as error:
        print(f"could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
