// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
import assert from 'node:assert/strict';
import test, { describe } from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { compilePatterns, githubApi, isCorpusPath, parseList, run, verifyImpact } from '../verify-ai-corpus-impact.mjs';

const repository = 'Cratis/Screenplay';
const number = 495;
const config = {
    paths: [
        'Source/DotNET/Screenplay/**', 'Source/DotNET/Screenplay.Mcp/**', 'Source/DotNET/Tool/**', 'Source/DotNET/Screenplay.CanonicalCorpus/**',
        'Source/Screenplay/Compiler/**', 'Source/Screenplay/Monaco/**', 'Source/Screenplay/VSCodeExtension/**', 'Source/Screenplay/EventModels/**',
        'Source/Screenplay/McpApp/**', 'Documentation/screenplay/**', 'Samples/**'
    ],
    excludePaths: ['**/for_*/**', '**/*.Specs/**'],
    corpusRepository: 'Cratis/AI'
};
const configEnv = { CORPUS_PATHS: config.paths.join('\n'), CORPUS_EXCLUDE_PATHS: config.excludePaths.join(',') };
const languageFile = { filename: 'Source/DotNET/Screenplay/Parsing/Parser.cs' };
const tracked = [{ name: 'ai-corpus: tracked' }];
const none = [{ name: 'ai-corpus: none' }];

function fixture({ labels = [], files = [languageFile], comments = [], body = '', draft = false, author = 'woksin', sources = [], exists = [] } = {}) {
    return {
        async pullRequest() { return { labels, changed_files: files.length, body, draft, user: { login: author } }; },
        async files() { return files; },
        async comments() { return comments.map(body => ({ body })); },
        async crossReferences() { return sources; },
        async issueExists(number) { return exists.includes(number); }
    };
}

function verify(options) {
    return verifyImpact({ repository, number, config, api: fixture(options) });
}

test('no corpus-relevant paths passes without a decision', async () => {
    assert.equal((await verify({ files: [{ filename: '.github/workflows/verify-semver-label.yml' }] })).code, 0);
});

test('missing label fails with exact commands for either choice', async () => {
    const result = await verify();
    assert.equal(result.code, 1);
    assert.deepEqual(result.fixes, [
        'gh pr edit 495 --repo Cratis/Screenplay --add-label "ai-corpus: tracked"',
        'gh pr edit 495 --repo Cratis/Screenplay --add-label "ai-corpus: none"'
    ]);
});

test('both labels fails and tells the author to remove the other label', async () => {
    const result = await verify({ labels: [...tracked, ...none] });
    assert.equal(result.code, 1);
    assert.match(result.fixes[0], /--remove-label "ai-corpus: none"/);
    assert.match(result.fixes[1], /--remove-label "ai-corpus: tracked"/);
});

test('tracked without a link fails', async () => {
    assert.equal((await verify({ labels: tracked })).code, 1);
});

test('tracked comment link passes only when the mock API verifies it exists', async () => {
    const options = { labels: tracked, comments: ['Corpus impact: https://github.com/Cratis/AI/issues/529'] };
    assert.equal((await verify(options)).code, 1);
    const result = await verify({ ...options, exists: [529] });
    assert.equal(result.code, 0);
    assert.equal(result.evidence, 'https://github.com/Cratis/AI/issues/529');
});

test('tracked accepts body shorthand and pull request links', async () => {
    assert.equal((await verify({ labels: tracked, body: '- Updated syntax (Cratis/AI#529)', exists: [529] })).code, 0);
    assert.equal((await verify({ labels: tracked, comments: ['https://github.com/Cratis/AI/pull/529'], exists: [529] })).code, 0);
});

test('tracked accepts a verified cross-reference from Cratis/AI, but not another repository', async () => {
    const source = { number: 529, repository: { nameWithOwner: 'Cratis/AI' } };
    assert.equal((await verify({ labels: tracked, sources: [source], exists: [529] })).code, 0);
    assert.equal((await verify({ labels: tracked, sources: [source] })).code, 1);
    assert.equal((await verify({ labels: tracked, sources: [{ ...source, repository: { nameWithOwner: 'Cratis/Other' } }], exists: [529] })).code, 1);
});

