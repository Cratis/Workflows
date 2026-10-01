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
// `gh api <path>` prints STUB_PULL for a pull request read, or fails when it is unset; every call is recorded.
writeFileSync(join(directory, "gh"), `#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
if [ "$1 $2" = "api repos/Cratis/Example/pulls/7" ]; then
  [ -n "\${STUB_PULL:-}" ] || { echo '{"message":"Resource not accessible by integration"}'; exit 1; }
  printf '%s' "$STUB_PULL"
fi
`);
chmodSync(join(directory, "gh"), 0o755);

function run(workflow, { eventLabels = [], eventAuthor = "someone", live, number = "7" } = {}) {
    const calls = join(directory, `calls-${Math.random().toString(16).slice(2)}`);
    writeFileSync(calls, "");
    const result = spawnSync("bash", ["-c", script(workflow)], {
        encoding: "utf8",
        env: {
            PATH: `${directory}:${process.env.PATH}`,
            STUB_CALLS: calls,
            ...(live ? { STUB_PULL: JSON.stringify(live) } : {}),
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

test("a Dependabot pull request carries only no-release", () => {
    assert.equal(run(INTENT, { live: pull(["no-release", "dependencies"], "dependabot[bot]") }).status, 0);
    for (const labels of [["major"], ["major", "no-release"], ["patch", "no-release", "dependencies"], [], ["dependencies"]]) {
        const result = run(INTENT, { live: pull(labels, "dependabot[bot]") });
        assert.equal(result.status, 1, JSON.stringify(labels));
        assert.match(result.out, /::error::Dependabot pull requests carry only no-release, with none of major, minor or patch\. Dependabot applies major\/minor\/patch from the dependency's own version; release-action never releases Dependabot PRs\./);
    }
    // The author is read live as well: the event payload's author is not trusted over the pull request's.
    assert.equal(run(INTENT, { eventAuthor: "someone", live: pull(["major"], "dependabot[bot]") }).status, 1);
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
    ]);
    const already = run(NORMALIZE, { live: pull(["no-release", "minor"], "dependabot[bot]") });
    assert.deepEqual(already.calls.slice(1), ["api -X DELETE repos/Cratis/Example/issues/7/labels/minor"]);
    assert.deepEqual(run(NORMALIZE, { live: pull(["no-release"], "dependabot[bot]") }).calls.slice(1), []);
    const human = run(NORMALIZE, { live: pull(["major"], "someone") });
    assert.equal(human.status, 0);
    assert.deepEqual(human.calls, ["api repos/Cratis/Example/pulls/7"]);
    assert.match(human.out, /not Dependabot; its labels are left alone/);
    assert.equal(script(NORMALIZE).includes("${{"), false, "no expression is interpolated into the script");
});
