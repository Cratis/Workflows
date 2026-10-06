// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { applySubscriptionUpdate, planSubscriptionUpdate } from "./update-ai-profile-subscription.mjs";
import test from "node:test";

const workflow = readFileSync(
    ".github/workflows/update-ai-profile-subscription.yml",
    "utf8",
);

test("subscriber workflow is explicit immutable and repository scoped", () => {
    for (const required of [
        "workflow_dispatch:",
        "repository_dispatch:",
        "types: [cratis-ai-profile-release]",
        "environment: ai-subscriber-updates",
        "release_manifest_sha256",
        "raw\\.githubusercontent\\.com/Cratis/AI\\.Distribution/[0-9a-f]{40}",
        "confirm_repository to exactly match target_repository",
        "repository: Cratis/Workflows",
        "ref: refs/heads/main",
        "repositories: ${{ steps.inputs.outputs.repository }}",
        "permission-contents: ${{ steps.inputs.outputs.apply == 'true' && 'write' || 'read' }}",
        "permission-pull-requests: ${{ steps.inputs.outputs.apply == 'true' && 'write' || 'read' }}",
        "sha256sum --check --strict",
        "update-ai-profile-subscription.mjs",
        "--release-manifest",
        "--rollback",
        "--apply",
        ".cratis/ai.json|.pi/settings.json",
        "does not auto-merge or change project context",
        "subscription-update-receipt.json",
        "retention-days: 90",
    ])
        assert(workflow.includes(required), required);
});

test("workflow stages a controller-created .gitignore in a subscriber without one", () => {
    const root = mkdtempSync(join(tmpdir(), "subscription-workflow-"));
    try {
        const repository = join(root, "subscriber");
        mkdirSync(join(repository, ".cratis"), { recursive: true });
        mkdirSync(join(repository, ".pi"));
        writeFileSync(join(repository, ".cratis/ai.json"), JSON.stringify({
            schemaVersion: "1.0.0", channel: "public", version: "1.0.0",
            profiles: ["public-fundamentals"], harnesses: ["pi"],
            updatePolicy: "reviewed-pull-request", projectContext: ".cratis/PROJECT.md",
        }));
        writeFileSync(join(repository, ".pi/settings.json"), JSON.stringify({ packages: ["npm:@cratis/ai-fundamentals@1.0.0"] }));
        execFileSync("git", ["init", "-q", repository]);
        execFileSync("git", ["-C", repository, "add", "."]);
        execFileSync("git", ["-C", repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid.example", "commit", "-qm", "Initial"]);
        const release = join(root, "release.json");
        writeFileSync(release, JSON.stringify({
            schemaVersion: "1.0.0", state: "APPROVED_PROFILE_RELEASE", publicationEligible: true,
            profileId: "public-fundamentals", packageName: "@cratis/ai-fundamentals",
            audience: "public", version: "1.1.0",
        }));
        const receipt = applySubscriptionUpdate(planSubscriptionUpdate({ repositoryRoot: repository, releaseManifestPath: release }));
        writeFileSync(join(root, "subscription-update-receipt.json"), JSON.stringify(receipt));
        const step = workflow.split("      - name: Open reviewed update pull request\n")[1]
            .split("        run: |\n")[1].split("      - name: Upload update receipt")[0]
            .split("          git -C subscriber commit")[0]
            .split("\n").map(line => line.slice(10)).join("\n");
        const tools = join(root, "tools");
        mkdirSync(tools);
        writeFileSync(join(tools, "gh"), "#!/bin/sh\nexit 0\n", { mode: 0o755 });
        execFileSync("bash", ["-c", step], {
            cwd: root,
            env: { ...process.env, RUNNER_TEMP: root, GITHUB_RUN_ID: "1", PATH: `${tools}:${process.env.PATH}` },
        });
        assert.deepEqual(execFileSync("git", ["-C", repository, "diff", "--cached", "--name-only"], { encoding: "utf8" }).trim().split("\n"),
            [".cratis/ai.json", ".gitignore", ".pi/settings.json"]);
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});

test("subscriber workflow has no ambient fleet or merge authority", () => {
    for (const forbidden of [
        "pull_request:",
        "push:",
        "secrets: inherit",
        "PAT_WORKFLOWS",
        "--auto",
        "gh pr merge",
        "push --force",
        "git push origin main",
        "repositories: *",
    ])
        assert.equal(workflow.includes(forbidden), false, forbidden);
});
