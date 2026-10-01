// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Offline regressions for the exact inline program in verify-release-notes.yml, run as the runner runs it:
// a separate node process fed only through the environment. Never contacts GitHub.

import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { availableParallelism, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

const WORKFLOW = readFileSync(".github/workflows/verify-release-notes.yml", "utf8");
const PROGRAM_WORKFLOW = readFileSync(".github/workflows/verify-release-notes-program.yml", "utf8");
const SPEC_SOURCE = readFileSync(".github/scripts/tests/verify-release-notes.spec.mjs", "utf8");
const TEMPLATE = readFileSync(".github/pull_request_template.md", "utf8");
const BOOTSTRAP_SCRIPT = readFileSync(".github/scripts/bootstrap-common-workflows.sh", "utf8");
const BOOTSTRAP_WORKFLOW = readFileSync(".github/workflows/bootstrap-common-workflows.yml", "utf8");
const RELEASES = JSON.parse(readFileSync(".github/scripts/tests/fixtures/release-notes/releases.json", "utf8"));
const VERBATIM = "This pull request description is published verbatim as the release notes; editing the description re-runs this check.";

// Every inline program, by the `// cratis:program <name>` marker on its second line.
const PROGRAMS = Object.fromEntries(WORKFLOW.split("node - <<'JS'\n").slice(1).map(block => {
    const program = block.split("\n          JS\n")[0].split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");
    return [/^\/\/ cratis:program ([\w-]+)$/m.exec(program)[1], program];
}));
const PROGRAM = PROGRAMS["release-notes"];
const directory = mkdtempSync(join(tmpdir(), "verify-release-notes-"));
const PROGRAM_FILE = join(directory, "program.cjs");
writeFileSync(PROGRAM_FILE, PROGRAM);
const DRIFT_FILE = join(directory, "drift.cjs");
writeFileSync(DRIFT_FILE, PROGRAMS["release-notes-drift"]);

function run(body, { labels = ["minor"], author = "someone", base = "main", defaultBranch = "main", live } = {}) {
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

const passes = (body, options) => {
    const result = run(body, options);
    assert.equal(result.status, 0, `expected to pass, got: ${JSON.stringify(result.errors, null, 1)}${result.stderr}`);
    return result;
};
const fails = (body, rules, options) => {
    const result = run(body, options);
    assert.equal(result.status, 1, `expected to fail with ${rules}${result.stderr}`);
    assert.deepEqual([...new Set(result.rules)].sort(), [...rules].sort());
    return result;
};

const GOOD = "Theme across the bullets.\n\n## Added\n\n- A new thing (#12)\n\n## Fixed\n\n- A fixed thing (#13)\n";

test("the programs reach the runner only through the environment", () => {
    const runs = WORKFLOW.split(/\n {8}run: \|\n/).slice(1).map(block => block.split(/\n {6}- |\n {2}[\w-]+:\n/)[0]);
    assert.equal(runs.length, 3, "the live read, the gate and the drift comparison");
    for (const block of runs)
        assert.equal(block.includes("${{"), false, "no expression may be interpolated into a program");
    assert.deepEqual(Object.keys(PROGRAMS).sort(), ["read-pull-request", "release-notes", "release-notes-drift"]);
    for (const variable of ["PR_JSON: ${{ runner.temp }}/pull-request.json",
        "PR_BODY: ${{ github.event.pull_request.body }}",
        "PR_LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}",
        "PR_AUTHOR: ${{ github.event.pull_request.user.login }}",
        "PR_BASE: ${{ github.event.pull_request.base.ref }}",
        "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
        "GH_TOKEN: ${{ github.token }}",
        "NUMBER: ${{ github.event.pull_request.number }}",
        "workflow_call:",
        "runs-on: ${{ inputs.runs-on }}"])
        assert(WORKFLOW.includes(variable), variable);
    // A called workflow can only narrow its caller's grant: a cap here would take away pull-requests: read.
    assert.equal(/^\s*permissions:/m.test(WORKFLOW), false, "the reusable workflow sets no permissions of its own");
});

test("node is set up with the pinned setup-node before the program runs", () => {
    const setup = WORKFLOW.indexOf("uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020");
    assert(setup > 0, "the verify job sets up node with the pinned actions/setup-node");
    assert(/uses: actions\/setup-node@820762786026740c76f36085b0efc47a31fe5020[^\n]*\n\s+with:\n\s+node-version: 24\n/.test(WORKFLOW), "node-version 24");
    assert(setup < WORKFLOW.indexOf("        run: |\n"), "set up before the program step");
    assert(readFileSync(".github/workflows/verify-release-notes-program.yml", "utf8").includes("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"), "same SHA as the program-test workflow");
});

test("a product bullet naming a review feature with its delivered issue passes; a result word before the issue fails", () => {
    passes("## Added\n\n- Cross-provider review (#12)\n- Same-provider review (#13)\n- Opus-only review (#14)\n- Review workflow: cross-provider review (#15)\n");
    const result = run("## Changed\n\n- Real change (#2)\n- Cross-provider review pending (#3)\n");
    assert.equal(result.status, 1);
});

test("a bullet naming a review feature in the present tense passes; past-tense claims with multi-word model names fail", () => {
    passes("## Added\n\n- Review with two models (#5)\n- Review using multiple providers (#6)\n- Review with Codex (#7)\n- `review` prompt: review with Claude (#8)\n");
    for (const bullet of ["- Reviewed with Claude Code (#5)", "- Reviewed by GPT-5.5 (#5)", "- Reviewed by Opus 5.5 (#5)", "- The review workflow passed with no findings (#5)"]) {
        const result = run(`## Changed\n\n- Real change (#2)\n${bullet}\n`);
        assert.equal(result.status, 1, bullet);
    }
});

test("an outright review result or provenance claim in a bullet fails even when it ends in an issue reference", () => {
    for (const bullet of ["- The review workflow passed (#5)", "- Reviewed by Claude (#5)", "- Reviewed with Anthropic models only (#5)"]) {
        const result = run(`## Changed\n\n- Real change (#2)\n${bullet}\n`);
        assert.equal(result.status, 1, bullet);
    }
});

test("product bullets saying a review is required, requested or only applies pass", () => {
    passes("## Changed\n\n- Cross-provider review is required (#4)\n- A same-provider review is requested for drafts (#5)\n");
});

test("a description in the allowed shape passes and writes a summary", () => {
    const result = passes(GOOD);
    assert.match(result.summary, /follows the release-note contract/);
});

test("allowed issue references pass: (#n), (part of #n), see #n and Cratis/Repo#n", () => {
    passes("## Changed\n\n- Delivered (#1)\n- Partial (part of #2)\n- Related, see #3\n- Elsewhere Cratis/Chronicle#4\n");
});

test("the runner is an optional runs-on input that defaults to ubuntu-latest", () => {
    const input = /^      runs-on:\n((?: {8}.*\n)+)/m.exec(WORKFLOW);
    assert(input, "workflow_call declares a runs-on input");
    assert(/^ {8}type: string$/m.test(input[1]));
    assert(/^ {8}required: false$/m.test(input[1]));
    assert(/^ {8}default: ubuntu-latest$/m.test(input[1]));
    assert.equal(WORKFLOW.includes("runs-on: ubuntu-latest"), false, "the job does not hard-code its runner");
});

test("allowed ### sub-headings group bullets inside a section", () => {
    passes("## Changed\n\n### Breaking changes\n\n- Renamed `Foo` to `Bar`; rename call sites (#1)\n\n### Upgrade\n\n- Run the migration (#2)\n");
});

test("a thematic break separated by a blank line and continuation lines are fine", () => {
    passes("Lead.\n\n---\n\n## Added\n\n- A thing\n  that wraps\n\n  and continues (#1)\n");
});

test("closing and linking keywords fail with the rewrite to (#n) or (part of #n)", () => {
    for (const keyword of ["Closes", "Closed", "Close", "Fixes", "Fixed", "Fix", "Resolves", "Resolved", "Resolve",
        "Refs", "Ref", "References", "closes", "Fixes:"]) {
        const result = fails(`## Fixed\n\n- A fix\n\n${keyword} #93\n`, ["Closing or linking keyword"]);
        assert.match(result.errors[0].message, /with `\(#93\)` at the end of the bullet that delivers it, or `\(part of #93\)` if it must stay open/);
    }
    const other = fails("## Fixed\n\n- A fix (Fixes Cratis/Chronicle#7)\n", ["Closing or linking keyword"]);
    assert.match(other.errors[0].message, /plain `Cratis\/Chronicle#7`/);
    passes("## Fixed\n\n- A hotfix #3 regression and the prefix #4 issue (#5)\n");
});

test("a keyword directly inside parentheses quotes the bracketed text as the thing to replace", () => {
    const bracketed = fails("## Fixed\n\n- Thing (Refs #2880)\n", ["Closing or linking keyword"]);
    assert.match(bracketed.errors[0].message, /Replace `\(Refs #2880\)` with `\(#2880\)` if this change delivers it, or `\(part of #2880\)` if it must stay open\./);
    const bare = fails("## Fixed\n\n- Thing, Refs #2880\n", ["Closing or linking keyword"]);
    assert.match(bare.errors[0].message, /Replace `Refs #2880` with `\(#2880\)` at the end of the bullet that delivers it, or `\(part of #2880\)` if it must stay open\./);
});

test("every error names the rule, the line and the fix, and says the description is published verbatim", () => {
    const result = fails("## Fixed\n\n- A fix\n\nRefs #93\n", ["Closing or linking keyword"]);
    assert.match(result.errors[0].message, /^Closing or linking keyword: Line 5 `Refs #93`\. Replace `Refs #93`/);
    assert(result.errors[0].message.endsWith(VERBATIM));
    assert.match(result.summary, /\| 5 \| Closing or linking keyword \| Refs #93 \|/);
});

test("forbidden headings fail at any level, other ## headings fail, and each says where the content goes", () => {
    for (const heading of ["Overview", "Description", "What", "Why", "How", "Context", "Changes", "What changed",
        "Test plan", "Testing", "Tests", "Verification", "Verified", "Validation", "Quality", "Review", "Notes",
        "Notes for reviewers", "Limitations", "Known follow-up", "Acceptance", "Details"]) {
        for (const level of ["#", "##"])
            fails(`${level} ${heading}\n\n## Added\n\n- A thing (#1)\n`, ["Heading not allowed"]);
        fails(`## Added\n\n- A thing (#1)\n\n### ${heading}\n\n- More\n`, ["Heading not allowed"]);
    }
    assert.match(fails("## Test plan\n\n- Ran it\n\n## Added\n\n- A thing\n", ["Heading not allowed"]).errors[0].message,
        /Move test plans, verification, review notes and provenance to a pull request comment/);
    assert.match(fails("## Overview\n\nText\n\n## Added\n\n- A thing\n", ["Heading not allowed"]).errors[0].message,
        /only an optional `## Summary` section or one lead paragraph of 1-3 sentences, without a heading/);
    fails("## Added\n\n- A thing (#1)\n\n### Summary\n\n- More\n", ["Heading not allowed"]);
    fails("## Breaking changes\n\n- Renamed things\n\n## Added\n\n- A thing\n", ["Heading not allowed"]);
    fails("Overview\n---\n\n## Added\n\n- A thing\n", ["Heading not allowed"]);
    fails("## Added <!-- template note -->\n\n- A thing\n\n## `Changed`\n\n- Another\n", ["Heading not allowed"]);
});

test("allowed section names at another level fail and name the level-2 form", () => {
    const result = fails("### Fixed\n\n- A fix (#1)\n", ["Section heading level"]);
    assert.match(result.errors[0].message, /write `## Fixed`/);
    fails("# Added\n\n- A thing\n", ["Section heading level"]);
    fails("A lead paragraph.\n\n### Added\n\n- A thing (#1)\n", ["Section heading level"]);
    // Inside an open section a `###` with an allowed name is only a sub-heading; the level rule applies at the top.
    passes("## Changed\n\n### Added\n\n- A thing (#1)\n\n### Fixed\n\n- Another (#2)\n");
    passes("## Changed\n\n<h3>Removed</h3>\n\n- A thing (#1)\n");
});

test("an optional ## Summary may come first, once, as prose, and not together with a lead paragraph", () => {
    passes("## Summary\n\nThe theme across the bullets, in prose.\n\nA second short paragraph.\n\n## Added\n\n- A thing (#1)\n");
    passes("## Summary\n\nOnly a theme, then a fix.\n\n## Fixed\n\n- A fix (#1)\n");
    const level = fails("# Summary\n\nText\n\n## Added\n\n- A thing (#1)\n", ["Section heading level"]);
    assert.match(level.errors[0].message, /write `## Summary`/);
    fails("### Summary\n\nText\n\n## Added\n\n- A thing (#1)\n", ["Section heading level"]);
    const late = fails("## Added\n\n- A thing (#1)\n\n## Summary\n\nText\n", ["Section order"]);
    assert.match(late.errors[0].message, /optional, appears once and comes first/);
    fails("## Summary\n\nText\n\n## Summary\n\nMore\n\n## Added\n\n- A thing\n", ["Section order"]);
    fails("A lead paragraph.\n\n## Summary\n\nAnd a summary.\n\n## Added\n\n- A thing\n", ["Summary and lead paragraph"]);
    fails("## Summary\n\n## Added\n\n- A thing\n", ["Empty section"]);
    fails("## Summary\n\n- merge a project-owned file\n- fix the fallback\n", ["Summary is prose", "No release notes"]);
    fails("## Summary\n\nText\n\n### Highlights\n\nMore\n\n## Added\n\n- A thing\n", ["Summary is prose"]);
    fails("## Summary\n\nIt closes the loop (#12).\n\n## Added\n\n- A thing (#1)\n", ["Issue reference position"]);
});

test("a None / N/A / Nothing bullet is a filler, not a change", () => {
    for (const filler of ["None", "None.", "**None**", "**None.**", "_N/A_", "n/a", "N/A.", "Nothing", "NOTHING!", "*Nothing.*", "<b>None</b>", "1. None"]) {
        const bullet = /^\d/.test(filler) ? filler : `- ${filler}`;
        fails(`## Added\n\n- A thing (#1)\n\n## Removed\n\n${bullet}\n`, ["Section without bullets"]);
    }
    fails("## Removed\n\n- None.\n", ["No release notes", "Section without bullets"]);
    fails("## Added\n\n- A thing (#1)\n\n## Removed\n\n- None\n- N/A\n", ["Section without bullets"]);
    // A filler beside a real bullet does not fail; a filler with more text is a real bullet.
    passes("## Added\n\n- A thing (#1)\n- None\n");
    passes("## Removed\n\n- None of the deprecated overloads (#2)\n");
    passes("## Removed\n\n- `None`\n");
});

test("sections appear once, in order, only when they have bullets", () => {
    fails("## Fixed\n\n- A fix\n\n## Added\n\n- A thing\n", ["Section order"]);
    fails("## Added\n\n- A thing\n\n## Added\n\n- Another\n", ["Section order"]);
    fails("## Added\n\n- A thing\n\n## Removed\n\n<!-- nothing -->\n", ["Empty section"]);
    fails("## Added\n\n## Changed\n\n- A change\n", ["Empty section"]);
    // A section is its bullets: prose or an example alone is not a change list.
    fails("## Added\n\n- A thing\n\n## Fixed\n\n```csharp\nvar example = 1;\n```\n", ["Section without bullets"]);
    fails("## Added\n\n- A thing\n\n## Removed\n\nNone.\n", ["Section without bullets"]);
    fails("## Added\n\n- A thing\n\n## Deprecated\n\n**Client layer**: converts skip/take to pages.\n", ["Section without bullets"]);
    fails("## Added\n\nSome prose about the change.\n\n- A thing\n\n## Fixed\n\nA fixed thing, in prose.\n", ["Section without bullets"]);
    passes("## Added\n\n- A thing\n\n## Changed\n\n### Group\n\n- A grouped change\n\n## Fixed\n\n1. A numbered fix\n");
    passes("## Added\n\n- Nothing but a thing (#1)\n- None of the old options remain (#2)\n- N/A handling is added (#3)\n");
    passes("## Summary\n\nProse is what a summary is.\n\n## Added\n\n- A thing\n");
});

test("the preamble is at most one lead paragraph without bullets or sub-headings", () => {
    passes("One lead paragraph that\nwraps over two lines.\n\n## Added\n\n- A thing\n");
    fails("First paragraph.\n\nSecond paragraph.\n\n## Added\n\n- A thing\n", ["More than one lead paragraph"]);
    fails("- A loose bullet\n- Another\n\n## Added\n\n- A thing\n", ["Bullets outside a section"]);
    fails("### Highlights\n\n## Added\n\n- A thing\n", ["Sub-heading outside a section"]);
});

test("review, verification and provenance notes fail", () => {
    for (const line of ["Review: two reviewers approved", "Reviewed: yes", "Reviewed by Opus", "Reviewed by the maintainers", "- Reviewed by Opus",
        "**Verification:** all green", "- Tested: locally", "Testing: specs pass", "__Validation__: done", "Validation: rules apply",
        "Test plan: run the suite", "- Test plan: run the suite", "Verified: locally", "Tests: 400 passed", "- Tests: 400 passed",
        "- Testing: 12 specs passed", "- Verification: CI green", "Reviewed with a same-provider review", "cross-provider review pending",
        "Opus-only review", "Anthropic-only review", "GPT-only review", "The review workflow passed", "the review workflow returned findings",
        "The review workflow ran twice", "The review workflow found nothing",
        "\u{1F916} Generated with [Claude Code](https://claude.com/claude-code)", "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Written by Claude", "Drafted by an AI", "CI green, all tests passed.", "All tests pass.", "CI is green", "ci green",
        "- Done; all the tests passed (#1)", "Review: Opus-only (same-provider) review.", "- Review: no findings", "- Review: all good",
        "> Generated with Claude Code", "Test results: 12 passed", "This pull request was generated with Copilot"]) {
        const result = fails(`## Added\n\n- A thing\n\n${line}\n`, ["Review, verification or provenance note"]);
        assert.match(result.errors[0].message, /pull request comment/);
    }
    // The same in a lead paragraph position, and as a bullet where only the result wording makes it a note.
    fails("Reviewed: yes\n\n## Added\n\n- A thing\n", ["Review, verification or provenance note"]);
    fails("## Added\n\n- Testing: 400 passed\n", ["Review, verification or provenance note"]);
});

test("product bullets that mention review, validation or testing pass", () => {
    passes([
        "## Added",
        "",
        "- The review workflow now runs in parallel (#1)",
        "- Adds cross-provider model routing (#1)",
        "- Adds a same-provider fallback for model routing (#2)",
        "- Validation: rules now apply to commands (#3)",
        "- Testing: new Specification helpers (#4)",
        "- Verification: signatures are now checked on import (#5)",
        "- Validation of a command on the same provider now rejects empty names (#6)",
        "- Reviewers can now approve a change from the review workflow page (#7)",
        "- Review requests are now sent when a pull request is marked ready (#8)",
        "- Generated with a new source generator for read models (#9)",
        "- Tests can now run in parallel with `dotnet test` (#10)",
        "",
        "## Fixed",
        "",
        "- **Validation:** an empty name is now rejected (#11)",
        "",
    ].join("\n"));
});

test("a bold-only line naming a forbidden heading fails like the heading", () => {
    for (const line of ["**Test plan**", "- **Test plan**", "**Verification**", "**Notes for reviewers**", "__Testing__", "**Test plan:**", "**Notes**:", "**Why**"]) {
        const result = fails(`## Added\n\n- A thing\n\n${line}\n\n- More\n`, ["Heading not allowed"]);
        assert.match(result.errors[0].message, /pull request comment|Remove the heading/);
    }
    passes("## Added\n\n- **Breaking:** renamed `Foo` to `Bar`; rename call sites (#1)\n\n**Note** that this is fine\n");
});

test("an unclosed HTML comment fails because GitHub hides the rest", () => {
    const result = fails("## Added\n\n- A thing (#1)\n\n<!-- TODO explain\n\n## Fixed\n\n- A fix (#2)\n", ["Unclosed HTML comment"]);
    assert.match(result.errors[0].message, /^Unclosed HTML comment: Line 5 /);
    assert.match(result.errors[0].message, /hides everything after an unclosed/);
    passes("## Added\n\n- A thing (#1)\n\n<!-- a closed note -->\n\n- Another `<!--` in code (#2)\n");
});

test("a closing keyword followed by an issue or pull request URL fails", () => {
    for (const keyword of ["Fixes", "closes", "Resolved", "Refs"]) {
        fails(`## Fixed\n\n- A fix\n\n${keyword} https://github.com/Cratis/Example/issues/12\n`, ["Closing or linking keyword"]);
        fails(`## Fixed\n\n- A fix\n\n${keyword}: https://github.com/Cratis/Chronicle/pull/12\n`, ["Closing or linking keyword"]);
    }
    assert.match(fails("## Fixed\n\n- A fix\n\nFixes https://github.com/Cratis/Example/issues/12\n", ["Closing or linking keyword"]).errors[0].message,
        /Replace `Fixes https:\/\/github\.com\/Cratis\/Example\/issues\/12` with `\(#12\)` at the end of the bullet that delivers it/);
    assert.match(fails("## Fixed\n\n- A fix (Fixes https://github.com/Cratis/Chronicle/issues/7)\n", ["Closing or linking keyword"]).errors[0].message,
        /plain `Cratis\/Chronicle#7`/);
    passes("## Fixed\n\n- See https://github.com/Cratis/Example/issues/12 for the background (#3)\n");
});

test("(#n) closes an issue, so it belongs at the end of the bullet that delivers it", () => {
    passes("## Fixed\n\n- One (#1)\n- Two (#2) (#3)\n- Wrapped\n  over lines (#4)\n- Ends with a period (#5).\n  - Nested (#6)\n");
    passes("## Fixed\n\n- A fix (#1)\n\n  ```csharp\n  var example = 1;\n  ```\n\n- Code shows `(#2)` inline, not closed (#3)\n");
    for (const body of [
        "Lead paragraph that closes it (#1)\n\n## Added\n\n- A thing (#2)\n",
        "## Summary\n\nA theme (#1)\n\n## Added\n\n- A thing (#2)\n",
        "## Added\n\n- A thing (#1) that continues\n",
        "## Added\n\n- A thing (#1)\n  and then more text\n",
        "## Added\n\n- A thing (#1) and (#2) both done\n",
        "## Added\n\n- A thing\n\nA paragraph (#1)\n",
    ]) {
        const result = fails(body, ["Issue reference position"]);
        // Outside a bullet there is no text to quote; inside one the error quotes what follows the delivering (#n).
        const outside = /Release-action closes `\(#\d+\)` wherever it appears; put `\(#\d+\)` at the end of the bullet that delivers the issue, or write `\(part of #\d+\)` or `see #\d+`/;
        const inside = /`\(#\d+\)` is followed by `[^`]+`; move that text before `\(#\d+\)` so the issue reference ends the bullet, or write `\(part of #\d+\)` or `see #\d+`/;
        assert.match(result.errors[0].message, /^(?:Lead|## Summary|## Added\n\n- A thing\n\nA)/.test(body) ? outside : inside);
    }
});

test("relative links fail with an absolute replacement; https, #anchors and mailto pass", () => {
    const result = fails("## Added\n\n- See [the guide](Documentation/guide.md)\n", ["Relative link"]);
    assert.match(result.errors[0].message, /Use an absolute https:\/\/ link, for example https:\/\/github\.com\/Cratis\/Example\/blob\/main\/Documentation\/guide\.md/);
    for (const link of ["[x](./x.md)", "![shot](Source/shot.png)", "<a href=\"docs/x.md\">x</a>", "<img src=\"img.png\">"])
        fails(`## Added\n\n- A thing ${link}\n`, ["Relative link"]);
    fails("## Added\n\n- A [thing][guide]\n\n[guide]: Documentation/guide.md\n", ["Relative link"]);
    passes("## Added\n\n- [a](https://cratis.io) [b](#usage) [c](mailto:x@cratis.io) <img src=\"https://x/y.png\">\n");
});

test("template placeholders and agent transcripts fail", () => {
    fails("## Added\n\n- Short statement of what was added (#123)\n", ["Template placeholder"]);
    fails("## Added\n\n- Describe a new user-facing capability\n", ["Template placeholder"]);
    fails("## Added\n\n- A thing\n\n<details><summary>Original prompt</summary>\n\ndo it\n</details>\n", ["Agent transcript"]);
    fails("## Added\n\n- A thing\n\nLet Copilot coding agent set things up for you\n", ["Agent transcript"]);
});

test("fenced code, inline code and HTML comments are exempt", () => {
    passes([
        "<!-- ## Summary",
        "Closes #1, Test plan: [relative](docs/x.md), Short statement of what was added -->",
        "## Added",
        "",
        "- `Closes #2` and `Refs Cratis/Chronicle#3` are shown, not written (#4)",
        "- A workflow example:",
        "",
        "  ```yaml",
        "  # Summary",
        "  ## Test plan",
        "  Closes #5",
        "  Review: [x](docs/y.md)",
        "  ```",
        "",
        "~~~",
        "Fixes #6",
        "~~~",
    ].join("\n"));
});

test("a release-bound pull request needs a bullet under an allowed section", () => {
    for (const label of ["major", "minor", "patch"]) {
        const result = fails("A lead paragraph with no sections.\n", ["No release notes"], { labels: [label] });
        assert.match(result.errors[0].message, /^No release notes: The description\. /);
        fails("", ["No release notes"], { labels: [label] });
    }
    fails("## Added\n\n```\ncode only\n```\n", ["No release notes", "Section without bullets"], { labels: ["patch"] });
});

test("Windows line endings are read like Unix ones", () => {
    passes(GOOD.replace(/\n/g, "\r\n"));
    fails("## Fixed\r\n\r\n- A fix\r\n\r\nRefs #9\r\n", ["Closing or linking keyword"]);
});

test("Dependabot pull requests and pull requests into another base are skipped with a notice", () => {
    const bad = "## Overview\n\nRefs #1\n";
    for (const [options, reason] of [
        [{ author: "dependabot[bot]" }, /Dependabot/],
        [{ base: "feature/x", defaultBranch: "main" }, /targets feature\/x, not the default branch main/],
        [{ base: "" }, /not triggered by a pull request/],
    ]) {
        const result = run(bad, options);
        assert.equal(result.status, 0, JSON.stringify(options));
        assert.equal(result.errors.length + result.warnings.length, 0);
        assert.match(result.notices.join("\n"), reason);
        assert.match(result.notices.join("\n"), /the check re-runs when the description or a label changes/);
        assert.match(result.summary, /Not checked/);
    }
    for (const label of ["major", "minor", "patch"])
        fails(bad, ["Heading not allowed", "Closing or linking keyword", "No release notes"], { labels: [label] });
    fails(bad, ["Heading not allowed", "Closing or linking keyword", "No release notes"], { labels: ["bug", "minor"], base: "develop", defaultBranch: "develop" });
});

test("a no-release or unlabelled pull request is checked too, with warnings that never fail it", () => {
    const bad = "## Overview\n\nRefs #1\n\nStacked on #207.\n";
    for (const [labels, said] of [[[], /Not labelled yet/], [["bug", "documentation"], /Not labelled yet/], [["no-release"], /Labelled no-release/]]) {
        const result = run(bad, { labels });
        assert.equal(result.status, 0, JSON.stringify(labels));
        assert.equal(result.errors.length, 0);
        assert.deepEqual([...new Set(result.warned)].sort(), labels.includes("no-release")
            ? ["Closing or linking keyword", "Heading not allowed", "Reviewer instruction"]
            : ["Closing or linking keyword", "Heading not allowed", "No release notes", "Reviewer instruction"]);
        for (const warning of result.warnings) {
            assert.match(warning.message, said);
            assert.match(warning.message, /it fails once the pull request is labelled major, minor or patch/);
        }
        assert.match(result.summary, /warning\(s\)/);
    }
    // A no-release pull request may describe itself without a change list; a clean description reports no warning.
    const plain = run("Moves the CI cache to a shared volume.\n", { labels: ["no-release"] });
    assert.equal(plain.status, 0);
    assert.equal(plain.warnings.length, 0);
    const clean = run(GOOD, { labels: [] });
    assert.equal(clean.warnings.length + clean.errors.length, 0);
    assert.match(clean.summary, /none of major, minor or patch yet/);
});

test("more than one intent label is checked as release-bound whenever major, minor or patch is among them", () => {
    const bad = "## Overview\n\nRefs #1\n";
    for (const labels of [["major", "minor"], ["minor", "no-release"], ["patch", "no-release", "major"]])
        fails(bad, ["Heading not allowed", "Closing or linking keyword", "No release notes"], { labels });
    passes(GOOD, { labels: ["minor", "no-release"] });
});

test("labels, description, author and base come from the pull request as it is now, falling back to the event", () => {
    const pull = (overrides = {}) => ({ number: 7, body: GOOD, labels: [{ name: "patch" }], user: { login: "someone" }, base: { ref: "main" }, ...overrides });
    // The event said no-release with a bad body; the pull request now says patch with a good one.
    passes("## Overview\n", { labels: ["no-release"], live: pull() });
    // The event said patch with a good body; the label was since swapped for minor and the body broken.
    fails(GOOD, ["Closing or linking keyword"], { labels: ["patch"], live: pull({ body: `${GOOD}\nRefs #9\n`, labels: [{ name: "minor" }] }) });
    // Labels removed since the event: warnings only.
    const unlabelled = run(GOOD, { labels: ["patch"], live: pull({ body: "## Overview\n", labels: [] }) });
    assert.equal(unlabelled.status, 0);
    assert(unlabelled.warned.includes("Heading not allowed"));
    // Author and base are read live too.
    assert.match(run("## Overview\n", { live: pull({ body: "## Overview\n", user: { login: "dependabot[bot]" } }) }).notices.join(), /Dependabot/);
    assert.match(run("## Overview\n", { live: pull({ body: "## Overview\n", base: { ref: "release/1" } }) }).notices.join(), /targets release\/1/);
    // A null body is an empty description.
    fails(GOOD, ["No release notes"], { live: pull({ body: null }) });
    // A failed read (`{}`), an unreadable file or an API error object falls back to the event payload.
    for (const live of ["{}", "not json", JSON.stringify({ message: "Resource not accessible by integration" })])
        fails("## Overview\n", ["Heading not allowed", "No release notes"], { labels: ["minor"], live });
});

test("a (#n) inside an HTML comment still closes the issue, so it fails; comments stay ignored for everything else", () => {
    for (const body of [
        "<!-- follow-up (#7) -->\n\n## Added\n\n- A thing (#1)\n",
        "## Added\n\n- A thing (#1) <!-- also (#8) -->\n",
        "## Added\n\n- A thing (#1)\n  <!-- (#9) -->\n- Other (#2)\n",
        "## Added\n\n- A thing (#1)\n<!--\nsee (#9)\n-->\n",
        "## Added\n\n- A thing (#1) <!-- (#3) -->\r\n",
    ]) {
        const result = fails(body, ["Issue reference position"]);
        assert.match(result.errors[0].message, /sits inside an HTML comment.*release-action still closes #\d+.*never put issue references in comments/);
    }
    // Only fenced and inline code hide a reference, as in release-action, and what surrounds removed code joins up.
    passes("## Added\n\n- A thing (#1)\n\n```\n<!-- (#9) -->\n```\n- Another `<!-- (#8) -->` (#2)\n");
    fails("## Added\n\n- Thing (`x`#5) done\n", ["Issue reference position"]);
    passes("## Added\n\n- A \u{1F680} thing \u{1F389} (#5)\n");
    // Comments are still ignored for headings, keywords, placeholders and links.
    passes("## Added\n\n- A thing (#1) <!-- Closes Cratis/Chronicle#3, ## Test plan, [x](docs/y.md) -->\n");
});

test("closing emphasis, <br> and further issue references may follow the delivering (#n)", () => {
    passes("## Fixed\n\n- **Thing** (#3)**\n- Thing (#3) (part of #4)\n- **Thing (#3)**\n- _Thing (#3)_\n- Thing (#3)<br>\n- Thing (#3)  <br/>\n- Thing (#3) (#4).\n");
    fails("## Fixed\n\n- Thing (#3) and more\n", ["Issue reference position"]);
    const followed = fails("## Fixed\n\n- Thing (#12), see #11 and the follow-up work\n", ["Issue reference position"]);
    assert.match(followed.errors[0].message, /`\(#12\)` is followed by `, see #11 and the follow-up work`; move that text before `\(#12\)`/);
    const longer = fails(`## Fixed\n\n- Thing (#12) and ${"more words ".repeat(12)}\n  spilling onto a continuation line with \`code\`\n`, ["Issue reference position"]);
    assert.match(longer.errors[0].message, /`\(#12\)` is followed by `and more words more words[^`]* \.\.\.`; move that text before `\(#12\)`/);
    assert.equal(longer.errors[0].message.includes("code"), false, "the quoted text is capped");
    fails("## Fixed\n\n- Thing (#3) (part of #4) and more\n", ["Issue reference position"]);
    fails("## Fixed\n\n- Thing (#3) **and** more\n", ["Issue reference position"]);
});

test("sentence punctuation after the delivering (#n) is still the end of the bullet", () => {
    for (const ending of [".", ",", ";", ":", "!", "?", " ,"])
        passes(`## Added\n\n- Thing (#1)${ending}\n`);
    passes("## Added\n\n- Thing (#1):\n  - detail one\n  - detail two\n- Other (#2)\n");
    fails("## Added\n\n- Thing (#1): more text\n", ["Issue reference position"]);
    fails("## Added\n\n- Thing (#1):\n  continued text\n", ["Issue reference position"]);
});

test("cross-repository and (part of|see ...) references may follow the delivering (#n)", () => {
    passes("## Added\n\n- Thing (#1) (Cratis/Arc#7)\n- Thing (#2) (part of Cratis/Arc#7)\n- Thing (#3) (see #9)\n- Thing (#4) (see Cratis/Arc#9).\n- Thing (#5) (#6) (Cratis/Arc#7)\n");
    fails("## Added\n\n- Thing (#1) (Cratis/Arc#7) and more\n", ["Issue reference position"]);
    fails("## Added\n\n- Thing (#1) (see #9) and more\n", ["Issue reference position"]);
    fails("## Added\n\n- Thing (#1) (see the docs)\n", ["Issue reference position"]);
});

test("(#56, #57) closes nothing and fails: one (#n) per issue", () => {
    for (const grouped of ["(#56, #57)", "(#56,#57)", "(#56 #57)", "(#56, #57, #58)"]) {
        const result = fails(`## Added\n\n- Thing ${grouped}\n`, ["Issue reference position"]);
        assert.match(result.errors[0].message, /closes nothing: release-action only recognises a `\(#n\)` of its own\. Write each delivered issue as its own `\(#n\)`: `\(#56\) \(#57\)/);
    }
    fails("## Summary\n\nTheme (#56, #57)\n\n## Added\n\n- Thing (#1)\n", ["Issue reference position"]);
    passes("## Added\n\n- Thing (#56) (#57)\n- Thing (part of #58, #59)\n- Thing, see #58 and #59 (#60)\n- Thing `(#56, #57)` (#61)\n");
});

test("closing keywords fail when wrapped in emphasis or a link", () => {
    for (const line of ["**Closes** #1", "**Closes #1**", "_Fixes_ #2", "__Resolves__: #2", "Closes [#3](https://github.com/o/r/issues/3)",
        "Fixes: [Cratis/Arc#4](https://github.com/Cratis/Arc/issues/4)", "Resolves [the issue](https://github.com/Cratis/Example/issues/6)",
        "Closes <https://github.com/Cratis/Example/issues/6>", "Closes [#3][ref]\n\n[ref]: https://github.com/o/r/issues/3",
        "Fixes <a href=\"https://github.com/Cratis/Example/issues/6\">#6</a>", "**Refs** [#5](https://github.com/Cratis/Example/issues/5)",
        "- A fix (#9) Closes [#3](https://github.com/o/r/issues/3)"])
        assert(run(`## Fixed\n\n- A fix (#9)\n\n${line}\n`).rules.includes("Closing or linking keyword"), line);
    passes("## Fixed\n\n- The [fix strategy](https://cratis.io/fix) is now **configurable** (#1)\n- Adds _closes_ support (#2)\n");
});

test("HTML that renders as a reviewer heading fails like the Markdown heading; HTML section names are not sections", () => {
    for (const html of ["<h2>Test plan</h2>", "<h3>Verification</h3>", "<H2>Notes for reviewers</H2>", "<h4>Testing</h4>", "<b>Test plan</b>",
        "<strong>Verification</strong>", "<b>Test plan:</b>", "<p><b>Tests</b></p>", "- <b>Test plan</b>",
        "<details><summary>Test plan</summary>", "<summary>Verification</summary>"]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n${html}\n\n- ran it\n`, ["Heading not allowed"]);
        assert.match(result.errors[0].message, /pull request comment/);
    }
    const level = fails("<h2>Added</h2>\n\n- A thing (#1)\n", ["Section heading level"]);
    assert.equal(level.errors[0].rule, "Section heading level");
    assert.match(level.errors[0].message, /write `## Added`/);
    passes("## Added\n\n- Renders <b>bold</b> and <h2> headings in notes (#1)\n- Adds `<h2>Test plan</h2>` to the output (#2)\n");
});

test("ordinary product bullets are not mistaken for review notes, results or provenance", () => {
    const bullets = [
        "Specifications can now generate a test plan for a feature (#1)",
        "Test plan generation is available from the CLI (#2)",
        "The tests project template now targets .NET 10 (#3)",
        "Tests can now be filtered by tag (#4)",
        "Adds a `Verified` flag to signed events (#5)",
        "Events are now verified against their schema before append (#6)",
        "Reviewed items are now shown first in the workbench (#7)",
        "Reviewers see pending items in the inbox (#8)",
        "Review: requests are now sent when a pull request is ready (#9)",
        "Review: submitting a review now notifies the author (#10)",
        "Reviewed: items can be filtered in the workbench (#11)",
        "CI templates now target Ubuntu 24.04 (#12)",
        "Adds a CI status badge to the project template (#13)",
        "Release notes are now generated from pull requests (#14)",
        "Notes can now be attached to an observer (#15)",
        "A summary view is now available for projections (#16)",
        "Summary: the read model now includes totals (#17)",
        "Fixes the #1 priority ordering bug for queues (#18)",
        "See https://github.com/Cratis/Chronicle/blob/main/Source/Foo.cs#L10 for details (#19)",
        "Adds C# 14 support for generators (#20)",
        "Adds support for F# (#3)",
        "Supports `#region` folding in the generated code (#21)",
        "Supports #region folding in the generated code (#22)",
        "Renames the type in `Foo` (#12) (#13)",
        "Failed commands now return a 400 status (#23)",
        "Failing observers are now retried with backoff (#24)",
        "Passing a null name now throws an ArgumentNullException (#25)",
        "Green-field templates now include a Dockerfile (#26)",
        "Text generated with OpenAI is now cached (#27)",
        "Content written by Claude can now be imported as a document (#28)",
        "Code generated by Copilot can now be attached to a review (#29)",
        "Generated with the new source generator (#30)",
        "Adds an integration with Anthropic models for summarization (#31)",
        "Validation: a command with an empty name is now rejected (#32)",
        "Testing: new Specification helpers are available (#33)",
        "Verification: signatures are now checked on import (#34)",
        "Tests: the runner now supports parallel execution (#35)",
        "Test results are now exposed as a read model (#36)",
        "Test results: a new report format is available (#37)",
        "Tested: how the runner handles timeouts is now configurable (#38)",
        "The Copilot coding agent can now be configured per tenant (#39)",
        "Original prompt text is now preserved on the audit log (#40)",
        "The build now passes the analyzer settings through (#42)",
        "Checks now pass through the pipeline in order (#43)",
        "Refs are now resolved lazily (#44)",
        "References to removed types are reported as diagnostics (#45)",
        "Closes the connection gracefully on shutdown (#46)",
        "Closed sessions are purged after one hour (#49)",
        "Adds `Closes` and `Refs` as keywords for the parser (#53)",
        "The release is published when all tests pass (#54)",
        "When all tests pass, the release is published (#55)",
        "Publishing waits until CI is green (#56)",
        "AI models can now be selected per request (#57)",
        "An LLM-generated title is now stored on the read model (#58)",
        "Written by the user, the note is stored verbatim (#59)",
        "Approved requests are now routed to the owner (#60)",
        "Pull requests reviewed by the new bot are labelled (#61)",
        "Reviewed by status is now shown on the dashboard (#62)",
        "Reviewed by default, pull requests are now assigned (#63)",
        "Reviewed with Anthropic models is now supported (#64)",
        "Reviewed by Claude Code is now a supported reviewer (#65)",
        "Cross-provider review pending until the router lands (#66)",
        "The review workflow returned findings for the pull request now appear inline (#67)",
        "The review workflow ran twice as fast (#68)",
        "Adds a [Fixes](https://github.com/Cratis/Example/issues/3) page to the docs (#69)",
        "See the [Closes](https://github.com/Cratis/Example/issues/3) docs (#70)",
        "Use [the docs](https://cratis.io/docs and read on (#71)",
    ];
    assert(bullets.length >= 40);
    for (const bullet of bullets)
        assert.equal(run(`## Added\n\n- ${bullet}\n`).status, 0, bullet);
    passes("## Added\n\n" + bullets.map(bullet => `- ${bullet}`).join("\n") + "\n");
    passes("### Added in Chronicle\n\n".replace(/^/, "## Added\n\n") + "- A thing (#1)\n");
    passes("## Summary\n\nReviews are faster, tests run in parallel and a CI template ships with it.\n\n## Added\n\n- A thing (#1)\n");
    // A `## Summary` and a lead paragraph are product wording too: the note must end the clause to be one.
    for (const text of ["Reviewed with the new review page, comments are now threaded.", "Cross-provider review: routing is now configurable.",
        "Same-provider review: fallback now works.", "The review workflow ran twice as fast."]) {
        passes(`## Summary\n\n${text}\n\n## Added\n\n- A thing (#1)\n`);
        passes(`${text}\n\n## Added\n\n- A thing (#1)\n`);
    }
    fails("## Summary\n\nCross-provider review pending\n\n## Added\n\n- A thing (#1)\n", ["Review, verification or provenance note"]);
    fails("Opus-only review\n\n## Added\n\n- A thing (#1)\n", ["Review, verification or provenance note"]);
    // A stand-alone line outside a bullet, a summary or the lead paragraph keeps the loose tail.
    fails("## Added\n\n- A thing (#1)\n\nSame-provider review: no findings\n", ["Review, verification or provenance note"]);
});

test("a heading is a line of the description: its keywords, (#n), links, placeholders and provenance fail too", () => {
    const under = heading => `## Added\n\n${heading}\n\n- A thing (#1)\n`;
    for (const heading of ["### Big feature (#5)", "<h3>Thing (#4)</h3>", "### Thing (#4) and more"])
        fails(under(heading), ["Issue reference position"]);
    fails("## Added <!-- (#7) -->\n\n- A thing (#1)\n", ["Issue reference position"]);
    fails(under("### Big feature\n\n<!-- (#8) -->"), ["Issue reference position"]);
    for (const heading of ["### Closes #5", "### **Fixes** #5", "<h3>Refs #5</h3>", "### Resolves https://github.com/Cratis/Example/issues/5"])
        fails(under(heading), ["Closing or linking keyword"]);
    for (const heading of ["### See [x](docs/x.md)", "<h3><a href=\"docs/x.md\">x</a></h3>"])
        fails(under(heading), ["Relative link"]);
    fails(under("### Generated with Claude Code"), ["Review, verification or provenance note"]);
    fails(under("### Reviewed with a cross-provider review"), ["Review, verification or provenance note"]);
    fails(under("### Describe a new user-facing capability"), ["Template placeholder"]);
    // A setext heading is not an allowed section, but its text still closes the issue, and is reported at the text line, not its underline.
    const setext = run(under("Big feature (#5)\n---"));
    assert(setext.rules.includes("Issue reference position"));
    assert(setext.errors.some(error => /^Issue reference position: Line 3 `Big feature \(#5\)`/.test(error.message)));
    passes("## Added\n\n### Sub-heading with `Closes #5` in code\n\n- A thing (#1)\n\n### See [x](https://cratis.io/x)\n\n- Another (#2)\n");
});

test("a link keeps its text when the text is a closing keyword", () => {
    for (const line of ["[Closes #3](https://github.com/Cratis/Example/issues/3)", "[Closes](https://github.com/Cratis/Example/issues/3)",
        "[Fixes](https://github.com/Cratis/Example/pull/3)", "**[Fixes](https://github.com/Cratis/Example/issues/3)**",
        "<a href=\"https://github.com/Cratis/Example/issues/3\">Closes</a>"])
        fails(`## Fixed\n\n- A fix (#9)\n\n${line}\n`, ["Closing or linking keyword"]);
    // ... but only where the link opens a clause; inside a sentence the link is reduced to its URL.
    for (const line of ["- [Fixes](https://github.com/Cratis/Example/issues/3) (#9)", "- A fix. [Closes](https://github.com/Cratis/Example/issues/3) (#9)",
        "- A fix ([Fixes](https://github.com/Cratis/Example/issues/3)) (#9)", "- A fix; [Refs](https://github.com/Cratis/Example/pull/3) (#9)"])
        fails(`## Fixed\n\n${line}\n`, ["Closing or linking keyword"]);
    passes("## Fixed\n\n- Reverts [the fix](https://github.com/Cratis/Example/pull/3) for observers (#1)\n- See [#3](https://github.com/Cratis/Example/issues/3) (#2)\n");
});

test("a multi-line <summary> and an italic-only line name a forbidden heading like the one-line forms", () => {
    for (const name of ["Test plan", "Verification", "Notes for reviewers", "Testing"]) {
        fails(`## Added\n\n- A thing (#1)\n\n<details>\n  <summary>\n    ${name}\n  </summary>\n\n  - ran it\n</details>\n`, ["Heading not allowed"]);
        fails(`## Added\n\n- A thing (#1)\n\n<details><summary>\n${name}</summary>\n\n- ran it\n</details>\n`, ["Heading not allowed"]);
        for (const wrapper of ["*", "_", "- *", "- _"])
            fails(`## Added\n\n- A thing (#1)\n\n${wrapper}${name}${wrapper.replace("- ", "")}\n\n- More\n`, ["Heading not allowed"]);
        fails(`## Added\n\n- A thing (#1)\n\n<i>${name}</i>\n\n- More\n`, ["Heading not allowed"]);
        fails(`## Added\n\n- A thing (#1)\n\n*${name}*:\n\n- More\n`, ["Heading not allowed"]);
    }
    const located = fails("## Added\n\n- A thing (#1)\n\n<details>\n  <summary>\n    Test plan\n  </summary>\n</details>\n", ["Heading not allowed"]);
    assert.match(located.errors[0].message, /Line 6 /);
    passes("## Added\n\n- An *italic* word and _another_ one (#1)\n- *Emphasis* opens a bullet, then continues (#2)\n\n<details>\n  <summary>\n    More examples\n  </summary>\n\n  - Example\n</details>\n");
});

test("provenance fails at the start of a clause; product wording around the same words passes", () => {
    for (const line of ["Review: Opus-only (same-provider) review.", "Reviewed with a cross-provider review.", "The review workflow passed.",
        "Co-Authored-By: Claude <noreply@anthropic.com>", "Cross-provider review pending", "Same-provider review: no findings", "Quick cross-provider review (Opus 5.5 on GPT-written code).",
        "Reviewed with Anthropic models only (no cross-provider review available right now).", "Reviewed by Claude", "Opus-only review", "Anthropic-only review (same-provider).",
        "- Reviewed with a same-provider review", "- Cross-provider review pending (#3)", "- The review workflow passed", "- The review workflow returned findings",
        "> Co-authored-by: Someone <someone@example.com>", "Done - the review workflow ran twice", "Review: the review workflow found nothing"]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n${line}\n`, ["Review, verification or provenance note"]);
        assert.match(result.errors[0].message, /pull request comment/, line);
    }
    // Indented under a bullet, the line continues that bullet and still reads as a note.
    fails("## Added\n\n- A thing\n\n  Opus-only review\n", ["Review, verification or provenance note"]);
    passes([
        "## Added",
        "",
        "- Adds same-provider review routing to the review workflow (#1)",
        "- Same-provider review routing now falls back to Sonnet (#2)",
        "- Cross-provider review is now selectable per repository (#3)",
        "- Cross-provider review, when enabled, now routes to a second model (#4)",
        "- Opus-only review can now be enforced with a policy (#5)",
        "- The review workflow passed to the runner is now validated (#6)",
        "- The review workflow returned by the API now includes the reviewer (#7)",
        "- The review workflow found in the repository is now used by default (#8)",
        "- The review workflow now ran checks in parallel for large diffs (#9)",
        "- The review workflow ran through the queue is now traced (#10)",
        "- Co-authored-by trailers are now added to squash commits (#11)",
        "- Adds support for trailers: Co-Authored-By: is now parsed (#12)",
        "- Adds a Co-Authored-By: header to generated commits (#13)",
        "- Reviewed with the new review page, comments are now threaded (#14)",
        "- Anthropic-only review policies can now be configured per repository (#15)",
        "- Reviews use a same-provider fallback when the primary model is down (#16)",
        "- Supports a cross-provider review mode for pull request checks, configured in `review.json`",
        "  and a second continuation line about the cross-provider review mode (#17)",
        "",
        "## Changed",
        "",
        "- The Review workflow now reports which provider reviewed the change in its check summary (#18)",
        "",
    ].join("\n"));
});

test("the program workflow runs whenever a file the spec reads changes", () => {
    const paths = section => {
        const block = PROGRAM_WORKFLOW.split(`\n  ${section}:\n`)[1].split("\n    paths:\n")[1].split(/\n(?! {6}- )/)[0];
        return block.split("\n").map(line => line.replace(/^ {6}- /, "").trim());
    };
    const covers = (patterns, file) => patterns.some(pattern => pattern === file
        || (pattern.endsWith("/**") && file.startsWith(pattern.slice(0, -2))));
    const read = [...SPEC_SOURCE.matchAll(/readFileSync\(\s*"(\.github\/[^"]+)"/g)].map(match => match[1]);
    assert(read.length >= 3, read.join());
    for (const section of ["pull_request", "push"])
        for (const file of [...read, ".github/workflows/verify-release-notes-program.yml", ".github/scripts/tests/verify-release-notes.spec.mjs"])
            assert(covers(paths(section), file), `${section} paths must cover ${file}`);
});

test("real release bodies: flagged ones fail with the expected rules, clean ones pass", () => {
    assert(RELEASES.length >= 10);
    assert(RELEASES.some(release => release.passes) && RELEASES.some(release => !release.passes));
    for (const release of RELEASES) {
        const result = run(release.body);
        assert.equal(result.status, release.passes ? 0 : 1, `${release.repo} ${release.tag}: ${JSON.stringify(result.rules)}`);
        assert.deepEqual([...new Set(result.rules)].sort(), release.rules, `${release.repo} ${release.tag}`);
    }
});

test("the pull request template itself only fails on its placeholders", () => {
    const result = run(TEMPLATE);
    assert.deepEqual([...new Set(result.rules)], ["Template placeholder"]);
    // The placeholder bullets carry no issue reference: a description that keeps them all and only fills in the text
    // must not read as a clean one, and the comment above them already shows the (#n) syntax.
    assert.equal(TEMPLATE.split("-->\n")[1].includes("(#"), false, "the placeholder bullets carry no (#n)");
    let issue = 0;
    const filled = TEMPLATE.replace(/^(- )Short statement of .*$/gm, (_, bullet) => `${bullet}A real change (#${++issue})`);
    assert.equal(issue, 6);
    assert.equal(filled.split("-->\n")[1].includes("(#123)"), false, "the clean case does not repeat the comment's example number");
    passes(filled);
    passes(TEMPLATE.replace(/^(- )Short statement of .*$/gm, "$1A real change"));
    for (const rule of ["published verbatim", "(#123)", "(part of #123)", "Closes #123", "Test plan", "pull request comment", "https://",
        "## Summary", "re-runs the release-notes check where it is installed", "only when they have bullets", "headings and the summary included",
        "(there even `### Added` is only a sub-heading); a section itself is always level 2"])
        assert(TEMPLATE.includes(rule), rule);
});

test("the bootstrap installs the canonical thin caller", () => {
    const encoded = /BOOTSTRAPPED_FILES\["\.github\/workflows\/verify-release-notes\.yml"\]="([^"]+)"/.exec(BOOTSTRAP_SCRIPT);
    assert(encoded, "wrapper registered in bootstrap-common-workflows.sh");
    const wrapper = Buffer.from(encoded[1], "base64").toString("utf8");
    for (const required of [
        "name: Verify Release Notes",
        "cancel-in-progress: true",
        "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "types: [opened, edited, reopened, synchronize, labeled, unlabeled, ready_for_review]",
        "permissions:\n  contents: read",
        "permissions:\n  contents: read\n  pull-requests: read\n",
        "jobs:\n  release-notes:\n    uses: Cratis/Workflows/.github/workflows/verify-release-notes.yml@main",
        // RUNNER_GATE is an outage escape hatch, normally unset: the default stays the hosted runner of a public repository.
        "    with:\n      runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}\n",
    ])
        assert(wrapper.includes(required), required);
    assert.equal(/^\s*(?:run|steps):/m.test(wrapper), false, "the wrapper carries no logic");
    assert(wrapper.includes("RUNNER_GATE is an outage escape hatch and is normally unset"), "RUNNER_GATE is documented as an escape hatch");
    assert(wrapper.includes("`runs-on: ${{ vars.RUNNER_GATE || 'cratis-arc' }}`"), "a private repository is pointed at its own fallback");
    assert.equal(/setting the RUNNER_GATE/.test(wrapper), false, "the wrapper does not tell a repository to set RUNNER_GATE permanently");
    const documented = wrapper.trimEnd().split("\n").map(line => `#   ${line}`.trimEnd()).join("\n");
    assert(BOOTSTRAP_SCRIPT.includes(documented), "the decoded comment matches the encoded wrapper");
    assert(wrapper.trimEnd().endsWith("      runs-on: ${{ vars.RUNNER_GATE || 'ubuntu-latest' }}"), "the variable-driven runs-on is the caller's only input");
    assert(WORKFLOW.includes("runs-on: ${{ inputs.runs-on }}") && /runs-on:\n\s+description:[\s\S]*?\n\s+type: string\n\s+required: false\n\s+default: ubuntu-latest\n/.test(WORKFLOW),
        "the reusable workflow takes runs-on, defaulting to ubuntu-latest");
    // Deliberately not a bootstrap push path: logic changes do not change the wrapper, and must not trigger
    // organization-wide writes (bootstrap-package-update-safety.test.py pins that list).
    assert.equal(BOOTSTRAP_WORKFLOW.includes("- \".github/workflows/verify-release-notes.yml\""), false);
});

test("the bootstrap leaves private and deliberately customized repositories to their own change", () => {
    const ignored = JSON.parse(/^ {2}REPOS_TO_IGNORE: '([^']+)'$/m.exec(BOOTSTRAP_WORKFLOW)[1]);
    // Private repositories have no GitHub-hosted Actions budget: their wrappers run on cratis-arc, and Strategy and
    // Identity have none of the canonical ones (removed on purpose, or a new repository whose CI is its own).
    const privateRepositories = ["Studio", "Direct", "Ensemble", "Infrastructure", "Experiments", "Strategy", "Identity"];
    // Public repositories whose update-packages.yml differs from the canonical wrapper on purpose.
    const customized = ["Ante", "Chronicle.Elixir", "Components", "Orleans"];
    for (const repository of [...privateRepositories, ...customized]) {
        assert(ignored.includes(repository), `${repository} must stay in REPOS_TO_IGNORE`);
        assert(new RegExp(`^ {2}#[^\\n]*\\b${repository.replace(".", "\\.")}\\b`, "m").test(BOOTSTRAP_WORKFLOW), `${repository} needs a comment saying why it is ignored`);
    }
    assert(BOOTSTRAP_WORKFLOW.includes("cratis-arc") && BOOTSTRAP_WORKFLOW.includes("RUNNER_GATE"), "the reason names the private runner routing");
    assert(BOOTSTRAP_WORKFLOW.includes("vars.ENSEMBLE_RUNNER"), "Ensemble's own runner variable is named");
    assert.equal(ignored.includes("Workflows"), true);
    // The ignore list is what keeps the overwrite semantics untouched: no repository-visibility branch is added.
    assert.equal(/visibility|isPrivate/i.test(BOOTSTRAP_SCRIPT), false);
});

test("how, where and whether a change was checked is a note, wherever it stands", () => {
    for (const line of ["Verified by building the sample against the local packages.", "Storybook only; verified in Storybook.",
        "Tested locally with `yarn test`.", "Verified locally: route specs pass", "not yet verified", "**Not verified against a real kernel**",
        "Not tested on Windows.", "Docs-only change.", "The rest was verified against a real running sandbox.",
        "Reproduced end-to-end with the Kotlin client.", "Checked locally:", "Checked locally: `./gradlew build` passes.",
        "Local: route specs 492/492", "Tier 1 PASS", "Local: Tier 1 PASS, Integration 110/110", "Testing.Specs 518/518",
        "9 of 9 runs green", "`dotnet build` - 0 warnings, 0 errors", "`dotnet test`: 1526/1526 passing", "12 passed, 0 failed",
        "`Cli.Specs`: 1,111 tests passing.", "CI: all green", "Checks: 4 of 4", "Gates: green", "Note for reviewers: 356 of 408 specs fail on macOS"]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n${line}\n`, ["Review, verification or provenance note"]);
        assert.match(result.errors[0].message, /pull request comment/, line);
    }
    // The same inside a product bullet, a `## Summary` and the lead paragraph.
    for (const body of ["## Fixed\n\n- A fix (#1)\n- Verified locally with the sample app.\n",
        "## Fixed\n\n- Registration no longer drops entries. Reproduced end-to-end with the Kotlin client (#1)\n",
        "## Summary\n\nFaster appends; specs 518/518.\n\n## Fixed\n\n- A fix (#1)\n",
        "Faster appends, not yet verified against a real kernel.\n\n## Fixed\n\n- A fix (#1)\n".replace(", not", ". Not")])
        fails(body, ["Review, verification or provenance note"]);
    const verification = fails("## Added\n\n- A thing (#1)\n\nTested locally.\n", ["Review, verification or provenance note"]);
    assert.match(verification.errors[0].message, /How or where the change was checked, what was not checked, and test or CI results are for reviewers: move them to a pull request comment\./);
});

test("provenance of a check is a note that asks for compatibility as a fact", () => {
    for (const line of ["Verified against Arc.TypeScript main (v0.34.0).", "Tested against the default branch.", "Tested against a real kernel.",
        "- Verified with Chronicle main.", "Verified end to end against a live server.", "Checked on main."]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n${line}\n`, ["Review, verification or provenance note"]);
        assert.match(result.errors[0].message, /checked against is for reviewers: move it to a pull request comment\. If compatibility matters to the reader, state it as a fact, for example `Requires @cratis\/arc 0\.34 or later`\./, line);
    }
});

test("stacking, retargeting, merge and deploy instructions and draft status are reviewer instructions", () => {
    for (const line of ["Stacked on #207.", "Stacked on Cratis/Arc#12; retarget to main once it merges.", "Retarget to main once #207 merges.",
        "Part of the consolidation. Stacked on #330.", "> Stacked on #46 \u2014 that PR carries the discovery", "Stacked on top of the other pull request.",
        "This PR should be deployed separately.", "This should be deployed separately from the kernel.", "This pull request is for review rather than merge.",
        "Draft: opened so the change is not lost.", "**Draft:** still needs a green build", "Reviewers: please look at the projections first.",
        "Do not merge until the kernel ships.", "DNM: waiting on Chronicle", "Merge after #12.", "Depends on Cratis/Chronicle#2880.",
        "Once merging this, bump the kernel.", "This branch needs #12 first.", "Should merge promptly: this should be released first."]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n${line}\n`, ["Reviewer instruction"]);
        assert.match(result.errors[0].message, /Stacking, retargeting, merge or deploy order and draft status are for reviewers: move them to a pull request comment\. If consumers must upgrade in a set order, write that as an upgrade bullet, for example `Upgrade the Chronicle kernel to 19\.25 before this client`\./, line);
    }
});

test("headings and labels that report local checks or results fail; a bare CI sub-heading groups bullets", () => {
    for (const heading of ["Checks (local)", "Checks (local, mirroring dotnet-build.yml)", "Local checks", "CI results", "Checked locally",
        "Out of scope", "Not verified", "Not tested", "Draft"]) {
        const result = fails(`## Added\n\n- A thing (#1)\n\n### ${heading}\n\n- More\n`, ["Heading not allowed"]);
        assert.match(result.errors[0].message, /pull request comment/, heading);
        fails(`## Added\n\n- A thing (#1)\n\n**${heading}**\n\n- More\n`, ["Heading not allowed"]);
    }
    passes("## Changed\n\n### CI\n\n- Publishing now uses Node 24 (#1)\n");
    passes("## Changed\n\n- CI: builds now cache packages (#3)\n- Checks: a new command lists failing projections (#4)\n- Local: a new flag runs the kernel in-process (#5)\n- Gates: release gates now run in parallel (#6)\n");
});

test("product wording near the new notes still passes", () => {
    const bullets = [
        "Tokens are signed, verified against the issuer JWKS (#1)",
        "Toasts are stacked on top of each other (#2)",
        "Accounts that are not yet verified receive a reminder (#3)",
        "Not verified events are rejected (#4)",
        "Docs-only builds no longer publish packages (#5)",
        "Commands are validated on the server before they are handled (#6)",
        "Events are now verified against their schema before append (#7)",
        "Release notes are now checked on main as well (#8)",
        "Forms are submitted, validated in the browser and then saved (#9)",
        "Validated locally stored tokens before use (#10)",
        "Tested components now render in Storybook (#11)",
        "Retargets projections to the new event store (#12)",
        "Pull requests stacked on top of each other are now merged in order (#13)",
        "Draft releases are no longer published (#14)",
        "Draft: events can now be saved as drafts (#15)",
        "The CLI now warns when this branch is behind main (#16)",
        "Shows 3/3 steps completed in the wizard (#17)",
        "Builds tested against .NET 10 now run in CI (#18)",
        "Verified publishers are now shown with a badge (#19)",
        "Checked exceptions are now reported by the analyzer (#20)",
        "Commits checked against main are now labelled (#21)",
        "Supports `main` as the default branch (#22)",
        "Bare names select values, checked against what the event declares (#23)",
        "Merge conflicts are now reported before deploying (#24)",
        "Deployments that depend on another service now wait for it (#25)",
        "The kernel now verifies locally signed tokens (#26)",
        "Tier 1 storage now supports compaction (#27)",
        "Runs are now retried 3 of 5 times by default (#28)",
        "Reviewers can now be assigned from the dashboard (#29)",
        "Do not merge projections with conflicting keys anymore: they are now rejected (#30)",
    ];
    for (const bullet of bullets)
        assert.equal(run(`## Added\n\n- ${bullet}\n`).status, 0, bullet);
    passes("## Summary\n\nTokens are verified against the issuer and toasts are stacked on top of each other.\n\n## Added\n\n- A thing (#1)\n");
});

// The regression corpus: published Cratis release bodies (fixtures/release-notes/corpus.json), each with the rules the
// program reports for it as release-bound. A rule change that alters any of them shows up here, and the new rules'
// false positives were measured against it.
const CORPUS = JSON.parse(readFileSync(".github/scripts/tests/fixtures/release-notes/corpus.json", "utf8"));

function runAsync(body) {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [PROGRAM_FILE], {
            env: { PATH: process.env.PATH, PR_BODY: body, PR_LABELS: "[\"minor\"]", PR_AUTHOR: "someone", PR_BASE: "main",
                DEFAULT_BRANCH: "main", GITHUB_REPOSITORY: "Cratis/Example" },
        });
        let stdout = "";
        child.stdout.on("data", chunk => stdout += chunk);
        child.on("error", reject);
        child.on("close", status => resolve({ status, rules: [...new Set(stdout.split("\n")
            .filter(line => line.startsWith("::error "))
            .map(line => decodeURIComponent(/^::error title=([^:]*)::/.exec(line)[1]).replace(/^Release notes: /, "")))].sort() }));
    });
}

test("published release bodies keep exactly the rules recorded for them", { timeout: 240_000 }, async () => {
    assert(CORPUS.releases.length >= 500, "a corpus of several hundred real bodies");
    const results = new Array(CORPUS.releases.length);
    let next = 0;
    await Promise.all(Array.from({ length: Math.max(2, availableParallelism()) }, async () => {
        while (next < CORPUS.releases.length) {
            const index = next++;
            results[index] = await runAsync(CORPUS.releases[index].body);
        }
    }));
    const changed = CORPUS.releases.map((release, index) => ({ release, result: results[index] }))
        .filter(({ release, result }) => JSON.stringify(result.rules) !== JSON.stringify(release.rules) || (result.status === 0) !== (release.rules.length === 0))
        .map(({ release, result }) => `${release.repo} ${release.tag}: recorded ${JSON.stringify(release.rules)}, now ${JSON.stringify(result.rules)}`);
    assert.deepEqual(changed, []);
});

// A throwaway repository with a `main` and a pull request branch, `origin/main` standing in for the fetched base.
function repository() {
    const root = mkdtempSync(join(tmpdir(), "drift-"));
    const git = (...args) => spawnSync("git", args, { cwd: root, encoding: "utf8", env: { ...process.env,
        GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" } }).stdout.trim();
    const write = (file, text) => {
        mkdirSync(dirname(join(root, file)), { recursive: true });
        writeFileSync(join(root, file), text);
    };
    const commit = (message, files) => {
        for (const [file, text] of Object.entries(files))
            write(file, text);
        git("add", "-A");
        git("commit", "-q", "-m", message);
        return git("rev-parse", "HEAD");
    };
    git("init", "-q", "-b", "main");
    git("config", "commit.gpgsign", "false");
    commit("base", { "Source/Store.cs": "class Store {}\n", "Directory.Build.props": "<Version>1.2.3</Version>\n",
        ".github/workflows/publish.yml": "steps:\n  - run: dotnet nuget push\n" });
    git("checkout", "-q", "-b", "feature");
    return { root, git, commit, publish: () => git("update-ref", "refs/remotes/origin/main", "main") };
}

function drift(root, body, { action = "synchronize", before = "" } = {}) {
    const result = spawnSync(process.execPath, [DRIFT_FILE], { cwd: root, encoding: "utf8",
        env: { PATH: process.env.PATH, PR_BODY: body, BASE: "main", BEFORE: before, ACTION: action } });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.split("\n").filter(line => line.startsWith("::warning ")).map(line => decodeURIComponent(line.split("::")[2]));
}

test("the drift comparison warns about bullets, versions and registries the diff does not back, and never fails", () => {
    const repo = repository();
    repo.commit("feature", { "Source/Projections.cs": "class ProjectionReplayer {}\n", "Directory.Build.props": "<Version>1.3.0</Version>\n" });
    repo.publish();
    const warnings = drift(repo.root, [
        "## Added",
        "",
        "- Adds `ProjectionReplayer` (#1)",
        "- Adds `ObserverSnapshots` to `Source/Observers.cs` (#2)",
        "- Mentions `v2.0.0` and `12` only (#3)",
        "",
        "## Changed",
        "",
        "- Upgrades from 1.2.3 to 1.3.0 (#4)",
        "- Requires Chronicle 1.2.3 (#5)",
        "- Packages are now published to NuGet and PyPI (#6)",
        "",
    ].join("\n"));
    assert.equal(warnings.length, 3, warnings.join("\n"));
    assert.match(warnings[0], /The bullet `Adds 'ObserverSnapshots' to 'Source\/Observers\.cs' \(#2\)` names `ObserverSnapshots`, `Source\/Observers\.cs`, which this pull request's diff no longer touches; the change may already be on main or was dropped in conflict resolution/);
    assert.match(warnings[1], /names 1\.2\.3, which the diff only removes: it is the version this pull request replaces/);
    assert.match(warnings[2], /names PyPI, but no workflow in \.github\/workflows publishes there/);
    // A description that matches the diff, or a run that cannot compare, warns about nothing and still exits 0.
    assert.deepEqual(drift(repo.root, "## Added\n\n- Adds `ProjectionReplayer` (#1)\n- Version 1.3.0 is published to NuGet (#2)\n"), []);
    const elsewhere = mkdtempSync(join(tmpdir(), "drift-empty-"));
    const result = spawnSync(process.execPath, [DRIFT_FILE], { cwd: elsewhere, encoding: "utf8", env: { PATH: process.env.PATH, PR_BODY: "## Added\n\n- `X` (#1)\n", BASE: "main" } });
    assert.equal(result.status, 0);
    assert.match(result.stdout, /::notice title=Release notes drift not checked::/);
});

test("the drift comparison warns when a merge from the base branch changed what the pull request contains", () => {
    const repo = repository();
    const before = repo.commit("feature", { "Source/Store.cs": "class Store { int Count; }\n", "Source/Reader.cs": "class Reader {}\n" });
    repo.git("checkout", "-q", "main");
    // The same change lands on main through another pull request.
    repo.commit("other pull request", { "Source/Store.cs": "class Store { int Count; }\n" });
    repo.git("checkout", "-q", "feature");
    repo.git("merge", "-q", "--no-edit", "main");
    repo.publish();
    const body = "## Added\n\n- Adds `Reader` (#1)\n";
    const warnings = drift(repo.root, body, { before });
    assert.equal(warnings.length, 1, warnings.join("\n"));
    assert.match(warnings[0], /The merge from main changed what this pull request contains: it no longer changes `Source\/Store\.cs`\. Re-read the notes against `git diff origin\/main\.\.\.HEAD`\./);
    // Only a synchronize that brought in the base branch is compared.
    assert.deepEqual(drift(repo.root, body, { before, action: "edited" }), []);
    assert.deepEqual(drift(repo.root, body, { before: "" }), []);
});

test("the drift job only warns, reads the pull request head with history, and skips label changes and Dependabot", () => {
    const job = WORKFLOW.split("\n  drift:\n")[1];
    assert(job, "a drift job");
    for (const required of ["github.event.pull_request.user.login != 'dependabot[bot]'", "github.event.action != 'labeled'",
        "github.event.action != 'unlabeled'", "runs-on: ${{ inputs.runs-on }}", "ref: ${{ github.event.pull_request.head.sha }}",
        "fetch-depth: 0", "filter: blob:none", "continue-on-error: true", "BEFORE: ${{ github.event.before }}", "ACTION: ${{ github.event.action }}"])
        assert(job.includes(required), required);
    assert(/actions\/checkout@[0-9a-f]{40} # v7\.0\.1/.test(job), "checkout is pinned to a SHA");
    assert(PROGRAMS["release-notes-drift"].includes("process.exit(0);"));
    assert.equal(/process\.exit\([^0]/.test(PROGRAMS["release-notes-drift"]), false, "the drift program never exits non-zero");
});

test("the bootstrap installs the release-intent caller, which corrects Dependabot's labels before the gate", () => {
    const encoded = /BOOTSTRAPPED_FILES\["\.github\/workflows\/verify-semver-label\.yml"\]="([^"]+)"/.exec(BOOTSTRAP_SCRIPT);
    assert(encoded, "caller registered in bootstrap-common-workflows.sh");
    const caller = Buffer.from(encoded[1], "base64").toString("utf8");
    for (const required of [
        "name: Verify Semver Label",
        "types: [opened, reopened, synchronize, labeled, unlabeled]",
        "    branches:\n      - main\n",
        "permissions:\n  contents: read\n\njobs:",
        "  dependabot-labels:\n    if: github.event.pull_request.user.login == 'dependabot[bot]'\n    uses: Cratis/Workflows/.github/workflows/normalize-dependabot-labels.yml@main\n    permissions:\n      pull-requests: write\n",
        // Named release-intent, so the check reads `release-intent / verify`, not `verify / verify` like verify-no-work-records.
        "  release-intent:\n    needs: dependabot-labels\n    # Also after a failed or skipped correction: the gate then reports the labels as they are.\n    if: ${{ !cancelled() }}\n    uses: Cratis/Workflows/.github/workflows/verify-release-intent.yml@main\n    permissions:\n      contents: read\n      pull-requests: read\n",
    ])
        assert(caller.includes(required), required);
    assert.equal(/^\s*(?:run|steps):/m.test(caller), false, "the caller carries no logic");
    assert.equal(/^  verify:/m.test(caller), false, "no job named verify");
    const documented = caller.trimEnd().split("\n").map(line => `#   ${line}`.trimEnd()).join("\n");
    assert(BOOTSTRAP_SCRIPT.includes(documented), "the decoded comment matches the encoded caller");
    assert(readFileSync(".github/workflows/normalize-dependabot-labels.yml", "utf8").includes("workflow_call:"));
});

test("the bootstrap skips a file only for the repositories that keep their own copy of it", () => {
    assert(/^SKIP_FILE_REPOS\["\.github\/workflows\/verify-semver-label\.yml"\]="Arc\.Kotlin"$/m.test(BOOTSTRAP_SCRIPT));
    assert(/^#\s+Arc\.Kotlin - /m.test(BOOTSTRAP_SCRIPT), "the reason is documented");
    // The loop consults the skip list before it creates a blob for the file.
    assert(/for file_path in "\$\{!BOOTSTRAPPED_FILES\[@\]\}"; do\n    if skips_file "\$file_path" "\$repo"; then\n[^\n]*\n      continue\n    fi\n/.test(BOOTSTRAP_SCRIPT));
    const definitions = BOOTSTRAP_SCRIPT.split("declare -A SKIP_FILE_REPOS\n")[1].split("\n# ====")[0];
    const probe = `set -euo pipefail\ndeclare -A SKIP_FILE_REPOS\n${definitions}
for pair in ".github/workflows/verify-semver-label.yml Arc.Kotlin" ".github/workflows/verify-semver-label.yml Arc" ".github/workflows/verify-release-notes.yml Arc.Kotlin" ".github/workflows/verify-semver-label.yml Arc.Kotlin.Extra"; do
  set -- $pair
  if skips_file "$1" "$2"; then echo "skip $1 $2"; else echo "install $1 $2"; fi
done`;
    const result = spawnSync("bash", ["-c", probe], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.stdout.trim().split("\n"), [
        "skip .github/workflows/verify-semver-label.yml Arc.Kotlin",
        "install .github/workflows/verify-semver-label.yml Arc",
        "install .github/workflows/verify-release-notes.yml Arc.Kotlin",
        "install .github/workflows/verify-semver-label.yml Arc.Kotlin.Extra",
    ]);
});
