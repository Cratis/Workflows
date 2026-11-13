# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the shared dotnet-cache and yarn-cache composite actions.

Pull requests must only restore, and every step of one action must use the same path list
and key: a different path list is a different cache version, which is why pull request
restores never matched the caches main saved.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
ACTIONS = {
    "dotnet-cache": ROOT / ".github/actions/dotnet-cache/action.yml",
    "yarn-cache": ROOT / ".github/actions/yarn-cache/action.yml",
}
FIXTURE = ".github/scripts/tests/fixtures/cache-actions"
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
RESTORE_ACTION = "actions/cache/restore@"
SAVE_ACTION = "actions/cache@"
SAVE_CONDITION = "steps.resolve.outputs.mode == 'save'"
BASH = shutil.which("bash")


def load(name):
    return yaml.safe_load(ACTIONS[name].read_text())


def step(action, uses_prefix):
    matches = [s for s in action["runs"]["steps"] if s.get("uses", "").startswith(uses_prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected one {uses_prefix} step, found {len(matches)}")
    return matches[0]


def resolve(name, *, mode="auto", event="pull_request", ref="refs/pull/1/merge",
            default_branch="main", path=None, working_directory=None, key_hash="abc123", cwd=ROOT,
            yarn=None, exclude=None):
    """Run the action's production resolve script with the env GitHub would give it.

    PATH holds only a temporary bin directory, so the script sees no yarn unless the
    test supplies `yarn`, the body of a fake yarn shell script.
    """
    action = load(name)
    script = next(s for s in action["runs"]["steps"] if s.get("id") == "resolve")["run"]
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "github_output"
        output.write_text("")
        bin_dir = Path(temp) / "bin"
        bin_dir.mkdir()
        if yarn is not None:
            fake = bin_dir / "yarn"
            fake.write_text("#!/bin/sh\n" + yarn)
            fake.chmod(0o755)
        env = {
            "PATH": str(bin_dir),
            "GITHUB_OUTPUT": str(output),
            "INPUT_MODE": mode,
            "INPUT_PATH": path if path is not None else action["inputs"]["path"]["default"],
            "CACHE_KEY_PREFIX": f"{action['inputs']['key-prefix']['default']}-Linux-",
            "CACHE_KEY_HASH": key_hash,
            "EVENT_NAME": event,
            "GIT_REF": ref,
            "DEFAULT_BRANCH": default_branch,
        }
        if "exclude" in action["inputs"]:
            env["INPUT_EXCLUDE"] = exclude or ""
        if working_directory is not None:
            env["WORKING_DIRECTORY"] = working_directory
        result = subprocess.run([BASH, "-c", script], env=env, cwd=cwd,
                                capture_output=True, text=True)
        return result, parse_output(output.read_text())


def parse_output(text):
    values, lines = {}, iter(text.splitlines())
    for line in lines:
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            values[name] = "\n".join(iter(lambda: next(lines), delimiter))
        elif "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
    return values


class CacheActionShapeTests(unittest.TestCase):
    def test_actions_are_composite_with_the_documented_outputs(self):
        for name in ACTIONS:
            with self.subTest(name):
                action = load(name)
                self.assertEqual(action["runs"]["using"], "composite")
                self.assertEqual(set(action["outputs"]), {"cache-hit", "matched-key", "key", "mode"})
                self.assertIn("steps.restore.outputs.cache-hit", action["outputs"]["cache-hit"]["value"])
                self.assertEqual(action["outputs"]["matched-key"]["value"],
                                 "${{ steps.restore.outputs.cache-matched-key }}")
                self.assertEqual(action["inputs"]["mode"]["default"], "auto")

    def test_every_uses_is_pinned_by_full_commit_sha(self):
        for name, path in ACTIONS.items():
            uses = [s["uses"] for s in load(name)["runs"]["steps"] if "uses" in s]
            self.assertTrue(uses, name)
            for value in uses:
                with self.subTest(name=name, uses=value):
                    self.assertRegex(value, PINNED)
            for line in path.read_text().splitlines():
                if "uses:" in line:
                    self.assertRegex(line, r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_restore_always_runs_through_restore_only_action(self):
        for name in ACTIONS:
            with self.subTest(name):
                restore = step(load(name), RESTORE_ACTION)
                self.assertEqual(restore.get("id"), "restore")
                self.assertNotIn("if", restore)
                self.assertEqual(restore["with"]["restore-keys"], "${{ steps.resolve.outputs.restore-key }}")

    def test_save_step_runs_only_in_resolved_save_mode(self):
        for name in ACTIONS:
            with self.subTest(name):
                save = step(load(name), SAVE_ACTION)
                self.assertEqual(save["if"], SAVE_CONDITION)
                self.assertIs(save["with"]["lookup-only"], True)

    def test_save_and_restore_share_path_and_key(self):
        for name in ACTIONS:
            with self.subTest(name):
                action = load(name)
                restore, save = step(action, RESTORE_ACTION), step(action, SAVE_ACTION)
                self.assertEqual(restore["with"]["path"], "${{ steps.resolve.outputs.path }}")
                self.assertEqual(save["with"]["path"], restore["with"]["path"])
                self.assertEqual(restore["with"]["key"], "${{ steps.resolve.outputs.key }}")
                self.assertEqual(save["with"]["key"], restore["with"]["key"])

    def test_dotnet_defaults(self):
        inputs = load("dotnet-cache")["inputs"]
        self.assertEqual(inputs["path"]["default"], "~/.nuget/packages")
        self.assertEqual(inputs["key-prefix"]["default"], "nuget-v1")
        self.assertEqual(inputs["key-files"]["default"].split(), [
            "**/packages.lock.json", "**/*.props", "**/*.targets", "**/*.csproj",
            "**/[Nn]u[Gg]et.[Cc]onfig", "*.slnx", "global.json"])
        env = next(s for s in load("dotnet-cache")["runs"]["steps"] if s.get("id") == "resolve")["env"]
        self.assertEqual(env["CACHE_KEY_HASH"], "${{ hashFiles(inputs.key-files) }}")
        self.assertEqual(env["CACHE_KEY_PREFIX"], "${{ inputs.key-prefix }}-${{ runner.os }}-")
        self.assertNotIn("/obj", ACTIONS["dotnet-cache"].read_text())

    def test_yarn_defaults_never_cache_node_modules_or_the_lockfile(self):
        action = load("yarn-cache")
        self.assertEqual(action["inputs"]["working-directory"]["default"], ".")
        self.assertEqual(action["inputs"]["path"]["default"], "")
        self.assertEqual(action["inputs"]["key-prefix"]["default"], "yarn-v1")
        env = next(s for s in action["runs"]["steps"] if s.get("id") == "resolve")["env"]
        self.assertEqual(env["CACHE_KEY_HASH"],
                         "${{ hashFiles(format('{0}/yarn.lock', inputs.working-directory), "
                         "format('{0}/.yarnrc.yml', inputs.working-directory)) }}")
        self.assertNotIn("node_modules", yaml.safe_dump(action["runs"]))


class CacheModeResolutionTests(unittest.TestCase):
    RESTORE_ONLY = [
        ("pull_request", "refs/pull/7/merge"),
        ("pull_request_target", "refs/heads/main"),
        ("merge_group", "refs/heads/gh-readonly-queue/main/pr-7"),
        ("push", "refs/heads/feature"),
        ("push", "refs/tags/v1.0.0"),
        ("workflow_dispatch", "refs/heads/feature"),
        ("workflow_call", "refs/heads/main"),
    ]
    SAVE = [
        ("push", "refs/heads/main"),
        ("schedule", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/main"),
    ]

    def assert_mode(self, name, expected, **kwargs):
        result, output = resolve(name, working_directory=FIXTURE, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(output["mode"], expected, kwargs)

    def test_auto_restores_only_outside_the_default_branch(self):
        for name in ACTIONS:
            for event, ref in self.RESTORE_ONLY:
                with self.subTest(name=name, event=event, ref=ref):
                    self.assert_mode(name, "restore", event=event, ref=ref)

    def test_auto_saves_on_the_default_branch(self):
        for name in ACTIONS:
            for event, ref in self.SAVE:
                with self.subTest(name=name, event=event, ref=ref):
                    self.assert_mode(name, "save", event=event, ref=ref)

    def test_auto_uses_the_repository_default_branch(self):
        for name in ACTIONS:
            with self.subTest(name):
                self.assert_mode(name, "save", event="push", ref="refs/heads/trunk", default_branch="trunk")
                self.assert_mode(name, "restore", event="push", ref="refs/heads/main", default_branch="trunk")
                self.assert_mode(name, "restore", event="push", ref="refs/heads/", default_branch="")

    def test_explicit_modes_override_the_event(self):
        for name in ACTIONS:
            with self.subTest(name):
                self.assert_mode(name, "restore", mode="restore", event="push", ref="refs/heads/main")
                self.assert_mode(name, "save", mode="save", event="pull_request")

    def test_unknown_mode_cannot_run(self):
        for name in ACTIONS:
            with self.subTest(name):
                result, output = resolve(name, mode="always", working_directory=FIXTURE)
                self.assertEqual(result.returncode, 2)
                self.assertIn("mode must be auto, restore or save", result.stdout)
                self.assertEqual(output, {})

    def test_key_and_restore_key(self):
        _, output = resolve("dotnet-cache")
        self.assertEqual(output["key"], "nuget-v1-Linux-abc123")
        self.assertEqual(output["restore-key"], "nuget-v1-Linux-")
        self.assertEqual(output["path"], "~/.nuget/packages")
        _, output = resolve("yarn-cache", working_directory=FIXTURE)
        self.assertEqual(output["key"], "yarn-v1-Linux-abc123")
        self.assertEqual(output["restore-key"], "yarn-v1-Linux-")

    def test_dotnet_key_files_matching_nothing_cannot_run(self):
        result, output = resolve("dotnet-cache", key_hash="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No file matched key-files", result.stdout)
        self.assertEqual(output, {})

    def test_multi_line_paths_survive_the_output(self):
        _, output = resolve("dotnet-cache", path="~/.nuget/packages\n~/.local/share/NuGet")
        self.assertEqual(output["path"], "~/.nuget/packages\n~/.local/share/NuGet")


class DotnetExcludeTests(unittest.TestCase):
    DEFAULT_PATH = "~/.nuget/packages"

    def test_exclude_is_opt_in_and_empty_by_default(self):
        self.assertEqual(load("dotnet-cache")["inputs"]["exclude"]["default"], "")
        env = next(s for s in load("dotnet-cache")["runs"]["steps"] if s.get("id") == "resolve")["env"]
        self.assertEqual(env["INPUT_EXCLUDE"], "${{ inputs.exclude }}")

    def test_empty_or_unset_exclude_leaves_the_path_byte_identical(self):
        for exclude in [None, "", "\n", "  \n\t\n"]:
            with self.subTest(exclude=exclude):
                _, output = resolve("dotnet-cache", exclude=exclude)
                self.assertEqual(output["path"], self.DEFAULT_PATH)
                self.assertEqual(output["key"], "nuget-v1-Linux-abc123")
                self.assertEqual(output["restore-key"], "nuget-v1-Linux-")

    def test_exclude_adds_negated_lines_after_the_path(self):
        _, output = resolve("dotnet-cache", exclude="~/.nuget/packages/**/*.nupkg\n~/.nuget/packages/ilcompiler*")
        self.assertEqual(output["path"], "\n".join([
            self.DEFAULT_PATH, "!~/.nuget/packages/**/*.nupkg", "!~/.nuget/packages/ilcompiler*"]))

    def test_blank_lines_and_surrounding_whitespace_are_ignored(self):
        _, output = resolve("dotnet-cache", exclude="\n  \n  ~/.nuget/packages/**/*.nupkg  \n\n")
        self.assertEqual(output["path"], f"{self.DEFAULT_PATH}\n!~/.nuget/packages/**/*.nupkg")

    def test_an_existing_negation_is_not_doubled(self):
        # A second ! would cancel the first inside actions/glob and re-include the files.
        _, output = resolve("dotnet-cache", exclude="!~/.nuget/packages/**/*.nupkg\n! ~/x")
        self.assertEqual(output["path"], f"{self.DEFAULT_PATH}\n!~/.nuget/packages/**/*.nupkg\n!~/x")

    def test_exclude_composes_with_a_custom_path_and_leaves_the_keys_alone(self):
        _, output = resolve("dotnet-cache", path="a\nb", exclude="c")
        self.assertEqual(output["path"], "a\nb\n!c")
        self.assertEqual(output["key"], "nuget-v1-Linux-abc123")
        self.assertEqual(output["restore-key"], "nuget-v1-Linux-")


PRUNE_NAME = "Prune stale NuGet packages on a cache miss"
PRUNE_CONDITION = ("inputs.prune-on-miss == 'true' && steps.resolve.outputs.mode == 'save' "
                   "&& steps.restore.outputs.cache-hit != 'true' "
                   "&& github.event_name != 'pull_request' && github.event_name != 'pull_request_target'")


def condition_allows(condition, event_name):
    """Evaluate the prune condition for an event, assuming every other clause is true."""
    for clause in condition.split(" && "):
        left, op, right = clause.split(" ")
        if left == "github.event_name":
            matches = event_name == right.strip("'")
            if (op == "==") != matches:
                return False
    return True


def prune_step():
    return next((s for s in load("dotnet-cache")["runs"]["steps"] if s.get("name") == PRUNE_NAME), None)


def prune(home, *, nuget_packages=None, path="~/.nuget/packages"):
    """Run the production prune script against a fake HOME; PATH has no dotnet."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "INPUT_PATH": path}
    if nuget_packages is not None:
        env["NUGET_PACKAGES"] = nuget_packages
    return subprocess.run([BASH, "-c", prune_step()["run"]], env=env, capture_output=True, text=True)


class DotnetPruneOnMissTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.packages = self.home / ".nuget/packages"
        self.packages.mkdir(parents=True)
        (self.packages / "pkg/1.0.0").mkdir(parents=True)
        (self.packages / "pkg/1.0.0/a.nupkg").write_text("x")
        (self.packages / ".hidden").mkdir()
        (self.packages / ".hidden/f").write_text("x")
        (self.packages / "other").mkdir()

    def test_input_is_opt_in_and_defaults_to_false(self):
        self.assertEqual(load("dotnet-cache")["inputs"]["prune-on-miss"]["default"], "false")

    def test_step_runs_only_on_a_save_mode_miss_between_restore_and_save(self):
        steps = load("dotnet-cache")["runs"]["steps"]
        names = [s.get("name") for s in steps]
        self.assertEqual(names.index(PRUNE_NAME), names.index("Restore NuGet packages") + 1)
        self.assertEqual(names.index(PRUNE_NAME) + 1, names.index("Save NuGet packages at the end of the job"))
        self.assertEqual(prune_step()["if"], PRUNE_CONDITION)
        self.assertEqual(prune_step()["shell"], "bash")
        self.assertNotIn("${{", prune_step()["run"])

    def test_populated_folder_is_emptied_including_hidden_entries(self):
        result = prune(self.home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.packages.is_dir())
        self.assertEqual(list(self.packages.iterdir()), [])
        self.assertIn("3", result.stdout)

    def test_trailing_slash_nuget_packages_is_accepted(self):
        result = prune(self.home, nuget_packages=f"{self.packages}//")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.packages.iterdir()), [])

    def test_unsafe_nuget_packages_is_refused_and_nothing_is_deleted(self):
        other = Path(self.temp.name) / "elsewhere"
        other.mkdir()
        (other / "keep").write_text("x")
        for unsafe in [str(other), "/", str(self.home), f"{self.packages}/pkg", f"{self.packages}/../packages"]:
            with self.subTest(unsafe):
                result = prune(self.home, nuget_packages=unsafe)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("::notice", result.stdout)
                self.assertTrue((other / "keep").exists())
                self.assertTrue((self.packages / "pkg/1.0.0/a.nupkg").exists())
                self.assertTrue((self.packages / ".hidden/f").exists())

    def test_condition_rejects_pull_request_events(self):
        condition = prune_step()["if"]
        self.assertIn("github.event_name != 'pull_request'", condition)
        self.assertIn("github.event_name != 'pull_request_target'", condition)
        for event in ["pull_request", "pull_request_target"]:
            with self.subTest(event):
                self.assertFalse(condition_allows(condition, event))
        for event in ["push", "workflow_dispatch", "schedule"]:
            with self.subTest(event):
                self.assertTrue(condition_allows(condition, event))

    def test_symlinked_dot_nuget_is_refused_and_the_external_target_survives(self):
        external = Path(self.temp.name) / "external"
        (external / "packages/sentinel").mkdir(parents=True)
        shutil.rmtree(self.home / ".nuget")
        (self.home / ".nuget").symlink_to(external)
        result = prune(self.home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::notice", result.stdout)
        self.assertTrue((external / "packages/sentinel").is_dir())

    def test_symlinked_home_is_refused_and_the_target_survives(self):
        link = Path(self.temp.name) / "homelink"
        link.symlink_to(self.home)
        result = prune(link)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::notice", result.stdout)
        self.assertTrue((self.packages / "pkg/1.0.0/a.nupkg").exists())

    def test_carriage_return_in_nuget_packages_is_stripped(self):
        result = prune(self.home, nuget_packages=f"{self.packages}\r")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.packages.iterdir()), [])

    def test_non_default_path_is_a_noop(self):
        result = prune(self.home, path="~/.nuget/packages\n~/other")
        self.assertEqual(result.returncode, 0)
        self.assertIn("::notice", result.stdout)
        self.assertTrue((self.packages / "pkg/1.0.0/a.nupkg").exists())

    def test_missing_directory_is_a_noop(self):
        shutil.rmtree(self.packages)
        result = prune(self.home)
        self.assertEqual(result.returncode, 0)
        self.assertIn("::notice", result.stdout)
        self.assertFalse(self.packages.exists())


class YarnPathTests(unittest.TestCase):
    def test_default_path_is_the_yarn_cache_of_the_working_directory(self):
        _, output = resolve("yarn-cache", working_directory=FIXTURE)
        self.assertEqual(output["path"], f"{FIXTURE}/.yarn/cache")
        _, output = resolve("yarn-cache", working_directory=f"{FIXTURE}/")
        self.assertEqual(output["path"], f"{FIXTURE}/.yarn/cache")

    def test_root_working_directory_uses_a_relative_yarn_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "yarn.lock").write_text("# fixture\n")
            for working_directory in [".", "./", ""]:
                with self.subTest(working_directory=working_directory):
                    result, output = resolve("yarn-cache", working_directory=working_directory, cwd=temp)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(output["path"], ".yarn/cache")

    def test_default_path_is_the_cache_folder_yarn_reports(self):
        yarn = ('[ "$*" = "config get cacheFolder" ] || exit 1\n'
                'echo "$(pwd -P)/berry-cache"\n')
        result, output = resolve("yarn-cache", working_directory=FIXTURE, yarn=yarn)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(output["path"], f"{os.path.realpath(ROOT / FIXTURE)}/berry-cache")

    def test_falls_back_when_yarn_reports_no_cache_folder(self):
        for case, yarn in [("no yarn", None), ("classic yarn", "echo undefined\n"),
                           ("yarn fails", "echo /never >&2\nexit 1\n"), ("empty", "exit 0\n"),
                           ("multi-line", "echo /a\necho /b\n")]:
            with self.subTest(case):
                result, output = resolve("yarn-cache", working_directory=FIXTURE, yarn=yarn)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(output["path"], f"{FIXTURE}/.yarn/cache")

    def test_explicit_path_wins_without_asking_yarn(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "yarn-was-called"
            # PATH holds only the fake yarn, so record the call with a shell builtin.
            yarn = f': > "{marker}"\necho /detected/cache\n'
            for detected in [None, yarn]:
                with self.subTest(yarn_on_path=detected is not None):
                    _, output = resolve("yarn-cache", working_directory=FIXTURE,
                                        path="Source/.yarn/cache", yarn=detected)
                    self.assertEqual(output["path"], "Source/.yarn/cache")
            self.assertFalse(marker.exists())

    def test_missing_lockfile_cannot_run(self):
        result, output = resolve("yarn-cache", working_directory=".github/actions")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No yarn.lock", result.stdout)
        self.assertEqual(output, {})

    def test_unhashed_lockfile_cannot_run(self):
        result, output = resolve("yarn-cache", working_directory=FIXTURE, key_hash="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("hashFiles matched no lockfile", result.stdout)
        self.assertEqual(output, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
