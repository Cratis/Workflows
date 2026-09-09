# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Offline regressions for the exact inline production program; never contacts GitHub."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/cleanup-pr-artifacts.yml"
SOURCE = WORKFLOW.read_text()
SHELL = textwrap.dedent(SOURCE.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
PROGRAM = SHELL.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
TOKEN = "fake-credential-never-log-this"
REPOSITORY = "Cratis/Chronicle"


def package(kind="nuget", name="Cratis.Test", repository=REPOSITORY, identity=1):
    return {"id": identity, "name": name, "package_type": kind,
            "repository": {"full_name": repository, "name": repository.split("/")[1]}}


def version(identity=123, name="1.0.0-pr42.1", tags=None):
    result = {"id": identity, "name": name}
    if tags is not None:
        result["metadata"] = {"container": {"tags": tags}}
    return result


class Response(io.BytesIO):
    def __init__(self, body=None, status=200, headers=None, raw=None):
        super().__init__(raw if raw is not None else json.dumps(body).encode())
        self.code = status
        self.headers = headers or {}


class FakeAPI:
    """In-memory API at the HTTP boundary, including pagination and deletion state."""
    def __init__(self):
        self.packages = {"container": [], "nuget": [package()]}
        self.versions = {("nuget", "Cratis.Test"): [version()]}
        self.calls = []
        self.hook = lambda method, endpoint: None
        self.delete_status = 204
        self.keep_deleted = False
        self.closed = True

    @property
    def deletes(self):
        return [endpoint for method, endpoint in self.calls if method == "DELETE"]

    def open(self, request, timeout):
        assert timeout == 60
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        assert request.full_url.startswith("https://api.github.com/")
        endpoint = request.full_url.removeprefix("https://api.github.com/")
        method = request.get_method()
        self.calls.append((method, endpoint))
        override = self.hook(method, endpoint)
        if override is not None:
            return override
        parsed = urllib.parse.urlsplit(endpoint)
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/")]
        if parts[:2] == ["repos", "Cratis"]:
            assert method == "GET" and parts == ["repos", "Cratis", "Chronicle", "pulls", "42"]
            return Response({"number": 42, "state": "closed" if self.closed else "open",
                             "base": {"repo": {"full_name": REPOSITORY}}})
        assert parts[:3] == ["orgs", "Cratis", "packages"]
        if len(parts) == 3:
            query = urllib.parse.parse_qs(parsed.query)
            return self.page(self.packages[query["package_type"][0]], endpoint)
        kind, name = parts[3:5]
        packages = [p for p in self.packages[kind] if p["name"] == name]
        if len(parts) == 5:
            return Response(packages[0]) if packages else Response(status=404)
        assert parts[5] == "versions"
        versions = self.versions.get((kind, name), [])
        if len(parts) == 6:
            return self.page(versions, endpoint)
        assert len(parts) == 7
        selected = [v for v in versions if str(v["id"]) == parts[6]]
        if method == "DELETE":
            if not self.keep_deleted and self.delete_status in (204, 404):
                self.versions[(kind, name)] = [v for v in versions if v not in selected]
            return Response(status=self.delete_status)
        assert method == "GET"
        return Response(selected[0]) if selected else Response(status=404)

    def page(self, items, endpoint):
        parsed = urllib.parse.urlsplit(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        page = int(query["page"][0])
        assert query["per_page"] == ["100"]
        headers = {}
        if len(items) > page * 100:
            query["page"] = [str(page + 1)]
            url = "https://api.github.com/" + parsed.path + "?" + urllib.parse.urlencode(query, doseq=True)
            headers["Link"] = f'<{url}>; rel="next"'
        return Response(items[(page - 1) * 100:page * 100], headers=headers)


class CleanupTests(unittest.TestCase):
    def run_cleanup(self, api=None, manual=False, **environment):
        api = api or FakeAPI()
        env = {"GH_TOKEN": TOKEN, "CALLER_REPO": REPOSITORY, "PR_NUMBER": "42",
               "EVENT_NAME": "pull_request", "WORKFLOW_REF": "refs/heads/main"}
        if manual:
            env.update(CALLER_REPO="Cratis/Workflows", TARGET_REPO=REPOSITORY,
                       EVENT_NAME="workflow_dispatch", DRY_RUN="true")
        env.update(environment)
        with tempfile.TemporaryDirectory() as directory:
            env.update(PLAN_PATH=str(Path(directory) / "plan.json"),
                       GITHUB_STEP_SUMMARY=str(Path(directory) / "summary.md"))
            output = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch("urllib.request.build_opener", return_value=api), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    exec(compile(PROGRAM, str(WORKFLOW), "exec"), {"__name__": "__main__"})
                    status = 0
                except SystemExit as error:
                    status = error.code
            path = Path(env["PLAN_PATH"])
            plan = json.loads(path.read_text()) if path.exists() else None
            self.assertNotIn(TOKEN, output.getvalue())
            return status, plan, output.getvalue(), api

    def assert_failed_without_deletes(self, result):
        status, _, output, api = result
        self.assertEqual(status, 1, output)
        self.assertEqual(api.deletes, [])

    def approved(self):
        status, plan, _, _ = self.run_cleanup(manual=True)
        self.assertEqual(status, 0)
        return {"DRY_RUN": "false", "CONFIRMATION": "DELETE Cratis/Chronicle PR 42",
                "EXPECTED_TARGETS": json.dumps(plan)}

    def test_actual_shell_syntax(self):
        result = subprocess.run(["bash", "-n"], input=SHELL, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_actual_shell_shellcheck(self):
        result = subprocess.run(["shellcheck", "-s", "bash", "-"], input=SHELL, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_both_families_is_valid(self):
        api = FakeAPI()
        api.packages = {"container": [], "nuget": []}
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual(plan["targets"], [])
        self.assertEqual(api.deletes, [])
        self.assertEqual(len(api.calls), 2)

    def test_numeric_ids_survive_and_delete_with_readback(self):
        status, plan, output, api = self.run_cleanup()
        self.assertEqual(status, 0, output)
        self.assertEqual(plan["targets"][0]["version_id"], 123)
        self.assertEqual(plan["targets"][0]["version_name"], "1.0.0-pr42.1")
        self.assertEqual(len(api.deletes), 1)
        self.assertIn("Verified absent", output)
        self.assertTrue(plan["targets"][0]["restore_endpoint"].endswith("/versions/123/restore"))

    def test_nuget_boundary_matching_and_unrelated_repository(self):
        api = FakeAPI()
        names = ["1.0-pr42", "1.0-pr42.1", "1.0-pr42-foo", "pr42", "1.0-pr420.1",
                 "1.0-pr142", "1.0-xpr42", "1.0-pr42x", "1.0.0"]
        api.versions[("nuget", "Cratis.Test")] = [version(i + 1, name) for i, name in enumerate(names)]
        api.packages["nuget"] += [package(name="Other", repository="Cratis/Other", identity=2),
                                   package(name="SameName", repository="Other/Chronicle", identity=3)]
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual([t["version_name"] for t in plan["targets"]], names[:4])
        self.assertFalse(any("/Other/" in endpoint or "/SameName/" in endpoint for _, endpoint in api.calls))

    def test_container_boundary_and_encoded_package_name(self):
        api = FakeAPI()
        api.packages["container"] = [package("container", "nested/image")]
        api.versions[("container", "nested/image")] = [
            version(44, "sha256:abc", ["pr42", "pr42-linux"]),
            version(45, "sha256:def", ["pr420", "pr142"]),
            version(46, "sha256:ghi", ["pr42.1"])]
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual(len(plan["targets"]), 2)
        self.assertIn("nested%2Fimage/versions/44", api.deletes[0])

    def test_mixed_container_tags_fail_before_any_delete(self):
        for tag in ["latest", "v1.0.0", "pr43", "pr420"]:
            with self.subTest(tag=tag):
                api = FakeAPI()
                api.packages["container"] = [package("container", "image")]
                api.versions[("container", "image")] = [version(tags=["pr42", tag])]
                self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_both_package_and_version_pagination(self):
        api = FakeAPI()
        api.packages["nuget"] = [package(name=f"Other{i}", repository="Cratis/Other", identity=i)
                                  for i in range(1, 101)] + [package(identity=101)]
        api.versions[("nuget", "Cratis.Test")] = [version(i, "1.0.0") for i in range(1, 101)] + [version(101)]
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual([t["version_id"] for t in plan["targets"]], [101])
        self.assertTrue(any("package_type=nuget" in e and "page=2" in e for _, e in api.calls))
        self.assertTrue(any("/versions?" in e and "page=2" in e for _, e in api.calls))

    def test_container_package_and_version_pagination(self):
        api = FakeAPI()
        api.packages["container"] = [package("container", f"Other{i}", "Cratis/Other", i)
                                      for i in range(1, 101)] + [package("container", "image", identity=101)]
        api.versions[("container", "image")] = [version(i, tags=["stable"]) for i in range(1, 101)]
        api.versions[("container", "image")].append(version(101, tags=["pr42"]))
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual(len(plan["targets"]), 2)
        self.assertTrue(any("package_type=container" in e and "page=2" in e for _, e in api.calls))
        self.assertTrue(any("container/image/versions?" in e and "page=2" in e for _, e in api.calls))

    def test_missing_association_is_not_interpreted_as_empty(self):
        for repository in ["missing", {}, {"name": "Chronicle"}]:
            api = FakeAPI()
            if repository == "missing":
                del api.packages["nuget"][0]["repository"]
            else:
                api.packages["nuget"][0]["repository"] = repository
            self.assert_failed_without_deletes(self.run_cleanup(api))
        api = FakeAPI()
        api.packages["nuget"][0]["repository"] = None
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 0, output)
        self.assertEqual(plan["targets"], [])
        self.assertEqual(api.deletes, [])

    def test_listing_failures_in_each_family_and_level(self):
        for failing in ["package_type=container", "package_type=nuget", "container/image/versions?", "nuget/Cratis.Test/versions?"]:
            for status in [401, 403, 404, 429, 500]:
                with self.subTest(failing=failing, status=status):
                    api = FakeAPI()
                    api.packages["container"] = [package("container", "image")]
                    api.versions[("container", "image")] = [version(tags=["pr42"])]
                    api.hook = lambda m, e: Response(status=status, raw=TOKEN.encode()) if failing in e else None
                    self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_invalid_json_or_schema(self):
        for raw in [b"not json", b"{}", b"null", b'[{}]', b'[{"id":true}]',
                    b'[{"id":1,"id":2}]', b"[NaN]", b"\xff"]:
            with self.subTest(raw=raw):
                api = FakeAPI()
                api.hook = lambda m, e: Response(raw=raw) if "package_type=nuget" in e else None
                self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_invalid_version_schema(self):
        for invalid in [{"id": 1}, {"id": "1", "name": "pr42"}, {"id": 0, "name": "pr42"}]:
            api = FakeAPI()
            api.versions[("nuget", "Cratis.Test")] = [invalid]
            self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_bad_pagination_and_repeated_ids(self):
        for link in ["garbage", '<https://evil.example/?page=2>; rel="next"',
                     '<https://api.github.com/orgs/Cratis/packages?package_type=nuget&per_page=100&page=2>; rel="last"',
                     '<https://api.github.com/orgs/Cratis/packages?package_type=nuget&per_page=100&page=1>; rel="next"',
                     '<https://api.github.com/orgs/Cratis/packages?package_type=nuget&per_page=100&page=2>; rel="next"']:
            api = FakeAPI()
            api.hook = lambda m, e: Response([package()], headers={"Link": link}) if "package_type=nuget" in e else None
            self.assert_failed_without_deletes(self.run_cleanup(api))
        api = FakeAPI()
        api.versions[("nuget", "Cratis.Test")] = [version()] * 101
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_page_two_failure_prevents_deletion(self):
        api = FakeAPI()
        api.versions[("nuget", "Cratis.Test")] = [version(i) for i in range(1, 102)]
        api.hook = lambda m, e: Response(status=403) if "page=2" in e else None
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_all_target_preflight_before_first_delete(self):
        api = FakeAPI()
        api.versions[("nuget", "Cratis.Test")] += [version(124)]
        api.hook = lambda m, e: Response(status=403) if e.endswith("/versions/124") else None
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_association_and_version_identity_rechecked(self):
        for body, suffix in [(package(repository="Cratis/Other"), "/Cratis.Test"),
                             (version(123, "1.0.0"), "/versions/123"),
                             (version(124), "/versions/123")]:
            api = FakeAPI()
            api.hook = lambda m, e: Response(body) if e.endswith(suffix) else None
            self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_identity_rechecked_again_immediately_before_delete(self):
        api = FakeAPI()
        def hook(method, endpoint):
            if endpoint.endswith("/versions/123") and sum(e == endpoint for _, e in api.calls) == 2:
                return Response(version(123, "1.0.0"))
        api.hook = hook
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_delete_failure_is_red_and_not_retried(self):
        for status in [401, 403, 429, 500]:
            api = FakeAPI()
            api.delete_status = status
            result = self.run_cleanup(api)
            self.assertEqual(result[0], 1, result[2])
            self.assertEqual(len(api.deletes), 1)

    def test_delete_404_requires_reconciliation(self):
        api = FakeAPI()
        api.delete_status = 404
        self.assertEqual(self.run_cleanup(api)[0], 0)
        api = FakeAPI()
        api.delete_status = 404
        api.keep_deleted = True
        self.assertEqual(self.run_cleanup(api)[0], 1)

    def test_successful_delete_with_present_readback_fails(self):
        api = FakeAPI()
        api.keep_deleted = True
        self.assertEqual(self.run_cleanup(api)[0], 1)

    def test_readback_auth_failure_or_list_mismatch_fails(self):
        for mode in ["package", "version", "listing", "still-listed"]:
            api = FakeAPI()
            def hook(method, endpoint):
                if not api.deletes or method != "GET":
                    return None
                if mode == "package" and endpoint.endswith("/Cratis.Test"):
                    return Response(status=403)
                if mode == "version" and endpoint.endswith("/versions/123"):
                    return Response(status=403)
                if mode in ("listing", "still-listed") and "/versions?" in endpoint:
                    return Response(status=403) if mode == "listing" else Response([version()])
            api.hook = hook
            self.assertEqual(self.run_cleanup(api)[0], 1, mode)

    def test_missing_token_and_invalid_inputs_do_not_call_api(self):
        for env in [{"GH_TOKEN": ""}, {"PR_NUMBER": "0"}, {"PR_NUMBER": "-1"},
                    {"PR_NUMBER": "1.5"}, {"PR_NUMBER": ""}, {"PR_NUMBER": "42;echo bad"},
                    {"CALLER_REPO": "Other/Chronicle"}, {"CALLER_REPO": "Cratis/../Chronicle"}]:
            result = self.run_cleanup(**env)
            self.assert_failed_without_deletes(result)
            self.assertEqual(result[3].calls, [])

    def test_manual_dry_run_has_zero_mutations_and_deterministic_plan(self):
        first = self.run_cleanup(manual=True)
        second = self.run_cleanup(manual=True)
        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[3].deletes, [])
        self.assertTrue(all(method == "GET" for method, _ in first[3].calls))

    def test_manual_apply_requires_confirmation_manifest_and_main(self):
        for env in [{"DRY_RUN": "false"}, {"DRY_RUN": "bad"}, {"TARGET_REPO": ""},
                    {"TARGET_REPO": "Other/Chronicle"}, {"WORKFLOW_REF": "refs/heads/unsafe"},
                    {"CALLER_REPO": REPOSITORY},
                    {"DRY_RUN": "false", "CONFIRMATION": "DELETE Cratis/Chronicle PR 42"},
                    {**self.approved(), "CONFIRMATION": "DELETE Cratis/Other PR 42"}]:
            self.assert_failed_without_deletes(self.run_cleanup(manual=True, **env))

    def test_manual_apply_requires_closed_pr_and_correct_pr_identity(self):
        approved = self.approved()
        api = FakeAPI()
        api.closed = False
        self.assert_failed_without_deletes(self.run_cleanup(api, manual=True, **approved))
        for body in [{"number": 43, "state": "closed", "base": {"repo": {"full_name": REPOSITORY}}},
                     {"number": 42, "state": "closed", "base": {"repo": {"full_name": "Cratis/Other"}}}]:
            api = FakeAPI()
            api.hook = lambda m, e: Response(body) if "/pulls/" in e else None
            self.assert_failed_without_deletes(self.run_cleanup(api, manual=True, **approved))

    def test_manual_expected_manifest_drift(self):
        approved = self.approved()
        for change in ["add", "remove", "rename", "id", "repository"]:
            env = copy.deepcopy(approved)
            manifest = json.loads(env["EXPECTED_TARGETS"])
            if change == "add":
                manifest["targets"].append({"version_id": 999})
            elif change == "remove":
                manifest["targets"] = []
            elif change == "repository":
                manifest["repository"] = "Cratis/Other"
            elif change == "id":
                manifest["targets"][0]["version_id"] = 999
            else:
                manifest["targets"][0]["version_name"] = "1.0.0-pr42.2"
            env["EXPECTED_TARGETS"] = json.dumps(manifest)
            self.assert_failed_without_deletes(self.run_cleanup(manual=True, **env))

    def test_manual_approved_apply(self):
        result = self.run_cleanup(manual=True, **self.approved())
        self.assertEqual(result[0], 0, result[2])
        self.assertEqual(len(result[3].deletes), 1)
        self.assertEqual(sum("/pulls/42" in e for _, e in result[3].calls), 2)

    def test_manual_pr_reopened_before_delete_fails(self):
        api = FakeAPI()
        def hook(method, endpoint):
            if "/pulls/42" in endpoint and sum("/pulls/42" in e for _, e in api.calls) == 2:
                api.closed = False
        api.hook = hook
        self.assert_failed_without_deletes(self.run_cleanup(api, manual=True, **self.approved()))

    def test_container_retagged_after_inventory_fails(self):
        api = FakeAPI()
        api.packages["container"] = [package("container", "image")]
        api.versions[("container", "image")] = [version(44, tags=["pr42"])]
        api.hook = lambda m, e: Response(version(44, tags=["pr42", "latest"])) if e.endswith("/versions/44") else None
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_failed_late_delete_stops_and_reports_partial_failure(self):
        api = FakeAPI()
        api.versions[("nuget", "Cratis.Test")] += [version(124), version(125)]
        api.hook = lambda m, e: Response(status=403) if m == "DELETE" and e.endswith("/124") else None
        status, plan, output, api = self.run_cleanup(api)
        self.assertEqual(status, 1)
        self.assertEqual(len(plan["targets"]), 3)
        self.assertEqual(len(api.deletes), 2)
        self.assertIn("Verified absent: nuget version ID 123", output)
        self.assertNotIn("Cleanup verified:", output)

    def test_transport_exception_is_redacted(self):
        api = FakeAPI()
        def fail(method, endpoint):
            raise OSError("untrusted stderr Authorization: Bearer " + TOKEN)
        api.hook = fail
        self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_http_error_body_headers_and_redirect_are_not_logged(self):
        for status in [302, 403]:
            api = FakeAPI()
            def fail(method, endpoint):
                raise urllib.error.HTTPError("https://api.github.com/" + TOKEN, status, TOKEN,
                                             {"Location": "https://evil.example/" + TOKEN}, io.BytesIO(TOKEN.encode()))
            api.hook = fail
            self.assert_failed_without_deletes(self.run_cleanup(api))

    def test_untrusted_plan_cannot_print_token(self):
        api = FakeAPI()
        api.versions[("nuget", "Cratis.Test")] = [version(name="pr42-" + TOKEN)]
        result = self.run_cleanup(api, manual=True)
        self.assert_failed_without_deletes(result)
        self.assertIsNone(result[1])

    def test_workflow_keeps_code_inline_and_safe_input_transport(self):
        self.assertNotIn("actions/checkout", SOURCE)
        self.assertNotIn("${{", SHELL)
        self.assertIn("default: true", SOURCE)
        self.assertIn("expected_targets:", SOURCE)
        self.assertIn("pr-package-cleanup", SOURCE)
        self.assertIn("secrets.PAT_WORKFLOWS", SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
