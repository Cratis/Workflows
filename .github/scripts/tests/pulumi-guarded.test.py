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

    def run_guard(self, output, status=0, args=("preview", "--stack", "production", "--yes"), extra=None, path_dirs=(), action=None):
        recorded = self.work / "args.txt"
        env = {
            "PATH": os.pathsep.join([*map(str, path_dirs), str(FIXTURES), os.environ["PATH"]]),
            "TMPDIR": str(self.tmp),
            "PULUMI_FAKE_OUTPUT": str(output),
            "PULUMI_FAKE_STATUS": str(status),
            "PULUMI_FAKE_ARGS": str(recorded),
            **(extra or {}),
        }
        invocation = ["bash", str(SCRIPT), *args]
        if action is not None:
            command, stack, flags = action
            step = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]
            env.update({
                "PULUMI_GUARD_COMMAND": command,
                "PULUMI_GUARD_STACK": stack,
                "PULUMI_GUARD_ARGS": flags,
                "PULUMI_GUARD_SCRIPT": str(SCRIPT),
            })
            invocation = ["bash", "-c", step["run"]]
        process = subprocess.run(invocation, env=env, capture_output=True, text=True, check=False)
        return Run(process, recorded, self.tmp)

    def assert_withheld(self, run, label=None):
        self.assertEqual(run.code, 1, run.text)
        self.assertIn(ANNOTATION, run.text)
        self.assertNotIn(MARKER, run.text, "guard output must never carry fixture material")
        if label:
            self.assertIn(label, run.process.stdout + run.process.stderr)

    def assert_temp_clean(self, run):
        self.assertEqual(list(run.tmp.iterdir()), [], "temp file must be removed")


