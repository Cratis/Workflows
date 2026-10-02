# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Exercise the peer synchronizer on actual package.json files, without network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/sync-package-update-peers.py"
FIXTURES = Path(__file__).parent / "fixtures/package-update-peers"


class PeerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peer-update-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(FIXTURES / "before", self.repo)
        self.snapshot = Path(self.temp.name) / "snapshot.json"
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True, env=self.env)
        self.run_script("snapshot")
        shutil.copytree(FIXTURES / "after", self.repo, dirs_exist_ok=True)

    def run_script(self, operation, check=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), operation, str(self.snapshot)],
                                cwd=self.repo, env=self.env, text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def manifest(self, path="package.json"):
        return json.loads((self.repo / path).read_text())

    def test_preserves_compatibility_claims_not_tracking_pins(self):
        before = self.manifest()
        self.run_script("sync")
        after = self.manifest()
        for package in ("react", "eslint", "primereact", "@cratis/arc", "wildcard", "unbounded",
                        "zero-unmatched", "ranged-pin", "unmatched", "peer-only", "unchanged"):
            with self.subTest(package=package):
                self.assertEqual(after["peerDependencies"][package], before["peerDependencies"][package])
        self.assertEqual(after["packageManager"], "npm@12.1.0")

    def test_updates_tracking_ranges_only_within_existing_compatibility(self):
        result = self.run_script("sync")
        for package, expected in {"production-pin": "^1.2.3", "caret-tracking": "^2.1.3",
                                  "tilde-tracking": "~1.4.6", "zero-tracking": "^0.2.1"}.items():
            with self.subTest(package=package):
                self.assertEqual(self.manifest()["peerDependencies"][package], expected)
        self.assertIn("::warning::package.json: production-pin peer ^1.2.3 unchanged; new pin 2.0.0", result.stdout)
        self.assertIn("::warning::package.json: zero-tracking peer ^0.2.1 unchanged; new pin 0.3.0", result.stdout)

    def pin_update(self, peer, new):
        path = self.repo / "package.json"
        manifest = {"devDependencies": {"tracking": peer[1:]}, "peerDependencies": {"tracking": peer}}
        path.write_text(json.dumps(manifest))
        self.run_script("snapshot")
        manifest["devDependencies"]["tracking"] = new
        path.write_text(json.dumps(manifest))
        return self.run_script("sync")

    def test_each_tracking_shape_accepts_only_satisfying_pins(self):
        for peer, new in (("^1.2.3", "1.3.0"), ("^0.2.1", "0.2.2"), ("~1.4.5", "1.4.6"),
                          ("^0.0.1-beta.1", "0.0.1"), ("^0.0.1+one", "0.0.1+two"),
                          ("~1.2.3-beta.2", "1.2.3-beta.10"), ("^1.2.3-beta.2", "1.2.3-beta.2.1")):
            with self.subTest(peer=peer, new=new):
                result = self.pin_update(peer, new)
                self.assertNotIn("::warning::", result.stdout)
                self.assertEqual(self.manifest()["peerDependencies"]["tracking"], peer[0] + new)

    def test_out_of_range_pins_warn_without_changing_the_peer_or_failing(self):
        for peer, new in (("^1.2.3", "2.0.0"), ("^0.2.1", "0.3.0"), ("^0.0.1", "0.0.2"),
                          ("~1.4.5", "1.5.0"), ("^1.2.3", "1.2.2"), ("~1.4.5", "1.4.4"),
                          ("^1.2.3", "1.3.0-beta.1"), ("^1.2.3-beta.1", "1.3.0-beta.1"),
                          ("^0.0.1-beta.2", "0.0.1-beta.1")):
            with self.subTest(peer=peer, new=new):
                result = self.pin_update(peer, new)
                self.assertIn(f"::warning::package.json: tracking peer {peer} unchanged; new pin {new}", result.stdout)
                self.assertEqual(self.manifest()["peerDependencies"]["tracking"], peer)
                self.assertEqual(self.manifest()["devDependencies"]["tracking"], new)

    def test_workspace_pins_are_local_and_formatting_is_preserved(self):
        path = "packages/client/package.json"
        expected = (self.repo / path).read_bytes().replace(b'"^1.0.0"', b'"^1.1.0"')
        self.run_script("sync")
        self.assertEqual((self.repo / path).read_bytes(), expected)
        self.assertEqual(self.manifest(path)["peerDependencies"]["production-pin"], "^1.2.3")

    def test_unchanged_pins_leave_manifests_byte_identical(self):
        shutil.copytree(FIXTURES / "before", self.repo, dirs_exist_ok=True)
        before = {path: path.read_bytes() for path in self.repo.rglob("package.json")}
        self.run_script("sync")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_new_nonexact_pin_and_changed_peer_are_not_synchronized(self):
        manifest = self.manifest()
        manifest["devDependencies"]["caret-tracking"] = "^2.1.3"
        manifest["peerDependencies"]["tilde-tracking"] = ">=1"
        (self.repo / "package.json").write_text(json.dumps(manifest))
        self.run_script("sync")
        self.assertEqual(self.manifest()["peerDependencies"]["caret-tracking"], "^2.1.2")
        self.assertEqual(self.manifest()["peerDependencies"]["tilde-tracking"], ">=1")

    def test_conflicting_production_and_dev_pins_are_not_guessed(self):
        snapshot = json.loads(self.snapshot.read_text())
        snapshot["package.json"]["dependencies"]["caret-tracking"] = "2.1.1"
        self.snapshot.write_text(json.dumps(snapshot))
        self.run_script("sync")
        self.assertEqual(self.manifest()["peerDependencies"]["caret-tracking"], "^2.1.2")

    def test_crlf_and_other_json_fields_are_preserved(self):
        path = self.repo / "package.json"
        before = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(before)
        self.run_script("sync")
        expected = before
        for old, new in (("^2.1.2", "^2.1.3"), ("~1.4.5", "~1.4.6")):
            expected = expected.replace(json.dumps(old).encode(), json.dumps(new).encode())
        # The unmatched peer uses ^1.0.0, not any of the replaced literals.
        self.assertEqual(path.read_bytes(), expected)

    def test_sync_is_idempotent(self):
        self.run_script("sync")
        before = (self.repo / "package.json").read_bytes()
        self.run_script("sync")
        self.assertEqual((self.repo / "package.json").read_bytes(), before)

    def test_symlinked_manifest_fails_without_writing_outside_checkout(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"name":"outside"}\n')
        path = self.repo / "package.json"
        path.unlink()
        path.symlink_to(outside)
        self.assertNotEqual(self.run_script("snapshot", check=False).returncode, 0)
        self.assertNotEqual(self.run_script("sync", check=False).returncode, 0)
        self.assertEqual(outside.read_text(), '{"name":"outside"}\n')

    def test_snapshot_ignores_malformed_manifest_outside_declared_workspaces(self):
        path = self.repo / "fixtures/template/package.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        self.run_script("snapshot")
        self.assertEqual(set(json.loads(self.snapshot.read_text())),
                         {"package.json", "packages/client/package.json"})
        self.run_script("sync")
        self.assertEqual(path.read_text(), "not json")

    def test_snapshot_resolves_workspace_array_and_packages_object(self):
        manifest = self.manifest()
        extra = self.repo / "packages/unrelated/package.json"
        extra.parent.mkdir()
        extra.write_text("not json")
        nested = self.repo / "packages/client/nested/package.json"
        nested.parent.mkdir()
        nested.write_text('{"name":"nested"}')
        for workspaces, expected in ((["packages/client"], {"packages/client/package.json"}),
                                     ({"packages": ["packages/client/**"]},
                                      {"packages/client/package.json", "packages/client/nested/package.json"})):
            with self.subTest(workspaces=workspaces):
                manifest["workspaces"] = workspaces
                (self.repo / "package.json").write_text(json.dumps(manifest))
                self.run_script("snapshot")
                self.assertEqual(set(json.loads(self.snapshot.read_text())), {"package.json", *expected})

    def test_snapshot_resolves_npm_style_globs_and_exclusions(self):
        manifest = self.manifest()
        for directory in ("shared", "template"):
            path = self.repo / "packages" / directory / "package.json"
            path.parent.mkdir()
            path.write_text("not json" if directory == "template" else '{"name":"shared"}')
        for workspaces in (["packages/{client,shared}"], ["packages/@(client|shared)"],
                           ["packages/*", "!packages/template"]):
            with self.subTest(workspaces=workspaces):
                manifest["workspaces"] = workspaces
                (self.repo / "package.json").write_text(json.dumps(manifest))
                self.run_script("snapshot")
                self.assertEqual(set(json.loads(self.snapshot.read_text())),
                                 {"package.json", "packages/client/package.json", "packages/shared/package.json"})

    def test_snapshot_ignores_installed_packages_in_workspace_globs(self):
        manifest = self.manifest()
        manifest["workspaces"] = ["packages/**"]
        (self.repo / "package.json").write_text(json.dumps(manifest))
        for directory in ("node_modules", ".pnpm-store"):
            path = self.repo / "packages/client" / directory / "dependency/package.json"
            path.parent.mkdir(parents=True)
            path.write_text("not json")
        self.run_script("snapshot")
        self.assertEqual(set(json.loads(self.snapshot.read_text())),
                         {"package.json", "packages/client/package.json"})

    def test_non_object_manifests_are_skipped_with_notice_at_snapshot_and_sync(self):
        path = self.repo / "packages/client/package.json"
        for value in (None, [], "fixture", 42):
            with self.subTest(value=value):
                before = self.snapshot.read_text()
                path.write_text(json.dumps(value))
                result = self.run_script("snapshot")
                self.assertIn("::notice::Skipping non-object manifest: packages/client/package.json", result.stdout)
                self.assertNotIn("packages/client/package.json", json.loads(self.snapshot.read_text()))
                self.snapshot.write_text(before)
                result = self.run_script("sync")
                self.assertIn("::notice::Skipping non-object manifest: packages/client/package.json", result.stdout)
                self.assertEqual(json.loads(path.read_text()), value)

    def test_null_and_non_object_dependency_sections_are_treated_as_empty(self):
        for section in ("peerDependencies", "dependencies", "devDependencies"):
            for value in (None, [], "fixture", 42):
                for operation in ("snapshot", "sync"):
                    with self.subTest(section=section, value=value, operation=operation):
                        shutil.copytree(FIXTURES / "before", self.repo, dirs_exist_ok=True)
                        self.run_script("snapshot")
                        manifest = self.manifest()
                        manifest[section] = value
                        (self.repo / "package.json").write_text(json.dumps(manifest))
                        if operation == "snapshot":
                            self.run_script("snapshot")
                            shutil.copytree(FIXTURES / "after", self.repo, dirs_exist_ok=True)
                        self.run_script("sync")

    def test_utf8_bom_is_accepted_and_preserved_when_synchronizing(self):
        path = self.repo / "package.json"
        shutil.copytree(FIXTURES / "before", self.repo, dirs_exist_ok=True)
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.run_script("snapshot")
        shutil.copytree(FIXTURES / "after", self.repo, dirs_exist_ok=True)
        before = b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(before)
        self.run_script("sync")
        expected = before.replace(b'"^2.1.2"', b'"^2.1.3"').replace(b'"~1.4.5"', b'"~1.4.6"')
        self.assertEqual(path.read_bytes(), expected)

    def test_symlinked_non_manifest_file_under_recursive_workspace_glob_is_skipped(self):
        outside = Path(self.temp.name) / "README.md"
        outside.write_text("outside documentation")
        (self.repo / "packages/client/README.md").symlink_to(outside)
        manifest = self.manifest()
        manifest["workspaces"] = ["packages/**/*"]
        (self.repo / "package.json").write_text(json.dumps(manifest))
        self.run_script("snapshot")
        self.assertEqual(set(json.loads(self.snapshot.read_text())),
                         {"package.json", "packages/client/package.json"})
        self.assertEqual(outside.read_text(), "outside documentation")

    def test_negated_workspace_pattern_over_symlink_is_only_an_exclusion(self):
        outside = Path(self.temp.name) / "corpus"
        outside.mkdir()
        (outside / "package.json").write_text("not json")
        (self.repo / "packages/client/corpus").symlink_to(outside, target_is_directory=True)
        manifest = self.manifest()
        for exclusion in ("!packages/client/corpus", "!packages/client/corpus/**"):
            with self.subTest(exclusion=exclusion):
                manifest["workspaces"] = ["packages/**/*", exclusion]
                (self.repo / "package.json").write_text(json.dumps(manifest))
                self.run_script("snapshot")
                self.assertEqual(set(json.loads(self.snapshot.read_text())),
                                 {"package.json", "packages/client/package.json"})
        self.assertEqual((outside / "package.json").read_text(), "not json")

    def test_symlinked_workspace_manifest_is_still_refused(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"name":"outside"}\n')
        path = self.repo / "packages/client/package.json"
        path.unlink()
        path.symlink_to(outside)
        for operation in ("snapshot", "sync"):
            with self.subTest(operation=operation):
                result = self.run_script(operation, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing symlinked manifest: packages/client/package.json", result.stderr)
        self.assertEqual(outside.read_text(), '{"name":"outside"}\n')

    def test_symlinked_workspace_directory_is_skipped(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "package.json").write_text('{"name":"outside"}')
        (self.repo / "packages/link").symlink_to(outside, target_is_directory=True)
        manifest = self.manifest()
        for pattern in ("packages/*", "packages/**/*", "packages/link"):
            with self.subTest(pattern=pattern):
                manifest["workspaces"] = ["packages/client", pattern]
                (self.repo / "package.json").write_text(json.dumps(manifest))
                self.run_script("snapshot")
                self.assertEqual(set(json.loads(self.snapshot.read_text())),
                                 {"package.json", "packages/client/package.json"})
        self.assertEqual((outside / "package.json").read_text(), '{"name":"outside"}')

    def test_positive_workspace_patterns_outside_checkout_are_still_refused(self):
        manifest = self.manifest()
        for pattern in ("../outside/*", str(self.repo / "packages/*")):
            with self.subTest(pattern=pattern):
                manifest["workspaces"] = [pattern]
                (self.repo / "package.json").write_text(json.dumps(manifest))
                result = self.run_script("snapshot", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing manifest outside checkout", result.stderr)

    def test_invalid_manifest_and_missing_snapshot_fail_visibly(self):
        (self.repo / "package.json").write_text("not json")
        self.assertNotEqual(self.run_script("snapshot", check=False).returncode, 0)
        self.snapshot.unlink()
        self.assertNotEqual(self.run_script("sync", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
