// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Executes the shipped inline programs without credentials or GitHub access.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

const workflow = readFileSync(".github/workflows/verify-pull-request-policy.yml", "utf8");
const old = readFileSync(".github/workflows/verify-release-notes.yml", "utf8");
const extract = source => Object.fromEntries(source.split("node - <<'JS'\n").slice(1).map(block => {
    const program = block.split("\n          JS\n")[0].split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");
    return [/^\/\/ cratis:program ([\w-]+)$/m.exec(program)[1], program];
}));
const programs = extract(workflow);
const root = mkdtempSync(join(tmpdir(), "policy-spec-"));
let counter = 0;
const pull = (labels, login = "someone") => ({ number: 7, labels: labels.map(name => ({ name })), user: { login } });
function run(name, env = {}, cwd = root) {
    const file = join(root, `program-${counter++}.cjs`);
    const summary = join(root, `summary-${counter++}.md`);
    writeFileSync(file, programs[name]);
    writeFileSync(summary, "");
    const result = spawnSync(process.execPath, [file], {
        cwd, encoding: "utf8", env: { PATH: process.env.PATH, GITHUB_STEP_SUMMARY: summary, ...env },
    });
    return { status: result.status, out: result.stdout + result.stderr, summary: readFileSync(summary, "utf8") };
}
function intent(labels, { live, author = "someone" } = {}) {
    const path = join(root, `pr-${counter++}.json`);
    writeFileSync(path, typeof live === "string" ? live : JSON.stringify(live ?? {}));
    return run("release-intent", { PR_JSON: path, PR_LABELS: JSON.stringify(labels), PR_AUTHOR: author });
}
const good = {
    EVENT_NAME: "pull_request", PR_BASE: "main", PR_AUTHOR: "someone", RELEASE_INTENT: "true",
    NODE_OUTCOME: "success", TOOLS_OUTCOME: "success", CHECKOUT_OUTCOME: "success", PULL_OUTCOME: "success",
    NOTES_OUTCOME: "success", INTENT_OUTCOME: "success", RECORDS_OUTCOME: "success", RECORDS_EXIT: "0",
    DRIFT_TREE_OUTCOME: "success", DRIFT_READY: "true", DRIFT_OUTCOME: "success",
};
const aggregate = env => run("aggregate-policy", { ...good, ...env });
const steps = workflow.split(/\n {6}- /).slice(1);
const step = id => steps.find(source => source.includes(`\n        id: ${id}\n`));
const shell = source => source.split("        run: |\n")[1].split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");

test("notes, shared live read and drift programs are byte-identical at the called ref", () => {
    const original = extract(old);
    for (const name of ["read-pull-request", "release-notes", "release-notes-drift"]) assert.equal(programs[name], original[name], name);
    assert.equal(/^\s*permissions:/m.test(workflow), false);
    assert.equal((workflow.match(/^  verify:/gm) || []).length, 1);
    assert.match(workflow, /runs-on: \$\{\{ inputs.runs-on \}\}/);
    assert.match(workflow, /timeout-minutes: 10/);
    for (const source of steps.filter(source => source.includes("        run: |"))) {
        assert.equal(shell(source).includes("${{"), false, "untrusted content only reaches scripts via env");
    }
});

test("blocking steps have ids, continue on error and guarded applicability", () => {
    for (const id of ["node", "tools", "checkout", "pull", "notes", "intent", "records", "drift-tree", "drift"]) {
        assert(step(id), id);
        assert.match(step(id), /continue-on-error: true/);
    }
    assert.match(step("intent"), /inputs.release-intent && github.event_name == 'pull_request' && github.event.pull_request.base.ref == 'main'/);
    assert.match(step("notes"), /github.event_name == 'pull_request'/);
    assert.match(step("pull"), /github.event_name == 'pull_request'/);
    assert.match(step("checkout"), /persist-credentials: false/);
    assert.doesNotMatch(step("checkout"), /\n\s+ref:/);
    for (const key of Object.keys(good).filter(key => key.endsWith("_OUTCOME"))) assert(workflow.includes(`${key}: \u0024{{ steps.`), key);
    assert.match(workflow, /RECORDS_EXIT: \$\{\{ steps.records.outputs.exit-code \}\}/);
});