class AdmissionSpecs(GuardCase):
    REFUSAL = ANNOTATION + "Pulumi invocation refused.\n"
    REJECTED = (
        ("config", "get", "k"),
        ("--non-interactive", "config", "get", "k"),
        ("--color", "always", "config", "get", "k"),
        ("--cwd=d", "config", "get", "k"),
        ("config", "--stack", "s", "get", "k"),
        ("config", "--", "get", "k"),
        ("config", "get"),
        ("config", "get", "TESTONLYKEY", "--path"),
        ("config", "--color=always", "--stack=s", "get", "k"),
        ("--cwd", "d", "config", "get", "k"),
        ("-C", "d", "config", "-s", "s", "get", "k"),
        ("--", "config", "get", "k"),
        ("--unknown", "config", "get", "k"),
        ("--unknown=value", "preview"),
        ("config", "--unknown", "get", "k"),
        ("env", "--unknown=value", "open", "TESTONLYENV"),
        ("config", "--stack"),
        ("--color",),
        ("--non-interactive",),
        ("config get", "TESTONLYKEY"),
        ("",),
        ("--show-secrets", "preview"),
        ("preview", "--show-secrets"),
        ("preview", "--", "--show-secrets=true"),
        ("--color", "--show-secrets", "preview"),
        ("config", "--show-secrets=false"),
        ("config", "get", "--show-secrets", "k"),
        ("env", "open", "TESTONLYENV"),
        ("env", "get", "TESTONLYENV"),
        ("--color=always", "env", "--stack", "s", "open", "TESTONLYENV"),
        ("env", "--", "get", "TESTONLYENV"),
    )

    def assert_refused(self, run):
        self.assertEqual(run.code, 1, run.text)
        self.assertEqual(run.process.stdout, self.REFUSAL)
        self.assertEqual(run.process.stderr, "")
        self.assertFalse(run.args.exists(), "refusal must precede any Pulumi invocation")
        self.assert_temp_clean(run)

    def test_direct_rejections(self):
        output = self.work / "ordinary.txt"
        output.write_text("TESTONLY ordinary non-pattern value\n")
        for args in (*self.REJECTED, ()):
            with self.subTest(args=args):
                self.assert_refused(self.run_guard(output, args=args))

    def test_real_composite_rejections(self):
        for args in self.REJECTED:
            with self.subTest(args=args):
                self.assert_refused(self.run_guard(
                    self.fixture("clean.txt"), action=(args[0], "", " ".join(args[1:])),
                ))
        self.assert_refused(self.run_guard(
            self.fixture("clean.txt"), action=("config", "TESTONLYSTACK", "get TESTONLYKEY"),
        ))

    def test_global_options_do_not_hide_config_get(self):
        values = ("--color", "--cwd", "-C", "--memprofilerate", "--otel-traces", "--profiling", "--tracing", "--verbose", "-v")
        booleans = (
            "--disable-integrity-checking", "--emoji", "-e", "--fully-qualify-stack-names",
            "-Q", "--help", "-h", "--logflow", "--logtostderr", "--non-interactive", "--version",
        )
        prefixes = [(flag, "TESTONLYVALUE") for flag in values]
        prefixes += [(flag + "=TESTONLYVALUE",) for flag in values]
        prefixes += [(flag,) for flag in booleans]
        prefixes += [(flag + "=false",) for flag in booleans if flag.startswith("--")]
        for prefix in prefixes:
            args = (*prefix, "config", "get", "k")
            with self.subTest(args=args):
                self.assert_refused(self.run_guard(self.fixture("clean.txt"), args=args))
                self.assert_refused(self.run_guard(
                    self.fixture("clean.txt"), action=(args[0], "", " ".join(args[1:])),
                ))

    def test_admitted_commands_preserve_argv(self):
        cases = (
            ("config",),
            ("config", "--stack", "s"),
            ("config", "set", "TESTONLYKEY", "TESTONLYVALUE"),
            ("config", "--stack=s", "set", "TESTONLYKEY", "TESTONLYVALUE"),
            ("--non-interactive", "--color=always", "--cwd", "d", "preview", "--diff"),
            ("--verbose", "3", "--memprofilerate=1", "--tracing", "file:t", "preview"),
            ("--otel-traces=file:t", "--profiling", "p", "-e", "-Q", "-h", "--version", "version"),
            ("env", "ls"),
        )
        for args in cases:
            with self.subTest(args=args):
                run = self.run_guard(self.fixture("clean.txt"), args=args)
                self.assertEqual(run.code, 0, run.text)
                self.assertEqual(run.args.read_text().strip(), " ".join(args))
                self.assert_temp_clean(run)

    def test_real_composite_normal_commands(self):
        for command, stack, flags, expected in (
            ("up", "prod", "--refresh --diff", "up --stack prod --yes --skip-preview --non-interactive --refresh --diff"),
            ("preview", "", "--target urn:x", "preview --target urn:x"),
            ("config", "prod", "set TESTONLYKEY TESTONLYVALUE", "config --stack prod set TESTONLYKEY TESTONLYVALUE"),
        ):
            with self.subTest(command=command):
                run = self.run_guard(self.fixture("clean.txt"), action=(command, stack, flags))
                self.assertEqual(run.code, 0, run.text)
                self.assertEqual(run.args.read_text().strip(), expected)
                self.assert_temp_clean(run)

    def test_refresh_caller_preserves_import_pairs(self):
        args = (
            "refresh", "--stack", "production", "--yes", "--skip-preview",
            "--import-pending-creates", "placeholder-resource",
            "--import-pending-creates", "placeholder-id",
            "--import-pending-creates", "another-resource",
            "--import-pending-creates", "another-id",
        )
        run = self.run_guard(self.fixture("clean.txt"), args=args)
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.args.read_text().strip(), " ".join(args))
        self.assert_temp_clean(run)

    def test_destroy_caller_preserves_flags(self):
        args = ("destroy", "--yes", "--skip-preview", "--non-interactive")
        run = self.run_guard(self.fixture("clean.txt"), args=args)
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.args.read_text().strip(), " ".join(args))
        self.assert_temp_clean(run)

    def test_up_caller_with_automatic_action_flags(self):
        run = self.run_guard(self.fixture("clean.txt"), action=("up", "", ""))
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.args.read_text().strip(), "up --yes --skip-preview --non-interactive")
        self.assert_temp_clean(run)

    def test_up_caller_without_args(self):
        run = self.run_guard(self.fixture("clean.txt"), args=("up",))
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.args.read_text().strip(), "up")
        self.assert_temp_clean(run)

    def test_signals_remove_capture(self):
        fake = self.work / "bin"
        fake.mkdir()
        pulumi = fake / "pulumi"
        for signal, status in (("TERM", 143), ("INT", 130)):
            with self.subTest(signal=signal):
                pulumi.write_text(f'#!/usr/bin/env bash\nkill -{signal} "$PPID"\n')
                pulumi.chmod(0o755)
                run = self.run_guard(self.fixture("clean.txt"), path_dirs=[fake])
                self.assertEqual(run.code, status, run.text)
                self.assert_temp_clean(run)


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


