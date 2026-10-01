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

    def test_updates_only_exact_caret_tilde_and_zero_pin_tracking_ranges(self):
        self.run_script("sync")
        for package, expected in {"production-pin": "^2.0.0", "caret-tracking": "^2.1.3",
                                  "tilde-tracking": "~1.4.6", "zero-tracking": "^0.3.0"}.items():
            with self.subTest(package=package):
                self.assertEqual(self.manifest()["peerDependencies"][package], expected)

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
        expected = before.replace(b'"production-pin": "^1.2.3"', b'"production-pin": "^2.0.0"')
        for old, new in (("^2.1.2", "^2.1.3"),
                         ("~1.4.5", "~1.4.6"), ("^0.2.1", "^0.3.0")):
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

    def test_invalid_manifest_and_missing_snapshot_fail_visibly(self):
        (self.repo / "package.json").write_text("not json")
        self.assertNotEqual(self.run_script("snapshot", check=False).returncode, 0)
        self.snapshot.unlink()
        self.assertNotEqual(self.run_script("sync", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