test("work-record guard retains its pin, checksum and allowlist inputs and reports exit 2", () => {
    const guard = readFileSync(".github/workflows/verify-no-work-records.yml", "utf8");
    for (const pattern of [/https:\/\/raw.githubusercontent.com\/Cratis\/Workflows\/[^'\s]+/, /script_sha256='[a-f0-9]+'/]) {
        assert.equal(pattern.exec(workflow)[0], pattern.exec(guard)[0]);
    }
    for (const input of ["extra-allowed", "extra-allowed-paths"]) assert(step("records").includes(`inputs.${input}`));
    assert.match(step("records"), /echo "exit-code=\$status"/);
    for (const exit of ["1", "2", ""]) {
        const result = aggregate({ RECORDS_EXIT: exit });
        assert.equal(result.status, 1, result.out);
        assert.match(result.out, /::error title=Work records::/);
    }
});

test("each blocking failure, missing or skipped outcome independently fails aggregation", () => {
    for (const key of ["NODE_OUTCOME", "TOOLS_OUTCOME", "CHECKOUT_OUTCOME", "PULL_OUTCOME", "NOTES_OUTCOME", "INTENT_OUTCOME", "RECORDS_OUTCOME"]) {
        for (const outcome of ["failure", "", "skipped", "cancelled"]) {
            const result = aggregate({ [key]: outcome });
            assert.equal(result.status, 1, `${key}=${outcome}: ${result.out}`);
            assert.match(result.out, /::error title=/);
            assert.match(result.summary, /\| Gate \| Result \|/);
            assert.match(result.summary, /fail/);
        }
    }
    assert.equal(aggregate({}).status, 0);
});

function probe(version) {
    const bin = join(root, `bin-${counter++}`);
    mkdirSync(bin);
    if (version !== null) {
        writeFileSync(join(bin, "node"), `#!/bin/sh\necho ${version}\n`, { mode: 0o755 });
    }
    const output = join(root, `probe-output-${counter++}`);
    writeFileSync(output, "");
    // Only the fake node directory and the system directories bash needs; no real node on PATH.
    const result = spawnSync("/bin/bash", ["-e", "-o", "pipefail", "-c", shell(step("node-present"))], {
        encoding: "utf8", env: { PATH: `${bin}:/usr/bin:/bin`, GITHUB_OUTPUT: output },
    });
    return { status: result.status, out: result.stdout + result.stderr, output: readFileSync(output, "utf8") };
}

test("setup-node is skipped only when Node 24 or later is already installed, never the job", () => {
    for (const version of ["v24.0.0", "v24.11.1", "v25.2.0", "v100.0.0"]) {
        const result = probe(version);
        assert.equal(result.status, 0, result.out);
        assert.equal(result.output, "ready=true\n", version);
    }
    for (const version of ["v22.21.1", "v20.0.0", "v8.17.0", "garbage", "", null]) {
        const result = probe(version);
        assert.equal(result.status, 0, result.out);
        assert.equal(result.output, "", String(version));
    }
    assert.match(step("node-present"), /continue-on-error: true/);
    assert.match(step("node"), /\n {8}if: \$\{\{ steps.node-present.outputs.ready != 'true' \}\}\n/);
    assert.match(step("node"), /node-version: 24$/);
    assert(steps.indexOf(step("node-present")) < steps.indexOf(step("node")), "probe runs before setup");
    assert.doesNotMatch(workflow.split("\n    steps:\n")[0], /\n {4}if:/, "the verify job is never skipped");
    assert.match(workflow, /NODE_PRESENT: \$\{\{ steps.node-present.outputs.ready \}\}/);
    for (const outcome of ["skipped", "success"]) {
        const result = aggregate({ NODE_PRESENT: "true", NODE_OUTCOME: outcome });
        assert.equal(result.status, 0, result.out);
        assert.match(result.summary, /\| Node setup \| (not applicable|pass) /);
    }
    assert.equal(aggregate({ NODE_PRESENT: "true", NODE_OUTCOME: "failure" }).status, 1);
    for (const present of ["", "false"]) assert.equal(aggregate({ NODE_PRESENT: present, NODE_OUTCOME: "skipped" }).status, 1);
});

test("drift failures only warn; non-PR events and disabled or stacked intent may skip", () => {
    for (const env of [{ DRIFT_OUTCOME: "failure" }, { DRIFT_TREE_OUTCOME: "failure", DRIFT_READY: "", DRIFT_OUTCOME: "skipped" }]) {
        const result = aggregate(env);
        assert.equal(result.status, 0, result.out);
        assert.match(result.summary, /warn \(not checked\)/);
    }
    assert.equal(aggregate({ EVENT_NAME: "push", PULL_OUTCOME: "skipped", NOTES_OUTCOME: "skipped", INTENT_OUTCOME: "skipped", DRIFT_OUTCOME: "skipped" }).status, 0);
    assert.equal(aggregate({ PR_BASE: "feature", INTENT_OUTCOME: "skipped" }).status, 0);
    assert.equal(aggregate({ RELEASE_INTENT: "false", INTENT_OUTCOME: "skipped" }).status, 0);
});

test("node port preserves exactly-one-label rules and diagnostics", () => {
    for (const labels of [["major"], ["minor", "bug"], ["patch"], ["no-release"]]) assert.equal(intent(labels).status, 0);
    for (const [labels, message] of [[[], /states no release intent/], [["bug"], /states no release intent/],
        [["minor", "no-release"], /no-release as well as a semantic version/], [["major", "patch"], /carries 2 semantic version labels/]]) {
        const result = intent(labels);
        assert.equal(result.status, 1);
        assert.match(result.out, message);
    }
    assert.equal(intent([], { live: pull(["patch"]) }).status, 0);
    assert.equal(intent(["patch"], { live: pull([]) }).status, 1);
    assert.match(intent(["patch"], { live: "invalid JSON" }).out, /Release intent read from the event/);
});

test("Dependabot always passes without label writes, including fallback and live author", () => {
    for (const labels of [["major"], ["major", "no-release"], ["patch", "no-release", "dependencies"], [], ["dependencies"], ["no-release"]]) {
        assert.equal(intent([], { live: pull(labels, "dependabot[bot]") }).status, 0);
        assert.equal(intent(labels, { author: "dependabot[bot]" }).status, 0);
    }
    assert.doesNotMatch(programs["release-intent"], /fetch\(|exec|https:/);
});

function git(cwd, ...args) {
    const result = spawnSync("git", args, { cwd, encoding: "utf8" });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    return result.stdout.trim();
}

test("drift compares the PR head, not the different base or merge-tree content", () => {
    const repository = join(root, "repo");
    const runner = join(root, "runner");
    mkdirSync(repository); mkdirSync(runner);
    git(repository, "init", "-b", "main");
    git(repository, "config", "user.email", "test@example.invalid"); git(repository, "config", "user.name", "Test");
    writeFileSync(join(repository, "base.txt"), "initial\n");
    git(repository, "add", "."); git(repository, "commit", "-m", "Initial");
    git(repository, "checkout", "-b", "feature");
    writeFileSync(join(repository, "head-only.txt"), "HeadOnly\n");
    git(repository, "add", "."); git(repository, "commit", "-m", "Head");
    const head = git(repository, "rev-parse", "HEAD");
    git(repository, "checkout", "main");
    writeFileSync(join(repository, "base-only.txt"), "BaseOnly\n");
    git(repository, "add", "."); git(repository, "commit", "-m", "Base");
    git(repository, "branch", "origin/main");
    git(repository, "remote", "add", "origin", repository);
    git(repository, "merge", "--no-ff", "feature", "-m", "Merge tree");
    const merge = git(repository, "rev-parse", "HEAD");
    const output = join(root, "prepare-output"); writeFileSync(output, "");
    const result = spawnSync("bash", ["-e", "-c", shell(step("drift-tree"))], {
        cwd: repository, encoding: "utf8",
        env: { PATH: process.env.PATH, GH_TOKEN: "", GITHUB_SERVER_URL: "https://github.com", PR_HEAD: head, BASE: "main", RUNNER_TEMP: runner, GITHUB_OUTPUT: output },
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.equal(readFileSync(output, "utf8"), "ready=true\n");
    assert.equal(git(repository, "rev-parse", "HEAD"), merge);
    const worktree = join(runner, "pr-head");
    assert.equal(git(worktree, "rev-parse", "HEAD"), head);
    assert.equal(git(worktree, "rev-parse", "origin/main"), git(repository, "rev-parse", "origin/main"));
    const drift = run("release-notes-drift", { BASE: "main", PR_BODY: "## Added\n\n- Adds `head-only.txt`.\n- Adds `base-only.txt`.\n" }, worktree);
    assert.equal(drift.status, 0, drift.out);
    assert.match(drift.out, /bullet not in the diff.*base-only.txt/);
    assert.doesNotMatch(drift.out, /bullet not in the diff.*head-only.txt/);
    git(repository, "worktree", "remove", worktree);
    git(repository, "remote", "set-url", "origin", join(root, "missing-remote"));
    writeFileSync(output, "");
    const failed = spawnSync("bash", ["-e", "-c", shell(step("drift-tree"))], {
        cwd: repository, encoding: "utf8",
        env: { PATH: process.env.PATH, GH_TOKEN: "", GITHUB_SERVER_URL: "https://github.com", PR_HEAD: head, BASE: "main", RUNNER_TEMP: runner, GITHUB_OUTPUT: output },
    });
    assert.equal(failed.status, 0);
    assert.match(failed.stdout, /::warning title=Release notes drift not checked::/);
    assert.equal(readFileSync(output, "utf8"), "");
    assert.match(step("drift"), /working-directory: \$\{\{ runner.temp \}\}\/pr-head/);
});

test.after(() => rmSync(root, { recursive: true }));
