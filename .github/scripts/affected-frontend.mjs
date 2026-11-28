#!/usr/bin/env node
// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Selects, and runs, only the JavaScript/TypeScript workspace packages a change can reach.
//
//   select  Reads the workspace packages of a Yarn/npm/pnpm monorepo (or a single package), maps the files a pull request
//           changed to the packages that own them, adds every package that depends on one of those (the reverse closure of
//           the dependency graph, from declared dependencies and from actual imports), and publishes the legs to run.
//   run     Runs the scripts of one leg in dependency order, over the packages the selection reaches.
//
// Safety first: anything the selector cannot place, and every shared build input, runs everything. A skipped package is
// never the result of a failure to read the change.
//
//   node affected-frontend.mjs select --root . --from origin/main --leg "lint:ci@changed,build,test"
//   node affected-frontend.mjs run    --root . --from origin/main --leg "lint:ci@changed,build,test"

import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { posix, join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const SKIPPED_FOLDERS = new Set(['node_modules', 'bin', 'obj', 'dist', 'wwwroot', '.git', '.ai-work', '.yarn', '.cratis-workflows', 'coverage', 'storybook-static']);
const DEPENDENCY_FIELDS = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'];
const SOURCE_EXTENSIONS = ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.vue', '.svelte'];

// A change to a file with one of these extensions can change what a package builds or tests. Documentation, C# and the like
// cannot, so a change to them reaches no package unless a caller names them as a global input.
export const DEFAULT_EXTENSIONS = [
    ...SOURCE_EXTENSIONS, '.json', '.jsonc', '.json5', '.css', '.scss', '.sass', '.less', '.html', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.ico', '.woff', '.woff2', '.ttf', '.eot', '.yml', '.yaml', '.graphql', '.gql', '.wasm', '.txt', '.map', '.snap', '.mdx'
];

// Root-level inputs that every package shares. Matched against paths relative to the project root.
const ROOT_GLOBALS = ['package.json', 'yarn.lock', '.yarnrc.yml', '.yarn/**', 'package-lock.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '.nvmrc', '.node-version'];

/** Compiles a glob (`*`, `**`, `?`) to a RegExp over posix paths. `**` crosses folders; `*` and `?` do not. */
export function globToRegExp(glob) {
    let source = '';
    for (let index = 0; index < glob.length; index++) {
        const character = glob[index];
        if (character === '*') {
            if (glob[index + 1] === '*') {
                index++;
                if (glob[index + 1] === '/') {
                    index++;
                    source += '(?:.*/)?';
                } else {
                    source += '.*';
                }
            } else {
                source += '[^/]*';
            }
        } else if (character === '?') {
            source += '[^/]';
        } else {
            source += character.replace(/[.+^${}()|[\]\\]/g, '\\$&');
        }
    }
    return new RegExp(`^${source}$`);
}

export const matchesAny = (path, globs) => globs.some(glob => globToRegExp(glob).test(path));

function readJson(path) {
    return JSON.parse(readFileSync(path, 'utf8'));
}

function walk(root, relative, visit) {
    let entries;
    try {
        entries = readdirSync(join(root, relative), { withFileTypes: true });
    } catch {
        return;
    }
    for (const entry of entries) {
        const path = relative ? posix.join(relative, entry.name) : entry.name;
        if (entry.isDirectory()) {
            if (!SKIPPED_FOLDERS.has(entry.name)) walk(root, path, visit);
        } else {
            visit(path, entry.name);
        }
    }
}

/**
 * The workspace packages under `root`: `{ name, dir, scripts, pkg }` with `dir` relative to `root` (`.` for a single package).
 * A root without a `workspaces` field is one package that owns the whole folder.
 */
export function discoverWorkspaces(root) {
    const rootPackage = readJson(join(root, 'package.json'));
    const definitions = Array.isArray(rootPackage.workspaces) ? rootPackage.workspaces : rootPackage.workspaces?.packages;
    if (!definitions) {
        return [{ name: rootPackage.name ?? 'root', dir: '.', scripts: rootPackage.scripts ?? {}, pkg: rootPackage }];
    }
    const include = definitions.filter(entry => !entry.startsWith('!'));
    const exclude = definitions.filter(entry => entry.startsWith('!')).map(entry => entry.slice(1));
    const folders = [];
    walk(root, '', (path, name) => {
        if (name === 'package.json' && path !== 'package.json') folders.push(posix.dirname(path));
    });
    const workspaces = [];
    for (const dir of folders.sort()) {
        if (!matchesAny(dir, include) || matchesAny(dir, exclude) || matchesAny(`${dir}/x`, exclude)) continue;
        const pkg = readJson(join(root, dir, 'package.json'));
        if (!pkg.name) continue;
        workspaces.push({ name: pkg.name, dir, scripts: pkg.scripts ?? {}, pkg });
    }
    return workspaces;
}

/** `name -> Set(names it depends on)`, from declared dependencies and, when `scanImports`, from actual imports. */
export function buildGraph(workspaces, { root = '.', scanImports = false } = {}) {
    const names = new Set(workspaces.map(workspace => workspace.name));
    const graph = new Map(workspaces.map(workspace => [workspace.name, new Set()]));
    for (const workspace of workspaces) {
        for (const field of DEPENDENCY_FIELDS) {
            for (const dependency of Object.keys(workspace.pkg[field] ?? {})) {
                if (names.has(dependency) && dependency !== workspace.name) graph.get(workspace.name).add(dependency);
            }
        }
    }
    if (scanImports && workspaces.length > 1) {
        const escaped = [...names].map(name => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).sort((a, b) => b.length - a.length);
        const pattern = new RegExp(`['"\`](${escaped.join('|')})(?:/[^'"\`]*)?['"\`]`, 'g');
        for (const workspace of workspaces) {
            if (workspace.dir === '.') continue;
            walk(root, workspace.dir, (path, name) => {
                if (!SOURCE_EXTENSIONS.some(extension => name.endsWith(extension))) return;
                let text;
                try {
                    text = readFileSync(join(root, path), 'utf8');
                } catch {
                    return;
                }
                for (const match of text.matchAll(pattern)) {
                    if (match[1] !== workspace.name) graph.get(workspace.name).add(match[1]);
                }
            });
        }
    }
    return graph;
}

/** Everything that depends on one of `seeds`, directly or not, and the seeds themselves. */
export function reverseClosure(graph, seeds) {
    const dependents = new Map([...graph.keys()].map(name => [name, new Set()]));
    for (const [name, dependencies] of graph) for (const dependency of dependencies) dependents.get(dependency).add(name);
    const reached = new Map();
    const queue = [];
    for (const seed of seeds) {
        reached.set(seed, null);
        queue.push(seed);
    }
    while (queue.length) {
        const current = queue.shift();
        for (const dependent of [...dependents.get(current) ?? []].sort()) {
            if (!reached.has(dependent)) {
                reached.set(dependent, current);
                queue.push(dependent);
            }
        }
    }
    return reached;
}

/** Dependencies first. Cycles (which a workspace graph should not have) are broken by name so the order is always total. */
export function topologicalOrder(graph, among) {
    const members = new Set(among);
    const remaining = new Map([...members].map(name => [name, new Set([...graph.get(name) ?? []].filter(dependency => members.has(dependency)))]));
    const order = [];
    while (remaining.size) {
        let ready = [...remaining].filter(([, dependencies]) => dependencies.size === 0).map(([name]) => name).sort();
        if (ready.length === 0) ready = [[...remaining.keys()].sort()[0]];
        for (const name of ready) {
            remaining.delete(name);
            order.push(name);
            for (const dependencies of remaining.values()) dependencies.delete(name);
        }
    }
    return order;
}

/**
 * Places each changed file. `prefix` is the project root relative to the repository root (empty at the top). Returns which
 * packages changed, whether everything must run, and why; files that reach nothing are listed so the report is honest.
 */
export function classify(files, workspaces, { prefix = '', globals = [], extensions = DEFAULT_EXTENSIONS } = {}) {
    const owners = workspaces.filter(workspace => workspace.dir !== '.').map(workspace => ({ name: workspace.name, dir: posix.join(prefix, workspace.dir) })).sort((a, b) => b.dir.length - a.dir.length);
    const single = workspaces.length === 1 && workspaces[0].dir === '.' ? workspaces[0].name : undefined;
    const changed = new Map();
    const ignored = [];
    let everything;
    const mark = (name, file) => changed.has(name) ? changed.get(name).push(file) : changed.set(name, [file]);
    const rootGlobals = ROOT_GLOBALS.map(glob => (prefix ? `${prefix}/${glob}` : glob));
    const hasExtension = file => extensions.some(extension => file.endsWith(extension));

    for (const file of files) {
        if (matchesAny(file, globals)) {
            everything ??= `${file} is a shared input`;
            continue;
        }
        const inside = prefix === '' || file.startsWith(`${prefix}/`);
        if (!inside) {
            ignored.push(file);
            continue;
        }
        if (matchesAny(file, rootGlobals)) {
            everything ??= `${file} is a shared input`;
            continue;
        }
        const owner = owners.find(candidate => file.startsWith(`${candidate.dir}/`));
        if (owner) {
            if (hasExtension(file)) mark(owner.name, file); else ignored.push(file);
            continue;
        }
        if (single) {
            if (hasExtension(file)) mark(single, file); else ignored.push(file);
            continue;
        }
        // Directly in the project root, and not in a package: configuration every package may use.
        const relative = prefix ? file.slice(prefix.length + 1) : file;
        if (!relative.includes('/') && hasExtension(file)) {
            everything ??= `${file} is a shared input`;
        } else {
            ignored.push(file);
        }
    }
    return { changed, ignored, everything };
}

/** Reads `lint:ci@changed,build,test` into `[{ script, scope }]`. */
export function parseLeg(leg) {
    return leg.split(',').map(token => token.trim()).filter(Boolean).map(token => {
        const [script, scope = 'affected'] = token.split('@');
        if (!script || !['affected', 'changed'].includes(scope)) throw new Error(`invalid leg entry '${token}': use script, script@affected or script@changed`);
        return { script, scope };
    });
}

/** The targets of each script of a leg, in dependency order. */
export function planLeg(leg, selection) {
    return parseLeg(leg).map(({ script, scope }) => {
        const pool = scope === 'changed' ? selection.changedOrAll : selection.affected;
        const targets = selection.order.filter(name => pool.has(name) && selection.byName.get(name).scripts[script] !== undefined);
        return { script, scope, targets };
    });
}

/**
 * The complete selection. `files` is the list of changed files, or undefined when it could not be read, which selects everything.
 */
export function select({ workspaces, graph, files, prefix = '', globals = [], extensions, runAll = false, reason }) {
    const byName = new Map(workspaces.map(workspace => [workspace.name, workspace]));
    const all = new Set(byName.keys());
    let everything = runAll ? 'a full run was requested' : reason;
    let changed = new Set();
    let reasons = new Map();
    let ignored = [];
    if (!everything && !Array.isArray(files)) everything = 'the change could not be read (fail closed)';
    if (!everything) {
        const classified = classify(files, workspaces, { prefix, globals, extensions });
        everything = classified.everything;
        ignored = classified.ignored;
        changed = new Set(classified.changed.keys());
        reasons = classified.changed;
    }
    let affected;
    let dependents = new Map();
    if (everything) {
        affected = new Set(all);
        changed = new Set(all);
    } else {
        dependents = reverseClosure(graph, changed);
        affected = new Set(dependents.keys());
    }
    return {
        everything: everything ?? null,
        byName,
        changed,
        changedOrAll: changed,
        affected,
        order: topologicalOrder(graph, all),
        reasons,
        dependents,
        ignored,
        total: all.size
    };
}

function git(args, cwd, runner = spawnSync) {
    const result = runner('git', args, { cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, timeout: 60000 });
    if (result.error || result.status !== 0) return undefined;
    return result.stdout;
}

/** `{ prefix, files }`: the project root's folder inside the repository, and the files changed since `from`. */
export function readChange({ root, from, to = 'HEAD', runner = spawnSync }) {
    const prefix = (git(['rev-parse', '--show-prefix'], root, runner) ?? '').trim().replace(/\/$/, '');
    if (!from) return { prefix, files: undefined };
    const diff = git(['diff', '--name-only', '--no-renames', '-z', `${from}...${to}`], root, runner);
    if (diff === undefined || !diff.endsWith('\0')) return { prefix, files: undefined };
    const files = diff.slice(0, -1).split('\0');
    // An empty diff on a pull request is not "nothing to do": it is a diff that cannot be trusted.
    return { prefix, files: files.length === 0 || (files.length === 1 && files[0] === '') ? undefined : files };
}

function summary(selection, legs) {
    const names = set => [...set].sort().map(name => `\`${name}\``).join(', ') || '_none_';
    const lines = [
        '### Affected frontend packages',
        '',
        selection.everything ? `**Everything runs:** ${selection.everything}.` : `${selection.affected.size} of ${selection.total} packages are reached.`,
        ''
    ];
    if (!selection.everything) {
        lines.push(`- Changed: ${names(selection.changed)}`);
        const dependent = [...selection.affected].filter(name => !selection.changed.has(name));
        lines.push(`- Reached through a dependency: ${names(dependent)}`);
        if (selection.ignored.length) lines.push(`- Changed files that reach no package: ${selection.ignored.length}`);
    }
    lines.push('', '| Leg | Packages |', '|---|---|');
    for (const entry of legs) lines.push(`| \`${entry.leg}\` | ${entry.count} |`);
    return `${lines.join('\n')}\n`;
}

function writeOutputs(values) {
    const destination = process.env.GITHUB_OUTPUT;
    const lines = Object.entries(values).map(([key, value]) => `${key}=${value}`);
    if (destination) appendFileSync(destination, `${lines.join('\n')}\n`);
    console.log(lines.join('\n'));
}

function prepare(options) {
    const root = options.root;
    const workspaces = discoverWorkspaces(root);
    const graph = buildGraph(workspaces, { root, scanImports: options['scan-imports'] !== false });
    const prMode = (options.mode ?? 'pr') === 'pr';
    const { prefix, files } = prMode ? readChange({ root, from: options.from, to: options.to ?? 'HEAD' }) : { prefix: readChange({ root, from: undefined }).prefix, files: undefined };
    const extensions = options.extension?.length ? options.extension : undefined;
    const selection = select({
        workspaces, graph, files, prefix,
        globals: options.global ?? [],
        extensions,
        runAll: Boolean(options['run-all']),
        reason: prMode ? undefined : `${options.mode} run`
    });
    return { workspaces, graph, selection };
}

function selectCommand(options) {
    const legs = options.leg ?? [];
    let selection;
    let plans;
    try {
        ({ selection } = prepare(options));
        plans = legs.map(leg => ({ leg, steps: planLeg(leg, selection) }));
    } catch (error) {
        // A selection that cannot be computed must never skip the work.
        console.error(`::warning title=affected-frontend::selection failed (${error.message}); running every leg`);
        writeOutputs({ run: 'true', everything: 'true', legs: JSON.stringify(legs.map(leg => ({ leg }))), packages: 'unknown' });
        return 0;
    }
    const active = plans.filter(plan => plan.steps.some(step => step.targets.length > 0));
    const counts = plans.map(plan => ({ leg: plan.leg, count: new Set(plan.steps.flatMap(step => step.targets)).size }));
    const report = {
        everything: selection.everything,
        total: selection.total,
        changed: [...selection.changed].sort(),
        affected: [...selection.affected].sort(),
        legs: plans.map(plan => ({ leg: plan.leg, steps: plan.steps.map(step => ({ script: step.script, scope: step.scope, targets: step.targets })) })),
        ignored: selection.ignored
    };
    if (options['report-directory']) {
        mkdirSync(options['report-directory'], { recursive: true });
        writeFileSync(join(options['report-directory'], 'selection.json'), `${JSON.stringify(report, null, 2)}\n`);
    }
    const text = summary(selection, counts);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, text);
    console.log(text);
    writeOutputs({
        run: String(active.length > 0),
        everything: String(Boolean(selection.everything)),
        legs: JSON.stringify(active.map(plan => ({ leg: plan.leg }))),
        packages: String(selection.affected.size)
    });
    return 0;
}

export function runCommand(options, runner = spawnSync) {
    const { selection } = prepare(options);
    const manager = options['package-manager'] ?? 'yarn';
    const failures = [];
    for (const leg of options.leg ?? []) {
        for (const { script, targets } of planLeg(leg, selection)) {
            for (const name of targets) {
                const workspace = selection.byName.get(name);
                const directory = workspace.dir === '.' ? options.root : join(options.root, workspace.dir);
                console.log(`::group::${script} ${name}`);
                const result = runner(manager, ['run', script], { cwd: directory, stdio: 'inherit', env: process.env });
                console.log('::endgroup::');
                if (result.status !== 0) {
                    failures.push(`${name}: ${script}`);
                    console.log(`::error title=affected-frontend::${script} failed in ${name}`);
                }
            }
        }
    }
    if (failures.length) {
        console.log(`Failed:\n${failures.map(entry => `  ${entry}`).join('\n')}`);
        return 1;
    }
    return 0;
}

const OPTIONS = {
    root: { type: 'string', default: '.' },
    from: { type: 'string' },
    to: { type: 'string' },
    mode: { type: 'string', default: 'pr' },
    global: { type: 'string', multiple: true },
    extension: { type: 'string', multiple: true },
    leg: { type: 'string', multiple: true },
    'run-all': { type: 'boolean' },
    'scan-imports': { type: 'boolean' },
    'no-scan-imports': { type: 'boolean' },
    'package-manager': { type: 'string' },
    'report-directory': { type: 'string' }
};

export function main(argv) {
    const { values, positionals } = parseArgs({ args: argv, options: OPTIONS, allowPositionals: true });
    if (values['no-scan-imports']) values['scan-imports'] = false;
    const [command] = positionals;
    if (!['select', 'run'].includes(command)) {
        console.error('usage: affected-frontend.mjs <select|run> --root <dir> --from <rev> --leg "script@changed,script" [--global <glob>]');
        return 2;
    }
    if (!existsSync(join(values.root, 'package.json'))) {
        console.error(`::error title=affected-frontend::no package.json in ${values.root}`);
        return 2;
    }
    return command === 'select' ? selectCommand(values) : runCommand(values);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    process.exit(main(process.argv.slice(2)));
}
