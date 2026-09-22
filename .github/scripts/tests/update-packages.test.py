# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Execute the reusable workflow's actual inline Bash, offline in temporary Git repos."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / ".github/workflows/update-packages.yml").read_text()
STEPS = dict(re.findall(r"^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", SOURCE, re.M | re.S))
PREFLIGHT = "Check publication options and default-branch base"
HOOK = "Run post-update command"
FREEZE = "Freeze pull request candidate before builds"
PUBLISH = "Publish validated pull request"
LEGACY = "Commit and push changes"
UPDATES = ["Update NuGet packages", "Update NPM packages", "Update Gradle packages", "Update Mix packages"]
BUILDS = ["Build .NET", "Build NPM", "Build Gradle", "Build Mix"]
REAL_GIT = shutil.which("git")


def shell(name):
    block = STEPS[name]
    match = re.search(r"^        run: (.*)\n?", block, re.M)
    if match[1] != "|":
        return match[1] + "\n"
    lines = []
    for line in block[match.end():].splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


# Stubs are at process boundaries, not alternative publishing implementations.
# Every Git operation goes to a local bare repo; gh and toolchains cannot use network.
STUB = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "git":
    forbidden = {"rebase", "reset", "--amend", "--force", "--force-with-lease", "-f", "pull"}
    assert not forbidden.intersection(args), args
    if args[0] == "push":
        assert len(args) == 3 and args[1] == "origin", args
        assert args[2].endswith(":refs/heads/automation/package-update-123-1"), args
        assert not args[2].startswith("+"), args
        if os.environ.get("FAIL_PUSH") == "1":
            sys.exit(24)
    if args[0] == "ls-remote" and os.environ.get("FAIL_BRANCH_LOOKUP") == "1" and "--exit-code" not in args:
        sys.exit(17)
    status = subprocess.run([os.environ["REAL_GIT"], *args]).returncode
    if args[0] == "ls-remote" and os.environ.get("FAIL_AFTER_REF_OUTPUT") == "1":
        sys.exit(18)
    if not status and args[0] == "push":
        for option, ref in [("ADVANCE_AFTER_PUSH", "refs/heads/main"),
                            ("MUTATE_BRANCH_AFTER_PUSH", "refs/heads/automation/package-update-123-1")]:
            if os.environ.get(option):
                subprocess.run([os.environ["REAL_GIT"], "--git-dir", os.environ["REMOTE"],
                                "update-ref", ref, os.environ[option]], check=True)
    sys.exit(status)
