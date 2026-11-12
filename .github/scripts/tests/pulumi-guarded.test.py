# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the pulumi-guarded script and composite action, using a fake pulumi."""
from pathlib import Path
import hashlib
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/pulumi-guarded.sh"
ACTION = ROOT / ".github/actions/pulumi-guarded/action.yml"
FIXTURES = ROOT / ".github/scripts/tests/fixtures/pulumi-guarded"
MARKER = "TESTONLY"
ANNOTATION = "::error title=Pulumi output withheld::"
CERT_BODY = "TESTONLYCERTBODY"

# Token-shaped values are assembled here so no complete token sits in the repository.
GENERATED = {
    "github-token": "token: " + "gh" + "p_" + MARKER * 5,
    "jwt": "jwt: " + "ey" + "J" + MARKER * 2 + "." + "ey" + "J" + MARKER * 2 + "." + MARKER * 2,
    "aws-access-key": "key: " + "AK" + "IA" + MARKER + "00000000",
}
FILES = {
    "private-key": "private-key.txt",
    "certificate": "certificate.txt",
    "kubeconfig-credential": "kubeconfig.txt",
    "base64-armour": "base64-armour.txt",
}


class Run:
    def __init__(self, process, args, tmp):
        self.process = process
        self.args = args
        self.tmp = tmp

    @property
    def code(self):
        return self.process.returncode

    @property
    def text(self):
        return self.process.stdout + self.process.stderr


class GuardCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.work = Path(self._dir.name)
        self.tmp = self.work / "tmp"
        self.tmp.mkdir()

    def fixture(self, name):
        return FIXTURES / name

    def generated(self, name):
        path = self.work / f"{name}.txt"
        path.write_text(GENERATED[name] + "\n")
        return path

    def run_guard(self, output, status=0, args=("preview", "--stack", "production", "--yes"), extra=None, path_dirs=()):
        recorded = self.work / "args.txt"
        env = {
            "PATH": os.pathsep.join([*map(str, path_dirs), str(FIXTURES), os.environ["PATH"]]),
            "TMPDIR": str(self.tmp),
            "PULUMI_FAKE_OUTPUT": str(output),
            "PULUMI_FAKE_STATUS": str(status),
            "PULUMI_FAKE_ARGS": str(recorded),
            **(extra or {}),
        }
        process = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False)
        return Run(process, recorded, self.tmp)

    def assert_withheld(self, run, label=None):
        self.assertEqual(run.code, 1, run.text)
        self.assertIn(ANNOTATION, run.text)
        self.assertNotIn(MARKER, run.text, "guard output must never carry fixture material")
        if label:
            self.assertIn(label, run.process.stdout + run.process.stderr)

    def assert_temp_clean(self, run):
        self.assertEqual(list(run.tmp.iterdir()), [], "temp file must be removed")


class LeakSpecs(GuardCase):
    def test_every_file_leak_class_is_withheld(self):
        for label, name in FILES.items():
            with self.subTest(label):
                run = self.run_guard(self.fixture(name))
                self.assert_withheld(run, label)
                self.assert_temp_clean(run)

    def test_every_token_class_is_withheld(self):
        for label in GENERATED:
            with self.subTest(label):
                run = self.run_guard(self.generated(label))
                self.assert_withheld(run, label)

    def test_leak_wins_over_pulumi_failure(self):
        run = self.run_guard(self.fixture("private-key.txt"), status=4)
        self.assert_withheld(run)

    def test_annotation_names_command(self):
        run = self.run_guard(self.fixture("private-key.txt"), args=("up", "--yes"))
        self.assertIn("'pulumi up'", run.text)


class PassThroughSpecs(GuardCase):
    def test_clean_output_is_printed(self):
        run = self.run_guard(self.fixture("clean.txt"))
        self.assertEqual(run.code, 0, run.text)
        self.assertIn("Previewing update (production)", run.process.stdout)
        self.assert_temp_clean(run)

    def test_pulumi_failure_status_and_output_survive(self):
        run = self.run_guard(self.fixture("clean.txt"), status=3)
        self.assertEqual(run.code, 3, run.text)
        self.assertIn("Previewing update (production)", run.process.stdout)
        self.assert_temp_clean(run)

    def test_args_pass_through(self):
        run = self.run_guard(self.fixture("clean.txt"))
        self.assertEqual(run.args.read_text().strip(), "preview --stack production --yes")


class FailClosedSpecs(GuardCase):
    def test_scan_error_withholds(self):
        fake = self.work / "bin"
        fake.mkdir()
        grep = fake / "grep"
        grep.write_text("#!/usr/bin/env bash\nexit 2\n")
        grep.chmod(0o755)
        run = self.run_guard(self.fixture("clean.txt"), path_dirs=[fake])
        self.assertEqual(run.code, 1, run.text)
        self.assertIn(ANNOTATION, run.text)
        self.assertIn("could not be checked", run.text)
        self.assertNotIn("Previewing update", run.text)
        self.assert_temp_clean(run)


