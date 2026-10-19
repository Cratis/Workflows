#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Fail when a public repository's pull_request job resolves to a self-hosted runner.

Any fork can open a pull request, so a public repository's pull_request and pull_request_target jobs
must never run on our self-hosted runners (`self-hosted`, `cratis-arc*`). The checker resolves the
effective runner of every job reachable from those triggers: literal `runs-on`, `vars.X || 'label'` and
`inputs.X || 'label'` fallbacks, `matrix.X` lists, and reusable workflows with the caller's `with:`
inputs and the callee's input defaults. An expression it cannot resolve is a finding: unknown is not
pass. Private repositories run on self-hosted runners by design and are never reported.

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
PULL_REQUEST_TRIGGERS = {"pull_request", "pull_request_target"}
MAX_DEPTH = 8
USES_PATTERN = re.compile(r"^([^/@\s]+)/([^/@\s]+)/(\.github/workflows/[^@\s]+)@(\S+)$")
SINGLE_EXPRESSION = re.compile(r"^\s*\$\{\{(.*)\}\}\s*$", re.DOTALL)
LITERAL = re.compile(r"^'((?:[^']|'')*)'$")
NAMED = re.compile(r"^(vars|inputs|matrix)(?:\.([A-Za-z_][\w-]*)|\['([^']+)'\]|\[\"([^\"]+)\"\])$")


class CouldNotRun(Exception):
    pass


class NotFound(Exception):
    pass


class Unresolved(Exception):
    pass


def is_self_hosted(label):
    lower = label.strip().lower()
    return lower == "self-hosted" or lower.startswith("cratis-arc")


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


# ---- expression resolution -------------------------------------------------------------------------


def split_alternatives(expression):
    """Split on top-level `||`; return None when the expression uses anything more than that."""
    parts, current, quoted, index = [], "", False, 0
    while index < len(expression):
        char = expression[index]
        if char == "'":
            quoted = not quoted
        if not quoted and expression.startswith("||", index):
            parts.append(current.strip())
            current, index = "", index + 2
            continue
        current += char
        index += 1
    parts.append(current.strip())
    return None if quoted else parts


class Context:
    """The names an expression can read: repository variables, workflow inputs and the job's matrix."""

    def __init__(self, variables=None, inputs=None, matrix=None):
        self.variables = {key.lower(): value for key, value in (variables or {}).items()}
        self.inputs = inputs or {}
        self.matrix = matrix if matrix is not None else {}

    def lookup(self, scope, name):
        """Return the value(s) of a name as a list of strings; empty means unset. May raise Unresolved."""
        if scope == "vars":
            value = self.variables.get(name.lower(), "")
            return [str(value)] if str(value) else []
        source = self.inputs if scope == "inputs" else self.matrix
        if scope == "inputs":
            value = next((item for key, item in source.items() if key.lower() == name.lower()), "")
        else:
            value = source.get(name)
            if value is None:
                raise Unresolved(f"matrix.{name} is not defined in strategy.matrix")
        if isinstance(value, Unresolved):
            raise value
        values = value if isinstance(value, list) else [value]
        return [text for text in (str(item) for item in values) if text]


def resolve_operand(operand, context):
    literal = LITERAL.match(operand)
    if literal:
        text = literal.group(1).replace("''", "'")
        return [text] if text else []
    named = NAMED.match(operand)
    if not named:
        raise Unresolved(f"expression '{operand}' cannot be resolved offline")
    return context.lookup(named.group(1), named.group(2) or named.group(3) or named.group(4))


def resolve_text(value, context):
    """Resolve one scalar to its possible labels; raise Unresolved for anything unknown."""
    if isinstance(value, (bool, int, float)):
        return [str(value)]
    if not isinstance(value, str):
        raise Unresolved(f"unsupported value {value!r}")
    if "${{" not in value:
        return [value] if value.strip() else []
    expression = SINGLE_EXPRESSION.match(value)
    if not expression or "${{" in expression.group(1):
        raise Unresolved(f"'{value.strip()}' mixes text and expressions")
    alternatives = split_alternatives(expression.group(1))
    if alternatives is None:
        raise Unresolved(f"'{value.strip()}' is not a well-formed expression")
    for operand in alternatives:
        values = resolve_operand(operand, context)
        if values:
            return values
    return []


class UnknownMatrix(dict):
    """A matrix that is itself an expression: no key can be resolved."""

    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    def get(self, key, default=None):
        return Unresolved(f"matrix.{key} is unknown: {self.reason}")


