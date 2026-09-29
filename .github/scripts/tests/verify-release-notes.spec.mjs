// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Offline regressions for the exact inline program in verify-release-notes.yml, run as the runner runs it:
// a separate node process fed only through the environment. Never contacts GitHub.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const WORKFLOW = readFileSync(".github/workflows/verify-release-notes.yml", "utf8");
const RELEASES = JSON.parse(readFileSync(".github/scripts/tests/fixtures/release-notes/releases.json", "utf8"));
const VERBATIM = "This pull request description is published verbatim as the release notes; editing the description re-runs this check.";

const RUN_BLOCK = WORKFLOW.split("        run: |\n")[1];
const PROGRAM = RUN_BLOCK.split("node - <<'JS'\n")[1].split("\n          JS\n")[0]
    .split("\n").map(line => line.replace(/^ {10}/, "")).join("\n");
const directory = mkdtempSync(join(tmpdir(), "verify-release-notes-"));
const PROGRAM_FILE = join(directory, "program.cjs");
writeFileSync(PROGRAM_FILE, PROGRAM);

function run(body, { labels = ["minor"], author = "someone", base = "main", defaultBranch = "main" } = {}) {
    const summaryFile = join(directory, `summary-${Math.random().toString(16).slice(2)}.md`);
    writeFileSync(summaryFile, "");
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
        },
    });
    const lines = result.stdout.split("\n");
    const decode = value => value.replace(/%0A/g, "\n").replace(/%0D/g, "\r").replace(/%3A/g, ":").replace(/%2C/g, ",").replace(/%25/g, "%");
    const errors = lines.filter(line => line.startsWith("::error ")).map(line => {
        const [, title, message] = /^::error title=([^:]*)::(.*)$/.exec(line);
        return { rule: decode(title).replace(/^Release notes: /, ""), message: decode(message) };
    });
    const notices = lines.filter(line => line.startsWith("::notice ")).map(decode);
    return { status: result.status, stderr: result.stderr, errors, rules: errors.map(error => error.rule), notices,
        summary: readFileSync(summaryFile, "utf8") };
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

test("the program reaches the runner only through the environment", () => {
    const run = WORKFLOW.split("        run: |\n")[1];
    assert.equal(run.includes("${{"), false, "no expression may be interpolated into the program");
    for (const variable of ["PR_BODY: ${{ github.event.pull_request.body }}",
        "PR_LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}",
        "PR_AUTHOR: ${{ github.event.pull_request.user.login }}",
        "PR_BASE: ${{ github.event.pull_request.base.ref }}",
        "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
        "workflow_call: {}",
        "contents: read"])
        assert(WORKFLOW.includes(variable), variable);
});

test("a description in the allowed shape passes and writes a summary", () => {
    const result = passes(GOOD);
    assert.match(result.summary, /follows the release-note contract/);
});

test("allowed issue references pass: (#n), (part of #n), see #n and Cratis/Repo#n", () => {
    passes("## Changed\n\n- Delivered (#1)\n- Partial (part of #2)\n- Related, see #3\n- Elsewhere Cratis/Chronicle#4\n");
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

test("sections appear once, in order, only when non-empty", () => {
    fails("## Fixed\n\n- A fix\n\n## Added\n\n- A thing\n", ["Section order"]);
    fails("## Added\n\n- A thing\n\n## Added\n\n- Another\n", ["Section order"]);
    fails("## Added\n\n- A thing\n\n## Removed\n\n<!-- nothing -->\n", ["Empty section"]);
    fails("## Added\n\n## Changed\n\n- A change\n", ["Empty section"]);
    passes("## Added\n\n- A thing\n\n## Fixed\n\n```csharp\nvar example = 1;\n```\n");
});

test("the preamble is at most one lead paragraph without bullets or sub-headings", () => {
    passes("One lead paragraph that\nwraps over two lines.\n\n## Added\n\n- A thing\n");
    fails("First paragraph.\n\nSecond paragraph.\n\n## Added\n\n- A thing\n", ["More than one lead paragraph"]);
    fails("- A loose bullet\n- Another\n\n## Added\n\n- A thing\n", ["Bullets outside a section"]);
    fails("### Highlights\n\n## Added\n\n- A thing\n", ["Sub-heading outside a section"]);
});

test("review, verification and provenance notes fail", () => {
    for (const line of ["Review: two reviewers approved", "Reviewed: yes", "Reviewed by Opus", "- Reviewed by the maintainers",
        "**Verification:** all green", "- Tested: locally", "Testing: specs pass", "__Validation__: done", "Validation: rules apply",
        "Test plan: run the suite", "- Test plan: run the suite", "Verified: locally", "Tests: 400 passed", "- Tests: 400 passed",
        "- Testing: 12 specs passed", "- Verification: CI green", "Reviewed with a same-provider review", "cross-provider review pending",
        "Opus-only review", "Anthropic-only review", "GPT-only review", "The review workflow passed", "the review workflow returned findings",
        "The review workflow ran twice", "The review workflow found nothing",
        "\u{1F916} Generated with [Claude Code](https://claude.com/claude-code)", "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Written by Claude", "Drafted by an AI", "CI green, all tests passed.", "All tests pass.", "CI is green", "ci green",
        "- Done; all the tests passed (#1)"]) {
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
    for (const line of ["**Test plan**", "**Verification**", "**Notes for reviewers**", "__Testing__", "**Test plan:**", "**Notes**:", "**Why**"]) {
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
        assert.match(result.errors[0].message, /Release-action closes `\(#\d+\)` wherever it appears; put `\(#\d+\)` at the end of the bullet that delivers the issue, or write `\(part of #\d+\)` or `see #\d+`/);
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
    fails("## Added\n\n```\ncode only\n```\n", ["No release notes"], { labels: ["patch"] });
});

test("Windows line endings are read like Unix ones", () => {
    passes(GOOD.replace(/\n/g, "\r\n"));
    fails("## Fixed\r\n\r\n- A fix\r\n\r\nRefs #9\r\n", ["Closing or linking keyword"]);
});

test("only a release-bound pull request is checked; every other is skipped with a notice", () => {
    const bad = "## Overview\n\nRefs #1\n";
    for (const [options, reason] of [
        [{ labels: [] }, /carries none of major, minor or patch/],
        [{ labels: ["bug", "documentation"] }, /carries none of major, minor or patch/],
        [{ labels: ["no-release"] }, /labelled no-release/],
        [{ labels: ["major", "minor"] }, /carries major, minor; release intent must be exactly one/],
        [{ author: "dependabot[bot]" }, /Dependabot/],
        [{ base: "feature/x", defaultBranch: "main" }, /targets feature\/x, not the default branch main/],
        [{ base: "" }, /not triggered by a pull request/],
    ]) {
        const result = run(bad, options);
        assert.equal(result.status, 0, JSON.stringify(options));
        assert.equal(result.errors.length, 0);
        assert.match(result.notices.join("\n"), reason);
        assert.match(result.notices.join("\n"), /the check re-runs when a release label is added/);
        assert.match(result.summary, /Not checked/);
    }
    for (const label of ["major", "minor", "patch"])
        fails(bad, ["Heading not allowed", "Closing or linking keyword", "No release notes"], { labels: [label] });
    fails(bad, ["Heading not allowed", "Closing or linking keyword", "No release notes"], { labels: ["bug", "minor"], base: "develop", defaultBranch: "develop" });
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
