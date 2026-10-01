// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Offline regressions for the inline shell of verify-release-intent.yml and normalize-dependabot-labels.yml, run with
// bash, jq and a stub `gh` that answers from the test instead of GitHub.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const INTENT = readFileSync(".github/workflows/verify-release-intent.yml", "utf8");
const NORMALIZE = readFileSync(".github/workflows/normalize-dependabot-labels.yml", "utf8");

const script = workflow => workflow.split("        run: |\n")[1].split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");
const directory = mkdtempSync(join(tmpdir(), "verify-release-intent-"));
// `gh api <path>` prints STUB_PULL for a pull request read, or fails when it is unset; a label write (`-X DELETE|POST`)
// fails when STUB_READ_ONLY is set, as it does with a read-only token; every call is recorded.
writeFileSync(join(directory, "gh"), `#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
if [ "$2" = "-X" ] && [ -n "\${STUB_READ_ONLY:-}" ]; then
  echo '{"message":"Resource not accessible by integration"}'; exit 1
fi
if [ "$1 $2" = "api repos/Cratis/Example/pulls/7" ]; then
  [ -n "\${STUB_PULL:-}" ] || { echo '{"message":"Resource not accessible by integration"}'; exit 1; }
  printf '%s' "$STUB_PULL"
fi
`);
chmodSync(join(directory, "gh"), 0o755);

function run(workflow, { eventLabels = [], eventAuthor = "someone", live, number = "7", readOnly = false } = {}) {
    const calls = join(directory, `calls-${Math.random().toString(16).slice(2)}`);
    writeFileSync(calls, "");
    const result = spawnSync("bash", ["-c", script(workflow)], {
        encoding: "utf8",
        env: {
            PATH: `${directory}:${process.env.PATH}`,
            STUB_CALLS: calls,
            ...(live ? { STUB_PULL: JSON.stringify(live) } : {}),
            ...(readOnly ? { STUB_READ_ONLY: "1" } : {}),
            GH_TOKEN: "token",
            REPOSITORY: "Cratis/Example",
            NUMBER: number,
            EVENT_LABELS: JSON.stringify(eventLabels),
            EVENT_AUTHOR: eventAuthor,
        },
    });
    return { status: result.status, out: result.stdout + result.stderr, calls: readFileSync(calls, "utf8").trim().split("\n").filter(Boolean) };
}
const pull = (labels, login = "someone") => ({ number: 7, labels: labels.map(name => ({ name })), user: { login } });

test("exactly one release intent passes; none, two, or a bump with no-release fails", () => {
    for (const labels of [["patch"], ["minor", "bug"], ["major"], ["no-release"]])
        assert.equal(run(INTENT, { live: pull(labels) }).status, 0, JSON.stringify(labels));
    for (const [labels, message] of [[[], /states no release intent/], [["bug"], /states no release intent/],
        [["minor", "no-release"], /no-release as well as a semantic version/], [["major", "patch"], /carries 2 semantic version labels/]]) {
        const result = run(INTENT, { live: pull(labels) });
        assert.equal(result.status, 1, JSON.stringify(labels));
        assert.match(result.out, message);
    }
});

test("the labels come from the pull request as it is now, so a re-run sees a label added since the event", () => {
    const result = run(INTENT, { eventLabels: [], live: pull(["patch"]) });
    assert.equal(result.status, 0, result.out);
    assert.deepEqual(result.calls, ["api repos/Cratis/Example/pulls/7"]);
    // And a label removed since the event.
    assert.equal(run(INTENT, { eventLabels: ["patch"], live: pull([]) }).status, 1);
});

test("a failed read falls back to the event payload with a notice", () => {
    const passing = run(INTENT, { eventLabels: ["patch"] });
    assert.equal(passing.status, 0, passing.out);
    assert.match(passing.out, /::notice title=Release intent read from the event::.*Grant the caller pull-requests: read\./);
    assert.equal(run(INTENT, { eventLabels: [] }).status, 1);
    assert.equal(run(INTENT, { eventLabels: ["no-release"], eventAuthor: "dependabot[bot]" }).status, 0);
    // Not a pull request event: nothing to read.
    const none = run(INTENT, { eventLabels: ["minor"], number: "" });
    assert.equal(none.status, 0);
    assert.deepEqual(none.calls, []);
});

