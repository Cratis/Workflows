// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

function yamlFiles(directory) {
    return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) return yamlFiles(path);
        return /\.ya?ml$/.test(entry.name) ? [path] : [];
    }).sort();
}

function isInternalReference(reference) {
    // Organization-owned references, local actions and container images are outside this pin contract.
    return /^cratis\//i.test(reference) || reference.startsWith("./") || reference.startsWith("docker://");
}

function checkUses(source, name) {
    let references = 0;
    let thirdParty = 0;
    for (const [index, line] of source.split("\n").entries()) {
        if (line.trimStart().startsWith("#") || !/\buses["']?\s*:/.test(line)) continue;
        const location = `${name}:${index + 1}`;
        // Deliberately accept only the existing block-form, plain-scalar uses contract.
        // Quoted keys, flow mappings and multiline/expression values must fail, not evade the scan.
        const match = line.match(/^\s*(?:-\s+)?uses:\s+([^\s"'#{}[\],]+)(?:[ \t]+(#.*?))?[ \t]*$/);
        assert(match, `${location}: unrecognized uses form`);
        const [, reference, comment] = match;
        references++;
        if (isInternalReference(reference)) continue;
        assert.match(reference, /^[^/@\s]+\/[^@\s]+@[a-fA-F0-9]{40}$/, `${location}: expected a full commit SHA`);
        assert.match(comment ?? "", /^# v\d+\.\d+\.\d+$/, `${location}: expected a plain exact-version comment`);
        thirdParty++;
    }
    return { references, thirdParty };
}

function bootstrapPayloads(source) {
    const assignments = [...source.matchAll(/^BOOTSTRAPPED_FILES\["([^"]+)"\]="([A-Za-z0-9+/=]+)"$/gm)];
    assert(assignments.length > 0, "Discover bootstrap payloads");
    assert.equal(assignments.length, [...source.matchAll(/^BOOTSTRAPPED_FILES\[/gm)].length,
        "Every bootstrap assignment must be recognized");
    const variants = [...source.matchAll(/^POLICY_REUSABLE_WRAPPER="([A-Za-z0-9+/=]+)"$/gm)];
    assert.equal(variants.length, 1, "Discover the reusable policy variant");
    return [...assignments.map(([, path, encoded]) => ({ path, encoded })),
        { path: "POLICY_REUSABLE_WRAPPER", encoded: variants[0][1] }].map(({ path, encoded }) => {
        const bytes = Buffer.from(encoded, "base64");
        assert.equal(bytes.toString("base64"), encoded, `${path}: canonical base64`);
        return { path, source: bytes.toString("utf8") };
    });
}

const pinned = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1";

test("pin checker accepts full commits and exact version comments, including sub-actions", () => {
    assert.deepEqual(checkUses(`- uses: ${pinned}`, "fixture"), { references: 1, thirdParty: 1 });
    assert.deepEqual(checkUses("uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0", "fixture"),
        { references: 1, thirdParty: 1 });
});

for (const reference of [
    "actions/checkout@v7 # v7.0.1",
    "actions/checkout@main # v7.0.1",
    "actions/checkout@3d3c42e # v7.0.1",
    pinned.split(" #")[0],
    pinned.replace("# v7.0.1", "# v7"),
    `${pinned} extra metadata`,
]) {
    test(`pin checker rejects ${reference}`, () => {
        assert.throws(() => checkUses(`uses: ${reference}`, "fixture"), /expected a (full commit SHA|plain exact-version comment)/);
    });
}

for (const line of [
    'uses: "actions/checkout@v7"',
    '"uses": actions/checkout@v7',
    "- { uses: actions/checkout@v7 }",
    "uses: >\n  actions/checkout@v7",
    "uses: ${{ inputs.action }}",
    "uses:",
]) {
    test(`pin checker fails closed on ${JSON.stringify(line)}`, () => {
        assert.throws(() => checkUses(line, "fixture"), /unrecognized uses form|expected a full commit SHA/);
    });
}

test("Cratis, local and docker references are not third-party action pins", () => {
    const source = [
        "uses: Cratis/Workflows/.github/workflows/workflow-lint.yml@main",
        "uses: cratis/release-action@main",
        "uses: CRATIS/Workflows/.github/actions/dotnet-cache@main",
        "uses: ./.github/actions/dotnet-cache",
        "uses: docker://alpine:3.22",
        "# uses: actions/checkout@main",
    ].join("\n");
    assert.deepEqual(checkUses(source, "fixture"), { references: 5, thirdParty: 0 });
    assert.throws(() => checkUses("uses: CratisOther/action@main", "fixture"), /expected a full commit SHA/);
});

test("executable workflows and composites retain immutable third-party action pins", t => {
    for (const directory of [".github/workflows", ".github/actions"]) {
        const files = yamlFiles(directory);
        assert(files.length > 0, `${directory}: discover YAML files`);
        const counts = files.map(path => checkUses(readFileSync(path, "utf8"), path));
        const references = counts.reduce((sum, count) => sum + count.references, 0);
        const thirdParty = counts.reduce((sum, count) => sum + count.thirdParty, 0);
        assert(thirdParty > 0, `${directory}: discover third-party references`);
        t.diagnostic(`Checked ${files.length} files in ${directory}: ${references} uses, ${thirdParty} third-party pins`);
    }
});

test("bootstrap definitions and the reusable policy variant retain pin coverage without installing Dependabot in any repository", t => {
    assert(!existsSync(".github/dependabot.yml"), "Workflows must not configure Dependabot version updates");
    const script = readFileSync(".github/scripts/bootstrap-common-workflows.sh", "utf8");
    const payloads = bootstrapPayloads(script);
    const legacyPaths = [...script.matchAll(/^LEGACY_WRAPPER_HASHES\["([^"]+)"\]=/gm)].map(([, path]) => path);
    assert(legacyPaths.length > 0, "Discover retired wrappers");
    const active = payloads.filter(({ path }) => path !== "POLICY_REUSABLE_WRAPPER" && !legacyPaths.includes(path));
    assert(active.length > 0, "Discover active bootstrap paths");
    assert(active.every(({ path }) => !/dependabot/i.test(path)), "Bootstrap must not install Dependabot in any repository");
    const counts = payloads.map(({ path, source }) => checkUses(source, path));
    const references = counts.reduce((sum, count) => sum + count.references, 0);
    assert(references > 0, "Discover decoded action references");
    t.diagnostic(`Checked ${payloads.length} decoded definitions (${active.length} active paths): ${references} uses`);
});

test("decoded bootstrap content cannot hide an unpinned third-party action", () => {
    const encoded = Buffer.from("uses: actions/checkout@main\n").toString("base64");
    const variant = Buffer.from(`uses: ${pinned}\n`).toString("base64");
    const payloads = bootstrapPayloads(`BOOTSTRAPPED_FILES["fixture.yml"]="${encoded}"\nPOLICY_REUSABLE_WRAPPER="${variant}"`);
    assert.throws(() => checkUses(payloads[0].source, payloads[0].path), /expected a full commit SHA/);
    assert.equal(checkUses(payloads[1].source, payloads[1].path).thirdParty, 1);
    assert.throws(() => bootstrapPayloads(""), /Discover bootstrap payloads/);
});
