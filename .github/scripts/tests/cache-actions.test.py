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


def load(name):
    return yaml.safe_load(ACTIONS[name].read_text())


def step(action, uses_prefix):
    matches = [s for s in action["runs"]["steps"] if s.get("uses", "").startswith(uses_prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected one {uses_prefix} step, found {len(matches)}")
    return matches[0]


def resolve(name, *, mode="auto", event="pull_request", ref="refs/pull/1/merge",
            default_branch="main", path=None, working_directory=None, key_hash="abc123", cwd=ROOT):
    """Run the action's production resolve script with the env GitHub would give it."""
    action = load(name)
    script = next(s for s in action["runs"]["steps"] if s.get("id") == "resolve")["run"]
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "github_output"
        output.write_text("")
        env = {
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(output),
            "INPUT_MODE": mode,
            "INPUT_PATH": path if path is not None else action["inputs"]["path"]["default"],
            "CACHE_KEY_PREFIX": f"{action['inputs']['key-prefix']['default']}-Linux-",
            "CACHE_KEY_HASH": key_hash,
            "EVENT_NAME": event,
            "GIT_REF": ref,
            "DEFAULT_BRANCH": default_branch,
        }
        if working_directory is not None:
            env["WORKING_DIRECTORY"] = working_directory
        result = subprocess.run(["bash", "-c", script], env=env, cwd=cwd,
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

    def test_multi_line_paths_survive_the_output(self):
        _, output = resolve("dotnet-cache", path="~/.nuget/packages\n~/.local/share/NuGet")
        self.assertEqual(output["path"], "~/.nuget/packages\n~/.local/share/NuGet")


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

    def test_explicit_path_wins(self):
        _, output = resolve("yarn-cache", working_directory=FIXTURE, path="Source/.yarn/cache")
        self.assertEqual(output["path"], "Source/.yarn/cache")

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