test("a Dependabot pull request is never failed for its labels: the gate corrects them when it can write", () => {
    const clean = run(INTENT, { live: pull(["no-release", "dependencies"], "dependabot[bot]") });
    assert.equal(clean.status, 0);
    assert.deepEqual(clean.calls, ["api repos/Cratis/Example/pulls/7"]);
    const major = run(INTENT, { live: pull(["major", "dependencies"], "dependabot[bot]") });
    assert.equal(major.status, 0, major.out);
    assert.deepEqual(major.calls, [
        "api repos/Cratis/Example/pulls/7",
        "api -X DELETE repos/Cratis/Example/issues/7/labels/major",
        "api -X POST repos/Cratis/Example/issues/7/labels -f labels[]=no-release",
    ]);
    assert.match(major.out, /replaced major, minor and patch with no-release/);
    const both = run(INTENT, { live: pull(["patch", "no-release"], "dependabot[bot]") });
    assert.equal(both.status, 0);
    assert.deepEqual(both.calls.slice(1), ["api -X DELETE repos/Cratis/Example/issues/7/labels/patch"]);
    const none = run(INTENT, { live: pull([], "dependabot[bot]") });
    assert.equal(none.status, 0);
    assert.deepEqual(none.calls.slice(1), ["api -X POST repos/Cratis/Example/issues/7/labels -f labels[]=no-release"]);
    // The author is read live as well: the event payload's author is not trusted over the pull request's.
    assert.equal(run(INTENT, { eventAuthor: "someone", live: pull(["major"], "dependabot[bot]") }).status, 0);
    assert.equal(run(INTENT, { eventAuthor: "someone", live: pull(["major"], "dependabot[bot]"), readOnly: true }).status, 0);
});

test("a Dependabot pull request passes with a notice when the token cannot write labels", () => {
    for (const labels of [["major"], ["major", "no-release"], ["patch", "no-release", "dependencies"], [], ["dependencies"]]) {
        const result = run(INTENT, { live: pull(labels, "dependabot[bot]"), readOnly: true });
        assert.equal(result.status, 0, JSON.stringify(labels));
        assert.match(result.out, /::notice title=Dependabot labels not corrected::/);
        assert.equal(result.out.includes("::error"), false);
    }
    // Without a readable pull request the event's labels are used, and the write fails as well.
    const fallback = run(INTENT, { eventLabels: ["major"], eventAuthor: "dependabot[bot]", readOnly: true });
    assert.equal(fallback.status, 0, fallback.out);
    assert.match(fallback.out, /Dependabot labels not corrected/);
    // A pull request someone else opened is still held to the one-label rule.
    assert.equal(run(INTENT, { live: pull(["major"], "someone"), readOnly: true }).status, 0);
    assert.equal(run(INTENT, { live: pull(["major", "patch"], "someone"), readOnly: true }).status, 1);
});

test("the gate sets no permissions of its own, so the caller's pull-requests: read reaches it", () => {
    assert.equal(/^\s*permissions:/m.test(INTENT), false);
    assert(INTENT.includes("GH_TOKEN: ${{ github.token }}"));
    assert.equal(script(INTENT).includes("${{"), false, "no expression is interpolated into the script");
});

test("the normalizer swaps Dependabot's major, minor and patch for no-release and leaves everyone else alone", () => {
    const swapped = run(NORMALIZE, { live: pull(["major", "dependencies"], "dependabot[bot]") });
    assert.equal(swapped.status, 0, swapped.out);
    assert.deepEqual(swapped.calls, [
        "api repos/Cratis/Example/pulls/7",
        "api -X DELETE repos/Cratis/Example/issues/7/labels/major",
        "api -X POST repos/Cratis/Example/issues/7/labels -f labels[]=no-release",
        "api repos/Cratis/Example/pulls/7 --jq [.labels[].name] | join(\", \")",
    ]);
    const already = run(NORMALIZE, { live: pull(["no-release", "minor"], "dependabot[bot]") });
    assert.deepEqual(already.calls.slice(1, 2), ["api -X DELETE repos/Cratis/Example/issues/7/labels/minor"]);
    assert.deepEqual(run(NORMALIZE, { live: pull(["no-release"], "dependabot[bot]") }).calls.slice(1), []);
    const human = run(NORMALIZE, { live: pull(["major"], "someone") });
    assert.equal(human.status, 0);
    assert.deepEqual(human.calls, ["api repos/Cratis/Example/pulls/7"]);
    assert.match(human.out, /not Dependabot; its labels are left alone/);
    assert.equal(script(NORMALIZE).includes("${{"), false, "no expression is interpolated into the script");
});