test('none without a comment reason fails, including a reason only in the body', async () => {
    assert.equal((await verify({ labels: none })).code, 1);
    assert.equal((await verify({ labels: none, body: 'Corpus impact: none - internal refactor' })).code, 1);
    assert.equal((await verify({ labels: none, comments: ['Corpus impact: none -   ', 'Corpus impact: none - ---'] })).code, 1);
});

test('none accepts hyphen, en dash, em dash and colon separators case-insensitively', async () => {
    for (const separator of ['-', '–', '—', ':']) {
        const result = await verify({ labels: none, comments: [`cOrPuS iMpAcT: NoNe${separator} internal refactor, no behavior change`] });
        assert.equal(result.code, 0, separator);
        assert.equal(result.evidence, 'internal refactor, no behavior change');
    }
});

test('none rejects empty, non-letter and deferred reasons with every separator', async () => {
    for (const separator of ['-', '–', '—', ':']) {
        for (const reason of ['', '   ', '---', 'will do later', 'follow-up', 'followup', 'TBD', 'todo']) {
            assert.equal((await verify({ labels: none, comments: [`Corpus impact: none ${separator} ${reason}`] })).code, 1, `${separator} ${reason}`);
        }
    }
});

test('none with an actual reason in a comment line passes', async () => {
    const result = await verify({ labels: none, comments: ['Review notes\nCorpus impact: none - internal refactor, no behavior change\nChecks passed'] });
    assert.equal(result.code, 0);
    assert.equal(result.evidence, 'internal refactor, no behavior change');
});

test('draft and Dependabot pass with a skip notice and no evidence reads', async () => {
    for (const options of [{ draft: true }, { author: 'dependabot[bot]' }]) {
        const api = fixture(options);
        api.files = async () => { throw new Error('Must not read files for exempt PRs'); };
        const result = await verifyImpact({ repository, number, config, api });
        assert.equal(result.code, 0);
        assert.equal(result.decision, 'skipped');
    }
});

test('corpus paths match all included roots and exclude specs', () => {
    for (const path of [
        'Source/DotNET/Screenplay/Syntax/Node.cs', 'Source/DotNET/Screenplay.Mcp/Tools.cs',
        'Source/DotNET/Tool/Check.cs', 'Source/DotNET/Screenplay.CanonicalCorpus/Corpus/source.play',
        'Source/Screenplay/Compiler/Parser.ts', 'Source/Screenplay/Monaco/language.ts',
        'Source/Screenplay/VSCodeExtension/syntaxes/grammar.json', 'Source/Screenplay/EventModels/Mapping.ts',
        'Source/Screenplay/McpApp/main.ts', 'Documentation/screenplay/grammar.md', 'Samples/Library/library.play'
    ]) assert.equal(isCorpusPath(path, config), true, path);
    for (const path of [
        'Source/DotNET/Screenplay/Parsing/for_Parser/when_parsing.cs',
        'Source/Screenplay/Compiler/for_parse/when_parsing.ts',
        'Source/DotNET/Screenplay.CanonicalVectors.Specs/Test.cs',
        'Source/DotNET/Screenplay/Nested.Specs/Test.cs',
        'Source/DotNET/Screenplay.Other/Node.cs', 'Source/Screenplay/Views/View.ts', 'README.md'
    ]) assert.equal(isCorpusPath(path, config), false, path);
});

test('spec-only changes pass and moving out of a corpus root still needs a decision', async () => {
    assert.equal((await verify({ files: [{ filename: 'Source/DotNET/Screenplay/for_Parser/when_parsing.cs' }] })).code, 0);
    assert.equal((await verify({ files: [{ filename: 'Other/Parser.cs', previous_filename: languageFile.filename }] })).code, 1);
});

function response(data, status = 200) {
    return { ok: status >= 200 && status < 300, status, async json() { return data; } };
}

