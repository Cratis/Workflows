// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { WORKFLOW, passes, fails, GOOD } from "./given/release-notes-program.mjs";

const POLICY_WORKFLOW = readFileSync(".github/workflows/verify-pull-request-policy.yml", "utf8");

test("both workflows expose the same agent-trace inputs and pass them through the environment", () => {
    for (const [name, variable] of [["agent-authors", "AGENT_AUTHORS"], ["require-agent-trace", "REQUIRE_AGENT_TRACE"]]) {
        const marker = `      ${name}:\n`;
        const input = WORKFLOW.split(marker)[1].split(/\n(?= {0,6}\S)/)[0].trim();
        const policyInput = POLICY_WORKFLOW.split(marker)[1].split(/\n(?= {0,6}\S)/)[0].trim();
        assert.equal(policyInput, input, `${name} has the same description, type and defaults in both workflows`);
        for (const source of [WORKFLOW, POLICY_WORKFLOW])
            assert(source.includes(`${variable}: \${{ inputs.${name} }}`), `the workflow passes through ${name}`);
    }
});

test("a trailing agent trace is accepted with only notes or with a summary and optional fields", () => {
    for (const body of [GOOD, "## Added\n\n- A new thing (#12)\n"]) {
        for (const marker of ["<!-- agent-trace:v1 session=abc -->",
            "<!-- agent-trace:v1 session=abc orchestrator=lead work=task issue=Cratis/AI#858 model=example-v1 -->",
            "<!-- agent-trace:v1 session=refs-123 work=fix-mail-guard -->"]) {
            assert.deepEqual(passes(`${body}\n${marker}\n \t\n`).warned, []);
        }
    }
    const marker = "<!-- agent-trace:v1 session=abc -->";
    const agent = { author: "explicit-agent", agentAuthors: "explicit-agent", requireAgentTrace: true };
    for (const example of [`\`\`\`html\n${marker}\n\`\`\``, `~~~html\n${marker}\n~~~`,
        `\`\`\`html\r\n${marker}\r\n\`\`\``, `\`${marker}\``, `\`\`${marker}\`\``,
        "```html\n<!-- agent-trace:v2 session=(#12) -->\n```", "`<!-- agent-trace:v1 session=abc`"]) {
        const body = `${GOOD}\n${example}\n`;
        assert.deepEqual(passes(body).warned, []);
        fails(body, ["Missing agent trace"], agent);
        assert.deepEqual(passes(`${body}\n${marker}`, agent).warned, []);
    }
});

