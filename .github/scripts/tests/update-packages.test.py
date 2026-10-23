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

import yaml

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / ".github/workflows/update-packages.yml").read_text()
STEPS = dict(re.findall(r"^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", SOURCE, re.M | re.S))
PREFLIGHT = "Check publication options and default-branch base"
HOOK = "Run post-update command"
CHANGES = "Detect package changes"
CACHE_KEY = "Resolve NuGet package cache key"
PACKAGES_ON_SCRATCH = "Keep NuGet packages on the scratch volume"
CACHE_RESTORE = "Restore NuGet package cache"
CACHE_SAVE = "Save NuGet package cache at the end of the job"
CACHE_ACTION_SHA = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0"
FREEZE = "Freeze pull request candidate before builds"
PUBLISH = "Publish validated pull request"
LEGACY = "Commit and push changes"
DIRECT_FREEZE = "Freeze direct candidate after builds"
EXPORT = "Export validated candidate"
APPLY = "Apply validated candidate"
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
    forbidden = {"reset", "--amend", "--force", "--force-with-lease", "-f"}
    if os.environ.get("PUBLICATION_MODE") != "direct":
        forbidden.update({"rebase", "pull"})
    assert not forbidden.intersection(args), args
    if args[0] == "push":
        assert len(args) == 3 and args[1] == "origin", args
        if os.environ.get("PUBLICATION_MODE") == "direct":
            assert args[2] == "HEAD", args
        else:
            assert args[2].endswith(":refs/heads/automation/package-update-123-1"), args
            assert not args[2].startswith("+"), args
        if os.environ.get("FAIL_PUSH") == "1":
            sys.exit(24)
        if os.environ.get("REJECT_FIRST_PUSH") == "1":
            marker = Path(os.environ["RUNNER_TEMP"]) / "rejected-push"
            if not marker.exists():
                marker.touch()
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
        pulls = "repos/Cratis/Fixture/pulls/"
        open_prs = json.loads(Path(os.environ["OPEN_PRS"]).read_text()) if Path(os.environ["OPEN_PRS"]).exists() else []
        closed = Path(os.environ["CLOSED_PRS"])
        closed_numbers = [json.loads(line)["number"] for line in closed.read_text().splitlines()] if closed.exists() else []
        if args[1] == "repos/Cratis/Fixture/git/ref/heads/main":
            assert args == ["api", args[1], "--jq", ".object.sha"], args
            print(subprocess.check_output([*remote_git, "rev-parse", "refs/heads/main"], text=True).strip())
            sys.exit(0)
        if args[1] == "--paginate":
            assert args == ["api", "--paginate", "repos/Cratis/Fixture/pulls?state=open&per_page=100", "--jq",
                            '.[] | [.number, .html_url, .head.ref, .head.sha, (.head.repo.full_name // "-"), .base.ref] | @tsv'], args
            if os.environ.get("FAIL_PR_LIST") == "1":
                print("HTTP 502: Bad Gateway", file=sys.stderr)
                sys.exit(30)
            for pr in open_prs:
                if pr["number"] not in closed_numbers:
                    print("\t".join([str(pr["number"]), pr.get("url", "https://github.com/Cratis/Fixture/pull/%d" % pr["number"]),
                                     pr["head_ref"], pr["head_sha"], pr.get("head_repo", "Cratis/Fixture"),
                                     pr.get("base_ref", "main")]))
            if os.environ.get("FAIL_AFTER_PR_LIST_OUTPUT") == "1":
                sys.exit(31)
            sys.exit(0)
        if args[1].startswith(pulls):
            number = int(args[1][len(pulls):])
            pr = next(pr for pr in open_prs if pr["number"] == number)
            assert pr.get("head_repo", "Cratis/Fixture") == "Cratis/Fixture" and pr["head_ref"].startswith("automation/"), pr
            assert args == ["api", args[1], "--jq", "[.state, .head.sha, .head.ref, .base.ref] | @tsv"], args
            state = "closed" if number in closed_numbers or pr.get("closed") else "open"
            print("\t".join([state, pr.get("current_sha", pr["head_sha"]), pr["head_ref"], pr.get("base_ref", "main")]))
            sys.exit(0)
        if args[1:3] == ["--method", "POST"] and args[3].endswith("/comments"):
            number = int(args[3].split("/")[-2])
            assert args == ["api", "--method", "POST", f"repos/Cratis/Fixture/issues/{number}/comments",
                            "-f", args[5], "--jq", ".id"], args
            assert args[5].startswith("body=Superseded by https://github.com/Cratis/Fixture/pull/"), args
            if os.environ.get("FAIL_COMMENT") == "1":
                sys.exit(32)
            print("1")
            sys.exit(0)
        if args[1:3] == ["--method", "PATCH"]:
            assert args[3].startswith(pulls) and args[4:] == ["-f", "state=closed", "--jq", ".state"], args
            number = int(args[3][len(pulls):])
            if os.environ.get("FAIL_CLOSE") == "1":
                sys.exit(33)
            with closed.open("a") as record:
                record.write(json.dumps({"number": number}) + "\n")
            print("closed")
            sys.exit(0)
        if args[1:3] == ["--method", "DELETE"]:
            prefix = "repos/Cratis/Fixture/git/refs/heads/"
            assert len(args) == 4 and args[3].startswith(prefix + "automation/package-update-"), args
            assert args[3] != prefix + "automation/package-update-123-1", args
            if os.environ.get("FAIL_DELETE") == "1":
                sys.exit(34)
            subprocess.run([*remote_git, "update-ref", "-d", "refs/heads/" + args[3][len(prefix):]], check=True)
            sys.exit(0)
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
    def assert_credential_boundary(self, workflow):
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"update", "publish"})
        self.assertNotIn("PAT_WORKFLOWS", json.dumps(jobs["update"]))
        for job in jobs.values():
            self.assertEqual(job["runs-on"], "${{ inputs.runs-on }}")
        update_checkout = jobs["update"]["steps"][0]
        self.assertIs(update_checkout["with"]["persist-credentials"], False)
        self.assertEqual(update_checkout["with"]["token"], "${{ github.token }}")
        self.assertEqual(jobs["publish"]["needs"], "update")
        self.assertEqual(jobs["publish"]["if"], "needs.update.outputs.changed == 'true'")
        checkout = jobs["publish"]["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "${{ needs.update.outputs.base }}")
        for step in jobs["publish"]["steps"]:
            self.assert_publication_step(step)
        upload = next(step for step in jobs["update"]["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["retention-days"], 1)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")

    def assert_publication_step(self, step):
        self.assertNotIn("post-update-command", json.dumps(step))
        self.assertNotRegex(step.get("run", ""), r"(?im)(?:^|[\s/;|&])(dotnet|yarn|npm|npx|gradlew?|mix|bash|sh|python3?|node|source|eval|curl|wget)\b")
        self.assertNotRegex(step.get("run", ""), r"(?m)^\s*(?:\./|scripts/)")
        if "uses" in step:
            self.assertRegex(step["uses"], r"^actions/(checkout|download-artifact)@[0-9a-f]{40}$")

    def test_yaml_credential_boundary(self):
        self.assert_credential_boundary(yaml.safe_load(SOURCE))

    def test_credential_boundary_detects_seeded_violations(self):
        # Plant each forbidden behavior independently: this guard must fail red.
        for violation in ("secret", "persist", "toolchain", "hook", "script"):
            with self.subTest(violation=violation):
                workflow = yaml.safe_load(SOURCE)
                if violation == "secret":
                    workflow["jobs"]["update"]["env"] = {"GH_TOKEN": "${{ secrets.PAT_WORKFLOWS }}"}
                elif violation == "persist":
                    workflow["jobs"]["update"]["steps"][0]["with"]["persist-credentials"] = True
                else:
                    command = {"toolchain": "dotnet build", "hook": "${{ inputs.post-update-command }}",
                               "script": "./publish.sh"}[violation]
                    workflow["jobs"]["publish"]["steps"].append({"run": command})
                with self.assertRaises(AssertionError):
                    self.assert_credential_boundary(workflow)

    def test_direct_bundle_round_trip_preserves_postbuild_tree_and_message(self):
        self.env.update(PUBLICATION_MODE="direct", DIRTY_BUILD="1")
        self.assertTrue(self.pipeline(), self.last_result.stderr)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.env["CANDIDATE_COMMIT"])
        self.assertEqual(self.git("show", "HEAD:Dockerfile"), "unvalidated build mutation")
        self.assertEqual(self.git("show", "HEAD:unrelated-output.tmp"), "not for publication")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "chore: update NuGet and NPM and Gradle and Mix packages")
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.base)
        self.assertFalse(self.pr.exists())

    def test_direct_mode_retries_rejected_push_after_bundle_round_trip(self):
        self.env.update(PUBLICATION_MODE="direct", REJECT_FIRST_PUSH="1")
        self.assertTrue(self.pipeline(), self.last_result.stderr)
        self.assertEqual(len([c for c in self.calls("git") if c[1] == "push"]), 2)
        self.assertIn(["git", "pull", "--rebase", "origin", "main"], self.calls("git"))

    def export_to_fresh_checkout(self):
        self.assertTrue(self.prepare(), self.last_result.stderr)
        self.assert_step_ok(EXPORT)
        self.update_repo = self.repo
        self.repo = self.root / "publisher"
        self.git("clone", str(self.remote), str(self.repo), cwd=self.root)
        self.git("checkout", "--detach", self.base)

    def test_artifact_metadata_mismatches_fail_before_publication(self):
        self.export_to_fresh_checkout()
        metadata = self.root / "package-candidate/metadata"
        original = metadata.read_text()
        for index in range(7):
            with self.subTest(field=index):
                fields = original.splitlines()
                fields[index] = "mismatch"
                metadata.write_text("\n".join(fields) + "\n")
                self.assertNotEqual(self.run_step(APPLY), 0)
                self.assert_not_published()
        metadata.write_text(original)
        self.assert_step_ok(APPLY)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.env["CANDIDATE_COMMIT"])

    def test_artifact_corrupt_bundle_fails_before_publication(self):
        self.export_to_fresh_checkout()
        (self.root / "package-candidate/candidate.bundle").write_text("not a bundle\n")
        self.assertNotEqual(self.run_step(APPLY), 0)
        self.assert_not_published()

    def test_bundle_commit_and_tree_must_match_metadata_and_outputs(self):
        self.export_to_fresh_checkout()
        metadata = self.root / "package-candidate/metadata"
        original = metadata.read_text()
        for index, output in ((2, "CANDIDATE_COMMIT"), (3, "CANDIDATE_TREE")):
            with self.subTest(output=output):
                fields = original.splitlines()
                expected = self.env[output]
                self.env[output] = fields[index] = "f" * 40
                metadata.write_text("\n".join(fields) + "\n")
                self.assertNotEqual(self.run_step(APPLY), 0)
                self.assert_not_published()
                self.env[output] = expected
        metadata.write_text(original)
        self.assert_step_ok(APPLY)

    def test_artifact_requires_exact_checkout_base(self):
        self.export_to_fresh_checkout()
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("commit", "--allow-empty", "-m", "wrong base")
        self.assertNotEqual(self.run_step(APPLY), 0)
        self.assert_not_published()

    def test_noop_artifact_records_no_changes_without_bundle(self):
        self.assert_step_ok(PREFLIGHT)
        self.env["CHANGED"] = "false"
        self.assert_step_ok(EXPORT)
        directory = self.root / "package-candidate"
        self.assertEqual((directory / "metadata").read_text().splitlines()[-1], "false")
        self.assertFalse((directory / "candidate.bundle").exists())

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
            "OPEN_PRS": str(self.root / "open-prs.json"), "CLOSED_PRS": str(self.root / "closed-prs.jsonl"),
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

    def git(self, *args, cwd=None, input=None, env=None):
        result = subprocess.run([REAL_GIT, *args], cwd=cwd or self.repo, env={**self.env, **(env or {})},
                                input=input, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def ensure_publisher(self):
        if hasattr(self, "update_repo"):
            return True
        if self.run_step(EXPORT):
            return False
        self.update_repo = self.repo
        self.repo = self.root / "publisher"
        self.git("clone", str(self.remote), str(self.repo), cwd=self.root)
        self.git("checkout", "--detach", self.env["EXPECTED_BASE"])
        return self.run_step(APPLY) == 0

    def run_step(self, name):
        if name in (PUBLISH, LEGACY) and not self.ensure_publisher():
            return self.last_result.returncode
        output = self.root / "step-output"
        output.write_text("")
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", shell(name)],
                                cwd=self.repo, env={**self.env, "GITHUB_OUTPUT": str(output)},
                                text=True, capture_output=True)
        self.last_result = result
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.record_outputs(name, values)
        return result.returncode

    def record_outputs(self, name, values):
        if name == PREFLIGHT:
            self.env.update(EXPECTED_BASE=values.get("base", ""), BASE_BRANCH=values.get("base_branch", ""))
        if name == "Snapshot NPM peer pins":
            self.env["PEER_UPDATE_DIR"] = values.get("directory", "")
        if name == CHANGES:
            self.package_changes = values.get("changed")
        if name in (FREEZE, DIRECT_FREEZE):
            self.changed = values.get("changed") == "true"
            self.env["CHANGED"] = "true" if self.changed else "false"
        if name in (FREEZE, EXPORT):
            self.env.update(CANDIDATE_COMMIT=values.get("commit", ""), CANDIDATE_TREE=values.get("tree", ""))

    def assert_step_ok(self, name):
        status = self.run_step(name)
        self.assertEqual(status, 0, self.last_result.stdout + self.last_result.stderr)

    def run_npm(self):
        for name in NPM_UPDATES:
            status = self.run_step(name)
            if status:
                return status
        return 0

    def run_steps(self, names):
        for name in names:
            if self.run_step(name):
                return False
        return True

    def prepare(self, hook="printf 'updated image\\n' > Dockerfile", builds=True):
        # Model Actions' default success() gate; structural tests below protect that gate.
        if not self.run_steps([PREFLIGHT, *UPDATES]):
            return False
        if hook:
            self.env["POST_UPDATE_COMMAND"] = hook
            if self.run_step(HOOK):
                return False
        if self.run_step(CHANGES):
            return False
        if self.package_changes == "false":
            # Freeze, builds and publication are all gated on steps.changes.outputs.changed.
            self.changed = False
            return True
        return self.prepare_changed(builds)

    def prepare_changed(self, builds):
        if self.env["PUBLICATION_MODE"] == "pull-request" and self.run_step(FREEZE):
            return False
        if builds and not self.run_steps(BUILDS):
            return False
        if self.env["PUBLICATION_MODE"] == "direct":
            self.env.update(UPDATE_NUGET="true", UPDATE_NPM="true", UPDATE_GRADLE="true", UPDATE_MIX="true")
            return self.run_step(DIRECT_FREEZE) == 0
        return True

    def pipeline(self, **kwargs):
        if not self.prepare(**kwargs):
            return False
        publish = LEGACY if self.env["PUBLICATION_MODE"] == "direct" else PUBLISH
        return not self.changed or self.run_step(publish) == 0

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
        self.assertLess(names.index(HOOK), names.index(CHANGES))
        self.assertLess(names.index(CHANGES), names.index(FREEZE))
        for name in BUILDS:
            self.assertLess(names.index(FREEZE), names.index(name))
            self.assertLess(names.index(name), names.index(PUBLISH))
            self.assertLess(names.index(name), names.index(LEGACY))
            self.assertIn("if: steps.detect.outputs.update_", STEPS[name])
        self.assertIn("if: inputs.post-update-command != ''", STEPS[HOOK])
        self.assertIn("if: inputs.publication-mode == 'pull-request'", STEPS[FREEZE])
        self.assertIn("if: inputs.publication-mode == 'pull-request'", STEPS[PUBLISH])
        self.assertIn("if: inputs.publication-mode == 'direct'", STEPS[LEGACY])
        self.assertNotIn("continue-on-error:", SOURCE)
        self.assertNotIn("always()", SOURCE)
        for name in [PREFLIGHT, HOOK, CHANGES, FREEZE, DIRECT_FREEZE, EXPORT, APPLY, PUBLISH, LEGACY, CACHE_KEY, *NPM_UPDATES]:
            self.assertNotIn("${{", shell(name))
        inputs = SOURCE.split("    inputs:\n", 1)[1].split("    secrets:", 1)[0]
        for name, default in [("publication-mode", "direct"), ("post-update-command", "''"),
                              ("pull-request-label", "''"), ("runs-on", "ubuntu-latest"),
                              ("npm-package-exclusions", "typescript"),
                              ("nuget-package-exclusions", "StyleCop.Analyzers")]:
            # Input properties are more deeply indented; use the next top-level input.
            block = re.split(r"\n      [a-z]", inputs.split(f"      {name}:\n", 1)[1])[0]
            self.assertIn(f"default: {default}", block)
        self.assertIn("runs-on: ${{ inputs.runs-on }}", SOURCE)
        self.assertIn("token: ${{ github.token }}", STEPS["Checkout repository"])
        self.assertIn("token: ${{ secrets.PAT_WORKFLOWS || github.token }}", STEPS["Checkout publication base"])
        self.assertIsNone(re.search(r"^\s*permissions:", SOURCE, re.M))

    def test_shell_syntax(self):
        for name in [PREFLIGHT, HOOK, CHANGES, FREEZE, DIRECT_FREEZE, EXPORT, APPLY, PUBLISH, LEGACY, CACHE_KEY, *NPM_UPDATES]:
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

    def test_direct_default_preflight_only_records_git_base(self):
        self.env["PUBLICATION_MODE"] = "direct"
        self.assert_step_ok(PREFLIGHT)
        self.assertEqual(self.calls("gh"), [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.env["EXPECTED_BASE"], self.base)
        self.assertEqual(self.env["BASE_BRANCH"], "main")

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
        self.assertTrue((self.update_repo / "unrelated-before.tmp").exists())
        self.assertTrue((self.update_repo / "unrelated-output.tmp").exists())
        self.assertTrue((self.update_repo / ".ai-work/notes").exists())
        self.assertTrue((self.update_repo / "dist/cache").exists())
        self.assertFalse((self.repo / "unrelated-output.tmp").exists())
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

    def test_builds_commit_and_publication_are_gated_on_package_changes(self):
        gate = "steps.changes.outputs.changed == 'true'"
        for name in [FREEZE, *BUILDS, DIRECT_FREEZE]:
            with self.subTest(step=name):
                self.assertIn(gate, re.search(r"^        if: (.*)$", STEPS[name], re.M)[1])
        # Publication depends on a successfully exported, changed candidate.
        self.assertIn("if: needs.update.outputs.changed == 'true'", SOURCE)
        # A caller's hook can produce the only change, so it is never skipped as a no-op.
        self.assertNotIn("steps.changes", STEPS[HOOK])
        self.assertNotIn("if:", STEPS[CHANGES].split("run:", 1)[0])

    def test_no_package_changes_skips_builds_commit_and_publication(self):
        for mode in ("pull-request", "direct"):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                self.env.update(NO_UPDATES="1", PUBLICATION_MODE=mode)
                self.assertTrue(self.prepare(hook=""), self.last_result.stderr)
                self.assertEqual(self.package_changes, "false")
                self.assertIn("No package changes; skipping builds and publication.", self.last_result.stdout)
                self.assertFalse(self.changed)
                self.assertFalse(any(call[:2] in (["dotnet", "build"], ["yarn", "ci"], ["gradle", "build"],
                                                  ["mix", "compile"]) for call in self.calls()))
                self.assertFalse(any(c[:2] == ["git", "commit"] for c in self.calls()))
                self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
                self.assert_not_published()

    def test_package_change_detection_counts_tracked_staged_and_untracked_paths(self):
        for change in ("tracked", "staged", "untracked"):
            with self.subTest(change=change):
                self.git("reset", "--hard", self.base)
                self.git("clean", "-fdq")
                if change == "untracked":
                    (self.repo / "new-lockfile.json").write_text("{}\n")
                else:
                    (self.repo / "dependencies.txt").write_text("updated\n")
                if change == "staged":
                    self.git("add", "dependencies.txt")
                self.assert_step_ok(CHANGES)
                self.assertEqual(self.package_changes, "true")
                self.assertIn("changed 1 paths", self.last_result.stdout)

    def test_package_change_detection_ignores_ignored_output(self):
        (self.repo / "dist").mkdir()
        (self.repo / "dist/cache").write_text("restored, not a change")
        self.assert_step_ok(CHANGES)
        self.assertEqual(self.package_changes, "false")

    def test_package_change_detection_fails_closed_on_git_error(self):
        self.env["GIT_DIR"] = str(self.root / "missing.git")
        self.assertNotEqual(self.run_step(CHANGES), 0)
        self.assertIsNone(self.package_changes)

    def test_nuget_cache_is_pinned_restored_before_updates_and_saved_after_success(self):
        names = list(STEPS)
        self.assertLess(names.index("Setup .NET"), names.index(CACHE_KEY))
        for name in (CACHE_RESTORE, CACHE_SAVE):
            self.assertLess(names.index(CACHE_KEY), names.index(name))
            # Hash and restore before any install or update writes to the checkout.
            for later in ("Install NPM dependencies", "Install Mix dependencies", "Update NuGet packages"):
                self.assertLess(names.index(name), names.index(later))
        self.assertIn("if: steps.detect.outputs.update_nuget == 'true'", STEPS[CACHE_KEY])
        self.assertIn(f"uses: actions/cache/restore@{CACHE_ACTION_SHA}\n", STEPS[CACHE_RESTORE])
        self.assertIn(f"uses: actions/cache@{CACHE_ACTION_SHA}\n", STEPS[CACHE_SAVE])
        self.assertIn("lookup-only: true", STEPS[CACHE_SAVE])
        for name in (CACHE_RESTORE, CACHE_SAVE):
            with self.subTest(step=name):
                self.assertIn("if: steps.nuget_cache.outputs.enabled == 'true'", STEPS[name])
                self.assertIn("path: ${{ env.NUGET_PACKAGES || '~/.nuget/packages' }}\n", STEPS[name])
                self.assertIn("key: ${{ steps.nuget_cache.outputs.key }}", STEPS[name])
        self.assertIn("restore-keys: ${{ steps.nuget_cache.outputs.restore-key }}", STEPS[CACHE_RESTORE])
        # The same prefix and key files as the Cratis .NET package cache used by caller builds.
        self.assertIn("CACHE_KEY_PREFIX: nuget-v1-${{ runner.os }}-\n", STEPS[CACHE_KEY])
        self.assertIn("CACHE_KEY_HASH: ${{ hashFiles('**/packages.lock.json', '**/*.props', '**/*.targets', "
                      "'**/*.csproj', '**/[Nn]u[Gg]et.[Cc]onfig', '*.slnx', 'global.json') }}", STEPS[CACHE_KEY])
        # Saving is never forced: actions/cache saves in its post step only on success.
        self.assertNotIn("actions/cache/save", SOURCE)

    def test_self_hosted_runners_keep_nuget_packages_on_the_scratch_volume(self):
        names = list(STEPS)
        step = STEPS[PACKAGES_ON_SCRATCH]
        # Set before .NET or any cache step reads it; hosted runners keep ~/.nuget/packages.
        for later in ("Setup .NET", CACHE_KEY, CACHE_RESTORE, CACHE_SAVE, "Update NuGet packages"):
            self.assertLess(names.index(PACKAGES_ON_SCRATCH), names.index(later))
        self.assertIn("if: runner.environment == 'self-hosted'\n", step)
        self.assertIn('echo "NUGET_PACKAGES=$(dirname "$GITHUB_WORKSPACE")/.nuget-packages" >> "$GITHUB_ENV"', step)
        # actions/cache refuses a path containing '..'.
        self.assertNotIn("..", step.split("run:", 1)[1].replace(".nuget-packages", ""))

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        uses = re.findall(r"^\s+uses: (\S+)", SOURCE, re.M)
        self.assertGreater(len(uses), 5)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"@[0-9a-f]{40}$")

    def test_nuget_cache_key_is_exported_with_its_restore_prefix(self):
        self.env.update(CACHE_KEY_PREFIX="nuget-v1-Linux-", CACHE_KEY_HASH="abc123")
        self.assert_step_ok(CACHE_KEY)
        values = (self.root / "step-output").read_text().splitlines()
        self.assertEqual(values, ["enabled=true", "restore-key=nuget-v1-Linux-", "key=nuget-v1-Linux-abc123"])

    def test_nuget_cache_without_key_files_restores_without_the_cache(self):
        self.env.update(CACHE_KEY_PREFIX="nuget-v1-Linux-", CACHE_KEY_HASH="")
        self.assert_step_ok(CACHE_KEY)
        self.assertEqual((self.root / "step-output").read_text(), "enabled=false\n")
        self.assertIn("::notice::No NuGet cache key file matched", self.last_result.stdout)

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

    BOT = {"GIT_AUTHOR_NAME": "github-actions[bot]", "GIT_AUTHOR_EMAIL": "github-actions[bot]@users.noreply.github.com",
           "GIT_COMMITTER_NAME": "github-actions[bot]",
           "GIT_COMMITTER_EMAIL": "github-actions[bot]@users.noreply.github.com",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}

    def update_commit(self, files=None, tree=None, parent=None, identity=None, message="chore: update packages"):
        """A commit shaped like an earlier run's update: one commit with the workflow's identity."""
        parent = parent or self.base
        if tree is None:
            index = {"GIT_INDEX_FILE": str(self.root / "fixture-index")}
            self.git("read-tree", parent, env=index)
            for name, content in files.items():
                blob = self.git("hash-object", "-w", "--stdin", input=content)
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=index)
            tree = self.git("write-tree", env=index)
        return self.git("commit-tree", tree, "-p", parent, "-m", message, env=identity or self.BOT)

    def open_pr(self, number, commit=None, run=None, **fields):
        """Register an open PR and push its head branch to the caller's remote."""
        pr = {"number": number, "head_ref": f"automation/package-update-{run or number}-1", **fields}
        if commit:
            pr["head_sha"] = commit
            if pr.get("head_repo", "Cratis/Fixture") == "Cratis/Fixture":
                self.git("push", "origin", f"{commit}:refs/heads/{pr['head_ref']}")
        path = Path(self.env["OPEN_PRS"])
        prs = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps([*prs, pr]))
        return pr

    def identical_commit(self):
        return self.update_commit(tree=self.env["CANDIDATE_TREE"])

    def remote_branches(self):
        return self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname:short)").splitlines()

    def closed(self):
        path = Path(self.env["CLOSED_PRS"])
        return [json.loads(line)["number"] for line in path.read_text().splitlines()] if path.exists() else []

    def close_calls(self):
        # Calls are [program, *args]: gh api --method VERB PATH ...
        return [c for c in self.calls("gh") if c[2:4] in (["--method", "PATCH"], ["--method", "DELETE"])
                or (c[2:4] == ["--method", "POST"] and c[4].endswith("/comments"))]

    def test_identical_open_pr_suppresses_another_pr(self):
        self.assertTrue(self.prepare())
        self.open_pr(7, self.identical_commit())
        self.assert_step_ok(PUBLISH)
        self.assertIn("::notice::https://github.com/Cratis/Fixture/pull/7 already carries this exact package update",
                      self.last_result.stdout)
        self.assertFalse(self.pr.exists())
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
        self.assertFalse(any(c[:4] == ["gh", "api", "--method", "POST"] for c in self.calls()))
        self.assertEqual(self.close_calls(), [])
        self.assertNotIn("automation/package-update-123-1", self.remote_branches())
        self.assertIn("automation/package-update-7-1", self.remote_branches())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "main"), self.base)

    def test_identical_open_pr_is_kept_and_other_workflow_prs_are_superseded(self):
        self.assertTrue(self.prepare())
        self.open_pr(5, self.update_commit({"dependencies.txt": "older update\n"}))
        self.open_pr(6, self.identical_commit())
        self.open_pr(7, self.identical_commit())
        self.assert_step_ok(PUBLISH)
        self.assertFalse(self.pr.exists())
        self.assertEqual(self.closed(), [5, 6])
        branches = self.remote_branches()
        self.assertNotIn("automation/package-update-5-1", branches)
        self.assertNotIn("automation/package-update-6-1", branches)
        self.assertIn("automation/package-update-7-1", branches)
        comments = [c for c in self.close_calls() if c[3] == "POST"]
        self.assertEqual([c[4] for c in comments], ["repos/Cratis/Fixture/issues/5/comments",
                                                    "repos/Cratis/Fixture/issues/6/comments"])
        self.assertEqual([c[6] for c in comments], [
            "body=Superseded by https://github.com/Cratis/Fixture/pull/7, which carries the current package updates."] * 2)
        self.assertIn("Closed 2 superseded package-update PRs.", self.last_result.stdout)

    def test_different_update_opens_new_pr_then_closes_the_older_one(self):
        self.assertTrue(self.prepare())
        self.open_pr(5, self.update_commit({"dependencies.txt": "older update\n"}))
        self.assert_step_ok(PUBLISH)
        self.assertTrue(self.pr.exists())
        self.assertEqual(self.closed(), [5])
        branches = self.remote_branches()
        self.assertNotIn("automation/package-update-5-1", branches)
        self.assertIn("automation/package-update-123-1", branches)
        calls = self.calls()
        create = calls.index(next(c for c in calls if c[:3] == ["gh", "pr", "create"]))
        comment = calls.index(next(c for c in calls if c[4:5] == ["repos/Cratis/Fixture/issues/5/comments"]))
        close = calls.index(next(c for c in calls if c[2:4] == ["--method", "PATCH"]))
        delete = calls.index(next(c for c in calls if c[2:4] == ["--method", "DELETE"]))
        self.assertLess(create, comment)
        self.assertLess(comment, close)
        self.assertLess(close, delete)
        self.assertEqual(calls[comment][6], "body=Superseded by https://github.com/Cratis/Fixture/pull/1, "
                         "which carries the current package updates.")
        self.assertIn("Checked 1 open PRs; 1 are open package-update PRs from this workflow.", self.last_result.stdout)

    def test_pr_lookup_failure_fails_before_any_publication(self):
        for option in ("FAIL_PR_LIST", "FAIL_AFTER_PR_LIST_OUTPUT"):
            with self.subTest(option=option):
                self.setUp()
                self.assertTrue(self.prepare())
                self.open_pr(5, self.update_commit({"dependencies.txt": "older update\n"}))
                self.env[option] = "1"
                self.assertNotEqual(self.run_step(PUBLISH), 0)
                self.assertIn("Looking up open package-update PRs in Cratis/Fixture failed", self.last_result.stderr)
                self.assertFalse(self.pr.exists())
                self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
                self.assertFalse(any(c[:4] == ["gh", "api", "--method", "POST"] for c in self.calls()))
                self.assertEqual(self.close_calls(), [])
                self.assertEqual(self.remote_branches(), ["automation/package-update-5-1", "main"])

    def test_unexpected_pr_listing_fails_closed(self):
        for fields in ({"head_sha": "not-a-sha"}, {"head_sha": "a" * 40, "url": "https://github.com/Other/Fixture/pull/5"}):
            with self.subTest(fields=fields):
                self.setUp()
                self.assertTrue(self.prepare())
                self.open_pr(5, **fields)
                self.assertNotEqual(self.run_step(PUBLISH), 0)
                self.assertFalse(self.pr.exists())
                self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))

    def test_only_this_workflows_own_prs_are_compared_or_closed(self):
        self.assertTrue(self.prepare())
        same = self.identical_commit()
        person = {**self.BOT, "GIT_AUTHOR_NAME": "Person", "GIT_COMMITTER_NAME": "Person"}
        side = self.update_commit({"Dockerfile": "side\n"})
        self.open_pr(2, same, head_repo="Other/Fixture")  # fork with the run-owned name
        self.open_pr(3, same, base_ref="release")
        self.open_pr(4, same, head_ref="feature/package-update")
        self.open_pr(5, self.update_commit(tree=self.env["CANDIDATE_TREE"], identity=person))
        self.open_pr(6, self.update_commit(tree=self.env["CANDIDATE_TREE"], message="chore: tweak"))
        self.open_pr(8, self.update_commit(tree=self.env["CANDIDATE_TREE"], parent=side))
        self.open_pr(9, self.update_commit({"Dockerfile": "manual fix\n"}, parent=same, identity=person,
                                           message="Fix the image"))  # a person pushed on top
        before = self.remote_branches()
        self.assert_step_ok(PUBLISH)
        self.assertTrue(self.pr.exists())
        self.assertEqual(self.close_calls(), [])
        self.assertEqual(sorted(set(before) | {"automation/package-update-123-1"}), self.remote_branches())
        for number in (5, 6, 8, 9):
            self.assertIn(f"::notice::Leaving https://github.com/Cratis/Fixture/pull/{number} unchanged",
                          self.last_result.stdout)
        fetched = [c[-1] for c in self.calls("git") if c[1] == "fetch" and "origin" in c]
        self.assertEqual(fetched, [f"refs/heads/automation/package-update-{n}-1" for n in (5, 6, 8, 9)])

    def test_pr_changed_after_lookup_is_left_open(self):
        self.assertTrue(self.prepare())
        self.open_pr(5, self.update_commit({"dependencies.txt": "older update\n"}), current_sha="f" * 40)
        self.assert_step_ok(PUBLISH)
        self.assertTrue(self.pr.exists())
        self.assertIn("::warning::Leaving https://github.com/Cratis/Fixture/pull/5 unchanged", self.last_result.stdout)
        self.assertEqual(self.close_calls(), [])
        self.assertIn("automation/package-update-5-1", self.remote_branches())

    def test_failure_to_supersede_fails_the_job_after_publishing(self):
        for option, closed in (("FAIL_COMMENT", []), ("FAIL_CLOSE", []), ("FAIL_DELETE", [5])):
            with self.subTest(option=option):
                self.setUp()
                self.assertTrue(self.prepare())
                self.open_pr(5, self.update_commit({"dependencies.txt": "older update\n"}))
                self.env[option] = "1"
                self.assertNotEqual(self.run_step(PUBLISH), 0)
                self.assertTrue(self.pr.exists())
                self.assertIn("Superseding https://github.com/Cratis/Fixture/pull/5 failed", self.last_result.stderr)
                self.assertEqual(self.closed(), closed)
                self.assertIn("automation/package-update-5-1", self.remote_branches())
                self.assertNotIn("Closed https://github.com/Cratis/Fixture/pull/5", self.last_result.stdout)

    def test_fuse_refuses_to_supersede_more_than_ten_prs_before_publishing(self):
        self.assertTrue(self.prepare())
        for number in range(10, 21):
            self.open_pr(number, self.update_commit({"dependencies.txt": f"older update {number}\n"}))
        before = self.remote_branches()
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assertIn("Refusing to supersede 11 open package-update PRs in Cratis/Fixture (limit 10)",
                      self.last_result.stderr)
        self.assertFalse(self.pr.exists())
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
        self.assertFalse(any(c[:4] == ["gh", "api", "--method", "POST"] for c in self.calls()))
        self.assertEqual(self.close_calls(), [])
        self.assertEqual(self.remote_branches(), before)

    def test_fuse_counts_only_prs_to_close_when_one_is_kept(self):
        self.assertTrue(self.prepare())
        for number in range(10, 20):
            self.open_pr(number, self.update_commit({"dependencies.txt": f"older update {number}\n"}))
        self.open_pr(30, self.identical_commit())
        self.assert_step_ok(PUBLISH)
        self.assertFalse(self.pr.exists())
        self.assertEqual(self.closed(), list(range(10, 20)))
        self.assertIn("Closed 10 superseded package-update PRs.", self.last_result.stdout)

    def test_kept_pr_that_changed_after_lookup_fails_without_closing_or_publishing(self):
        for fields in ({"current_sha": "f" * 40}, {"closed": True}):
            with self.subTest(fields=fields):
                self.setUp()
                self.assertTrue(self.prepare())
                self.open_pr(6, self.update_commit({"dependencies.txt": "older update\n"}))
                self.open_pr(7, self.identical_commit(), **fields)
                self.assertNotEqual(self.run_step(PUBLISH), 0)
                self.assertIn("https://github.com/Cratis/Fixture/pull/7 carried this exact update at lookup but changed",
                              self.last_result.stderr)
                self.assertFalse(self.pr.exists())
                self.assertFalse(any(c[:2] == ["git", "push"] for c in self.calls()))
                self.assertEqual(self.close_calls(), [])
                self.assertEqual(self.closed(), [])
                reads = [c for c in self.calls("gh") if c[2:3] == ["repos/Cratis/Fixture/pulls/7"]]
                self.assertEqual(len(reads), 1)

    def test_invalid_run_identity_cannot_become_branch(self):
        self.assertTrue(self.prepare())
        self.env["GITHUB_RUN_ID"] = "123; touch injected"
        self.assertNotEqual(self.run_step(PUBLISH), 0)
        self.assert_not_published()


if __name__ == "__main__":
    unittest.main(verbosity=2)