test('REST adapter reads live state and paginates files/comments', async () => {
    const requests = [];
    const fetchApi = async (url, options) => {
        requests.push({ url, options });
        if (url.endsWith('/pulls/495')) return response({ draft: false, user: { login: 'woksin' }, labels: tracked, body: '', changed_files: 101 });
        if (url.includes('/files?')) return response(url.endsWith('page=1') ? Array.from({ length: 100 }, (_, i) => ({ filename: `Other/${i}` })) : [languageFile]);
        if (url.includes('/comments?')) return response(url.endsWith('page=1') ? Array.from({ length: 100 }, () => ({ body: '' })) : [{ body: 'Cratis/AI#529' }]);
        if (url.endsWith('/Cratis/AI/issues/529')) return response({ number: 529 });
        throw new Error(`Unexpected API read ${url}`);
    };
    const result = await verifyImpact({ repository, number, config, api: githubApi('token', 'Cratis/AI', fetchApi) });
    assert.equal(result.code, 0);
    assert.equal(requests.length, 6);
    assert.ok(requests.every(({ options }) => options.headers.Authorization === 'Bearer token' && options.signal));
});

test('GraphQL adapter pages cross-references on the live PR', async () => {
    const cursors = [];
    const api = githubApi('token', 'Cratis/AI', async (url, options) => {
        assert.equal(url, 'https://api.github.com/graphql');
        const { variables, query } = JSON.parse(options.body);
        cursors.push(variables.cursor);
        assert.equal(variables.number, 495);
        assert.match(query, /CROSS_REFERENCED_EVENT/);
        return response({ data: { repository: { pullRequest: { timelineItems: {
            nodes: [{ source: { number: 529, repository: { nameWithOwner: 'Cratis/AI' } } }],
            pageInfo: { hasNextPage: variables.cursor === null, endCursor: 'next' }
        } } } } });
    });
    assert.equal((await api.crossReferences(repository, number)).length, 2);
    assert.deepEqual(cursors, [null, 'next']);
});

test('issue adapter treats 404 and 410 as missing and accepts a transferred issue', async () => {
    for (const status of [404, 410]) {
        assert.equal(await githubApi('token', 'Cratis/AI', async () => response({}, status)).issueExists(529), false, status);
    }
    assert.equal(await githubApi('token', 'Cratis/AI', async () => response({ message: 'Moved Permanently' }, 301)).issueExists(529), true);
});

