// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Offline specs of affected-frontend.mjs against a real temporary monorepo with real git history.

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';

import {
    buildGraph, classify, discoverWorkspaces, globToRegExp, main, parseLeg, planLeg, readChange, reverseClosure, runCommand, select, topologicalOrder
} from '../affected-frontend.mjs';

const sh = (cwd, ...args) => {
    const result = spawnSync('git', args, { cwd, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout;
};

const write = (root, path, content) => {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), typeof content === 'string' ? content : `${JSON.stringify(content, null, 2)}\n`);
};

const pkg = (name, dependencies = {}, scripts = { build: 'x', test: 'x', 'lint:ci': 'x' }) => ({ name, version: '1.0.0', scripts, dependencies });

/** core -> product -> components; crm -> components; tools has no dependents. */
function monorepo({ prefix = '' } = {}) {
    const repo = mkdtempSync(join(tmpdir(), 'affected-frontend-'));
    sh(repo, 'init', '-q', '-b', 'main');
    sh(repo, 'config', 'user.email', 'test@example.com');
    sh(repo, 'config', 'user.name', 'Test');
    const root = prefix ? join(repo, prefix) : repo;
    const at = path => (prefix ? `${prefix}/${path}` : path);
    write(repo, at('package.json'), { private: true, workspaces: ['Source/*', '!Source/Ignored'] });
    write(repo, at('yarn.lock'), '# lock\n');
    write(repo, at('Source/Components/package.json'), pkg('@x/components'));
    write(repo, at('Source/Components/index.ts'), 'export const a = 1;\n');
    write(repo, at('Source/Product/package.json'), pkg('@x/product', { '@x/components': 'workspace:*' }, { 'lint:ci': 'x' }));
    write(repo, at('Source/Product/index.ts'), 'export const b = 1;\n');
    write(repo, at('Source/Crm/package.json'), pkg('@x/crm', {}));
    // An import the package.json does not declare.
    write(repo, at('Source/Crm/index.ts'), "import { a } from '@x/components/deep';\n");
    write(repo, at('Source/Core/package.json'), pkg('core', { '@x/product': 'workspace:*', '@x/crm': 'workspace:*' }));
    write(repo, at('Source/Core/index.ts'), 'export const c = 1;\n');
    write(repo, at('Source/Tools/package.json'), pkg('tools', {}));
    write(repo, at('Source/Tools/index.ts'), 'export const t = 1;\n');
    write(repo, at('Source/Ignored/package.json'), pkg('ignored', {}));
    write(repo, at('Source/Product/Backend.cs'), '// cs\n');
    sh(repo, 'add', '-A');
    sh(repo, 'commit', '-q', '-m', 'base');
    return { repo, root, at };
}

const commit = (repo, files) => {
    for (const [path, content] of Object.entries(files)) write(repo, path, content);
    sh(repo, 'add', '-A');
    sh(repo, 'commit', '-q', '-m', 'change');
};

const selectionFor = (root, files, options = {}) => {
    const workspaces = discoverWorkspaces(root);
    const graph = buildGraph(workspaces, { root, scanImports: true });
    return select({ workspaces, graph, files, ...options });
};
const names = set => [...set].sort();

test('globs: * stays in a folder, ** crosses them', () => {
    assert.ok(globToRegExp('Source/*/x.ts').test('Source/a/x.ts'));
    assert.ok(!globToRegExp('Source/*/x.ts').test('Source/a/b/x.ts'));
    assert.ok(globToRegExp('Source/**').test('Source/a/b/x.ts'));
    assert.ok(globToRegExp('**/tsconfig*.json').test('a/b/tsconfig.base.json'));
    assert.ok(globToRegExp('**/tsconfig*.json').test('tsconfig.json'));
    assert.ok(!globToRegExp('a.b').test('axb'));
});

test('workspaces honour includes and negations, and skip folders without a name', () => {
    const { root } = monorepo();
    assert.deepEqual(discoverWorkspaces(root).map(workspace => workspace.name).sort(), ['@x/components', '@x/crm', '@x/product', 'core', 'tools']);
});

test('a root without workspaces is one package that owns the folder', () => {
    const root = mkdtempSync(join(tmpdir(), 'affected-single-'));
    write(root, 'package.json', pkg('single'));
    const workspaces = discoverWorkspaces(root);
    assert.equal(workspaces.length, 1);
    assert.equal(workspaces[0].dir, '.');
    const result = classify(['Source/Foo/a.ts', 'Source/Foo/readme.md'], workspaces, { prefix: 'Source/Foo' });
    assert.deepEqual(names(result.changed.keys()), ['single']);
    assert.deepEqual(result.ignored, ['Source/Foo/readme.md']);
});