test("invalid agent traces explain malformed fields, count, position and forbidden references or keywords", () => {
    for (const [marker, message, additionalRules = []] of [
        ["<!-- agent-trace:v2 session=abc -->", /agent-trace:v1/],
        ["<!-- agent-trace:v1 session=abc extra=value -->", /Unknown agent-trace key: extra/],
        ["<!-- agent-trace:v1 session=abc session=def -->", /Duplicate agent-trace key: session/],
        ["<!-- agent-trace:v1 model=example -->", /requires a session key/],
        ["<!-- agent-trace:v1 -->", /key=value/],
        ["<!-- agent-trace:v1 session= -->", /non-empty value/],
        ["<!-- agent-trace:v1 session=abc stray -->", /key=value/],
        ["<!-- agent-trace:v1 session=two words -->", /key=value/],
        ["<!-- agent-trace:v1 session=abc issue=858 -->", /Owner\/Repo#n/],
        ["<!-- agent-trace:v1 session=abc issue=Cratis/AI#x -->", /Owner\/Repo#n/],
        ["<!-- agent-trace:v1 session=abc", /Close the agent-trace marker/],
        ["<!-- agent-trace:v1 session=abc-->suffix -->", /key=value/, ["Issue reference position"]],
    ]) {
        const result = fails(`${GOOD}\n${marker}`, ["Malformed agent trace", ...additionalRules]);
        assert.match(result.errors[0].message, message);
        assert.match(result.errors[0].message, /Line 11 /);
    }
    const marker = "<!-- agent-trace:v1 session=abc -->";
    fails(`${GOOD}\n${marker}\n${marker}`, ["Multiple agent traces"]);
    fails(`${GOOD}\n${marker}\n<!-- agent-trace:v1 session=unclosed`, ["Multiple agent traces"]);
    fails(`${marker}\n${GOOD}`, ["Agent trace not trailing"]);
    fails(`${GOOD}\n${marker}\n<!-- another comment -->`, ["Agent trace not trailing"]);
    fails(`${GOOD}\n<!-- agent-trace:v1 session=abc work=(#12) -->`, ["Agent trace issue reference"]);
    fails(`${GOOD}\n<!-- agent-trace:v1 session=abc(#12) -->`, ["Agent trace issue reference"]);
    for (const keyword of ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved", "ref", "refs", "references"]) {
        fails(`${GOOD}\n<!-- agent-trace:v1 session=abc work=${keyword} -->`, ["Agent trace keyword"]);
    }
    fails(`${GOOD}\n<!-- agent-trace:v1 session=abc work=Fixes:#12 -->`, ["Agent trace keyword"]);
    fails(`${GOOD}\n<!-- (#5) -->\n<!-- agent-trace:v2 session=abc -->`, ["Issue reference position", "Malformed agent trace"]);
    for (const labels of [["minor"], ["no-release"], []]) {
        fails(`${GOOD}\n<!-- agent-trace:v2 session=abc -->`, ["Malformed agent trace"], { labels });
    }
});

test("removing an agent trace preserves ordinary release-note diagnostic line numbers", () => {
    const result = fails("## Added\r\n\r\n- A thing (#1) mid-bullet\r\n\r\n<!-- agent-trace:v1 session=abc -->\r\n", ["Issue reference position"]);
    assert.match(result.errors[0].message, /Line 3 /);
    const afterExample = fails("## Added\r\n\r\n- A thing (#1)\r\n\r\n```html\r\n<!-- agent-trace:v1 session=example -->\r\n```\r\n\r\n<!-- agent-trace:v2 session=abc -->", ["Malformed agent trace"]);
    assert.match(afterExample.errors[0].message, /Line 9 /);
});

test("only explicitly listed agent authors warn without a marker; enforcement is opt-in", () => {
    assert.deepEqual(passes(GOOD, { agentAuthors: "explicit-agent", requireAgentTrace: true }).warned, []);
    assert.deepEqual(passes(GOOD, { author: "agent[bot]" }).warned, []);
    for (const agentAuthors of ["other, explicit-agent", "other\nEXPLICIT-AGENT\r\n"]) {
        const result = passes(GOOD, { author: "explicit-agent", agentAuthors });
        assert.deepEqual(result.warned, ["Missing agent trace"]);
        assert.match(result.warnings[0].message, /last non-blank content/);
    }
    for (const labels of [["minor"], ["no-release"], []]) {
        fails(GOOD, ["Missing agent trace"], { author: "explicit-agent", agentAuthors: "explicit-agent", requireAgentTrace: true, labels });
    }
    assert.deepEqual(passes(`${GOOD}\n<!-- agent-trace:v1 session=abc -->`, {
        author: "explicit-agent", agentAuthors: "explicit-agent", requireAgentTrace: true,
    }).warned, []);
    for (const labels of [["minor"], ["no-release"], []]) {
        for (const [body, rule] of [[`${GOOD}\n<!-- agent-trace:v2 session=abc -->`, "Malformed agent trace"],
            [`<!-- agent-trace:v1 session=abc -->\n${GOOD}`, "Agent trace not trailing"]]) {
            fails(body, [rule, "Missing agent trace"], { author: "explicit-agent", agentAuthors: "explicit-agent", requireAgentTrace: true, labels });
        }
    }
    fails("## Added\n\n- A thing (#1) mid-bullet\n", ["Issue reference position", "Missing agent trace"], {
        author: "explicit-agent", agentAuthors: "explicit-agent", requireAgentTrace: true,
    });
});