test('runner distinguishes missing, transferred and failed issue lookups', async () => {
    for (const [status, expected] of [[404, 1], [410, 1], [301, 0], [200, 0], [302, 2], [400, 2], [401, 2], [403, 2], [429, 2], [500, 2]]) {
        const code = await run({
            env: { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', ...configEnv },
            fetchApi: async url => {
                if (url.endsWith('/pulls/495')) return response({ draft: false, user: { login: 'woksin' }, changed_files: 1, labels: tracked, body: 'Cratis/AI#529' });
                if (url.includes('/files?')) return response([languageFile]);
                if (url.includes('/comments?')) return response([]);
                if (url.endsWith('/Cratis/AI/issues/529')) return response({ message: 'Issue lookup' }, status);
                if (url.endsWith('/graphql')) return response({ data: { repository: { pullRequest: { timelineItems: {
                    nodes: [], pageInfo: { hasNextPage: false }
                } } } } });
                throw new Error(`Unexpected API read ${url}`);
            },
            log() {}
        });
        assert.equal(code, expected, status);
    }
});

test('API cannot-run errors are not policy failures', async () => {
    for (const status of [403, 429, 500]) {
        await assert.rejects(githubApi('token', 'Cratis/AI', async () => response({}, status)).issueExists(529), /GitHub API/);
    }
    await assert.rejects(githubApi('token', 'Cratis/AI', async () => response({ errors: [{ message: 'denied' }] })).crossReferences(repository, number), /GraphQL/);
    const api = fixture();
    api.pullRequest = async () => ({ changed_files: 3001, draft: false, user: { login: 'woksin' }, labels: tracked });
    await assert.rejects(verifyImpact({ repository, number, config, api }), /incomplete/);
});

test('runner reports exit 2 and step summary for an API failure', async () => {
    const logs = [];
    let summaryText;
    const code = await run({
        env: { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', ...configEnv, GITHUB_RUN_ID: '123', GITHUB_STEP_SUMMARY: 'summary' },
        fetchApi: async () => response({}, 403), log: line => logs.push(line),
        summary: async (path, text) => { assert.equal(path, 'summary'); summaryText = text; }
    });
    assert.equal(code, 2);
    assert.match(logs[0], /^::error::/);
    assert.match(logs[0], /gh run rerun 123 --repo Cratis\/Screenplay --failed/);
    assert.match(summaryText, /could not run/);
});

test('runner never reads the event payload and uses live draft status for its notice', async () => {
    const logs = [];
    assert.equal(await run({
        env: { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', ...configEnv, GITHUB_EVENT_PATH: '/nonexistent/event.json' },
        fetchApi: async () => response({ draft: true }), log: line => logs.push(line)
    }), 0);
    assert.match(logs[0], /^::notice::Draft/);
});

test('runner returns exit 1 for a policy failure with a summary and fix commands', async () => {
    const logs = [];
    let summaryText;
    const code = await run({
        env: { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', ...configEnv, GITHUB_STEP_SUMMARY: 'summary' },
        fetchApi: async url => url.endsWith('/pulls/495')
            ? response({ draft: false, user: { login: 'woksin' }, changed_files: 1, labels: [] })
            : response([languageFile]),
        log: line => logs.push(line), summary: async (_, text) => { summaryText = text; }
    });
    assert.equal(code, 1);
    assert.match(logs[0], /^::error::.*gh pr edit 495/);
    assert.match(summaryText, /Matched corpus-relevant paths:/);
});

const otherConfig = { paths: ['src/**'], excludePaths: [], corpusRepository: 'Other/Corpus' };
const srcFile = { filename: 'src/a.cs' };

test('excluded paths are not applicable, but a rename out of an included path still counts', async () => {
    const api = files => ({ ...fixture({ files }) });
    const excluded = await verifyImpact({ repository, number, config, api: api([{ filename: 'Source/DotNET/Screenplay/for_x/y.cs' }]) });
    assert.equal(excluded.decision, 'not applicable');
    const renamed = await verifyImpact({ repository, number, config, api: api([{ filename: 'Source/DotNET/Screenplay/for_x/y.cs', previous_filename: languageFile.filename }]) });
    assert.equal(renamed.code, 1);
    const renamedIn = await verifyImpact({ repository, number, config, api: api([{ filename: languageFile.filename, previous_filename: 'Source/DotNET/Screenplay/Foo.Specs/y.cs' }]) });
    assert.equal(renamedIn.code, 1);
});

test('glob semantics: ** crosses directories, * does not, **/ matches zero directories', () => {
    const matches = (glob, path) => compilePatterns([glob])[0].test(path);
    assert.equal(matches('src/**', 'src/a/b/c.cs'), true);
    assert.equal(matches('src/*.cs', 'src/a/c.cs'), false);
    assert.equal(matches('src/*.cs', 'src/c.cs'), true);
    assert.equal(matches('**/for_*/**', 'for_x/a.cs'), true);
    assert.equal(matches('**/for_*/**', 'a/b/for_x/a.cs'), true);
    assert.equal(matches('**/for_*/**', 'a/before_x/a.cs'), false);
    assert.equal(matches('a?.cs', 'ab.cs'), true);
    assert.equal(matches('a?.cs', 'a/.cs'), false);
    assert.equal(matches('a.b+c(1)/**', 'a.b+c(1)/x'), true);
    assert.equal(matches('a.b', 'axb'), false);
});

test('lists split on newlines and commas and ignore blanks and whitespace', () => {
    assert.deepEqual(parseList(' a/** ,b/**\n\n  c/**\r\n, ,'), ['a/**', 'b/**', 'c/**']);
    assert.deepEqual(parseList(undefined), []);
});

test('configuration errors exit 2, not 1', async () => {
    const base = { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', CORPUS_PATHS: 'src/**' };
    const cases = [
        { ...base, CORPUS_PATHS: '' },
        { ...base, CORPUS_PATHS: ' \n, ' },
        { ...base, CORPUS_REPOSITORY: 'not-a-repo' },
        { ...base, CORPUS_REPOSITORY: 'a/b/c' },
        { ...base, CORPUS_PATHS: '../secrets/**' },
        { ...base, CORPUS_PATHS: '/abs/**' },
        { ...base, CORPUS_EXCLUDE_PATHS: 'a/../b' },
        { ...base, PR_NUMBER: '' },
        { ...base, GITHUB_TOKEN: '' }
    ];
    for (const env of cases) {
        const code = await run({ env, fetchApi: async () => { throw new Error('Must not call the API'); }, log() {} });
        assert.equal(code, 2, JSON.stringify(env));
    }
});

test('a non-default corpus repository is used for links, cross-references and fix commands', async () => {
    const check = options => verifyImpact({ repository, number, config: otherConfig, api: fixture({ files: [srcFile], ...options }) });
    const options = { labels: tracked, exists: [7] };
    for (const text of ['https://github.com/Other/Corpus/issues/7', 'https://github.com/other/corpus/pull/7', 'Other/Corpus#7', 'other/CORPUS#7']) {
        assert.equal((await check({ ...options, comments: [text] })).code, 0, text);
    }
    assert.equal((await check({ ...options, comments: ['Cratis/AI#7', 'https://github.com/Cratis/AI/issues/7'] })).code, 1);
    const source = { number: 7, repository: { nameWithOwner: 'Other/Corpus' } };
    assert.equal((await check({ ...options, sources: [source] })).code, 0);
    assert.equal((await check({ ...options, sources: [{ ...source, repository: { nameWithOwner: 'Cratis/AI' } }] })).code, 1);
    const failure = await check({ labels: tracked });
    assert.match(failure.fixes[0], /https:\/\/github\.com\/Other\/Corpus\/issues\/<number>/);
    assert.match(failure.message, /Other\/Corpus/);
    const evidence = await check({ ...options, comments: ['Other/Corpus#7'] });
    assert.equal(evidence.evidence, 'https://github.com/Other/Corpus/issues/7');
    const urls = [];
    await githubApi('token', 'Other/Corpus', async url => { urls.push(url); return response({}); }).issueExists(7);
    assert.deepEqual(urls, ['https://api.github.com/repos/Other/Corpus/issues/7']);
});

test('corpus repository names containing . and - are matched literally', async () => {
    const dotted = { ...otherConfig, corpusRepository: 'My-Org/corpus.v2' };
    const check = comment => verifyImpact({ repository, number, config: dotted, api: fixture({ files: [srcFile], labels: tracked, comments: [comment], exists: [3] }) });
    assert.equal((await check('My-Org/corpus.v2#3')).code, 0);
    assert.equal((await check('My-Org/corpusxv2#3')).code, 1);
});

test('malformed or foreign links do not count as tracked evidence', async () => {
    for (const text of ['Cratis/AI#0', 'Cratis/AI#-1', 'Cratis/AI#12abc', 'Cratis/AI#', 'https://github.com/Cratis/AI/issues/', 'https://github.com/Cratis/AI/issues/abc',
        'https://github.com/Cratis/Other/issues/529', 'Cratis/Other#529', '#529']) {
        const result = await verify({ labels: tracked, comments: [text], exists: [0, 12, 529] });
        assert.equal(result.code, 1, text);
    }
    // Documented behavior carried over from Screenplay: a link inside a code span is still a link.
    assert.equal((await verify({ labels: tracked, comments: ['`Cratis/AI#529`'], exists: [529] })).code, 0);
});

test('none needs a real explanation', async () => {
    for (const text of ['Corpus impact: none -', 'Corpus impact: none - 123 !!', 'Corpus impact: none - follow-up']) {
        assert.equal((await verify({ labels: none, comments: [text] })).code, 1, text);
    }
});

test('a rerun with changed live decisions gives the new result without caching', async () => {
    assert.equal((await verify()).code, 1);
    assert.equal((await verify({ labels: tracked, comments: ['Cratis/AI#9'] })).code, 1);
    assert.equal((await verify({ labels: tracked, comments: ['Cratis/AI#9'], exists: [9] })).code, 0);
    assert.equal((await verify({ labels: none })).code, 1);
    assert.equal((await verify({ labels: none, comments: ['Corpus impact: none - docs only wording'] })).code, 0);
    assert.equal((await verify({ labels: [...tracked, ...none], comments: ['Corpus impact: none - docs only wording'] })).code, 1);
    assert.equal((await verify({ labels: [] })).code, 1);
});

test('the runner reads labels live and ignores a stale event payload', async () => {
    const dir = await import('node:fs/promises').then(fs => fs.mkdtemp(`${process.env.TMPDIR ?? '/tmp'}/corpus-impact-`));
    const eventPath = `${dir}/event.json`;
    await import('node:fs/promises').then(fs => fs.writeFile(eventPath, JSON.stringify({ pull_request: { labels: [{ name: 'ai-corpus: none' }] } })));
    const logs = [];
    const code = await run({
        env: { GITHUB_REPOSITORY: repository, PR_NUMBER: '495', GITHUB_TOKEN: 'token', GITHUB_EVENT_PATH: eventPath, ...configEnv },
        fetchApi: async url => url.endsWith('/pulls/495')
            ? response({ draft: false, user: { login: 'woksin' }, changed_files: 1, labels: [] })
            : response([languageFile]),
        log: line => logs.push(line)
    });
    await import('node:fs/promises').then(fs => fs.rm(dir, { recursive: true }));
    assert.equal(code, 1);
    assert.match(logs[0], /exactly one label/);
});

describe('workflow pin and interface', () => {
    const root = new URL('../../../', import.meta.url).pathname;
    const scriptPath = '.github/scripts/verify-ai-corpus-impact.mjs';
    const workflow = readFileSync(`${root}.github/workflows/verify-ai-corpus-impact.yml`, 'utf8');

    test('the checksum and the pinned commit identify the script in the working tree', () => {
        const urls = [...workflow.matchAll(/https:\/\/raw\.githubusercontent\.com\/Cratis\/Workflows\/([0-9a-f]{40})\/\.github\/scripts\/verify-ai-corpus-impact\.mjs/g)];
        const sums = [...workflow.matchAll(/script_sha256='([0-9a-f]{64})'/g)];
        assert.equal(urls.length, 1);
        assert.equal(sums.length, 1);
        const digest = createHash('sha256').update(readFileSync(`${root}${scriptPath}`)).digest('hex');
        assert.equal(sums[0][1], digest);
        const blob = spawnSync('git', ['cat-file', 'blob', `${urls[0][1]}:${scriptPath}`], { cwd: root, maxBuffer: 10_000_000 });
        if (blob.status === 0) {
            assert.equal(createHash('sha256').update(blob.stdout).digest('hex'), sums[0][1]);
        }
    });

    test('inputs, permissions and steps match the contract', () => {
        const inputs = workflow.slice(workflow.indexOf('inputs:'), workflow.indexOf('\npermissions:'));
        assert.deepEqual([...inputs.matchAll(/^      ([\w-]+):$/gm)].map(match => match[1]).sort(), ['corpus-repository', 'exclude-paths', 'paths', 'runs-on']);
        assert.match(inputs, /corpus-repository:[\s\S]*?default: Cratis\/AI/);
        assert.match(inputs, /runs-on:[\s\S]*?default: ubuntu-latest/);
        assert.match(workflow, /^permissions:\n  contents: read\n  pull-requests: read\n/m);
        assert.doesNotMatch(workflow, /actions\/checkout/);
        const runBodies = workflow.split(/^ *run: \|\n/m).slice(1);
        assert.ok(runBodies.length > 0);
        for (const body of runBodies) assert.doesNotMatch(body, /\$\{\{\s*inputs\./);
    });
});
