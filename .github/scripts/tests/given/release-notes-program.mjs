// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const WORKFLOW = readFileSync(".github/workflows/verify-release-notes.yml", "utf8");

// Every inline program, by the `// cratis:program <name>` marker on its second line.
export const PROGRAMS = Object.fromEntries(WORKFLOW.split("node - <<'JS'\n").slice(1).map(block => {
    const program = block.split("\n          JS\n")[0].split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");
    return [/^\/\/ cratis:program ([\w-]+)$/m.exec(program)[1], program];
}));
const directory = mkdtempSync(join(tmpdir(), "verify-release-notes-"));
export const PROGRAM_FILE = join(directory, "program.cjs");
writeFileSync(PROGRAM_FILE, PROGRAMS["release-notes"]);
export const DRIFT_FILE = join(directory, "drift.cjs");
writeFileSync(DRIFT_FILE, PROGRAMS["release-notes-drift"]);

export function run(body, { labels = ["minor"], author = "someone", base = "main", defaultBranch = "main", live,
    agentAuthors = "", requireAgentTrace = false } = {}) {
    const summaryFile = join(directory, `summary-${Math.random().toString(16).slice(2)}.md`);
    writeFileSync(summaryFile, "");
    // `live` is what the "Read the pull request as it is now" step wrote: a pull request object, or raw file text.
    let liveFile;
    if (live !== undefined) {
        liveFile = join(directory, `pull-request-${Math.random().toString(16).slice(2)}.json`);
        writeFileSync(liveFile, typeof live === "string" ? live : JSON.stringify(live));
    }
    const result = spawnSync(process.execPath, [PROGRAM_FILE], {
        encoding: "utf8",
        env: {
            PATH: process.env.PATH,
            PR_BODY: body,
            PR_LABELS: JSON.stringify(labels),
            PR_AUTHOR: author,
            AGENT_AUTHORS: agentAuthors,
            REQUIRE_AGENT_TRACE: String(requireAgentTrace),
            PR_BASE: base,
            DEFAULT_BRANCH: defaultBranch,
            GITHUB_REPOSITORY: "Cratis/Example",
            GITHUB_STEP_SUMMARY: summaryFile,
            ...(liveFile ? { PR_JSON: liveFile } : {}),
        },
    });
    const lines = result.stdout.split("\n");
    const decode = value => value.replace(/%0A/g, "\n").replace(/%0D/g, "\r").replace(/%3A/g, ":").replace(/%2C/g, ",").replace(/%25/g, "%");
    const errors = lines.filter(line => line.startsWith("::error ")).map(line => {
        const [, title, message] = /^::error title=([^:]*)::(.*)$/.exec(line);
        return { rule: decode(title).replace(/^Release notes: /, ""), message: decode(message) };
    });
    const notices = lines.filter(line => line.startsWith("::notice ")).map(decode);
    const warnings = lines.filter(line => line.startsWith("::warning ")).map(line => {
        const [, title, message] = /^::warning title=([^:]*)::(.*)$/.exec(line);
        return { rule: decode(title).replace(/^Release notes: /, ""), message: decode(message) };
    });
    return { status: result.status, stderr: result.stderr, errors, rules: errors.map(error => error.rule), notices,
        warnings, warned: warnings.map(warning => warning.rule), summary: readFileSync(summaryFile, "utf8") };
}

export const passes = (body, options) => {
    const result = run(body, options);
    assert.equal(result.status, 0, `expected to pass, got: ${JSON.stringify(result.errors, null, 1)}${result.stderr}`);
    return result;
};
export const fails = (body, rules, options) => {
    const result = run(body, options);
    assert.equal(result.status, 1, `expected to fail with ${rules}${result.stderr}`);
    assert.deepEqual([...new Set(result.rules)].sort(), [...rules].sort());
    return result;
};

export const GOOD = "Theme across the bullets.\n\n## Added\n\n- A new thing (#12)\n\n## Fixed\n\n- A fixed thing (#13)\n";
