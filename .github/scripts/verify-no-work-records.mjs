// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(import.meta.url);
const ruleCount = 5;
const rootNames = 'README LICENSE AGENTS CLAUDE GEMINI CODE_OF_CONDUCT CONTRIBUTING SECURITY CHANGELOG CREDITS RESOURCES BRAND MESSAGING PAGES SITE PRIVACY_POLICY ROADMAP START-HERE CHRONICLE COMPATIBILITY NOTICE SUPPORT GOVERNANCE VERSION DECISIONS'.split(' ');
// Retain legacy all-caps matches within a filename; mixed-case matching is
// prefix-based so a numbered ADR about handover policy remains documentation.
const legacySession = /(^|\/)[^/]*HANDOVER[^/]*\.md$|(^|\/)PROMPT-[^/]+\.md$|(^|\/)[^/]*NEXT-SESSION[^/]*\.md$|(^|\/)SESSION-PROMPT[^/]*\.md$|(^|\/)[^/]*SESSION[-_]HANDOVER[^/]*\.md$/;
const session = /(^|\/)(HANDOVER|PROMPT|SESSION)[-_][^/]+\.md$|(^|\/)NEXT-SESSION([-_][^/]+)?\.md$/i;
const shape = /(^|\/)(PLAN|DESIGN|REPORT|STATUS)([-_][^/]*)?\.md$/i;
const splitInput = value => (value ?? '').split(',').map(part => part.trim()).filter(Boolean);

export function checkPaths(files, environment = process.env) {
    const allowed = new Set([...rootNames, ...splitInput(environment.EXTRA_ALLOWED).map(name => name.replace(/\.md$/i, '').toUpperCase())]);
    const docs = ['decisions', 'Documentation', 'docs', 'Knowledge', 'evidence', 'governance', ...splitInput(environment.EXTRA_ALLOWED_PATHS)].map(path => `${path.replace(/\/$/, '').toLowerCase()}/`);
    const violations = [];
    for (const path of files) {
        let rule;
        if (path.startsWith('.ai-work/')) {
            rule = 'ai-work';
        } else if (/^\.pi\/(delegate|fusion|tasks|[^/]*-session-[^/]*)\//.test(path)) {
            rule = 'pi-runtime';
        } else if (/\.md$/i.test(path)) {
            const allowedRoot = !path.includes('/') && allowed.has(path.slice(0, -3).toUpperCase());
            const documentation = /(^|\/)templates\//i.test(path) || docs.some(prefix => path.toLowerCase().startsWith(prefix));
            if (!path.includes('/') && /^[A-Z][A-Z0-9_.-]*[A-Z0-9]\.md$/.test(path) && !allowedRoot) {
                rule = 'root-document';
            } else if (!/^\.(claude|github|pi|agents|cratis)\//.test(path)) {
                if (legacySession.test(path) || session.test(path)) {
                    rule = 'session';
                } else if (shape.test(path) && !allowedRoot && !documentation) {
                    rule = 'work-record-shape';
                }
            }
        }
        if (rule) violations.push({ rule, path });
    }
    return violations;
}

function check() {
    // NUL-delimited output handles spaces, newlines and Git's quoted filenames.
    const listing = spawnSync('git', ['ls-files', '-z'], { encoding: 'utf8', maxBuffer: 1024 * 1024 * 1024 });
    if (listing.error || listing.status !== 0) {
        console.error(`could not run: git ls-files failed: ${listing.error?.message ?? listing.stderr.trim()}`);
        return 2;
    }
    const files = listing.stdout.split('\0').filter(Boolean);
    if (!files.length) {
        console.error('could not run: git ls-files listed no tracked files');
        return 2;
    }
    const violations = checkPaths(files);
    for (const violation of violations) {
        console.log(`[${violation.rule}] ${JSON.stringify(violation.path)}`);
        if (process.env.GITHUB_ACTIONS === 'true') {
            const escaped = violation.path.replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A');
            console.log(`::error title=${violation.rule}::${escaped}`);
        }
    }
    if (violations.length) {
        console.log('AI session work artifacts are local-only: keep them in the untracked .ai-work/ folder. A durable follow-up becomes a GitHub issue, not a file.');
    } else {
        console.log('No AI work records tracked.');
    }
    console.log(`scanned: ${files.length} tracked files, rules: ${ruleCount}, violations: ${violations.length}`);
    return violations.length ? 1 : 0;
}

function selfTest() {
    const scratch = mkdtempSync(join(tmpdir(), 'verify-no-work-records-'));
    const env = { ...process.env, EXTRA_ALLOWED: '', EXTRA_ALLOWED_PATHS: '', GIT_CEILING_DIRECTORIES: scratch };
    const git = (cwd, ...args) => {
        const result = spawnSync('git', args, { cwd, env, encoding: 'utf8' });
        if (result.error || result.status !== 0) throw new Error(result.error?.message ?? result.stderr);
    };
    const run = (name, files) => {
        const cwd = join(scratch, name);
        mkdirSync(cwd);
        git(cwd, 'init', '-q');
        for (const path of files) {
            mkdirSync(dirname(join(cwd, path)), { recursive: true });
            writeFileSync(join(cwd, path), 'x\n');
        }
        git(cwd, 'add', '-f', '-A');
        return spawnSync(process.execPath, [script], { cwd, env, encoding: 'utf8' });
    };
    try {
        const plants = [
            ['ai-work', '.ai-work/notes.md'],
            ['pi-runtime', '.pi/fusion/x/prompt.md'],
            ['root-document', 'IMPLEMENTATION_STATUS.md'],
            ['session', 'decisions/hAnDoVeR-notes.md'],
            ['work-record-shape', 'Notes/pLaN-something.md']
        ];
        let red = 0;
        for (const [index, [rule, path]] of plants.entries()) {
            const result = run(`plant-${index}`, ['README.md', path]);
            if (result.status === 1 && result.stdout.includes(`[${rule}] ${JSON.stringify(path)}`) && result.stdout.includes('violations: 1')) red++;
            else console.error(`self-test: planted defect not caught: ${path}`);
        }
        const clean = ['README.md', 'DECISIONS.md', 'decisions/D-0001-example.md', 'Documentation/decisions/0003-kernel-boundary.md', 'Source/Reporting.md', 'templates/build-kit/lib/prompt.md', 'templates/session.md', 'templates/handover.md', '.pi/settings.json'];
        if (process.env.VERIFY_SELF_TEST_BREAK === '1') clean.push('PLAN-break.md');
        const control = run('control', clean);
        const counts = control.stdout.includes(`scanned: ${clean.length} tracked files, rules: ${ruleCount}, violations: 0`);
        const cwd = join(scratch, 'not-a-repo');
        mkdirSync(cwd);
        const broken = spawnSync(process.execPath, [script], { cwd, env, encoding: 'utf8' });
        console.log(`self-test: ${plants.length} planted, ${red} red, control ${control.status}, broken-listing ${broken.status}`);
        return red === plants.length && control.status === 0 && counts && broken.status === 2 ? 0 : 1;
    } finally {
        rmSync(scratch, { recursive: true, force: true });
    }
}

if (process.argv[1] && realpathSync(process.argv[1]) === script) {
    try {
        const args = process.argv.slice(2);
        if (!args.length) process.exitCode = check();
        else if (args.length === 1 && args[0] === '--self-test') process.exitCode = selfTest();
        else {
            console.error('could not run: usage: node verify-no-work-records.mjs [--self-test]');
            process.exitCode = 2;
        }
    } catch (error) {
        console.error(`could not run: ${error.message}`);
        process.exitCode = 2;
    }
}