test('declared dependencies and undeclared imports both make a dependent', () => {
    const { root } = monorepo();
    const workspaces = discoverWorkspaces(root);
    const declaredOnly = buildGraph(workspaces, { root, scanImports: false });
    const scanned = buildGraph(workspaces, { root, scanImports: true });
    assert.ok(!declaredOnly.get('@x/crm').has('@x/components'));
    assert.ok(scanned.get('@x/crm').has('@x/components'));
    assert.ok(scanned.get('core').has('@x/product'));
});

test('a change reaches its dependents transitively and nothing else', () => {
    const { root } = monorepo();
    const selection = selectionFor(root, ['Source/Components/index.ts']);
    assert.equal(selection.everything, null);
    assert.deepEqual(names(selection.changed), ['@x/components']);
    assert.deepEqual(names(selection.affected), ['@x/components', '@x/crm', '@x/product', 'core']);
    assert.ok(!selection.affected.has('tools'));
});

test('a leaf change reaches only itself', () => {
    const { root } = monorepo();
    assert.deepEqual(names(selectionFor(root, ['Source/Tools/index.ts']).affected), ['tools']);
});

test('files that cannot change a build reach nothing', () => {
    const { root } = monorepo();
    const selection = selectionFor(root, ['Source/Product/Backend.cs', 'Documentation/a.md', 'Source/Product/README.md']);
    assert.equal(selection.everything, null);
    assert.equal(selection.affected.size, 0);
    assert.equal(selection.ignored.length, 3);
});

test('shared inputs run everything', () => {
    const { root } = monorepo();
    for (const file of ['package.json', 'yarn.lock', 'tsconfig.json', 'eslint.config.mjs', '.yarn/releases/yarn.cjs']) {
        const selection = selectionFor(root, [file]);
        assert.ok(selection.everything, file);
        assert.equal(selection.affected.size, 5, file);
    }
});

test('a caller-named global path runs everything', () => {
    const { root } = monorepo();
    const selection = selectionFor(root, ['.frontend/vite.config.ts'], { globals: ['.frontend/**'] });
    assert.match(selection.everything, /shared input/);
});

test('an unreadable change, an empty one and an explicit request run everything', () => {
    const { root } = monorepo();
    assert.match(selectionFor(root, undefined).everything, /fail closed/);
    assert.equal(selectionFor(root, ['Source/Tools/index.ts'], { runAll: true }).affected.size, 5);
});

test('topological order puts dependencies first', () => {
    const { root } = monorepo();
    const workspaces = discoverWorkspaces(root);
    const graph = buildGraph(workspaces, { root, scanImports: true });
    const order = topologicalOrder(graph, graph.keys());
    assert.ok(order.indexOf('@x/components') < order.indexOf('@x/product'));
    assert.ok(order.indexOf('@x/product') < order.indexOf('core'));
    assert.equal(order.length, 5);
});

test('a dependency cycle still yields a total order', () => {
    const graph = new Map([['a', new Set(['b'])], ['b', new Set(['a'])], ['c', new Set()]]);
    assert.deepEqual(topologicalOrder(graph, ['a', 'b', 'c']).sort(), ['a', 'b', 'c']);
    assert.equal(reverseClosure(graph, ['a']).size, 2);
});

test('legs: @changed ignores dependents, scripts a package lacks are skipped', () => {
    assert.deepEqual(parseLeg('lint:ci@changed, build'), [{ script: 'lint:ci', scope: 'changed' }, { script: 'build', scope: 'affected' }]);
    assert.throws(() => parseLeg('build@nope'));
    const { root } = monorepo();
    const selection = selectionFor(root, ['Source/Components/index.ts']);
    const [lint, build] = planLeg('lint:ci@changed,build', selection);
    assert.deepEqual(lint.targets, ['@x/components']);
    // @x/product defines no build script.
    assert.deepEqual(build.targets, ['@x/components', '@x/crm', 'core']);
});

test('a leg with nothing to do in a change selects no work', () => {
    const { root } = monorepo();
    const selection = selectionFor(root, ['Source/Product/Backend.cs']);
    assert.deepEqual(planLeg('build,test', selection).flatMap(step => step.targets), []);
});

test('reads a real git diff from the merge base, in the repository root and in a subfolder project', () => {
    for (const prefix of ['', 'Source/Frontend']) {
        const { repo, root, at } = monorepo({ prefix });
        sh(repo, 'checkout', '-q', '-b', 'feature');
        commit(repo, { [at('Source/Components/index.ts')]: 'export const a = 2;\n' });
        sh(repo, 'checkout', '-q', 'main');
        commit(repo, { [at('Source/Tools/index.ts')]: 'export const t = 2;\n' });
        sh(repo, 'checkout', '-q', 'feature');
        const change = readChange({ root, from: 'main' });
        assert.equal(change.prefix, prefix);
        // Three-dot: the change since the merge base, not main's own commits.
        assert.deepEqual(change.files, [at('Source/Components/index.ts')]);
        const selection = selectionFor(root, change.files, { prefix: change.prefix });
        assert.deepEqual(names(selection.changed), ['@x/components']);
        assert.ok(!selection.affected.has('tools'));
    }
});

