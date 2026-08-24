// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
    existsSync,
    mkdirSync,
    mkdtempSync,
    readFileSync,
    readdirSync,
    rmSync,
    writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
    applySubscriptionUpdate,
    compareSemVer,
    planSubscriptionUpdate,
} from "./update-ai-profile-subscription.mjs";

function writeJson(path, value) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
}

function readJson(path) {
    return JSON.parse(readFileSync(path, "utf8"));
}

function sha256(content) {
    return createHash("sha256").update(content).digest("hex");
}

function withFixture(callback) {
    const root = mkdtempSync(join(tmpdir(), "cratis-ai-subscription-"));
    try {
        return callback(root);
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
}

function releaseManifest(overrides = {}) {
    return {
        schemaVersion: "1.0.0",
        state: "APPROVED_PROFILE_RELEASE",
        profileId: "public-fundamentals",
        packageName: "@cratis/ai-fundamentals",
        audience: "public",
        version: "1.1.0",
        publicationEligible: true,
        ...overrides,
    };
}

function subscription(overrides = {}) {
    return {
        schemaVersion: "1.0.0",
        channel: "public",
        version: "1.0.0",
        profiles: ["public-fundamentals"],
        harnesses: ["agent-plugin", "pi"],
        updatePolicy: "reviewed-pull-request",
        projectContext: ".cratis/PROJECT.md",
        ...overrides,
    };
}

function createRepository(root, options = {}) {
    const repository = join(root, options.name ?? "repository");
    writeJson(join(repository, ".cratis/ai.json"), options.subscription ?? subscription());
    mkdirSync(join(repository, ".cratis"), { recursive: true });
    writeFileSync(join(repository, ".cratis/PROJECT.md"), "# Private context\n");
    writeFileSync(join(repository, "AGENTS.md"), "# Project bootstrap\n");
    mkdirSync(join(repository, ".agents/skills/local-only"), { recursive: true });
    writeFileSync(
        join(repository, ".agents/skills/local-only/SKILL.md"),
        "---\nname: local-only\ndescription: Local only.\n---\n",
    );
    if ((options.subscription ?? subscription()).harnesses.includes("pi"))
        writeJson(
            join(repository, ".pi/settings.json"),
            options.piSettings ?? {
                packages: ["npm:@cratis/ai-fundamentals@1.0.0"],
                enableSkillCommands: true,
            },
        );
    return repository;
}

function protectedHashes(repository) {
    const paths = [
        "AGENTS.md",
        ".cratis/PROJECT.md",
        ".agents/skills/local-only/SKILL.md",
    ];
    return Object.fromEntries(
        paths.map((path) => [path, sha256(readFileSync(join(repository, path)))]),
    );
}

function manifestPath(root, manifest = releaseManifest()) {
    const path = join(root, "release-manifest.json");
    writeJson(path, manifest);
    return path;
}

test("SemVer comparison supports releases and prereleases", () => {
    assert.equal(compareSemVer("1.0.0", "1.0.0"), 0);
    assert.equal(compareSemVer("1.0.1", "1.0.0"), 1);
    assert.equal(compareSemVer("1.0.0-preview.2", "1.0.0-preview.1"), 1);
    assert.equal(compareSemVer("1.0.0", "1.0.0-preview.9"), 1);
    assert.equal(compareSemVer("1.0.0-preview.1", "1.0.0"), -1);
    assert.throws(() => compareSemVer("latest", "1.0.0"), /exact SemVer/);
});

test("dry-run plans only subscription and exact Pi package changes", () => {
    withFixture((root) => {
        const repository = createRepository(root);
        const beforeSubscription = readFileSync(join(repository, ".cratis/ai.json"));
        const beforePi = readFileSync(join(repository, ".pi/settings.json"));
        const plan = planSubscriptionUpdate({
            repositoryRoot: repository,
            releaseManifestPath: manifestPath(root),
        });
        assert.equal(plan.state, "UPDATE_READY");
        assert.equal(plan.fromVersion, "1.0.0");
        assert.equal(plan.toVersion, "1.1.0");
        assert.deepEqual(
            plan.changes.map((entry) => entry.path),
            [".cratis/ai.json", ".pi/settings.json"],
        );
        assert(readFileSync(join(repository, ".cratis/ai.json")).equals(beforeSubscription));
        assert(readFileSync(join(repository, ".pi/settings.json")).equals(beforePi));
    });
});

test("apply updates exact pins and preserves project-owned context", () => {
    withFixture((root) => {
        const repository = createRepository(root);
        const beforeProtected = protectedHashes(repository);
        const applied = applySubscriptionUpdate(
            planSubscriptionUpdate({
                repositoryRoot: repository,
                releaseManifestPath: manifestPath(root),
            }),
        );
        assert.equal(applied.state, "UPDATE_APPLIED");
        assert.equal(readJson(join(repository, ".cratis/ai.json")).version, "1.1.0");
        assert.deepEqual(readJson(join(repository, ".pi/settings.json")).packages, [
            "npm:@cratis/ai-fundamentals@1.1.0",
        ]);
        assert.deepEqual(protectedHashes(repository), beforeProtected);
    });
});

test("apply rolls back earlier writes after a later write failure", () => {
    withFixture((root) => {
        const repository = createRepository(root);
        const subscriptionPath = join(repository, ".cratis/ai.json");
        const beforeSubscription = readFileSync(subscriptionPath);
        writeFileSync(
            join(repository, ".pi/settings.json.cratis-ai-update.tmp"),
            "block atomic write",
        );
        assert.throws(
            () =>
                applySubscriptionUpdate(
                    planSubscriptionUpdate({
                        repositoryRoot: repository,
                        releaseManifestPath: manifestPath(root),
                    }),
                ),
            /rolled back after a write failure/,
        );
        assert(readFileSync(subscriptionPath).equals(beforeSubscription));
    });
});

test("object-shaped Pi package filters survive an engineering update", () => {
    withFixture((root) => {
        const engineeringSubscription = subscription({
            channel: "cratis-engineering",
            profiles: ["engineering-chronicle"],
        });
        const repository = createRepository(root, {
            subscription: engineeringSubscription,
            piSettings: {
                packages: [
                    {
                        source: "npm:@cratis/ai-engineering-chronicle@1.0.0",
                        skills: ["skills/cratis-chronicle-projection"],
                        extensions: [],
                    },
                ],
                enableSkillCommands: true,
            },
        });
        applySubscriptionUpdate(
            planSubscriptionUpdate({
                repositoryRoot: repository,
                releaseManifestPath: manifestPath(
                    root,
                    releaseManifest({
                        profileId: "engineering-chronicle",
                        packageName: "@cratis/ai-engineering-chronicle",
                        audience: "cratis-engineering",
                    }),
                ),
            }),
        );
        const packageEntry = readJson(join(repository, ".pi/settings.json")).packages[0];
        assert.equal(
            packageEntry.source,
            "npm:@cratis/ai-engineering-chronicle@1.1.0",
        );
        assert.deepEqual(packageEntry.skills, [
            "skills/cratis-chronicle-projection",
        ]);
        assert.deepEqual(packageEntry.extensions, []);
    });
});

test("non-Pi repositories update without creating Pi settings", () => {
    withFixture((root) => {
        const repository = createRepository(root, {
            subscription: subscription({ harnesses: ["agent-plugin"] }),
        });
        applySubscriptionUpdate(
            planSubscriptionUpdate({
                repositoryRoot: repository,
                releaseManifestPath: manifestPath(root),
            }),
        );
        assert.equal(existsSync(join(repository, ".pi/settings.json")), false);
    });
});

test("rollback requires an explicitly lower exact release", () => {
    withFixture((root) => {
        const repository = createRepository(root, {
            subscription: subscription({ version: "1.1.0" }),
            piSettings: {
                packages: ["npm:@cratis/ai-fundamentals@1.1.0"],
            },
        });
        const release = manifestPath(
            root,
            releaseManifest({ version: "1.0.0" }),
        );
        assert.throws(
            () =>
                planSubscriptionUpdate({
                    repositoryRoot: repository,
                    releaseManifestPath: release,
                }),
            /higher/,
        );
        const applied = applySubscriptionUpdate(
            planSubscriptionUpdate({
                repositoryRoot: repository,
                releaseManifestPath: release,
                rollback: true,
            }),
        );
        assert.equal(applied.state, "ROLLBACK_APPLIED");
        assert.equal(readJson(join(repository, ".cratis/ai.json")).version, "1.0.0");
    });
});

test("application framework client and documentation repositories plan independently", () => {
    withFixture((root) => {
        const release = manifestPath(root);
        for (const kind of ["application", "framework", "client", "documentation"]) {
            const repository = createRepository(root, { name: kind });
            const plan = planSubscriptionUpdate({
                repositoryRoot: repository,
                releaseManifestPath: release,
            });
            assert.equal(plan.state, "UPDATE_READY", kind);
            assert.equal(plan.changes.length, 2, kind);
        }
    });
});

test("mismatched unsafe and incomplete updates fail closed", async (context) => {
    const cases = [
        [
            "audience mismatch",
            { subscription: subscription({ channel: "cratis-engineering" }) },
            releaseManifest(),
            /does not match/,
        ],
        [
            "profile mismatch",
            { subscription: subscription({ profiles: ["public-arc"] }) },
            releaseManifest(),
            /does not match/,
        ],
        [
            "missing Pi settings",
            { subscription: subscription() },
            releaseManifest(),
            /Pi is selected/,
            true,
        ],
        [
            "wrong Pi package",
            {
                subscription: subscription(),
                piSettings: { packages: ["npm:@cratis/ai-fundamentals@0.9.0"] },
            },
            releaseManifest(),
            /exactly one current profile package/,
        ],
        [
            "unapproved release",
            { subscription: subscription() },
            releaseManifest({ publicationEligible: false }),
            /not publication-approved/,
        ],
    ];
    for (const [name, options, manifest, expected, removePi] of cases)
        await context.test(name, () => {
            withFixture((root) => {
                const repository = createRepository(root, options);
                if (removePi) rmSync(join(repository, ".pi"), { recursive: true });
                assert.throws(
                    () =>
                        planSubscriptionUpdate({
                            repositoryRoot: repository,
                            releaseManifestPath: manifestPath(root, manifest),
                        }),
                    expected,
                );
            });
        });
});

test("APM-managed Cratis dependencies block partial updates", () => {
    withFixture((root) => {
        const repository = createRepository(root);
        writeFileSync(
            join(repository, "apm.yml"),
            "dependencies:\n  apm:\n    - Cratis/AI.Distribution#v1.0.0\n",
        );
        assert.throws(
            () =>
                planSubscriptionUpdate({
                    repositoryRoot: repository,
                    releaseManifestPath: manifestPath(root),
                }),
            /APM-managed Cratis dependencies require lock refresh/,
        );
    });
});

test("CLI is dry-run by default and emits an auditable receipt", () => {
    withFixture((root) => {
        const repository = createRepository(root);
        const release = manifestPath(root);
        const output = execFileSync(
            process.execPath,
            [
                ".github/scripts/update-ai-profile-subscription.mjs",
                "--repository",
                repository,
                "--release-manifest",
                release,
            ],
            {
                cwd: resolve(dirname(fileURLToPath(import.meta.url)), "../.."),
                encoding: "utf8",
            },
        );
        const receipt = JSON.parse(output);
        assert.equal(receipt.state, "UPDATE_READY");
        assert.equal(Object.hasOwn(receipt, "writes"), false);
        assert.equal(Object.hasOwn(receipt, "repositoryRoot"), false);
        assert.equal(readJson(join(repository, ".cratis/ai.json")).version, "1.0.0");
        assert.deepEqual(
            readdirSync(join(repository, ".cratis")).sort(),
            ["PROJECT.md", "ai.json"],
        );
    });
});
