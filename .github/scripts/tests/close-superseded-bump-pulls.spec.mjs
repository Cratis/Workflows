// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { compareVersions, decodeBranch, parseTitle, planActions, run } from '../close-superseded-bump-pulls.mjs';

const repository = 'Cratis/Studio';
const options = { repository };
const branch = 'align-versions/stage-4.1.0-scene-4.2.0-screenplay-4.3.0';
function pull(number, title, overrides = {}) {
    return { number, title, state: 'open', created_at: `2026-10-${String(number).padStart(2, '0')}T00:00:00Z`,
        base: { ref: 'main' }, head: { ref: `bump/${number}`, repo: { full_name: repository } },
        user: { login: 'github-actions[bot]' }, labels: [], ...overrides };
}
const oldPull = () => pull(1, 'Bump Stage to 4.1.0');
const newPull = () => pull(2, 'Bump Stage to 4.2.0');

function fakeApi(pulls, failure) {
    const writes = [];
    const comments = new Map();
    const check = method => { if (method === failure) throw new Error(`API failure: ${method}`); };
    return { writes, comments,
        async listPulls() { check('listPulls'); return pulls.filter(item => item.state === 'open'); },
        async listComments(number) { check('listComments'); return comments.get(number) ?? []; },
        async comment(number, body) {
            check('comment'); writes.push(['comment', number, body]);
            comments.set(number, [...(comments.get(number) ?? []), { body }]);
        },
        async close(number) {
            check('close'); writes.push(['close', number]);
            pulls.find(item => item.number === number).state = 'closed';
        },
        async deleteBranch(ref) { check('deleteBranch'); writes.push(['deleteBranch', ref]); },
    };
}

test('parses single, from/to, multi-dependency and real Cratis titles', () => {
    for (const [title, expected] of [
        ['Bump Arc to 19.1 and Chronicle to 19.2', { arc: '19.1', chronicle: '19.2' }],
        ['Bump Arc from 19.0.0 to 19.1.0 and Chronicle to 19.2.0', { arc: '19.1.0', chronicle: '19.2.0' }],
        ['Bump Cratis Screenplay to 4.128.0', { screenplay: '4.128.0' }],
        ['Bump the Stage images and packages to 4.177.2', { stage: '4.177.2' }],
        ['Bump @cratis/arc from 1.0.0 to 2.0.0-rc.2+build.1', { '@cratis/arc': '2.0.0-rc.2+build.1' }],
    ]) assert.deepEqual(parseTitle(title), expected, title);
});

test('rejects ambiguous or non-version titles rather than partially matching', () => {
    for (const title of ['Update Stage to 4.1.0', 'Bump Stage to latest', 'Bump Stage to 01.0.0',
        'Bump Stage to 4.1.0-01', 'Bump Arc to 1.0.0 and Arc to 2.0.0',
        'Bump Arc to 1.0.0 and Chronicle to latest', 'Bump Stage to 4.177.2 (extra changes)']) {
        assert.equal(parseTitle(title), null, title);
    }
});

test('decodes align-versions branches and shares dependency names with title parsing', () => {
    assert.deepEqual(decodeBranch(branch), { stage: '4.1.0', scene: '4.2.0', screenplay: '4.3.0' });
    assert.deepEqual(decodeBranch('align-versions/stage-4.1.0-rc.1-scene-4.2.0-screenplay-4.3.0'),
        { stage: '4.1.0-rc.1', scene: '4.2.0', screenplay: '4.3.0' });
    assert.equal(decodeBranch('other/' + branch), null);
    assert.equal(decodeBranch('align-versions/stage-latest-scene-1.0-screenplay-1.0'), null);
    const newer = pull(2, 'Align versions', { head: { ref: branch, repo: { full_name: repository } } });
    assert.equal(planActions([pull(1, 'Bump Cratis Screenplay to 4.2.0'), newer], options).length, 1);
});

test('compares numeric SemVer, shorthand, prereleases and build metadata', () => {
    for (const [a, b, expected] of [
        ['19.10.0', '19.2.0', 1], ['19.1', '19.1.0', 0], ['4', '4.0.0', 0],
        ['1.0.0-rc.10', '1.0.0-rc.2', 1], ['1.0.0', '1.0.0-rc.1', 1],
        ['1.0.0-1', '1.0.0-alpha', -1], ['1.0.0-alpha', '1.0.0-alpha.1', -1],
        ['1.0.0-alpha-beta', '1.0.0-alpha', 1], ['1.0.0+first', '1.0.0+second', 0],
        ['9007199254740993.0.0', '9007199254740992.0.0', 1],
    ]) assert.equal(compareVersions(a, b), expected, `${a} vs ${b}`);
    assert.throws(() => compareVersions('invalid', '1.0.0'), /Invalid version/);
});