if name == "gh":
    assert os.environ.get("GH_TOKEN") == "offline-placeholder"
    if args[0] == "api":
        query = "[.ref, .object.sha, .object.type] | @tsv"
        ref = "refs/heads/automation/package-update-123-1"
        base = os.environ["EXPECTED_BASE"] if "EXPECTED_BASE" in os.environ else ""
        remote_git = [os.environ["REAL_GIT"], "--git-dir", os.environ["REMOTE"]]
        if args[1:3] == ["--method", "POST"]:
            assert args == ["api", "--method", "POST", "repos/Cratis/Fixture/git/refs",
                            "-f", "ref=" + ref, "-f", "sha=" + base, "--jq", query], args
            if os.environ.get("FAIL_RESERVE") == "1":
                sys.exit(25)
            if os.environ.get("RACE_RESERVE") == "1":
                # A competing actor creates the same branch AFTER ls-remote but
                # BEFORE the API's atomic expected-absent create. Same base means
                # an ordinary push alone would fast-forward the competing branch.
                subprocess.run([*remote_git, "update-ref", ref, base, "0" * 40], check=True)
            created = subprocess.run([*remote_git, "update-ref", ref, base, "0" * 40])
            if created.returncode:
                print("HTTP 422: Reference already exists", file=sys.stderr)
                sys.exit(26)
            print("\t".join([os.environ.get("RESERVE_RESPONSE_REF", ref), base, "commit"]))
            if os.environ.get("FAIL_AFTER_RESERVE_OUTPUT") == "1":
                sys.exit(27)
        elif args[1] == "repos/Cratis/Fixture/git/ref/heads/automation/package-update-123-1":
            assert args == ["api", args[1], "--jq", query], args
            if os.environ.get("FAIL_RESERVATION_READBACK") == "1":
                sys.exit(28)
            sha = subprocess.check_output([*remote_git, "rev-parse", ref], text=True).strip()
            print("\t".join([ref, os.environ.get("RESERVATION_READBACK_SHA", sha), "commit"]))
            if os.environ.get("FAIL_AFTER_RESERVATION_READBACK_OUTPUT") == "1":
                sys.exit(29)
        else:
            assert args == ["api", "repos/Cratis/Fixture", "--jq", ".default_branch"], args
            if os.environ.get("FAIL_API") == "1":
                sys.exit(19)
            print(os.environ.get("REMOTE_DEFAULT", "main"))
            if os.environ.get("FAIL_AFTER_API_OUTPUT") == "1":
                sys.exit(20)
    elif args[:2] == ["pr", "create"]:
        assert args[args.index("--head") + 1] == "automation/package-update-123-1", args
        assert args[args.index("--base") + 1] == "main", args
        assert args[args.index("--repo") + 1] == "Cratis/Fixture", args
        assert not {"--draft", "--fill", "--recover"}.intersection(args), args
        if os.environ.get("FAIL_CREATE") == "1":
            sys.exit(21)
        Path(os.environ["PR_CREATED"]).write_text(json.dumps(args))
        if os.environ.get("ADVANCE_AFTER_CREATE"):
            subprocess.run([os.environ["REAL_GIT"], "--git-dir", os.environ["REMOTE"],
                            "update-ref", "refs/heads/main", os.environ["ADVANCE_AFTER_CREATE"]], check=True)
        print("https://github.com/Cratis/Fixture/pull/1")
    elif args[:2] == ["pr", "view"]:
        assert args == ["pr", "view", "https://github.com/Cratis/Fixture/pull/1", "--repo", "Cratis/Fixture",
                        "--json", "headRefOid,baseRefOid,headRefName,baseRefName", "--jq",
                        "[.headRefOid, .baseRefOid, .headRefName, .baseRefName] | @tsv"], args
        print("\t".join([os.environ.get("READBACK_COMMIT", os.environ["CANDIDATE_COMMIT"]),
                         os.environ["EXPECTED_BASE"], "automation/package-update-123-1",
                         os.environ.get("READBACK_BASE_BRANCH", "main")]))
    else:
        raise AssertionError(args)
else:
    assert name in ("dotnet", "yarn", "npx", "gradle", "mix"), name
    command = " ".join([name, *args])
    if command == os.environ.get("FAIL_NATIVE"):
        sys.exit(23)
    update = command in ("dotnet package update", "npx npm-check-updates -u -w -x typescript",
                         "gradle useLatestVersions --no-daemon", "mix deps.update --all")
    if update and os.environ.get("NO_UPDATES") != "1":
        with open("dependencies.txt", "a") as dependencies:
            dependencies.write(command + "\n")
    build = command in ("dotnet build --configuration Release", "yarn ci", "gradle build --no-daemon", "mix compile")
    if build:
        # Simulate arbitrary untracked AND ignored artifacts; none may be published.
        Path("unrelated-output.tmp").write_text("not for publication")
        Path(".ai-work").mkdir(exist_ok=True)
        Path(".ai-work/notes").write_text("not for publication")
        Path("dist").mkdir(exist_ok=True)
        Path("dist/cache").write_text("not for publication")
        if os.environ.get("DIRTY_BUILD") == "1":
            Path("Dockerfile").write_text("unvalidated build mutation\n")
