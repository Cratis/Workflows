#!/usr/bin/env node
// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import { createHash } from "node:crypto";
import {
    existsSync,
    readFileSync,
    renameSync,
    writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const semVerPattern = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;
const packageNamePattern = /^@cratis\/ai-[a-z0-9]+(?:-[a-z0-9]+)*$/;
const profileIdPattern = /^(?:public|engineering)-[a-z0-9]+(?:-[a-z0-9]+)*$/;

function sha256(content) {
    return createHash("sha256").update(content).digest("hex");
}

function parseJson(path, label) {
    try {
        return JSON.parse(readFileSync(path, "utf8"));
    } catch (error) {
        throw new Error(`${label} is not valid JSON: ${path}`, { cause: error });
    }
}

function parseSemVer(value) {
    const match = semVerPattern.exec(value);
    if (!match) throw new Error(`Version must be exact SemVer: ${value}`);
    return {
        core: [Number(match[1]), Number(match[2]), Number(match[3])],
        prerelease: match[4]?.split(".") ?? [],
    };
}

function compareIdentifiers(left, right) {
    const leftNumeric = /^[0-9]+$/.test(left);
    const rightNumeric = /^[0-9]+$/.test(right);
    if (leftNumeric && rightNumeric) return Number(left) - Number(right);
    if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
    if (left === right) return 0;
    return left < right ? -1 : 1;
}

export function compareSemVer(left, right) {
    const leftVersion = parseSemVer(left);
    const rightVersion = parseSemVer(right);
    for (let index = 0; index < 3; index++) {
        const difference = leftVersion.core[index] - rightVersion.core[index];
        if (difference) return Math.sign(difference);
    }
    if (!leftVersion.prerelease.length && !rightVersion.prerelease.length)
        return 0;
    if (!leftVersion.prerelease.length) return 1;
    if (!rightVersion.prerelease.length) return -1;
    const length = Math.max(
        leftVersion.prerelease.length,
        rightVersion.prerelease.length,
    );
    for (let index = 0; index < length; index++) {
        const leftIdentifier = leftVersion.prerelease[index];
        const rightIdentifier = rightVersion.prerelease[index];
        if (leftIdentifier === undefined) return -1;
        if (rightIdentifier === undefined) return 1;
        const difference = compareIdentifiers(leftIdentifier, rightIdentifier);
        if (difference) return Math.sign(difference);
    }
    return 0;
}

function expectedChannel(audience) {
    if (audience === "public") return "public";
    if (audience === "cratis-engineering") return "cratis-engineering";
    throw new Error(`Unsupported release audience: ${audience}`);
}

function validateReleaseManifest(manifest) {
    const requiredStrings = [
        "profileId",
        "packageName",
        "audience",
        "version",
    ];
    if (
        manifest?.schemaVersion !== "1.0.0" ||
        manifest?.state !== "APPROVED_PROFILE_RELEASE" ||
        manifest?.publicationEligible !== true ||
        requiredStrings.some(
            (property) =>
                typeof manifest[property] !== "string" ||
                !manifest[property],
        ) ||
        !profileIdPattern.test(manifest.profileId) ||
        !packageNamePattern.test(manifest.packageName)
    )
        throw new Error("Release manifest is not publication-approved");
    parseSemVer(manifest.version);
    expectedChannel(manifest.audience);
}

function validateSubscription(subscription, manifest) {
    if (
        subscription?.schemaVersion !== "1.0.0" ||
        subscription?.updatePolicy !== "reviewed-pull-request" ||
        !Array.isArray(subscription.profiles) ||
        subscription.profiles.length !== 1 ||
        subscription.profiles[0] !== manifest.profileId ||
        subscription.channel !== expectedChannel(manifest.audience) ||
        !Array.isArray(subscription.harnesses) ||
        !subscription.harnesses.length ||
        ![".cratis/PROJECT.md", ".agents/PROJECT.md"].includes(
            subscription.projectContext,
        )
    )
        throw new Error(
            "Repository subscription does not match the release profile and audience",
        );
    parseSemVer(subscription.version);
}

function packageSource(packageName, version) {
    return `npm:${packageName}@${version}`;
}

function updatePiSettings(settings, manifest, currentVersion) {
    if (!Array.isArray(settings?.packages))
        throw new Error("Pi settings must contain a packages array");
    const currentSource = packageSource(manifest.packageName, currentVersion);
    const nextSource = packageSource(manifest.packageName, manifest.version);
    let matches = 0;
    const packages = settings.packages.map((entry) => {
        if (typeof entry === "string") {
            if (entry !== currentSource) return entry;
            matches++;
            return nextSource;
        }
        if (!entry || typeof entry !== "object" || entry.source !== currentSource)
            return entry;
        matches++;
        return { ...entry, source: nextSource };
    });
    if (matches !== 1)
        throw new Error(
            `Pi settings must contain exactly one current profile package: ${currentSource}`,
        );
    return { ...settings, packages };
}

function jsonBytes(value) {
    return Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
}

function atomicWrite(path, content) {
    const temporaryPath = `${path}.cratis-ai-update.tmp`;
    writeFileSync(temporaryPath, content, { flag: "wx" });
    renameSync(temporaryPath, path);
}

function change(path, before, after) {
    return {
        path,
        beforeSha256: sha256(before),
        afterSha256: sha256(after),
    };
}

export function planSubscriptionUpdate({
    repositoryRoot,
    releaseManifestPath,
    rollback = false,
}) {
    const root = resolve(repositoryRoot);
    const manifest = parseJson(releaseManifestPath, "Release manifest");
    validateReleaseManifest(manifest);
    const subscriptionPath = join(root, ".cratis/ai.json");
    if (!existsSync(subscriptionPath))
        throw new Error("Repository has no .cratis/ai.json subscription");
    const subscription = parseJson(subscriptionPath, "Repository subscription");
    validateSubscription(subscription, manifest);
    const comparison = compareSemVer(manifest.version, subscription.version);
    if ((!rollback && comparison <= 0) || (rollback && comparison >= 0))
        throw new Error(
            rollback
                ? "Rollback version must be lower than the current version"
                : "Update version must be higher than the current version",
        );
    const apmPath = join(root, "apm.yml");
    if (
        existsSync(apmPath) &&
        /(?:@cratis\/ai-|Cratis\/AI\.Distribution)/i.test(
            readFileSync(apmPath, "utf8"),
        )
    )
        throw new Error(
            "APM-managed Cratis dependencies require lock refresh before this controller can update them",
        );

    const beforeSubscription = readFileSync(subscriptionPath);
    const nextSubscription = {
        ...subscription,
        version: manifest.version,
    };
    const writes = [
        {
            absolutePath: subscriptionPath,
            relativePath: ".cratis/ai.json",
            before: beforeSubscription,
            after: jsonBytes(nextSubscription),
        },
    ];

    if (subscription.harnesses.includes("pi")) {
        const piSettingsPath = join(root, ".pi/settings.json");
        if (!existsSync(piSettingsPath))
            throw new Error("Pi is selected but .pi/settings.json is missing");
        const beforePiSettings = readFileSync(piSettingsPath);
        const piSettings = parseJson(piSettingsPath, "Pi settings");
        writes.push({
            absolutePath: piSettingsPath,
            relativePath: ".pi/settings.json",
            before: beforePiSettings,
            after: jsonBytes(
                updatePiSettings(piSettings, manifest, subscription.version),
            ),
        });
    }

    return {
        schemaVersion: "1.0.0",
        state: rollback ? "ROLLBACK_READY" : "UPDATE_READY",
        mode: rollback ? "rollback" : "update",
        repositoryRoot: root,
        profileId: manifest.profileId,
        channel: subscription.channel,
        packageName: manifest.packageName,
        fromVersion: subscription.version,
        toVersion: manifest.version,
        releaseManifestSha256: sha256(readFileSync(releaseManifestPath)),
        changes: writes.map((entry) =>
            change(entry.relativePath, entry.before, entry.after),
        ),
        writes,
    };
}

export function applySubscriptionUpdate(plan) {
    const applied = [];
    try {
        for (const entry of plan.writes) {
            atomicWrite(entry.absolutePath, entry.after);
            applied.push(entry);
        }
    } catch (error) {
        for (const entry of applied.reverse())
            atomicWrite(entry.absolutePath, entry.before);
        throw new Error("Subscription update was rolled back after a write failure", {
            cause: error,
        });
    }
    return {
        ...plan,
        state: plan.mode === "rollback" ? "ROLLBACK_APPLIED" : "UPDATE_APPLIED",
    };
}

function publicReceipt(plan) {
    const {
        writes: _writes,
        repositoryRoot: _repositoryRoot,
        ...receipt
    } = plan;
    return receipt;
}

function parseArguments(args) {
    const options = {
        apply: false,
        rollback: false,
        repositoryRoot: ".",
        releaseManifestPath: "",
        outputPath: "",
    };
    for (let index = 0; index < args.length; index++) {
        const argument = args[index];
        if (argument === "--apply") options.apply = true;
        else if (argument === "--rollback") options.rollback = true;
        else if (argument === "--repository")
            options.repositoryRoot = args[++index] ?? "";
        else if (argument === "--release-manifest")
            options.releaseManifestPath = args[++index] ?? "";
        else if (argument === "--output")
            options.outputPath = args[++index] ?? "";
        else throw new Error(`Unknown argument: ${argument}`);
    }
    if (!options.repositoryRoot || !options.releaseManifestPath)
        throw new Error(
            "Usage: update-ai-profile-subscription.mjs --repository <path> --release-manifest <path> [--rollback] [--apply] [--output <path>]",
        );
    return options;
}

function main() {
    try {
        const options = parseArguments(process.argv.slice(2));
        let plan = planSubscriptionUpdate(options);
        if (options.apply) plan = applySubscriptionUpdate(plan);
        const receipt = publicReceipt(plan);
        const output = `${JSON.stringify(receipt, null, 2)}\n`;
        if (options.outputPath) writeFileSync(options.outputPath, output);
        process.stdout.write(output);
    } catch (error) {
        process.stderr.write(
            `${error instanceof Error ? error.message : "Subscription update failed"}\n`,
        );
        process.exitCode = 1;
    }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main();