test('requires a newer superset, no downgrades and at least one higher version', () => {
    const older = pull(1, 'Bump Arc to 19.1 and Chronicle to 19.2');
    for (const [title, count] of [
        ['Bump Arc to 19.2 and Chronicle to 19.2', 1],
        ['Bump Arc to 19.1 and Chronicle to 19.2 and Stage to 4.1.0', 0],
        ['Bump Arc to 19.1 and Chronicle to 19.3 and Stage to 4.1.0', 1],
        ['Bump Arc to 19.2', 0], ['Bump Arc to 19.2 and Chronicle to 19.1', 0],
        ['Bump Arc to 19.1.0 and Chronicle to 19.2.0', 0],
    ]) assert.equal(planActions([older, pull(2, title)], options).length, count, title);
    assert.equal(planActions([oldPull(), { ...newPull(), base: { ref: 'release' } }], options).length, 0);
    assert.equal(planActions([oldPull(), { ...newPull(), created_at: oldPull().created_at }], options).length, 0);
    assert.equal(planActions([oldPull(), { ...newPull(), created_at: 'invalid' }], options).length, 0);
});

test('retains the newest replacement, never itself, and ignores closed, keep-open and Dependabot PRs', () => {
    const actions = planActions([oldPull(), newPull(), pull(3, 'Bump Stage to 4.3.0')], options);
    assert.deepEqual(actions.map(({ number, supersededBy }) => [number, supersededBy]), [[2, 3], [1, 3]]);
    assert.equal(planActions([newPull()], options).length, 0);
    for (const overrides of [{ state: 'closed' }, { labels: [{ name: 'keep-open' }] },
        { user: { login: 'dependabot[bot]' } }, { user: { login: 'dependabot' } }]) {
        assert.equal(planActions([{ ...oldPull(), ...overrides }, newPull()], options).length, 0);
    }
    assert.equal(planActions([oldPull(), { ...newPull(), user: { login: 'dependabot[bot]' } }], options).length, 0);
});

test('unlisted human authors get an idempotent comment only, even on a matching prefix', async () => {
    const human = { ...oldPull(), user: { login: 'human' }, head: { ref: branch, repo: { full_name: repository } } };
    const replacement = pull(2, 'Align versions', {
        head: { ref: branch.replace('stage-4.1.0', 'stage-4.2.0'), repo: { full_name: repository } },
    });
    const api = fakeApi([human, replacement]);
    await run({ api, ...options, log() {} });
    await run({ api, ...options, log() {} });
    assert.deepEqual(api.writes, [['comment', 1, 'Superseded by #2']]);
    assert.equal(human.state, 'open');
});

test('closes configured bot/PAT authors and deletes only same-repo prefixed branches', async () => {
    for (const [ref, repo, deleteExpected] of [
        ['align-versions/old', repository, true], ['automation/old', repository, false],
        ['align-versions/old', 'someone/fork', false], ['align-versions/old', null, false],
    ]) {
        const older = { ...oldPull(), user: { login: 'service-account' },
            head: { ref, repo: repo ? { full_name: repo } : null } };
        const api = fakeApi([older, newPull()]);
        await run({ api, ...options, authors: 'github-actions[bot] service-account', log() {} });
        assert.deepEqual(api.writes.slice(0, 2), [['comment', 1, 'Superseded by #2'], ['close', 1]]);
        assert.deepEqual(api.writes.slice(2), deleteExpected ? [['deleteBranch', ref]] : []);
    }
    const older = { ...oldPull(), head: { ref: 'custom/bump', repo: { full_name: repository } } };
    assert.equal(planActions([older, newPull()], { ...options, branchPrefixes: 'other/ custom/' })[0].deleteBranch, 'custom/bump');
    assert.equal(planActions([older, newPull()], { ...options, branchPrefixes: '' })[0].deleteBranch, null);
});

test('dry-run logs the plan and makes no writes', async () => {
    const api = fakeApi([oldPull(), newPull()]);
    const logs = [];
    const actions = await run({ api, ...options, dryRun: true, log: line => logs.push(JSON.parse(line)) });
    assert.deepEqual(api.writes, []);
    assert.deepEqual(logs, actions);
    assert.equal(actions[0].number, 1);
    assert.equal(actions[0].comment, true);
});

test('every API failure propagates without continuing to later writes', async () => {
    for (const failure of ['listPulls', 'listComments', 'comment', 'close', 'deleteBranch']) {
        const older = { ...oldPull(), head: { ref: 'align-versions/old', repo: { full_name: repository } } };
        const api = fakeApi([older, newPull()], failure);
        await assert.rejects(run({ api, ...options, log() {} }), new RegExp(`API failure: ${failure}`));
        const count = { listPulls: 0, listComments: 0, comment: 0, close: 1, deleteBranch: 2 }[failure];
        assert.equal(api.writes.length, count);
    }
});