test('a change outside a subfolder project reaches nothing in it, unless it is a global', () => {
    const { root } = monorepo({ prefix: 'Source/Frontend' });
    const workspaces = discoverWorkspaces(root);
    assert.equal(classify(['Source/Kernel/a.cs'], workspaces, { prefix: 'Source/Frontend' }).changed.size, 0);
    assert.ok(classify(['Source/Kernel/a.proto'], workspaces, { prefix: 'Source/Frontend', globals: ['Source/Kernel/**'] }).everything);
});

test('an unknown revision fails closed', () => {
    const { root } = monorepo();
    assert.equal(readChange({ root, from: 'no-such-ref' }).files, undefined);
});

test('select publishes only the legs that have work, as outputs', () => {
    const { repo, root } = monorepo();
    sh(repo, 'checkout', '-q', '-b', 'feature');
    commit(repo, { 'Source/Product/Backend.cs': '// changed\n' });
    const output = join(repo, 'out.txt');
    writeFileSync(output, '');
    process.env.GITHUB_OUTPUT = output;
    const originalLog = console.log;
    console.log = () => {};
    try {
        assert.equal(main(['select', '--root', root, '--from', 'main', '--leg', 'build,test']), 0);
    } finally {
        console.log = originalLog;
        delete process.env.GITHUB_OUTPUT;
    }
    const text = readFileSync(output, 'utf8');
    assert.match(text, /^run=false$/m);
    assert.match(text, /^legs=\[\]$/m);

    commit(repo, { 'Source/Core/index.ts': 'export const c = 2;\n' });
    writeFileSync(output, '');
    process.env.GITHUB_OUTPUT = output;
    console.log = () => {};
    try {
        main(['select', '--root', root, '--from', 'main', '--leg', 'build,test', '--leg', 'missing-script']);
    } finally {
        console.log = originalLog;
        delete process.env.GITHUB_OUTPUT;
    }
    const after = readFileSync(output, 'utf8');
    assert.match(after, /^run=true$/m);
    assert.match(after, /^legs=\[\{"leg":"build,test"\}\]$/m);
});

test('non-pr modes run everything', () => {
    const { root } = monorepo();
    const output = join(root, 'out.txt');
    writeFileSync(output, '');
    process.env.GITHUB_OUTPUT = output;
    const originalLog = console.log;
    console.log = () => {};
    try {
        main(['select', '--root', root, '--mode', 'nightly', '--leg', 'build']);
    } finally {
        console.log = originalLog;
        delete process.env.GITHUB_OUTPUT;
    }
    const text = readFileSync(output, 'utf8');
    assert.match(text, /^everything=true$/m);
    assert.match(text, /^packages=5$/m);
});

test('run executes scripts in dependency order, keeps going after a failure and reports it', () => {
    const { repo, root } = monorepo();
    sh(repo, 'checkout', '-q', '-b', 'feature');
    commit(repo, { 'Source/Components/index.ts': 'export const a = 3;\n' });
    const calls = [];
    const runner = (command, args, options) => {
        calls.push(`${command} ${args.join(' ')} @ ${options.cwd.replace(root, '.')}`);
        return { status: options.cwd.endsWith('Crm') ? 1 : 0 };
    };
    const originalLog = console.log;
    console.log = () => {};
    let status;
    try {
        status = runCommand({ root, from: 'main', leg: ['build'], mode: 'pr' }, runner);
    } finally {
        console.log = originalLog;
    }
    assert.equal(status, 1);
    assert.deepEqual(calls, ['yarn run build @ ./Source/Components', 'yarn run build @ ./Source/Crm', 'yarn run build @ ./Source/Core']);
});

test('only asks whether one application is reached, and rejects a name that is not a workspace', () => {
    const { root } = monorepo();
    const workspaces = discoverWorkspaces(root);
    const graph = buildGraph(workspaces, { root, scanImports: true });
    const reached = select({ workspaces, graph, files: ['Source/Components/index.ts'], only: ['core'] });
    assert.deepEqual(names(reached.affected), ['core']);
    assert.deepEqual(planLeg('build', reached)[0].targets, ['core']);
    // A leaf the application does not depend on does not reach it.
    const unrelated = select({ workspaces, graph, files: ['Source/Tools/index.ts'], only: ['core'] });
    assert.equal(unrelated.affected.size, 0);
    assert.deepEqual(planLeg('build', unrelated)[0].targets, []);
    // Shared inputs still reach it.
    assert.deepEqual(names(select({ workspaces, graph, files: ['yarn.lock'], only: ['core'] }).affected), ['core']);
    assert.throws(() => select({ workspaces, graph, files: ['yarn.lock'], only: ['nope'] }), /not a workspace package/);
});
