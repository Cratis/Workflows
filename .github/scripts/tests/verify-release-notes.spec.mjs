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
const PROGRAM_WORKFLOW = readFileSync(".github/workflows/verify-release-notes-program.yml", "utf8");
const SPEC_SOURCE = readFileSync(".github/scripts/tests/verify-release-notes.spec.mjs", "utf8");
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
    fails("## Added\n\n```\ncode only\n```\n", ["No release notes", "Section without bullets"], { labels: ["patch"] });
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
    fails("## Fixed\n\n- Thing (#3) (part of #4) and more\n", ["Issue reference position"]);
    fails("## Fixed\n\n- Thing (#3) **and** more\n", ["Issue reference position"]);
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