test("the normalizer never turns a run red: a token that cannot write, a failed read or a failed label call ends in a notice", () => {
    for (const labels of [["major"], ["major", "no-release"], ["patch", "no-release", "dependencies"], [], ["dependencies"]]) {
        const result = run(NORMALIZE, { live: pull(labels, "dependabot[bot]"), readOnly: true });
        assert.equal(result.status, 0, `${JSON.stringify(labels)}: ${result.out}`);
        assert.match(result.out, /::notice title=Dependabot labels not normalized::Could not (?:remove|add)/);
        assert.equal(result.out.includes("::error"), false);
        // The labels are read again at the end, whatever the writes did.
        assert.equal(result.calls.at(-1).startsWith("api repos/Cratis/Example/pulls/7 --jq"), true, JSON.stringify(result.calls));
    }
    // A pull request that cannot be read is left alone.
    const unreadable = run(NORMALIZE, { eventAuthor: "dependabot[bot]" });
    assert.equal(unreadable.status, 0, unreadable.out);
    assert.match(unreadable.out, /::notice title=Dependabot labels not normalized::Could not read pull request #7/);
    assert.deepEqual(unreadable.calls, ["api repos/Cratis/Example/pulls/7"]);
    // Not a pull request event: nothing to normalize.
    assert.equal(run(NORMALIZE, { number: "" }).status, 0);
});

// The bootstrap installs the release-intent caller only where a repository releases. Its decision function is run
// with bash and a stub `gh` that serves workflow blobs from the test.
const BOOTSTRAP = readFileSync(".github/scripts/bootstrap-common-workflows.sh", "utf8");

test("the bootstrapped work-record caller filters markdown and local work records and cancels superseded runs", () => {
    const encoded = /BOOTSTRAPPED_FILES\["\.github\/workflows\/verify-no-work-records\.yml"\]="([^"]+)"/.exec(BOOTSTRAP)[1];
    const caller = Buffer.from(encoded, "base64").toString("utf8");
    assert.match(caller, /pull_request:\n    paths: \["\*\*\.md", "\.ai-work\/\*\*"\]/);
    assert.match(caller, /push:\n    branches: \["main"\]\n    paths: \["\*\*\.md", "\.ai-work\/\*\*"\]/);
    assert(caller.includes("group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"));
    assert(caller.includes("cancel-in-progress: true"));
    assert(caller.includes("uses: Cratis/Workflows/.github/workflows/verify-no-work-records.yml@main"));
    assert.match(readFileSync(".github/workflows/verify-no-work-records.yml", "utf8"), /timeout-minutes: 5/);
});
const releasesFunction = /^releases_with_release_action\(\) \{\n[\s\S]*?^\}$/m.exec(BOOTSTRAP)[0];
const blobs = mkdtempSync(join(tmpdir(), "bootstrap-blobs-"));
writeFileSync(join(blobs, "gh"), `#!/usr/bin/env bash
# gh api repos/Cratis/<repo>/git/blobs/<sha> --jq .content
sha="\${2##*/}"
[ -f "${blobs}/$sha" ] || exit 1
base64 < "${blobs}/$sha"
`);
chmodSync(join(blobs, "gh"), 0o755);

function releases(files) {
    const tree = files.map(([path, content], index) => {
        const sha = `sha${index}`;
        writeFileSync(join(blobs, sha), content ?? "");
        return { path, type: "blob", sha };
    });
    const result = spawnSync("bash", ["-c", `set -euo pipefail\nSEMVER_LABEL_FILE=".github/workflows/verify-semver-label.yml"\n${releasesFunction}\nreleases_with_release_action Example "$TREE"`], {
        encoding: "utf8",
        env: { PATH: `${blobs}:${process.env.PATH}`, TREE: JSON.stringify({ tree }) },
    });
    return result.status === 0;
}

test("the bootstrap installs the release-intent caller only where a repository already has it or releases with release-action", () => {
    assert.equal(releases([[".github/workflows/verify-semver-label.yml", "name: x"]]), true, "an existing caller is kept current");
    assert.equal(releases([[".github/workflows/publish.yml", "jobs:\n  release:\n    steps:\n      - uses: Cratis/release-action@v1\n"]]), true);
    assert.equal(releases([[".github/workflows/publish.yml", "      - uses: cratis/release-action@v1\n"]]), true, "the owner is matched case-insensitively");
    assert.equal(releases([[".github/workflows/build.yaml", "      - uses: Cratis/release-action/notes@v1\n"], [".github/workflows/x.yml", "name: x"]]), true, "any workflow, .yaml too");
    assert.equal(releases([[".github/workflows/build.yml", "uses: actions/checkout@v4\n"], ["README.md", "Cratis/release-action"]]), false, "a site, sample or tap");
    assert.equal(releases([[".github/workflows/deploy.yml", "uses: Cratis/Workflows/.github/workflows/verify-no-work-records.yml@main\n"], [".github/workflows/nested/publish.yml", "Cratis/release-action"]]), false);
    assert.equal(releases([]), false);
    assert(/if \[ "\$file_path" = "\$SEMVER_LABEL_FILE" \]; then[\s\S]*?releases_with_release_action "\$repo" "\$subtree"/.test(BOOTSTRAP), "the file loop consults it");
});
