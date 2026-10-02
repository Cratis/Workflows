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
NPM_UPDATES = ["Snapshot NPM peer pins", "Update NPM packages", "Synchronize NPM peers and install"]
UPDATES = ["Update NuGet packages", *NPM_UPDATES, "Update Gradle packages", "Update Mix packages"]
BUILDS = ["Build .NET", "Build NPM", "Build Gradle", "Build Mix"]
REAL_GIT = shutil.which("git")
NUGET_FIXTURES = Path(__file__).parent / "fixtures/nuget-sdk-10.0.401"


def nuget_json(name):
    return (NUGET_FIXTURES / (name + ".json")).read_text()


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
    if name == "yarn" and args == ["--version"]:
        print(os.environ.get("YARN_VERSION", "4.18.1"))
        sys.exit(0)
    if name == "dotnet" and args[:2] == ["package", "list"]:
        key = ("DOTNET_PRERELEASE" if "--include-prerelease" in args else
               "DOTNET_OUTDATED" if "--outdated" in args else "DOTNET_INVENTORY")
        print(os.environ.get(key, os.environ.get("DOTNET_OUTDATED", "")))
        sys.exit(int(os.environ.get("LIST_EXIT", "0")))
    if name == "dotnet" and args[:2] == ["package", "update"] and os.environ.get("UPDATE_EXIT"):
        print(os.environ.get("UPDATE_OUTPUT", ""))
        sys.exit(int(os.environ["UPDATE_EXIT"]))
    if name == "npx" and os.environ.get("NPM_NEW_PIN"):
        path = Path("package.json")
        manifest = json.loads(path.read_text())
        manifest["devDependencies"]["ms"] = os.environ["NPM_NEW_PIN"]
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    update = (name == "dotnet" and args[:2] == ["package", "update"]) or command in ("npx npm-check-updates -u -w -x typescript",
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
    def test_setup_installs_caller_sdk_pin_without_changing_selection_policy(self):
        setup = STEPS["Setup .NET"]
        self.assertIn("dotnet-version: ${{ env.DOTNET_VERSION }}", setup)
        self.assertIn("global-json-file: ${{ hashFiles('global.json') != '' && 'global.json' || '' }}", setup)
        self.assertIn("DOTNET_INSTALL_DIR:", setup)
        self.assertNotRegex(SOURCE, r"(?:rm|mv|sed|jq) .*global[.]json")

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
            "NUGET_PACKAGE_EXCLUSIONS": "", "RUNNER_TEMP": str(self.root),
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
        if name == "Snapshot NPM peer pins":
            self.env["PEER_UPDATE_DIR"] = values.get("directory", "")
        if name == FREEZE:
            self.changed = values.get("changed") == "true"
            self.env.update(CANDIDATE_COMMIT=values.get("commit", ""), CANDIDATE_TREE=values.get("tree", ""))
        return result.returncode

    def assert_step_ok(self, name):
        status = self.run_step(name)
        self.assertEqual(status, 0, self.last_result.stdout + self.last_result.stderr)

    def run_npm(self):
        for name in NPM_UPDATES:
            status = self.run_step(name)
            if status:
                return status
        return 0

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
        for before, after in zip(NPM_UPDATES, NPM_UPDATES[1:]):
            self.assertLess(names.index(before), names.index(after))
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
        for name in [PREFLIGHT, HOOK, FREEZE, PUBLISH, *NPM_UPDATES]:
            self.assertNotIn("${{", shell(name))
        inputs = SOURCE.split("    inputs:\n", 1)[1].split("    secrets:", 1)[0]
        for name, default in [("publication-mode", "direct"), ("post-update-command", "''"),
                              ("pull-request-label", "''"), ("runs-on", "ubuntu-latest"),
                              ("npm-package-exclusions", "typescript"),
                              ("nuget-package-exclusions", "''")]:
            # Input properties are more deeply indented; use the next top-level input.
            block = re.split(r"\n      [a-z]", inputs.split(f"      {name}:\n", 1)[1])[0]
            self.assertIn(f"default: {default}", block)
        self.assertIn("runs-on: ${{ inputs.runs-on }}", SOURCE)
        self.assertIn("token: ${{ secrets.PAT_WORKFLOWS || github.token }}", STEPS["Checkout repository"])
        self.assertIsNone(re.search(r"^\s*permissions:", SOURCE, re.M))

    def test_shell_syntax(self):
        for name in [PREFLIGHT, HOOK, FREEZE, PUBLISH, *NPM_UPDATES]:
            with self.subTest(step=name):
                result = subprocess.run(["/bin/bash", "-n"], input=shell(name), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def nuget_fixture(self, name="mixed"):
        self.env["DOTNET_INVENTORY"] = nuget_json(name + "-inventory")
        self.env["DOTNET_OUTDATED"] = nuget_json(name + "-outdated")
        if name == "prerelease":
            self.env["DOTNET_PRERELEASE"] = nuget_json("prerelease-prerelease-outdated")

    def test_nuget_exclusion_uses_exact_case_insensitive_ids_in_cpm_and_projects(self):
        for name in ("mixed", "cpm"):
            with self.subTest(fixture=name):
                self.log.unlink(missing_ok=True)
                self.nuget_fixture(name)
                self.env["NUGET_PACKAGE_EXCLUSIONS"] = "  nEwTonSoft.Json  ,  missing.package , SERILOG,"
                self.assert_step_ok("Update NuGet packages")
                self.assertEqual([call for call in self.calls("dotnet") if call[1:3] == ["package", "update"]], [])
        self.assertIn("::warning::NuGet exclusion 'missing.package' is not referenced", self.last_result.stdout)
        self.assertNotIn("SERILOG' is not referenced", self.last_result.stdout)
        self.assertNotIn("not referenced", self.last_result.stderr)

    def test_nuget_default_retains_unfiltered_update_without_listing(self):
        self.assert_step_ok("Update NuGet packages")
        self.assertEqual(self.calls("dotnet"), [["dotnet", "package", "update"]])

    def test_nuget_whitespace_only_exclusions_keep_the_default_update(self):
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "   "
        self.assert_step_ok("Update NuGet packages")
        self.assertEqual(self.calls("dotnet"), [["dotnet", "package", "update"]])

    def test_nuget_all_outdated_packages_excluded_is_a_noop(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog,Newtonsoft.Json"
        self.assert_step_ok("Update NuGet packages")
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_all_current_is_a_successful_noop(self):
        self.nuget_fixture("all-current")
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Newtonsoft.Json"
        self.assert_step_ok("Update NuGet packages")
        self.assertIn("No eligible NuGet updates", self.last_result.stdout)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_mixed_current_and_outdated_selects_only_eligible(self):
        self.nuget_fixture("mixed")
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog"
        self.assert_step_ok("Update NuGet packages")
        self.assertEqual([call for call in self.calls("dotnet") if call[1:3] == ["package", "update"]],
                         [["dotnet", "package", "update", "Newtonsoft.Json"]])

    def test_nuget_transitive_only_exclusion_warns_but_does_not_filter_parent(self):
        self.nuget_fixture("transitive")
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Microsoft.Extensions.Logging"
        self.assert_step_ok("Update NuGet packages")
        self.assertIn("not referenced", self.last_result.stdout)
        self.assertEqual([call for call in self.calls("dotnet") if call[1:3] == ["package", "update"]],
                         [["dotnet", "package", "update", "Serilog.AspNetCore"]])

    def test_nuget_prerelease_reference_keeps_default_update_semantics(self):
        self.nuget_fixture("prerelease")
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Newtonsoft.Json"
        self.assert_step_ok("Update NuGet packages")
        self.assertIn(["dotnet", "package", "list", "--outdated", "--include-prerelease", "--format", "json"],
                      self.calls("dotnet"))
        self.assertEqual([call for call in self.calls("dotnet") if call[1:3] == ["package", "update"]],
                         [["dotnet", "package", "update", "Microsoft.Extensions.Http"]])

    def test_nuget_empty_listing_is_not_a_successful_noop(self):
        for output in ("", "  \n  "):
            with self.subTest(output=output):
                self.nuget_fixture("all-current")
                self.env.update(NUGET_PACKAGE_EXCLUSIONS="Serilog", DOTNET_OUTDATED=output)
                self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
                self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_error_problems_fail_even_when_command_returns_zero(self):
        for name in ("DOTNET_INVENTORY", "DOTNET_OUTDATED", "DOTNET_PRERELEASE"):
            with self.subTest(listing=name):
                self.log.unlink(missing_ok=True)
                self.nuget_fixture("prerelease")
                data = json.loads(self.env[name])
                data["problems"] = json.loads(nuget_json("restore-failure-outdated"))["problems"]
                self.env[name] = json.dumps(data)
                self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Newtonsoft.Json"
                self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
                self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_unsupported_per_framework_version_fails_before_partial_update(self):
        self.nuget_fixture("multi-tfm")
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog"
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_exit_2_with_eligible_candidates_is_not_a_successful_noop(self):
        self.nuget_fixture()
        self.env.update(NUGET_PACKAGE_EXCLUSIONS="Serilog", UPDATE_EXIT="2",
                        UPDATE_OUTPUT="All packages are up to date")
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)

    def test_nuget_default_exit_2_remains_a_skip(self):
        self.env.update(UPDATE_EXIT="2", UPDATE_OUTPUT="All packages are up to date")
        self.assert_step_ok("Update NuGet packages")

    def test_nuget_exit_3_with_exclusions_is_a_skip(self):
        self.nuget_fixture()
        self.env.update(NUGET_PACKAGE_EXCLUSIONS="Serilog", UPDATE_EXIT="3")
        self.assert_step_ok("Update NuGet packages")
        self.assertIn("skipping NuGet update", self.last_result.stdout)

    def test_nuget_broken_inventory_fails_before_updates(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog"
        self.env["DOTNET_OUTDATED"] = '{"version":1,"projects":"invalid"}'
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_update_failure_does_not_publish_partial_updates(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog"
        self.env["FAIL_NATIVE"] = "dotnet package update Newtonsoft.Json"
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertEqual([call for call in self.calls("dotnet") if call[1:3] == ["package", "update"]],
                         [["dotnet", "package", "update", "Newtonsoft.Json"]])

    def test_nuget_outdated_id_missing_from_reference_inventory_fails_closed(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Cratis.Fundamentals"
        self.env["DOTNET_OUTDATED"] = json.dumps({"version": 1, "projects": [
            {"path": "Project.csproj", "frameworks": [{"framework": "net10.0",
             "topLevelPackages": [{"id": "Not.Referenced"}]}]}]})
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_invalid_exclusion_does_not_update_anything(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Cratis.Fundamentals, --help"
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

    def test_nuget_list_failure_does_not_publish_partial_updates(self):
        self.nuget_fixture()
        self.env["NUGET_PACKAGE_EXCLUSIONS"] = "Serilog"
        self.env["FAIL_NATIVE"] = "dotnet package list --outdated --format json"
        self.assertNotEqual(self.run_step("Update NuGet packages"), 0)
        self.assertFalse(any(call[1:3] == ["package", "update"] for call in self.calls("dotnet")))

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
        self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
        self.assertIn(["npx", "npm-check-updates", "-u", "-w", "-x", value], self.calls())
        self.assertFalse((self.repo / "injected").exists())

    def test_npm_updates_then_installs_and_dedupes_supported_yarn(self):
        for version in ("2.2.0", "2.4.3", "3.0.0", "4.18.1"):
            with self.subTest(version=version):
                self.log.unlink(missing_ok=True)
                self.env["YARN_VERSION"] = version
                self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
                calls = self.calls()
                self.assertEqual(self.calls("npx"), [["npx", "npm-check-updates", "-u", "-w", "-x", "typescript"]])
                self.assertLess(calls.index(self.calls("npx")[0]), calls.index(["yarn", "install"]))
                self.assertLess(calls.index(["yarn", "install"]), calls.index(["yarn", "dedupe"]))
                self.assertEqual(self.calls("yarn").count(["yarn", "dedupe"]), 1)

    def test_npm_legacy_yarn_installs_without_dedupe(self):
        for version in ("1.22.22", "2.0.0", "2.1.1"):
            with self.subTest(version=version):
                self.log.unlink(missing_ok=True)
                self.env.update(YARN_VERSION=version, FAIL_NATIVE="yarn dedupe")
                self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
                self.assertIn(["yarn", "install"], self.calls("yarn"))
                self.assertNotIn(["yarn", "dedupe"], self.calls("yarn"))

    def test_npm_update_install_and_version_failures_stop_the_step(self):
        for command, forbidden in (
                ("npx npm-check-updates -u -w -x typescript", ["yarn", "install"]),
                ("yarn install", ["yarn", "dedupe"]),
                ("yarn --version", ["yarn", "dedupe"])):
            with self.subTest(command=command):
                self.log.unlink(missing_ok=True)
                self.env["FAIL_NATIVE"] = command
                self.assertEqual(self.run_npm(), 23)
                self.assertNotIn(forbidden, self.calls())

    def test_npm_update_and_install_steps_disable_immutable_yarn_installs(self):
        for name in ("Update NPM packages", "Synchronize NPM peers and install"):
            with self.subTest(step=name):
                env = STEPS[name].split("        env:\n", 1)[1]
                self.assertRegex(env, r"(?m)^          YARN_ENABLE_IMMUTABLE_INSTALLS: false$")

    def test_npm_embedded_helper_matches_script(self):
        embedded = shell(NPM_UPDATES[0]).split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0] + "\n"
        self.assertEqual(embedded, (ROOT / ".github/scripts/sync-package-update-peers.py").read_text())

    def test_npm_synchronizes_only_tracking_peers_before_install(self):
        manifest = {"devDependencies": {"ms": "2.1.2"},
                    "peerDependencies": {"ms": "^2.1.2", "react": "^18.0.0 || ^19.0.0"}}
        (self.repo / "package.json").write_text(json.dumps(manifest))
        self.env["NPM_NEW_PIN"] = "2.1.3"
        self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
        after = json.loads((self.repo / "package.json").read_text())
        self.assertEqual(after["peerDependencies"], {"ms": "^2.1.3", "react": "^18.0.0 || ^19.0.0"})

    def test_npm_command_does_not_override_caller_dependency_section_configuration(self):
        for config in ({"dep": ["dev"]}, {"dep": ["prod", "dev", "peer", "optional"]},
                       {"mergeConfig": True, "dep": ["peer"]}):
            with self.subTest(config=config):
                self.log.unlink(missing_ok=True)
                path = self.repo / ".ncurc.json"
                contents = json.dumps(config)
                path.write_text(contents)
                self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
                self.assertEqual(self.calls("npx"), [["npx", "npm-check-updates", "-u", "-w", "-x", "typescript"]])
                self.assertEqual(path.read_text(), contents)

    def test_npm_out_of_range_pin_warns_without_blocking_install_or_dedupe(self):
        manifest = {"devDependencies": {"ms": "1.2.3"}, "peerDependencies": {"ms": "^1.2.3"}}
        (self.repo / "package.json").write_text(json.dumps(manifest))
        self.env["NPM_NEW_PIN"] = "2.0.0"
        self.assertEqual(self.run_npm(), 0, self.last_result.stderr)
        after = json.loads((self.repo / "package.json").read_text())
        self.assertEqual(after["peerDependencies"], {"ms": "^1.2.3"})
        self.assertIn("::warning::package.json: ms peer ^1.2.3 unchanged; new pin 2.0.0", self.last_result.stdout)
        self.assertIn(["yarn", "install"], self.calls("yarn"))
        self.assertIn(["yarn", "dedupe"], self.calls("yarn"))

    def test_npm_sync_failure_blocks_install_builds_and_publication(self):
        self.assert_step_ok("Snapshot NPM peer pins")
        self.assert_step_ok("Update NPM packages")
        (Path(self.env["PEER_UPDATE_DIR"]) / "manifests.json").write_text("invalid json")
        self.assertNotEqual(self.run_step("Synchronize NPM peers and install"), 0)
        self.assertEqual(self.calls("yarn"), [])
        self.assert_not_published()

    def test_npm_invalid_yarn_version_fails_closed(self):
        self.env["YARN_VERSION"] = "unknown"
        self.assertNotEqual(self.run_npm(), 0)
        self.assertNotIn(["yarn", "dedupe"], self.calls("yarn"))

    def test_npm_dedupe_failure_blocks_builds_and_publication(self):
        self.env["FAIL_NATIVE"] = "yarn dedupe"
        self.assertFalse(self.pipeline())
        self.assertEqual(self.last_result.returncode, 23)
        self.assertFalse(any(call[:2] in (["dotnet", "build"], ["yarn", "ci"])
                             for call in self.calls()))
        self.assert_not_published()

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
