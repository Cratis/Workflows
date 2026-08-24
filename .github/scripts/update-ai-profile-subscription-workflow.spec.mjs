// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