class AllowListSpecs(GuardCase):
    def allow_file(self, *fingerprints):
        path = self.work / "allow.txt"
        path.write_text("# approved CA\n\n" + "\n".join(fingerprints) + "\n")
        return {"PULUMI_GUARD_ALLOW_FILE": str(path)}

    def test_certificate_fails_by_default(self):
        self.assert_withheld(self.run_guard(self.fixture("certificate.txt")), "certificate")

    def test_listed_certificate_is_tolerated(self):
        fingerprint = hashlib.sha256(CERT_BODY.encode()).hexdigest()
        run = self.run_guard(self.fixture("certificate.txt"), extra=self.allow_file(fingerprint))
        self.assertEqual(run.code, 0, run.text)

    def test_unlisted_certificate_is_withheld(self):
        run = self.run_guard(self.fixture("certificate.txt"), extra=self.allow_file("0" * 64))
        self.assert_withheld(run, "certificate")

    def test_allow_list_never_tolerates_a_private_key(self):
        fingerprint = hashlib.sha256(CERT_BODY.encode()).hexdigest()
        run = self.run_guard(self.fixture("certificate-with-key.txt"), extra=self.allow_file(fingerprint))
        self.assert_withheld(run, "private-key")


class MalformedCertificateSpecs(AllowListSpecs):
    def listed(self, text):
        path = self.work / "blocks.txt"
        path.write_text(text)
        fingerprint = hashlib.sha256(CERT_BODY.encode()).hexdigest()
        return self.run_guard(path, extra=self.allow_file(fingerprint))

    def test_nested_begin_is_withheld(self):
        begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        run = self.listed(f"{begin}\nTESTONLYUNLISTED\n{begin}\n{CERT_BODY}\n{end}\n")
        self.assert_withheld(run, "certificate")

    def test_begin_without_end_is_withheld(self):
        begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        run = self.listed(f"{begin}\n{CERT_BODY}\n{end}\n{begin}\nTESTONLYOPEN\n")
        self.assert_withheld(run, "certificate")

    def test_empty_certificate_body_is_withheld(self):
        run = self.listed("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
        self.assert_withheld(run, "certificate")

    def test_other_pem_certificate_label_is_withheld(self):
        run = self.listed("-----BEGIN X509 CERTIFICATE-----\nTESTONLYUNLISTED\n-----END X509 CERTIFICATE-----\n")
        self.assert_withheld(run, "certificate")


class ActionSpecs(unittest.TestCase):
    def setUp(self):
        self.action = yaml.safe_load(ACTION.read_text())
        self.step = self.action["runs"]["steps"][0]

    def test_no_input_expression_in_run_body(self):
        self.assertNotIn("${{", self.step["run"])

    def test_inputs_reach_the_script_through_env(self):
        self.assertEqual(self.step["env"]["PULUMI_GUARD_COMMAND"], "${{ inputs.command }}")
        self.assertEqual(self.step["working-directory"], "${{ inputs.work-dir }}")

    def test_runs_the_shared_script(self):
        self.assertIn("pulumi-guarded.sh", self.step["env"]["PULUMI_GUARD_SCRIPT"])
        self.assertIn("--yes --skip-preview --non-interactive", self.step["run"])

    def run_body(self, stack, args, allow="", command="up"):
        recorded = Path(tempfile.mkdtemp()) / "args.txt"
        self.addCleanup(lambda: recorded.parent.exists() and __import__("shutil").rmtree(recorded.parent))
        stub = recorded.parent / "stub.sh"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$RECORD"\nprintf "%s" "${PULUMI_GUARD_ALLOW_FILE:-}" > "$RECORD.allow"\n')
        env = {
            "PATH": os.environ["PATH"],
            "PULUMI_GUARD_COMMAND": command,
            "PULUMI_GUARD_STACK": stack,
            "PULUMI_GUARD_ARGS": args,
            "PULUMI_GUARD_ALLOW_INPUT": allow,
            "PULUMI_GUARD_SCRIPT": str(stub),
            "GITHUB_WORKSPACE": "/workspace",
            "RECORD": str(recorded),
        }
        subprocess.run(["bash", "-c", self.step["run"]], env=env, check=True)
        return recorded.read_text().splitlines(), Path(f"{recorded}.allow").read_text()

    def test_run_body_splits_args_from_env(self):
        lines, _ = self.run_body("prod", "--refresh --diff --target urn:x")
        self.assertEqual(
            lines,
            ["up", "--stack", "prod", "--yes", "--skip-preview", "--non-interactive", "--refresh", "--diff", "--target", "urn:x"],
        )

    def test_run_body_without_stack_or_args(self):
        lines, _ = self.run_body("", "", command="preview")
        self.assertEqual(lines, ["preview"])

    def test_run_body_resolves_relative_allow_file(self):
        _, allow = self.run_body("", "", allow="infra/allow.txt", command="preview")
        self.assertEqual(allow, "/workspace/infra/allow.txt")

    def test_declares_the_interface(self):
        self.assertEqual(
            set(self.action["inputs"]),
            {"command", "stack-name", "work-dir", "args", "allow-patterns-file"},
        )


if __name__ == "__main__":
    unittest.main()