def flatten_labels(value, context):
    """Resolve a matrix value to flat labels; a mapping or an unresolvable expression raises Unresolved."""
    if isinstance(value, list):
        return [label for item in value for label in flatten_labels(item, context)]
    if isinstance(value, dict):
        raise Unresolved("a matrix value is a mapping, not a label")
    labels = resolve_text(value, context)
    if not labels:
        raise Unresolved(f"matrix value {value!r} resolves to no label")
    return labels


def matrix_values(job, context):
    """Map each matrix key of a job to its resolved labels (or an Unresolved), including `include` entries."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if matrix is None:
        return {}
    if not isinstance(matrix, dict):
        return UnknownMatrix("strategy.matrix is an expression")
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list) or \
            not all(isinstance(entry, dict) for entry in include):
        return UnknownMatrix("matrix include or exclude is an expression")
    entries = [{key: item} for key, item in matrix.items() if key not in ("include", "exclude")]
    entries += [{key: item} for entry in include for key, item in entry.items()]
    values = {}
    for entry in entries:
        for key, item in entry.items():
            try:
                resolved = flatten_labels(item, context)
            except Unresolved as error:
                values[key] = error
                continue
            if not isinstance(values.get(key), Unresolved):
                values.setdefault(key, [])
                values[key] += [label for label in resolved if label not in values[key]]
    return values


def runner_problems(runs_on, context):
    """Return the reasons a `runs-on` value selects a self-hosted runner or cannot be resolved."""
    problems = []
    try:
        if isinstance(runs_on, dict):
            if "group" in runs_on:
                problems.append(f"runner group '{runs_on['group']}' selects organization-managed runners")
            runs_on = runs_on.get("labels", [])
        items = runs_on if isinstance(runs_on, list) else [runs_on]
        labels = [label for item in items for label in resolve_text(item, context)]
    except Unresolved as error:
        return problems + [f"unresolved runner: {error}"]
    if not labels and not problems:
        return ["unresolved runner: runs-on resolves to no label"]
    return problems + [f"runs on self-hosted label '{label}'" for label in labels if is_self_hosted(label)]


# ---- scanning ---------------------------------------------------------------------------------------


class Source:
    """Where workflow files come from: the scanned repository, mapped local checkouts, or the API."""

    def __init__(self, loaders, local_repos=None, fetch_remote=None):
        self.loaders = loaders
        self.local_repos = local_repos or {}
        self.fetch_remote = fetch_remote
        self.cache = {}

    def load(self, uses):
        """Return (label, document) for a `uses:` reference, or None when it is not followed."""
        if uses.startswith("./.github/workflows/"):
            name = uses.rsplit("/", 1)[1]
            if name not in self.loaders:
                raise Unresolved(f"local reusable workflow {uses} was not found")
            return name, self.parsed(("local", name), uses, self.loaders[name])
        match = USES_PATTERN.match(uses)
        if not match:
            raise Unresolved(f"reusable workflow reference '{uses}' is not understood")
        owner, repo, path, ref = match.groups()
        if owner.lower() != OWNER.lower():
            return None  # An external repository's workflow is outside our runner labels.
        if repo in self.local_repos:
            file = Path(self.local_repos[repo]) / path.rsplit("/", 1)[1]
            return uses, self.parsed(("mapped", repo, path), uses, lambda: read_text(file))
        if self.fetch_remote is None:
            raise Unresolved(f"{uses} is not available offline; map it with --local-repo {repo}=DIRECTORY")
        return uses, self.parsed(("remote", repo, path, ref), uses, lambda: self.fetch_remote(repo, path, ref))

    def parsed(self, key, uses, loader):
        if key not in self.cache:
            try:
                self.cache[key] = parse(OWNER, uses, loader())
            except NotFound as error:
                raise Unresolved(f"{uses} was not found") from error
        return self.cache[key]


class Scan:
    """Accumulates counts and findings across repositories."""

    def __init__(self):
        self.repositories = 0
        self.private = 0
        self.files = 0
        self.subjects = 0
        self.external = 0
        self.findings = []

    def add_repository(self, repo, public, loaders, variables=None, local_repos=None, fetch_remote=None):
        self.repositories += 1
        if not public:
            self.private += 1
            return
        source = Source(loaders, local_repos, fetch_remote)
        for name in sorted(loaders):
            self.files += 1
            document = parse(repo, name, loaders[name]())
            if triggers(document) & PULL_REQUEST_TRIGGERS:
                self.check_workflow(repo, name, document, Context(variables), source, [name])

    def check_workflow(self, repo, name, document, context, source, chain):
        jobs = document.get("jobs")
        for job_id, job in (jobs.items() if isinstance(jobs, dict) else []):
            if isinstance(job, dict):
                self.check_job(repo, name, job_id, job, context, source, chain)

    def report(self, repo, name, chain, job_id, problem):
        self.findings.append((repo, name, f"{' > '.join([*chain[1:], f'job {job_id}'])}: {problem}"))

    def check_job(self, repo, name, job_id, job, context, source, chain):
        matrix = matrix_values(job, context)
        job_context = Context(context.variables, context.inputs, matrix)
        if "uses" in job:
            self.follow_reusable(repo, name, job_id, job, job_context, source, chain)
        elif "runs-on" in job:
            self.subjects += 1
            for problem in runner_problems(job["runs-on"], job_context):
                self.report(repo, name, chain, job_id, problem)
        else:
            self.subjects += 1
            self.report(repo, name, chain, job_id, "unresolved runner: job has neither runs-on nor uses")

    def follow_reusable(self, repo, name, job_id, job, context, source, chain):
        try:
            if len(chain) > MAX_DEPTH:
                raise Unresolved("reusable workflows are nested too deeply")
            loaded = source.load(str(job["uses"]))
            if loaded is None:
                self.external += 1
                return
            label, callee = loaded
            if label in chain:
                raise Unresolved(f"reusable workflow {label} calls itself")
            inputs = callee_inputs(callee, job.get("with"), context)
        except Unresolved as error:
            self.subjects += 1
            self.report(repo, name, chain, job_id, f"unresolved runner: {error}")
            return
        callee_context = Context(context.variables, inputs)
        self.check_workflow(repo, name, callee, callee_context, source, [*chain, f"job {job_id}", label])


def callee_inputs(callee, passed, context):
    """Build the callee's `inputs` context: values the caller passes, else the callee's defaults."""
    on = callee.get(True, callee.get("on"))
    declared = (on.get("workflow_call") or {}).get("inputs") if isinstance(on, dict) else None
    inputs = {}
    for key, spec in (declared or {}).items():
        default = spec.get("default", "") if isinstance(spec, dict) else ""
        inputs[key] = safe_resolve(default, Context())
    for key, value in (passed or {}).items():
        inputs[key] = safe_resolve(value, context)
    return inputs


def safe_resolve(value, context):
    """Resolve a value now, keeping an Unresolved result so only an actual use of it fails."""
    try:
        return resolve_text(value, context)
    except Unresolved as error:
        return error


# ---- sources: directory, organization ------------------------------------------------------------


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CouldNotRun(f"{path}: {error}") from error


def directory_loaders(path):
    directory = Path(path)
    if not directory.is_dir():
        raise CouldNotRun(f"{path}: not a directory")
    workflows = sorted(item for item in directory.iterdir()
                       if item.is_file() and item.name.lower().endswith(WORKFLOW_SUFFIXES))
    if not workflows:
        raise CouldNotRun(f"{path}: no workflow files found")
    return {item.name: (lambda item=item: read_text(item)) for item in workflows}


def scan_directory(path, repo, public=True, variables=None, local_repos=None, fetch_remote=None):
    scan = Scan()
    scan.add_repository(repo, public, directory_loaders(path), variables, local_repos, fetch_remote)
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
            # An unknown visibility is treated as public: the strict reading never lets a repository pass unseen.
            return sorted((repo["name"], not repo.get("private") and repo.get("visibility", "public") == "public")
                          for repo in repositories if not repo.get("archived"))
    raise CouldNotRun(f"orgs/{owner}/repos: pagination limit exceeded")


def fetch_text(get, owner, repo, path, ref=None):
    endpoint = f"repos/{owner}/{repo}/contents/{path}" + (f"?ref={ref}" if ref else "")
    try:
        content = get(endpoint)
    except NotFound as error:
        raise NotFound(f"{repo}/{path}") from error
    if not isinstance(content, dict) or content.get("encoding") != "base64":
        raise CouldNotRun(f"{repo}/{path}: unexpected contents response")
    try:
        return base64.b64decode(content["content"]).decode("utf-8")
    except (KeyError, ValueError) as error:
        raise CouldNotRun(f"{repo}/{path}: could not decode contents") from error


def workflow_loaders(get, owner, repo):
    try:
        entries = get(f"repos/{owner}/{repo}/contents/.github/workflows")
    except NotFound:
        return {}
    if not isinstance(entries, list):
        raise CouldNotRun(f"{repo}/.github/workflows: not a directory listing")
    return {entry["name"]: (lambda path=entry["path"]: fetch_text(get, owner, repo, path))
            for entry in entries
            if entry.get("type") == "file" and entry.get("name", "").lower().endswith(WORKFLOW_SUFFIXES)}


def repository_variables(get, owner, repo):
    """Repository and organization variables; a missing endpoint (404) yields what was read, any other error stops the run."""
    variables = {}
    for endpoint in (f"repos/{owner}/{repo}/actions/organization-variables", f"repos/{owner}/{repo}/actions/variables"):
        for page in range(1, 11):
            try:
                response = get(f"{endpoint}?per_page=30&page={page}")
            except NotFound:
                break
            batch = response.get("variables", []) if isinstance(response, dict) else []
            variables.update({item["name"]: item["value"] for item in batch})
            if len(batch) < 30:
                break
    return variables


def scan_organization(get=gh_api, owner=OWNER, local_repos=None):
    try:
        repositories = list_repositories(get, owner)
    except NotFound as error:
        raise CouldNotRun(f"organization {owner} not found or not visible to the token") from error
    if not repositories:
        raise CouldNotRun(f"no non-archived repositories found in {owner}")
    scan = Scan()

    def fetch_remote(repo, path, ref):
        return fetch_text(get, owner, repo, path, ref)

    for name, public in repositories:
        loaders = workflow_loaders(get, owner, name) if public else {}
        variables = repository_variables(get, owner, name) if public else {}
        scan.add_repository(name, public, loaders, variables, local_repos, fetch_remote)
    return scan


def report(scan, require_subjects=False):
    if scan.files == 0 and scan.private != scan.repositories:
        raise CouldNotRun("no workflow files in the public repositories; nothing was checked")
    if require_subjects and scan.subjects == 0:
        raise CouldNotRun(f"no pull_request jobs among {scan.files} workflow files; nothing was checked")
    for repo, name, reason in scan.findings:
        print(f"{repo}/{name}: {reason}")
    print(f"checked {scan.repositories} repositories ({scan.private} private skipped), {scan.files} workflow files, "
          f"{scan.subjects} pull_request jobs, {scan.external} external reusable workflows not followed, "
          f"{len(scan.findings)} violations")
    return 1 if scan.findings else 0


# ---- self-test --------------------------------------------------------------------------------------

CALLER = "on: pull_request\njobs:\n  gate:\n    uses: ./.github/workflows/callee.yml\n"
CALLEE_DEFAULT = ("on:\n  workflow_call:\n    inputs:\n      runs-on:\n        type: string\n"
                  "        default: {default}\njobs:\n  verify:\n    runs-on: ${{{{ inputs.runs-on }}}}\n")
PLANTED = {
    "literal.yml": ("on: pull_request\njobs:\n  build:\n    runs-on: cratis-arc\n", "self-hosted"),
    "list.yml": ("on: [pull_request]\njobs:\n  build:\n    runs-on: [self-hosted, linux]\n", "self-hosted"),
    "group.yml": ("on: pull_request\njobs:\n  build:\n    runs-on:\n      group: ci\n", "group"),
    "vars-fallback.yml": ("on: pull_request\njobs:\n  build:\n    runs-on: ${{ vars.RUNNER_GATE || 'cratis-arc-small' }}\n",
                          "self-hosted"),
    "inputs-fallback.yml": ("on: pull_request\njobs:\n  build:\n    runs-on: ${{ inputs.runner || 'cratis-arc' }}\n",
                            "self-hosted"),
    "reusable-default.yml": (CALLER, "self-hosted"),
    "callee.yml": (CALLEE_DEFAULT.format(default="cratis-arc"), None),
    "pull-request-target.yml": ("on: pull_request_target\njobs:\n  build:\n    runs-on: cratis-arc\n", "self-hosted"),
    "matrix-nested.yml": ("on: pull_request\njobs:\n  build:\n    strategy:\n      matrix:\n"
                          "        r: [[self-hosted, linux]]\n    runs-on: ${{ matrix.r }}\n", "self-hosted"),
    "unresolved.yml": ("on: pull_request\njobs:\n  build:\n    runs-on: ${{ github.event.repository.name }}\n",
                       "unresolved"),
}
HOSTED_CALLEE = "reusable-hosted-callee.yml"
PLANTED_OVERRIDE = (
    "reusable-override.yml",
    "on: pull_request\njobs:\n  gate:\n    uses: ./.github/workflows/" + HOSTED_CALLEE + "\n    with:\n"
    "      runs-on: ${{ vars.RUNNER_GATE || 'cratis-arc' }}\n",
)
CLEAN = {
    "hosted.yml": "on: pull_request\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
    "hosted-fallback.yml": "on: pull_request\njobs:\n  build:\n    runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}\n",
    "push-only.yml": "on: push\njobs:\n  build:\n    runs-on: cratis-arc\n",
    "reusable-hosted.yml": "on: pull_request\njobs:\n  gate:\n    uses: ./.github/workflows/" + HOSTED_CALLEE
    + "\n    with:\n      runs-on: ubuntu-latest\n",
}
SHARED = {HOSTED_CALLEE: CALLEE_DEFAULT.format(default="ubuntu-latest")}


def self_test_planted():
    """Return problems: a planted violation not found, or a clean file reported."""
    files = {**{name: text for name, (text, _) in PLANTED.items()}, **SHARED,
             PLANTED_OVERRIDE[0]: PLANTED_OVERRIDE[1], **CLEAN}
    expected = {(name, wanted) for name, (_, wanted) in PLANTED.items() if wanted}
    expected.add((PLANTED_OVERRIDE[0], "self-hosted"))
    with tempfile.TemporaryDirectory() as directory:
        for name, text in files.items():
            (Path(directory) / name).write_text(text, encoding="utf-8")
        scan = scan_directory(directory, "SelfTest")
        private = scan_directory(directory, "SelfTest", public=False)
    found = {(name, "unresolved" if "unresolved runner" in reason else "group" if "runner group" in reason
              else "self-hosted") for _, name, reason in scan.findings}
    problems = [f"planted {name} ({wanted}) was not found" for name, wanted in sorted(expected - found)]
    problems += [f"clean {name} was reported ({got})" for name, got in sorted(found - expected)]
    if private.findings or private.subjects:
        problems.append("a private repository was checked")
    return problems


def self_test():
    problems = self_test_planted()
    for problem in problems:
        print(f"self-test: {problem}")
    if problems:
        return 1
    planted = sum(1 for _, wanted in PLANTED.values() if wanted) + 1
    print(f"self-test: found all {planted} planted violations, "
          f"{len(CLEAN)} clean files and a private repository passed")
    return 0


def parse_local_repos(entries):
    mapping = {}
    for entry in entries or []:
        name, separator, directory = entry.partition("=")
        if not separator or not name or not directory:
            raise CouldNotRun(f"--local-repo expects NAME=DIRECTORY, got '{entry}'")
        mapping[name] = directory
    return mapping


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dir", help="scan the *.yml/*.yaml files in this workflows directory")
    mode.add_argument("--org", action="store_true", help=f"scan every public non-archived {OWNER} repository (needs GH_TOKEN)")
    mode.add_argument("--self-test", action="store_true", help="plant violations and fail unless every one is found")
    parser.add_argument("--repo", help="repository name reported for --dir (default: the directory name)")
    parser.add_argument("--visibility", choices=("public", "private"), default="public",
                        help="visibility of the --dir repository; unknown is treated as public (default: public)")
    parser.add_argument("--local-repo", action="append", metavar="NAME=DIRECTORY",
                        help=f"resolve {OWNER}/NAME reusable workflows from DIRECTORY instead of the API (repeatable)")
    parser.add_argument("--fetch-remote", action="store_true",
                        help=f"resolve other {OWNER} reusable workflows through the API (needs GH_TOKEN)")
    arguments = parser.parse_args(argv)
    if arguments.repo and not arguments.dir:
        parser.error("--repo applies only to --dir")
    return arguments


def remote_fetcher():
    return lambda repo, path, ref: fetch_text(gh_api, OWNER, repo, path, ref)


def main(argv=None):
    arguments = parse_arguments(argv)
    if yaml is None:
        print("could not run: PyYAML is not installed", file=sys.stderr)
        return 2
    try:
        if arguments.self_test:
            return self_test()
        local_repos = parse_local_repos(arguments.local_repo)
        if arguments.org:
            return report(scan_organization(local_repos=local_repos), require_subjects=True)
        scan = scan_directory(arguments.dir, arguments.repo or Path(arguments.dir).resolve().name,
                              arguments.visibility == "public", None, local_repos,
                              remote_fetcher() if arguments.fetch_remote else None)
        return report(scan)
    except (CouldNotRun, NotFound) as error:
        print(f"could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
