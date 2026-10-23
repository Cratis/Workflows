// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const versionPattern = '(?:0|[1-9]\\d*)(?:\\.(?:0|[1-9]\\d*)){0,2}(?:-[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?(?:\\+[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*)?';

function versionParts(value) {
    if (!new RegExp(`^${versionPattern}$`).test(value)) return null;
    const [core] = value.split('+')[0].split('-');
    // Split at the first hyphen only: prerelease identifiers may themselves contain hyphens.
    const suffix = value.split('+')[0].slice(core.length + 1);
    const identifiers = suffix ? suffix.split('.') : [];
    if (identifiers.some(id => /^\d+$/.test(id) && id.length > 1 && id.startsWith('0'))) return null;
    return { core: core.split('.').map(BigInt).concat([0n, 0n]).slice(0, 3), identifiers };
}

export function compareVersions(left, right) {
    const a = versionParts(left);
    const b = versionParts(right);
    if (!a || !b) throw new Error(`Invalid version: ${!a ? left : right}`);
    for (let index = 0; index < 3; index++) {
        if (a.core[index] !== b.core[index]) return a.core[index] > b.core[index] ? 1 : -1;
    }
    if (!a.identifiers.length || !b.identifiers.length) {
        return a.identifiers.length === b.identifiers.length ? 0 : a.identifiers.length ? -1 : 1;
    }
    for (let index = 0; index < Math.max(a.identifiers.length, b.identifiers.length); index++) {
        const x = a.identifiers[index];
        const y = b.identifiers[index];
        if (x === y) continue;
        if (x === undefined || y === undefined) return x === undefined ? -1 : 1;
        const numericX = /^\d+$/.test(x);
        const numericY = /^\d+$/.test(y);
        if (numericX !== numericY) return numericX ? -1 : 1;
        return (numericX ? BigInt(x) > BigInt(y) : x > y) ? 1 : -1;
    }
    return 0;
}

function dependencyName(value) {
    return value.trim().toLowerCase().replace(/^cratis\s+/, '').replace(/^the stage images and packages$/, 'stage');
}

export function parseTitle(title) {
    if (!/^Bump /i.test(title)) return null;
    const dependencies = {};
    // An "and" inside a dependency name (Stage images and packages) is not a separator.
    const segments = title.slice(5).split(new RegExp(`(?<= to ${versionPattern}) and `, 'i'));
    for (const segment of segments) {
        const match = segment.match(new RegExp(`^(.+?)(?: from ${versionPattern})? to (${versionPattern})$`, 'i'));
        if (!match || !versionParts(match[2])) return null;
        const name = dependencyName(match[1]);
        if (Object.hasOwn(dependencies, name)) return null;
        Object.defineProperty(dependencies, name, { value: match[2], enumerable: true });
    }
    return dependencies;
}

export function decodeBranch(branch) {
    const match = branch.match(new RegExp(`^align-versions/stage-(${versionPattern})-scene-(${versionPattern})-screenplay-(${versionPattern})$`));
    if (!match || match.slice(1).some(version => !versionParts(version))) return null;
    return { stage: match[1], scene: match[2], screenplay: match[3] };
}

function dependencies(pull) {
    return decodeBranch(pull.head.ref) ?? parseTitle(pull.title);
}

export function supersedes(newer, older) {
    if (newer.number === older.number || newer.base.ref !== older.base.ref ||
        !(Date.parse(newer.created_at) > Date.parse(older.created_at))) return false;
    const next = dependencies(newer);
    const previous = dependencies(older);
    if (!next || !previous) return false;
    const comparisons = Object.entries(previous).map(([name, version]) =>
        Object.hasOwn(next, name) ? compareVersions(next[name], version) : -1);
    return comparisons.every(value => value >= 0) && comparisons.some(value => value > 0);
}

const dependabot = pull => /^dependabot(?:\[bot\])?$/i.test(pull.user.login);
const words = value => value.split(/\s+/).filter(Boolean);

export function planActions(pulls, { repository, authors = 'github-actions[bot]', branchPrefixes = 'align-versions/' }) {
    const allowedAuthors = words(authors).map(author => author.toLowerCase());
    const prefixes = words(branchPrefixes);
    const candidates = pulls.filter(pull => pull.state === 'open' && !dependabot(pull))
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.number - a.number);
    return candidates.flatMap(pull => {
        if (pull.labels.some(label => label.name.toLowerCase() === 'keep-open')) return [];
        const replacement = candidates.find(candidate => supersedes(candidate, pull));
        if (!replacement) return [];
        // Prefixes never authorize closing an unlisted (human) author's PR.
        const close = allowedAuthors.includes(pull.user.login.toLowerCase());
        return [{ number: pull.number, supersededBy: replacement.number, body: `Superseded by #${replacement.number}`,
            close, deleteBranch: close && pull.head.repo?.full_name.toLowerCase() === repository.toLowerCase() &&
                prefixes.some(prefix => pull.head.ref.startsWith(prefix)) ? pull.head.ref : null }];
    });
}

export async function run({ api, repository, authors, branchPrefixes, dryRun = false, log = console.log }) {
    const actions = planActions(await api.listPulls(), { repository, authors, branchPrefixes });
    for (const action of actions) {
        const comments = await api.listComments(action.number);
        action.comment = !comments.some(comment => comment.body === action.body);
        log(JSON.stringify(action));
        if (dryRun) continue;
        if (action.comment) await api.comment(action.number, action.body);
        if (action.close) await api.close(action.number);
        if (action.deleteBranch) await api.deleteBranch(action.deleteBranch);
    }
    return actions;
}

export function githubApi(repository) {
    const endpoint = `repos/${repository}`;
    function request(method, path, body, paginate = false) {
        const args = ['api', '--method', method, path];
        if (paginate) args.push('--paginate', '--slurp');
        if (body) args.push('--input', '-');
        const output = execFileSync('gh', args, { encoding: 'utf8', input: body ? JSON.stringify(body) : undefined });
        return output.trim() ? JSON.parse(output) : null;
    }
    return {
        listPulls: async () => request('GET', `${endpoint}/pulls?state=open&per_page=100`, null, true).flat(),
        listComments: async number => request('GET', `${endpoint}/issues/${number}/comments?per_page=100`, null, true).flat(),
        comment: async (number, body) => request('POST', `${endpoint}/issues/${number}/comments`, { body }),
        close: async number => request('PATCH', `${endpoint}/pulls/${number}`, { state: 'closed' }),
        deleteBranch: async branch => request('DELETE', `${endpoint}/git/refs/heads/${encodeURIComponent(branch)}`),
    };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    const repository = process.env.GITHUB_REPOSITORY;
    if (!/^Cratis\/[A-Za-z0-9_.-]+$/i.test(repository ?? '')) throw new Error('GITHUB_REPOSITORY must name a Cratis repository');
    const dryRun = process.env.DRY_RUN ?? 'false';
    if (!['true', 'false'].includes(dryRun)) throw new Error('DRY_RUN must be true or false');
    const actions = await run({ api: githubApi(repository), repository, authors: process.env.AUTHORS,
        branchPrefixes: process.env.BRANCH_PREFIXES, dryRun: dryRun === 'true' });
    console.log(`${dryRun === 'true' ? 'Dry run' : 'Completed'}: ${actions.length} superseded bump PR(s)`);
}