'''


class UpdatePackagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update-packages-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "caller"
        self.remote = self.root / "remote.git"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.pr = self.root / "pr.json"
        # No inherited credentials, Git configuration, agents, hooks or network remotes.
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
            "REAL_GIT": REAL_GIT, "COMMAND_LOG": str(self.log), "PR_CREATED": str(self.pr),
            "REMOTE": str(self.remote), "GH_TOKEN": "offline-placeholder",
            "CALLER_REPOSITORY": "Cratis/Fixture", "PUBLICATION_MODE": "pull-request", "PR_LABEL": "",
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "NPM_PACKAGE_EXCLUSIONS": "typescript",
        }
        for name in ["git", "gh", "dotnet", "yarn", "npx", "gradle", "mix"]:
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(STUB))
            path.chmod(0o755)
        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        self.git("clone", str(self.remote), str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for name, content in {"dependencies.txt": "original\n", "Dockerfile": "original\n",
                              ".gitignore": ".ai-work/\ndist/\n", "gradlew": "#!/bin/bash\nexec gradle \"$@\"\n"}.items():
            (self.repo / name).write_text(content)
        (self.repo / "gradlew").chmod(0o755)
        self.git("add", "--all")
        self.git("commit", "-m", "fixture base")
        self.git("push", "origin", "main")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args, cwd=None, input=None):
        result = subprocess.run([REAL_GIT, *args], cwd=cwd or self.repo, env=self.env,
                                input=input, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def run_step(self, name):
        output = self.root / "step-output"
        output.write_text("")
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", shell(name)],
                                cwd=self.repo, env={**self.env, "GITHUB_OUTPUT": str(output)},
                                text=True, capture_output=True)
        self.last_result = result
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        if name == PREFLIGHT:
            self.env.update(EXPECTED_BASE=values.get("base", ""), BASE_BRANCH=values.get("base_branch", ""))
        if name == FREEZE:
            self.changed = values.get("changed") == "true"
            self.env.update(CANDIDATE_COMMIT=values.get("commit", ""), CANDIDATE_TREE=values.get("tree", ""))
        return result.returncode

    def assert_step_ok(self, name):
        status = self.run_step(name)
        self.assertEqual(status, 0, self.last_result.stdout + self.last_result.stderr)

    def prepare(self, hook="printf 'updated image\\n' > Dockerfile", builds=True):
        # Model Actions' default success() gate; structural tests below protect that gate.
        for name in [PREFLIGHT, *UPDATES]:
            if self.run_step(name):
                return False
        if hook:
            self.env["POST_UPDATE_COMMAND"] = hook
            if self.run_step(HOOK):
                return False
        if self.run_step(FREEZE):
            return False
        if builds:
            for name in BUILDS:
                if self.run_step(name):
                    return False
        return True

    def pipeline(self, **kwargs):
        if not self.prepare(**kwargs):
            return False
        return not self.changed or self.run_step(PUBLISH) == 0

    def calls(self, name=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if not name or call[0] == name]

    def assert_not_published(self):
        self.assertFalse(self.pr.exists())
        self.assertEqual([c for c in self.calls("git") if c[1] == "push"], [])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_structure_and_default_contract(self):
        names = list(STEPS)
        self.assertLess(names.index(PREFLIGHT), names.index(UPDATES[0]))
        for name in UPDATES:
            self.assertLess(names.index(name), names.index(HOOK))
        self.assertLess(names.index(HOOK), names.index(FREEZE))
        for name in BUILDS:
            self.assertLess(names.index(FREEZE), names.index(name))
            self.assertLess(names.index(name), names.index(PUBLISH))
            self.assertLess(names.index(name), names.index(LEGACY))
            self.assertIn("if: steps.detect.outputs.update_", STEPS[name])
        self.assertIn("if: inputs.post-update-command != ''", STEPS[HOOK])
        self.assertIn("if: inputs.publication-mode == 'pull-request'", STEPS[FREEZE])
        self.assertIn("if: success() && inputs.publication-mode == 'pull-request' && steps.candidate.outputs.changed == 'true'", STEPS[PUBLISH])
        self.assertIn("if: inputs.publication-mode == 'direct'", STEPS[LEGACY])
        self.assertNotIn("continue-on-error:", SOURCE)
        self.assertNotIn("always()", SOURCE)
        for name in [PREFLIGHT, HOOK, FREEZE, PUBLISH, "Update NPM packages"]:
            self.assertNotIn("${{", shell(name))
        inputs = SOURCE.split("    inputs:\n", 1)[1].split("    secrets:", 1)[0]
        for name, default in [("publication-mode", "direct"), ("post-update-command", "''"),
                              ("pull-request-label", "''"), ("runs-on", "ubuntu-latest"),
                              ("npm-package-exclusions", "typescript")]:
            # Input properties are more deeply indented; use the next top-level input.
            block = re.split(r"\n      [a-z]", inputs.split(f"      {name}:\n", 1)[1])[0]
            self.assertIn(f"default: {default}", block)
        self.assertIn("runs-on: ${{ inputs.runs-on }}", SOURCE)
        self.assertIn("token: ${{ secrets.PAT_WORKFLOWS || github.token }}", STEPS["Checkout repository"])
        self.assertIsNone(re.search(r"^\s*permissions:", SOURCE, re.M))

    def test_shell_syntax(self):
        for name in [PREFLIGHT, HOOK, FREEZE, PUBLISH]:
            with self.subTest(step=name):
                result = subprocess.run(["/bin/bash", "-n"], input=shell(name), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_direct_default_preflight_has_no_git_or_api_effects(self):
        self.env["PUBLICATION_MODE"] = "direct"
        self.assert_step_ok(PREFLIGHT)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        # Never execute the inherited legacy push/rebase loop locally.

    def test_exact_candidate_contains_hook_and_updates_but_not_artifacts(self):
        (self.repo / "unrelated-before.tmp").write_text("unrelated")
        self.assertTrue(self.pipeline(), self.last_result.stderr)
        commit = self.env["CANDIDATE_COMMIT"]
        branch = "automation/package-update-123-1"
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", branch), commit)
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), self.env["CANDIDATE_TREE"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)
        self.assertEqual(self.git("diff", "--name-only", self.base, commit), "Dockerfile\ndependencies.txt")
        self.assertEqual(self.git("show", f"{commit}:Dockerfile"), "updated image")
        self.assertTrue((self.repo / "unrelated-before.tmp").exists())
        self.assertTrue((self.repo / "unrelated-output.tmp").exists())
        self.assertTrue((self.repo / ".ai-work/notes").exists())
        self.assertTrue((self.repo / "dist/cache").exists())
        calls = self.calls()
        push = next(i for i, c in enumerate(calls) if c[:2] == ["git", "push"])
        for command in [["dotnet", "build", "--configuration", "Release"], ["yarn", "ci"],
                        ["gradle", "build", "--no-daemon"], ["mix", "compile"]]:
            self.assertLess(calls.index(command), push)
        self.assertEqual([c for c in calls if c[:2] == ["git", "push"]],
                         [["git", "push", "origin", f"{commit}:refs/heads/{branch}"]])
        args = json.loads(self.pr.read_text())
        self.assertNotIn("--label", args)
        self.assertEqual(args[args.index("--body") + 1],
                         "## Changed\n\n- Update dependency versions and required runtime compatibility settings.")
        reserve = next(i for i, c in enumerate(calls) if c[:4] == ["gh", "api", "--method", "POST"])
        readback = next(i for i, c in enumerate(calls) if c[:3] == [
            "gh", "api", "repos/Cratis/Fixture/git/ref/heads/automation/package-update-123-1"])
        self.assertLess(reserve, readback)
        self.assertLess(readback, push)

    def test_optional_hook_absent_still_publishes_package_changes(self):
        self.assertTrue(self.pipeline(hook=""), self.last_result.stderr)
        self.assertEqual(self.git("show", "HEAD:Dockerfile"), "original")

    def test_hook_failure_is_closed(self):
        self.assertFalse(self.pipeline(hook="printf 'changed\\n' > Dockerfile; false; true"))
        self.assertFalse(any(c[:2] == ["dotnet", "build"] for c in self.calls()))
        self.assert_not_published()

    def test_hook_pipeline_failure_is_closed(self):
        self.assertFalse(self.pipeline(hook="false | true"))
        self.assert_not_published()

    def assert_build_failure(self, command):
        self.env["FAIL_NATIVE"] = command
        self.assertFalse(self.pipeline())
        self.assert_not_published()

    def test_dotnet_build_failure_is_closed(self):
        self.assert_build_failure("dotnet build --configuration Release")

    def test_npm_build_failure_is_closed(self):
        self.assert_build_failure("yarn ci")

    def test_gradle_build_failure_is_closed(self):
        self.assert_build_failure("gradle build --no-daemon")

    def test_mix_build_failure_is_closed(self):
        self.assert_build_failure("mix compile")

    def test_build_tracked_mutation_fails_instead_of_committing_it(self):
        self.env["DIRTY_BUILD"] = "1"
        self.assertFalse(self.pipeline())
        self.assert_not_published()

    def test_build_staged_addition_is_not_published(self):
        self.assertTrue(self.prepare())
        self.git("add", "unrelated-output.tmp")
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_new_hook_owned_file_requires_separate_review(self):
        self.assertFalse(self.pipeline(hook="printf 'new\\n' > new-file; git add new-file"))
        self.assert_not_published()

    def test_no_updates_no_pr_even_with_untracked_output(self):
        self.env["NO_UPDATES"] = "1"
        self.assertTrue(self.pipeline(hook=""), self.last_result.stderr)
        self.assertFalse(self.changed)
        self.assert_not_published()

    def test_hook_only_tracked_update_is_a_candidate(self):
        self.env["NO_UPDATES"] = "1"
        self.assertTrue(self.pipeline(), self.last_result.stderr)
        self.assertEqual(self.git("diff", "--name-only", self.base, "HEAD"), "Dockerfile")

    def advance_remote(self):
        tree = self.git("rev-parse", f"{self.base}^{{tree}}")
        commit = self.git("commit-tree", tree, "-p", self.base, input="concurrent base\n")
        self.git("push", "origin", f"{commit}:refs/heads/main")
        return commit

    def test_remote_base_advancement_fails_before_push(self):
        self.assertTrue(self.prepare())
        advanced = self.advance_remote()
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertFalse(self.pr.exists())
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), advanced)

    def test_stale_checkout_fails_before_updates(self):
        self.advance_remote()
        self.assertNotEqual(self.run_step(PREFLIGHT), 0)
        self.assertFalse(self.pr.exists())

    def test_default_branch_change_fails_closed(self):
        self.assertTrue(self.prepare())
        self.env["REMOTE_DEFAULT"] = "other"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_candidate_commit_change_fails_closed(self):
        self.assertTrue(self.prepare())
        self.git("commit", "--allow-empty", "-m", "not validated")
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_existing_run_branch_is_never_reused(self):
        self.assertTrue(self.prepare())
        self.git("push", "origin", f"{self.base}:refs/heads/automation/package-update-123-1")
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertFalse(self.pr.exists())
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))

    def assert_reservation_failure(self, option, value="1", reserved=True):
        self.assertTrue(self.prepare())
        self.env[option] = value
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertFalse(self.pr.exists())
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in self.calls()))
        self.assertEqual(len([c for c in self.calls() if c[:4] == ["gh", "api", "--method", "POST"]]), 1)
        self.assertIn("possibly base-only", self.last_result.stderr)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)
        if reserved:
            self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse",
                                      "refs/heads/automation/package-update-123-1"), self.base)
        else:
            self.assert_not_published()

    def test_atomic_reservation_rejects_concurrent_same_base_branch(self):
        self.assert_reservation_failure("RACE_RESERVE")
        self.assertIn("HTTP 422", self.last_result.stderr)
        calls = self.calls()
        lookup = ["git", "ls-remote", "origin", "refs/heads/automation/package-update-123-1"]
        reserve = next(i for i, c in enumerate(calls) if c[:4] == ["gh", "api", "--method", "POST"])
        self.assertLess(calls.index(lookup), reserve)

    def test_reservation_api_failure_cannot_fall_through_to_push(self):
        self.assert_reservation_failure("FAIL_RESERVE", reserved=False)

    def test_failed_reservation_with_valid_output_leaves_base_only_branch(self):
        self.assert_reservation_failure("FAIL_AFTER_RESERVE_OUTPUT")

    def test_reservation_response_mismatch_blocks_push(self):
        self.assert_reservation_failure("RESERVE_RESPONSE_REF", "refs/heads/other")

    def test_reservation_readback_failure_blocks_push(self):
        self.assert_reservation_failure("FAIL_RESERVATION_READBACK")

    def test_failed_reservation_readback_with_valid_output_blocks_push(self):
        self.assert_reservation_failure("FAIL_AFTER_RESERVATION_READBACK_OUTPUT")

    def test_reservation_readback_mismatch_blocks_push(self):
        self.assert_reservation_failure("RESERVATION_READBACK_SHA", "0" * 40)

    def test_failed_nonforce_push_reports_base_only_reservation_without_retry(self):
        self.env["FAIL_PUSH"] = "1"
        self.assertFalse(self.pipeline())
        self.assertFalse(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse",
                                  "refs/heads/automation/package-update-123-1"), self.base)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)
        self.assertIn("possibly base-only", self.last_result.stderr)

    def test_failed_branch_lookup_is_not_treated_as_absence(self):
        self.assertTrue(self.prepare())
        self.env["FAIL_BRANCH_LOOKUP"] = "1"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_api_failure_is_closed(self):
        self.env["FAIL_API"] = "1"
        self.assertNotEqual(self.run_step(PREFLIGHT), 0)
        self.assert_not_published()

    def test_api_failure_with_valid_stdout_still_blocks_publication(self):
        self.assertTrue(self.prepare())
        self.env["FAIL_AFTER_API_OUTPUT"] = "1"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_git_failure_with_valid_stdout_still_blocks_publication(self):
        self.assertTrue(self.prepare())
        self.env["FAIL_AFTER_REF_OUTPUT"] = "1"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()

    def test_npm_exclusions_are_data_not_shell_source(self):
        value = 'typescript,$(touch injected);"quoted"'
        self.env["NPM_PACKAGE_EXCLUSIONS"] = value
        self.assert_step_ok("Update NPM packages")
        self.assertIn(["npx", "npm-check-updates", "-u", "-w", "-x", value], self.calls())
        self.assertFalse((self.repo / "injected").exists())

    def test_direct_hook_failure_also_stops_before_builds(self):
        self.env.update(PUBLICATION_MODE="direct", POST_UPDATE_COMMAND="false; true")
        self.assert_step_ok(PREFLIGHT)
        self.assertNotEqual(self.run_step(HOOK), 0)
        self.assert_not_published()

    def test_dirty_tracked_checkout_fails_before_updates(self):
        (self.repo / "Dockerfile").write_text("unrelated preexisting change\n")
        self.assertNotEqual(self.run_step(PREFLIGHT), 0)
        self.assert_not_published()

    def test_create_failure_does_not_retry_or_push_main(self):
        self.env["FAIL_CREATE"] = "1"
        self.assertFalse(self.pipeline())
        self.assertFalse(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)
        self.assertEqual(len([c for c in self.calls() if c[:3] == ["gh", "pr", "create"]]), 1)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)

    def test_readback_mismatch_fails_after_creation_without_retry(self):
        self.env["READBACK_COMMIT"] = self.base
        self.assertFalse(self.pipeline())
        self.assertTrue(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)

    def test_base_race_after_creation_is_reported(self):
        self.assertTrue(self.prepare())
        # The candidate object already exists in the bare repo after the publication push.
        self.env["ADVANCE_AFTER_CREATE"] = self.env["CANDIDATE_COMMIT"]
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertTrue(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)

    def test_base_race_after_push_blocks_pr_creation(self):
        self.assertTrue(self.prepare())
        self.env["ADVANCE_AFTER_PUSH"] = self.env["CANDIDATE_COMMIT"]
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertFalse(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)

    def test_remote_candidate_race_blocks_pr_creation(self):
        self.assertTrue(self.prepare())
        self.env["MUTATE_BRANCH_AFTER_PUSH"] = self.base
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertFalse(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)

    def test_pr_retargeting_is_reported_without_retry(self):
        self.env["READBACK_BASE_BRANCH"] = "other"
        self.assertFalse(self.pipeline())
        self.assertTrue(self.pr.exists())
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["git", "push"]]), 1)

    def test_explicit_label_is_passed_as_one_argument(self):
        self.env["PR_LABEL"] = "release:patch"
        self.assertTrue(self.pipeline(), self.last_result.stderr)
        args = json.loads(self.pr.read_text())
        self.assertEqual(args[args.index("--label") + 1], "release:patch")

    def test_invalid_options_cannot_inject_shell(self):
        for value in ["--bad", "patch,major", "$(touch injected)", "patch\nmajor", "patch;true"]:
            with self.subTest(label=value):
                self.env["PR_LABEL"] = value
                self.assertNotEqual(self.run_step(PREFLIGHT), 0)
                self.assert_not_published()
        self.assertFalse((self.repo / "injected").exists())
        self.env.update(PR_LABEL="", PUBLICATION_MODE="typo")
        self.assertNotEqual(self.run_step(PREFLIGHT), 0)
        self.assert_not_published()

    def test_invalid_run_identity_cannot_become_branch(self):
        self.assertTrue(self.prepare())
        self.env["GITHUB_RUN_ID"] = "123; touch injected"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()


if __name__ == "__main__":
    unittest.main(verbosity=2)