class CertificateSpecs(GuardCase):
    def withheld_text(self, text):
        path = self.work / "blocks.txt"
        path.write_text(text)
        self.assert_withheld(self.run_guard(path), "certificate")

    def test_certificate_is_withheld(self):
        self.assert_withheld(self.run_guard(self.fixture("certificate.txt")), "certificate")

    def test_certificate_with_private_key_is_withheld(self):
        self.assert_withheld(self.run_guard(self.fixture("certificate-with-key.txt")), "private-key")

    def test_allow_file_environment_has_no_effect(self):
        allow = self.work / "allow.txt"
        allow.write_text(hashlib.sha256(CERT_BODY.encode()).hexdigest() + "\n")
        run = self.run_guard(self.fixture("certificate.txt"), extra={"PULUMI_GUARD_ALLOW_FILE": str(allow)})
        self.assert_withheld(run, "certificate")

    def test_nested_begin_is_withheld(self):
        begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        self.withheld_text(f"{begin}\nTESTONLYUNLISTED\n{begin}\n{CERT_BODY}\n{end}\n")

    def test_begin_without_end_is_withheld(self):
        begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        self.withheld_text(f"{begin}\n{CERT_BODY}\n{end}\n{begin}\nTESTONLYOPEN\n")

    def test_empty_certificate_body_is_withheld(self):
        begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
        self.withheld_text(f"{begin}\n{CERT_BODY}\n{end}\n{begin}\n{end}\n")

    def test_other_pem_certificate_label_is_withheld(self):
        self.withheld_text("-----BEGIN X509 CERTIFICATE-----\nTESTONLY\n-----END X509 CERTIFICATE-----\n")


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

    def run_body(self, stack, args, command="up"):
        recorded = Path(tempfile.mkdtemp()) / "args.txt"
        self.addCleanup(lambda: recorded.parent.exists() and __import__("shutil").rmtree(recorded.parent))
        stub = recorded.parent / "stub.sh"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$RECORD"\n')
        env = {
            "PATH": os.environ["PATH"],
            "PULUMI_GUARD_COMMAND": command,
            "PULUMI_GUARD_STACK": stack,
            "PULUMI_GUARD_ARGS": args,
            "PULUMI_GUARD_SCRIPT": str(stub),
            "RECORD": str(recorded),
        }
        subprocess.run(["bash", "-c", self.step["run"]], env=env, check=True)
        return recorded.read_text().splitlines()

    def test_run_body_splits_args_from_env(self):
        lines = self.run_body("prod", "--refresh --diff --target urn:x")
        self.assertEqual(
            lines,
            ["up", "--stack", "prod", "--yes", "--skip-preview", "--non-interactive", "--refresh", "--diff", "--target", "urn:x"],
        )

    def test_run_body_without_stack_or_args(self):
        lines = self.run_body("", "", command="preview")
        self.assertEqual(lines, ["preview"])

    def test_declares_the_interface(self):
        self.assertEqual(
            set(self.action["inputs"]),
            {"command", "stack-name", "work-dir", "args"},
        )


if __name__ == "__main__":
    unittest.main()
