# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline checks of the pulumi-guarded script and composite action, using a fake pulumi."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
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
        self.platform_dirs = []
        if sys.platform == "darwin":
            # BSD mv lacks -T. Linux CI exercises the real GNU command; locally
            # os.replace supplies the same no-target-directory rename semantics.
            platform_bin = self.work / "platform-bin"
            platform_bin.mkdir()
            mv = platform_bin / "mv"
            mv.write_text(
                f'#!{sys.executable}\nimport os, sys\n'
                'if len(sys.argv) != 6 or sys.argv[1:4] != ["-f", "-T", "--"]: sys.exit(2)\n'
                'os.replace(sys.argv[4], sys.argv[5])\n'
            )
            mv.chmod(0o755)
            self.platform_dirs.append(platform_bin)

    def fixture(self, name):
        return FIXTURES / name

    def generated(self, name):
        path = self.work / f"{name}.txt"
        path.write_text(GENERATED[name] + "\n")
        return path

    def run_guard(self, output, status=0, args=("preview", "--stack", "production", "--yes"), extra=None, path_dirs=(), action=None):
        recorded = self.work / "args.txt"
        env = {
            "PATH": os.pathsep.join([*map(str, (*path_dirs, *self.platform_dirs)), str(FIXTURES), os.environ["PATH"]]),
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
        ("config", "--unknown", "get", "k"),
        ("env", "--unknown=value", "open", "TESTONLYENV"),
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
        ("esc", "open", "x"),
        ("esc", "get", "x", "y"),
        ("env", "run", "x", "--", "cmd"),
        ("esc", "run", "x", "--", "cmd"),
        ("--non-interactive", "esc", "open", "x"),
        ("--color", "always", "esc", "get", "x", "y"),
        ("--cwd=d", "esc", "run", "x", "--", "cmd"),
        ("config", "-s", "s", "get", "k"),
        ("config", "--config-file", "f.yaml", "get", "k"),
        ("config", "--json", "--open=false", "get", "k"),
        ("env", "--env", "x", "run", "--", "cmd"),
        ("esc", "--env=x", "open"),
        ("esc", "--", "run", "x", "--", "cmd"),
        ("esc", "--env", "x", "--unknown", "open"),
        ("-Cdir", "config", "get", "k"),
        ("-eh", "esc", "open", "x"),
        ("config", "-js", "s", "get", "k"),
        ("config", "-sstack", "get", "k"),
        ("esc", "-hQ", "run", "x", "--", "cmd"),
        ("esc", "ls", "--show-secrets=false"),
        ("preview", "--", "config", "set", "get"),
        ("preview", "env", "ls", "--", "open"),
        ("preview", "esc", "list", "--", "run"),
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
        for args in self.REJECTED:
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

    def test_option_compatibility(self):
        cases = (
            ("config", "-ss", "set", "--help"),
            ("-v0", "config", "set", "--help"),
            ("-e=false", "config", "set", "--help"),
            ("--help",),
            ("--unknown-flag", "up"),
            ("--unknown=value", "preview"),
            ("config", "--stack"),
            ("--color",),
            ("--non-interactive",),
            ("config get", "TESTONLYKEY"),
            ("",),
            ("config", "--env", "x", "set", "k", "v"),
            ("env", "--config-file=f.yaml", "ls"),
            ("esc", "--env"),
            ("config", "--config-file", "f.yaml", "set", "k", "v"),
            ("config", "--config-file=f.yaml", "set", "--help"),
            ("config", "--config-file", "f.yaml", "set", "--help"),
            ("config", "--stack", "s", "set", "k", "v"),
            ("config", "-s", "s", "set", "k", "v"),
            ("config", "-s=s", "set", "k", "v"),
            ("config", "--json", "set", "k", "v"),
            ("config", "-j", "--open", "set", "k", "v"),
            ("config", "--json=false", "--open=true", "set", "k", "v"),
            ("env", "--env", "x", "ls"),
            ("env", "--env=x", "list"),
            ("esc", "--env", "x", "ls"),
            ("--non-interactive", "esc", "--env=x", "list", "--help"),
        )
        for args in cases:
            with self.subTest(args=args):
                for action in (None, (args[0], "", " ".join(args[1:]))):
                    run = self.run_guard(self.fixture("clean.txt"), args=args, action=action)
                    self.assertEqual(run.code, 0, run.text)
                    self.assertEqual(run.args.read_text().strip(), " ".join(args))
                    self.assert_temp_clean(run)

    def test_scoped_options_do_not_hide_read_subcommands(self):
        config_options = (
            ("--config-file", "f.yaml"), ("--config-file=f.yaml",),
            ("--stack", "s"), ("--stack=s",), ("-s", "s"),
            ("--json",), ("-j",), ("--open",), ("--open=false",),
        )
        cases = [("config", *options, "get", "k") for options in config_options]
        cases += [
            (command, *options, subcommand, "x")
            for command in ("env", "esc")
            for options in (("--env", "x"), ("--env=x",))
            for subcommand in ("open", "get", "run")
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assert_refused(self.run_guard(self.fixture("clean.txt"), args=args))
                self.assert_refused(self.run_guard(
                    self.fixture("clean.txt"), action=(args[0], "", " ".join(args[1:])),
                ))

    def test_admitted_commands_preserve_argv(self):
        cases = (
            (),
            ("get", "config", "set"),
            ("open", "env", "ls"),
            ("run", "esc", "ls"),
            ("config", "--option=get", "set", "k", "v"),
            ("env", "--env=open", "ls"),
            ("esc", "get-value"),
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

    def test_annotation_does_not_echo_option_values_or_invalid_labels(self):
        for first in ("--tracing=TESTONLYARG", "up\nTESTONLYARG"):
            with self.subTest(first=first):
                run = self.run_guard(self.fixture("private-key.txt"), args=(first, "up"))
                self.assert_withheld(run)
                self.assertIn("'pulumi command'", run.text)
                self.assertNotIn("TESTONLYARG", run.text)
                self.assert_temp_clean(run)


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
        self.assertEqual(self.step["env"]["PULUMI_GUARD_OUTPUT_FILE"], "${{ inputs.output-file }}")
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
            {"command", "stack-name", "work-dir", "args", "output-file"},
        )


class OutputFileAdmissionSpecs(AdmissionSpecs):
    def run_guard(self, *args, extra=None, **kwargs):
        (self.work / "result.txt").write_text("stale")
        return super().run_guard(*args, extra={
            "GITHUB_WORKSPACE": str(self.work),
            "PULUMI_GUARD_OUTPUT_FILE": "result.txt",
            **(extra or {}),
        }, **kwargs)

    def assert_refused(self, run):
        super().assert_refused(run)
        self.assertFalse((self.work / "result.txt").exists())


class OutputFileSpecs(GuardCase):
    def run_action(self, stdout="clean.txt", stderr=None, status=0, destination="result.txt",
                   command="stack", args="output clusterId", output_mode=True, path_dirs=(), extra=None):
        step = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]
        self.destination = self.work / destination
        recorded = self.work / "args.txt"
        env = {
            "PATH": os.pathsep.join([*map(str, (*path_dirs, *self.platform_dirs)), str(FIXTURES), os.environ["PATH"]]),
            "TMPDIR": str(self.tmp),
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_WORKSPACE": str(self.work),
            "PULUMI_GUARD_COMMAND": command,
            "PULUMI_GUARD_STACK": "",
            "PULUMI_GUARD_ARGS": args,
            "PULUMI_GUARD_SCRIPT": str(SCRIPT),
            "PULUMI_FAKE_OUTPUT": str(self.fixture(stdout)),
            "PULUMI_FAKE_STDERR": str(self.fixture(stderr)) if stderr else "/dev/null",
            "PULUMI_FAKE_STATUS": str(status),
            "PULUMI_FAKE_ARGS": str(recorded),
            **(extra or {}),
        }
        if output_mode:
            env["PULUMI_GUARD_OUTPUT_FILE"] = str(destination)
        process = subprocess.run(["bash", "-c", step["run"]], env=env,
                                 cwd=self.tmp, capture_output=True, text=True, check=False)
        return Run(process, recorded, self.tmp)

    def test_absent_input_keeps_stdout_and_stderr_in_log(self):
        run = self.run_action(stderr="clean.txt", output_mode=False)
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.text, self.fixture("clean.txt").read_text() * 2)
        self.assertFalse(self.destination.exists())
        self.assert_temp_clean(run)

    def test_clean_stdout_is_exact_private_file_not_log(self):
        run = self.run_action()
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.text, "")
        self.assertEqual(self.destination.read_bytes(), self.fixture("clean.txt").read_bytes())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assert_temp_clean(run)
        self.assertEqual(list(self.work.glob(".pulumi-guarded.*")), [])

    def test_stdout_bytes_and_empty_output_replace_stale_file(self):
        source = self.work / "stdout.txt"
        for content in (b"TESTONLY\x00exact\r\nno trailing newline", b""):
            with self.subTest(content=content):
                source.write_bytes(content)
                (self.work / "result.txt").write_text("stale")
                run = self.run_action(stdout=source)
                self.assertEqual(run.code, 0, run.text)
                self.assertEqual(run.text, "")
                self.assertEqual(self.destination.read_bytes(), content)
                self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
                self.assert_temp_clean(run)

    def test_clean_stderr_is_logged_separately(self):
        run = self.run_action(stderr="clean.txt")
        self.assertEqual(run.code, 0, run.text)
        self.assertEqual(run.process.stdout, "")
        self.assertEqual(run.process.stderr, self.fixture("clean.txt").read_text())
        self.assertEqual(self.destination.read_bytes(), self.fixture("clean.txt").read_bytes())
        self.assert_temp_clean(run)

    def test_every_secret_class_in_either_stream_withholds_stale_file(self):
        samples = {label: self.fixture(name) for label, name in FILES.items()}
        samples.update({label: self.generated(label) for label in GENERATED})
        for label, sample in samples.items():
            for stream in ("stdout", "stderr"):
                with self.subTest(label=label, stream=stream):
                    (self.work / "result.txt").write_text("stale")
                    run = self.run_action(**{stream: sample})
                    self.assert_withheld(run, label)
                    self.assertFalse(self.destination.exists())
                    self.assert_temp_clean(run)

    def test_failure_removes_stale_file_and_preserves_exit_code(self):
        (self.work / "result.txt").write_text("stale")
        run = self.run_action(status=23, stderr="clean.txt")
        self.assertEqual(run.code, 23, run.text)
        self.assertEqual(run.process.stdout, "")
        self.assertEqual(run.process.stderr, self.fixture("clean.txt").read_text())
        self.assertFalse(self.destination.exists())
        self.assert_temp_clean(run)

    def test_scan_error_in_either_stream_withholds_stale_file(self):
        fake = self.work / "bin"
        fake.mkdir()
        grep = fake / "grep"
        stdout, stderr = self.work / "stdout.txt", self.work / "stderr.txt"
        stdout.write_text("TESTONLYstdout")
        stderr.write_text("TESTONLYstderr")
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                grep.write_text(f'#!/usr/bin/env bash\n[ "$(cat "$4")" = TESTONLY{stream} ] && exit 2\nexit 1\n')
                grep.chmod(0o755)
                (self.work / "result.txt").write_text("stale")
                run = self.run_action(stdout=stdout, stderr=stderr, path_dirs=[fake])
                self.assert_withheld(run)
                self.assertIn("could not be checked", run.text)
                self.assertFalse(self.destination.exists())
                self.assert_temp_clean(run)

    def test_read_commands_reach_pulumi(self):
        for command, args in (("stack", "output clusterId"), ("preview", "--json")):
            with self.subTest(command=command):
                run = self.run_action(command=command, args=args)
                self.assertEqual(run.code, 0, run.text)
                self.assertEqual(run.args.read_text().strip(), f"{command} {args}")

    def test_relative_path_is_workspace_relative_not_work_dir(self):
        run = self.run_action(destination="relative.txt")
        self.assertEqual(run.code, 0, run.text)
        self.assertTrue((self.work / "relative.txt").is_file())
        self.assertFalse((self.tmp / "relative.txt").exists())
        self.assert_temp_clean(run)

    def assert_publication_race_fails(self, mutation):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name)
        protected = outside / "protected.txt"
        protected.write_text("untouched")
        parent = self.work / "publish"
        parent.mkdir()
        destination = parent / "result.txt"
        destination.write_text("stale")
        run = self.run_action(destination="publish/result.txt", extra={
            "PULUMI_FAKE_MUTATION": mutation,
            "PULUMI_FAKE_PARENT": str(parent),
            "PULUMI_FAKE_DESTINATION": str(destination),
            "PULUMI_FAKE_TARGET": str(outside if mutation == "parent-symlink" else protected),
        })
        self.assertEqual(run.code, 1, run.text)
        self.assertTrue(run.args.exists(), "the mutation must happen during Pulumi")
        self.assertEqual(run.process.stdout, "")
        self.assertEqual(run.process.stderr, ANNOTATION + "Pulumi output file could not be published.\n")
        self.assertEqual(protected.read_text(), "untouched")
        self.assertEqual(list(outside.iterdir()), [protected])
        self.assertEqual(list(self.work.rglob(".pulumi-guarded.*")), [])
        self.assert_temp_clean(run)
        return parent, destination

    def test_parent_swapped_for_outside_symlink_before_publish_is_refused(self):
        parent, destination = self.assert_publication_race_fails("parent-symlink")
        self.assertTrue(parent.is_symlink())
        self.assertFalse(destination.exists())
        self.assertFalse((self.work / "publish.saved" / "result.txt").exists())

    def test_destination_swapped_for_symlink_before_publish_is_refused(self):
        _, destination = self.assert_publication_race_fails("destination-symlink")
        self.assertTrue(destination.is_symlink())

    def test_destination_swapped_for_directory_before_publish_is_refused(self):
        _, destination = self.assert_publication_race_fails("destination-directory")
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])

    def test_signals_remove_stale_output_and_all_capture_files(self):
        for signal, status in (("TERM", 143), ("INT", 130)):
            with self.subTest(signal=signal):
                (self.work / "result.txt").write_text("stale")
                run = self.run_action(extra={"PULUMI_FAKE_SIGNAL": signal})
                self.assertEqual(run.code, status, run.text)
                self.assertEqual(run.text, "")
                self.assertFalse(self.destination.exists())
                self.assert_temp_clean(run)
                self.assertEqual(list(self.work.rglob(".pulumi-guarded.*")), [])

    def test_invalid_paths_are_rejected_before_running(self):
        alias = self.work / "alias"
        alias.symlink_to(self.work.parent, target_is_directory=True)
        target = self.work / "target.txt"
        target.write_text("untouched")
        (self.work / "link.txt").symlink_to(target)
        (self.work / "dangling.txt").symlink_to(self.work / "missing.txt")
        for destination in (self.work, self.work / "result.txt", self.tmp / "result.txt",
                            "../outside.txt", "tmp/../result.txt", "alias/outside.txt",
                            "link.txt", "dangling.txt", "tmp", "missing/result", ".", " "):
            with self.subTest(destination=destination):
                run = self.run_action(destination=destination)
                self.assertEqual(run.code, 1, run.text)
                self.assertIn("Invalid Pulumi output path", run.text)
                self.assertFalse(run.args.exists())
                self.assert_temp_clean(run)
        self.assertEqual(target.read_text(), "untouched")
        self.assertFalse((self.work / "missing.txt").exists())


if __name__ == "__main__":
    unittest.main()
