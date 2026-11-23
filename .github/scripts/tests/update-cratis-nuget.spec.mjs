// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { applyEdits, compareVersions, familyKey, parseExclusions, parseVersion, planUpdates, renderDiff } from '../update-cratis-nuget.mjs';

const SCRIPT = fileURLToPath(new URL('../update-cratis-nuget.mjs', import.meta.url));

const props = (...items) => `<Project>\n  <ItemGroup>\n${items.map((i) => `    ${i}\n`).join('')}  </ItemGroup>\n</Project>\n`;
const pin = (id, version) => `<PackageVersion Include="${id}" Version="${version}" />`;
const index = (table) => async (id) => {
  const versions = table[id.toLowerCase()];
  if (!versions) throw new Error(`no index for ${id}`);
  return versions;
};

async function run(files, table, exclusions = []) {
  const map = new Map(Object.entries(files));
  const plan = await planUpdates({ files: map, exclusions, getVersions: index(table), checkImports: false });
  const out = {};
  for (const [file, text] of map) out[file] = plan.edits.has(file) ? applyEdits(text, plan.edits.get(file)) : text;
  return { plan, out, family: (key) => plan.families.find((f) => f.key === key) };
}

describe('versions', () => {
  it('compares numerically with normalized components and prereleases below stable', () => {
    assert.equal(compareVersions(parseVersion('1.10.0'), parseVersion('1.9.0')), 1);
    assert.equal(compareVersions(parseVersion('1.2'), parseVersion('1.2.0.0')), 0);
    assert.equal(compareVersions(parseVersion('1.2.0-beta.2'), parseVersion('1.2.0-beta.10')), -1);
    assert.equal(compareVersions(parseVersion('1.2.0-beta'), parseVersion('1.2.0')), -1);
    assert.equal(parseVersion('[1.0,2.0)'), null);
  });

  it('groups families on the Cratis prefix and not on lookalikes', () => {
    assert.equal(familyKey('Cratis.Arc.Core'), 'cratis.arc');
    assert.equal(familyKey('Cratis'), 'cratis.arc');
    assert.equal(familyKey('cratis.CHRONICLE'), 'cratis.chronicle');
    assert.equal(familyKey('Cratis.Chronicle.Api'), 'cratis.chronicle');
    assert.equal(familyKey('Cratisfoo'), null);
    assert.equal(familyKey('Serilog'), null);
    assert.notEqual(familyKey('Cratis.Arcade'), familyKey('Cratis.Arc'));
  });

  it('rejects invalid exclusions', () => {
    assert.deepEqual(parseExclusions(' A , ,b.c '), ['A', 'b.c']);
    assert.throws(() => parseExclusions('--help'), /Invalid NuGet exclusion ID/);
  });
});

describe('family planning', () => {
  const table = {
    'cratis.chronicle': ['1.0.0', '1.0.7', '1.0.9', '1.0.10', '1.1.0-beta.1'],
    'cratis.chronicle.api': ['1.0.9', '1.0.7', '1.0.10'],
  };

  it('takes the highest common stable version rather than the minimum of independent latests', async () => {
    const { family, out } = await run({ 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.0'), pin('Cratis.Chronicle.Api', '1.0.0')) },
      { ...table, 'cratis.chronicle': ['1.0.0', '1.0.9', '1.0.10', '1.0.11'] });
    assert.equal(family('cratis.chronicle').target, '1.0.10');
    assert.match(out['Directory.Packages.props'], /Cratis\.Chronicle" Version="1\.0\.10"/);
  });

  it('defers instead of lowering a sibling when the common version is below a current pin', async () => {
    const files = { 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.7'), pin('Cratis.Chronicle.Api', '1.0.9')) };
    const { family, out } = await run(files, { 'cratis.chronicle': ['1.0.7'], 'cratis.chronicle.api': ['1.0.9', '1.0.7'] });
    assert.equal(family('cratis.chronicle').outcome, 'deferred');
    assert.equal(out['Directory.Packages.props'], files['Directory.Packages.props']);
  });

  it('repairs an existing split upward to the highest current pin', async () => {
    const { family, out } = await run({ 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.7'), pin('Cratis.Chronicle.Api', '1.0.9')) },
      { 'cratis.chronicle': ['1.0.7', '1.0.9'], 'cratis.chronicle.api': ['1.0.9', '1.0.7'] });
    assert.equal(family('cratis.chronicle').outcome, 'updated');
    assert.match(out['Directory.Packages.props'], /Cratis\.Chronicle" Version="1\.0\.9"/);
  });

  it('leaves an aligned family with no newer common release unchanged', async () => {
    const { family, plan } = await run({ 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.9'), pin('Cratis.Chronicle.Api', '1.0.9')) },
      { 'cratis.chronicle': ['1.0.9'], 'cratis.chronicle.api': ['1.0.9'] });
    assert.equal(family('cratis.chronicle').outcome, 'unchanged');
    assert.equal(plan.edits.size, 0);
  });

  it('never selects a prerelease and lets a prerelease pin move to a stable one', async () => {
    const { family } = await run({ 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.9-rc.1')) },
      { 'cratis.chronicle': ['1.0.9-rc.1', '1.0.9', '1.1.0-beta.1'] });
    assert.equal(family('cratis.chronicle').target, '1.0.9');
  });

  it('holds the whole family when any member is excluded, even if already split', async () => {
    const files = { 'Directory.Packages.props': props(pin('Cratis.Chronicle', '1.0.7'), pin('Cratis.Chronicle.Api', '1.0.9'), pin('Serilog', '1.0.0')) };
    const { family, out, plan } = await run(files, {}, ['cratis.chronicle.API']);
    assert.equal(family('cratis.chronicle').outcome, 'held');
    assert.equal(out['Directory.Packages.props'], files['Directory.Packages.props']);
    assert.deepEqual(plan.protectedIds.sort(), ['Cratis.Chronicle', 'Cratis.Chronicle.Api']);
  });

  it('resolves families independently and ignores non-Cratis packages', async () => {
    const { plan, out } = await run({ 'a.props': props(pin('Cratis.Arc', '1.0.0'), pin('Cratis.Chronicle', '1.0.0'), pin('Serilog', '1.0.0')) },
      { 'cratis.arc': ['1.0.0', '2.0.0'], 'cratis.chronicle': ['1.0.0'] });
    assert.equal(plan.families.length, 2);
    assert.match(out['a.props'], /Cratis\.Arc" Version="2\.0\.0"/);
    assert.match(out['a.props'], /Serilog" Version="1\.0\.0"/);
    assert.match(out['a.props'], /Cratis\.Chronicle" Version="1\.0\.0"/);
  });

  it('includes unreferenced central pins and non-CPM references and preserves formatting', async () => {
    const central = '<Project>\r\n  <ItemGroup>\r\n    <PackageVersion Include="Cratis.Arc.Core" Version=\'1.0.0\'><!-- keep --></PackageVersion>\r\n  </ItemGroup>\r\n</Project>\r\n';
    const project = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Cratis.Arc"><Version>1.0.0</Version></PackageReference></ItemGroup></Project>';
    const { out } = await run({ 'Directory.Packages.props': central, 'src/App.csproj': project },
      { 'cratis.arc': ['1.0.0', '1.2.0'], 'cratis.arc.core': ['1.0.0', '1.2.0'] });
    assert.equal(out['Directory.Packages.props'], central.replace(`'1.0.0'`, `'1.2.0'`));
    assert.equal(out['src/App.csproj'], project.replace('>1.0.0<', '>1.2.0<'));
  });

  it('defers conditional, ranged and floating pins and leaves the file alone', async () => {
    for (const item of ['<PackageVersion Include="Cratis.Arc" Version="[1.0,2.0)" />', '<PackageVersion Include="Cratis.Arc" Version="1.*" />',
      '<PackageVersion Include="Cratis.Arc" Version="1.0.0" Condition="\'$(X)\' == \'y\'" />']) {
      const files = { 'a.props': props(item) };
      const { family, out } = await run(files, { 'cratis.arc': ['1.0.0', '2.0.0'] });
      assert.equal(family('cratis.arc').outcome, 'deferred', item);
      assert.equal(out['a.props'], files['a.props']);
    }
  });
});

describe('MSBuild properties', () => {
  const manifest = (extra = '', value = '1.0.0') => `<Project>\n  <PropertyGroup>\n    <ArcVersion>${value}</ArcVersion>\n  </PropertyGroup>\n  <ItemGroup>\n` +
    `    ${pin('Cratis.Arc', '$(ArcVersion)')}\n    ${pin('Cratis.Arc.Core', '$(ArcVersion)')}\n    ${pin('Cratis.Chronicle', '1.0.0')}\n${extra}  </ItemGroup>\n</Project>\n`;
  const table = { 'cratis.arc': ['1.0.0', '1.1.0'], 'cratis.arc.core': ['1.0.0', '1.1.0'], 'cratis.chronicle': ['1.0.0', '1.5.0'] };

  it('keeps the property reference and updates its single definition once', async () => {
    const { out, family } = await run({ 'Directory.Packages.props': manifest() }, table);
    assert.equal(family('cratis.arc').outcome, 'updated');
    assert.equal(out['Directory.Packages.props'], manifest('', '1.1.0').replace('Version="1.0.0" />\n  </ItemGroup>', 'Version="1.5.0" />\n  </ItemGroup>'));
  });

  it('defers when a consumer outside the family or a non-version use exists', async () => {
    for (const extra of [pin('Serilog', '$(ArcVersion)'), '<Foo Include="$(ArcVersion)" />']) {
      const { family } = await run({ 'Directory.Packages.props': manifest(`    ${extra}\n`) }, table);
      assert.equal(family('cratis.arc').outcome, 'deferred', extra);
    }
  });

  it('defers ambiguous, conditional and complex definitions', async () => {
    const second = '<Project><PropertyGroup><ArcVersion>1.0.0</ArcVersion></PropertyGroup></Project>';
    assert.equal((await run({ 'a.props': manifest(), 'b.props': second }, table)).family('cratis.arc').outcome, 'deferred');
    const conditional = manifest().replace('<ArcVersion>', '<ArcVersion Condition="\'$(Z)\'==\'1\'">');
    assert.equal((await run({ 'a.props': conditional }, table)).family('cratis.arc').outcome, 'deferred');
    assert.equal((await run({ 'a.props': manifest('', '$(Other)') }, table)).family('cratis.arc').outcome, 'deferred');
    assert.equal((await run({ 'a.props': manifest().replace('<ArcVersion>1.0.0</ArcVersion>', '') }, table)).family('cratis.arc').outcome, 'deferred');
  });

  it('defers property-backed families when an import cannot be resolved', async () => {
    const withImport = manifest().replace('<PropertyGroup>', '<Import Project="$([MSBuild]::GetPathOfFileAbove(x))" /><PropertyGroup>');
    const plan = await planUpdates({ files: new Map([['a.props', withImport]]), getVersions: index(table) });
    assert.equal(plan.families.find((f) => f.key === 'cratis.arc').outcome, 'deferred');
  });
});

describe('conditional manifests', () => {
  const root = (extra) => `<Project>\n  <Import Project="net8.props" Condition="'$(TargetFramework)' == 'net8.0'" />\n${extra}</Project>\n`;
  const table = { 'cratis.arc': ['1.0.0', '2.0.0', '3.0.0'] };
  const deferred = async (files) => (await run(files, table)).family('cratis.arc');

  it('defers a family pinned in a conditionally imported file', async () => {
    const files = { 'Directory.Packages.props': root(''), 'net8.props': props(pin('Cratis.Arc', '1.0.0')) };
    const { family, out } = await run(files, table);
    assert.equal(family('cratis.arc').outcome, 'deferred');
    assert.equal(out['net8.props'], files['net8.props']);
  });

  it('defers pins reached through a conditional import transitively', async () => {
    const nested = '<Project><Import Project="deep.props" /></Project>';
    assert.equal((await deferred({ 'Directory.Packages.props': root(''), 'net8.props': nested, 'deep.props': props(pin('Cratis.Arc', '1.0.0')) })).outcome, 'deferred');
  });

  it('defers a pin in a conditional item group and an Update override', async () => {
    const group = '<Project><ItemGroup Condition="\'$(X)\' == \'1\'"><PackageVersion Include="Cratis.Arc" Version="1.0.0" /></ItemGroup></Project>';
    assert.equal((await deferred({ 'a.props': group })).outcome, 'deferred');
    const update = props(pin('Cratis.Arc', '1.0.0'), '<PackageVersion Update="Cratis.Arc" Version="2.0.0" />');
    assert.equal((await deferred({ 'a.props': update })).outcome, 'deferred');
  });

  it('still updates a family pinned only in an unconditionally imported file', async () => {
    const files = { 'Directory.Packages.props': '<Project><Import Project="all.props" /></Project>', 'all.props': props(pin('Cratis.Arc', '1.0.0')) };
    const plan = await planUpdates({ files: new Map(Object.entries(files)), getVersions: index(table) });
    assert.equal(plan.families[0].outcome, 'updated');
  });
});

describe('failures', () => {
  it('fails on an unavailable inventory instead of treating it as no release', async () => {
    const files = new Map([['a.props', props(pin('Cratis.Arc', '1.0.0'))]]);
    await assert.rejects(planUpdates({ files, getVersions: index({}) }), /no index/);
    await assert.rejects(planUpdates({ files: new Map([['a.props', '<Project><Item']]), getVersions: index({}) }), /Malformed XML/);
  });

  it('rejects an edit whose before-image is stale', () => {
    assert.throws(() => applyEdits('abc', [{ start: 0, end: 1, before: 'x', text: 'y' }]), /changed/);
  });

  it('renders a diff of planned changes only', async () => {
    const files = new Map([['a.props', props(pin('Cratis.Arc', '1.0.0'))]]);
    const plan = await planUpdates({ files, getVersions: index({ 'cratis.arc': ['1.0.0', '1.1.0'] }), checkImports: false });
    assert.match(renderDiff(files, plan.edits), /^--- a\/a\.props\n\+\+\+ b\/a\.props\n@@ line 3 @@\n-.*1\.0\.0.*\n\+.*1\.1\.0/);
  });
});

describe('command line', () => {
  const work = (files, table) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cratis-nuget-'));
    const checkout = path.join(root, 'checkout');
    for (const [file, text] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(checkout, file)), { recursive: true });
      fs.writeFileSync(path.join(checkout, file), text);
    }
    fs.mkdirSync(path.join(root, 'index'));
    for (const [id, versions] of Object.entries(table)) fs.writeFileSync(path.join(root, 'index', `${id}.json`), JSON.stringify({ versions }));
    const cli = (...args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8', input: '',
      env: { ...process.env, CRATIS_NUGET_INDEX_DIR: path.join(root, 'index'),
        PATH: `${path.join(root, 'bin')}${path.delimiter}${process.env.PATH}` } });
    return { root, checkout, state: path.join(root, 'state'), cli, read: (f) => fs.readFileSync(path.join(checkout, f), 'utf8') };
  };
  const files = { 'Directory.Packages.props': props(pin('Cratis.Arc', '1.0.0'), pin('Cratis.Arc.Core', '1.0.0')) };
  const table = { 'cratis.arc': ['1.0.0', '1.1.0'], 'cratis.arc.core': ['1.0.0', '1.1.0'] };

  function downgrade(id = 'Microsoft.Extensions.DependencyModel', required = '10.0.15', current = '10.0.14') {
    return `Project.csproj : error NU1109: Detected package downgrade: ${id} from ${required} to centrally defined ${current}. Update the centrally managed package version to a higher version.`;
  }

  function repairWork(outputs, centralPin = pin('Microsoft.Extensions.DependencyModel', '10.0.14'), extra = {}) {
    const w = work({ ...files, 'Nested/Directory.Packages.props': props(centralPin), ...extra }, table);
    fs.mkdirSync(path.join(w.root, 'bin'));
    fs.writeFileSync(path.join(w.root, 'outputs.json'), JSON.stringify(outputs));
    fs.writeFileSync(path.join(w.root, 'bin/dotnet'), `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const file = path.join(__dirname, '..', 'outputs.json');
const outputs = JSON.parse(fs.readFileSync(file, 'utf8'));
fs.appendFileSync(file + '.calls', process.argv.slice(2).join(' ') + '\\n');
const output = outputs.shift();
fs.writeFileSync(file, JSON.stringify(outputs));
if (output) { console.error(output); process.exit(1); }
`, { mode: 0o755 });
    w.calls = () => fs.readFileSync(path.join(w.root, 'outputs.json.calls'), 'utf8').trim().split('\n');
    const result = w.cli('prepare', '--root', w.checkout, '--state-dir', w.state);
    assert.equal(result.status, 0, result.stderr);
    return w;
  }

  it('repairs to the highest reported minimum, retries restore and rolls back both original files', () => {
    const w = repairWork([downgrade() + '\n' + downgrade(undefined, '10.0.16'), null]);
    const before = w.read('Nested/Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Microsoft.Extensions.DependencyModel.*10.0.14 -> 10.0.16/);
    assert.equal(w.read('Nested/Directory.Packages.props'), before.replace('10.0.14', '10.0.16'));
    assert.deepEqual(w.calls(), ['restore', 'restore']);
    assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
    assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
    assert.equal(w.read('Nested/Directory.Packages.props'), before);
    assert.equal(w.read('Directory.Packages.props'), files['Directory.Packages.props']);
  });

  it('leaves restore duration to the workflow job rather than imposing a per-call timeout', () => {
    const w = repairWork([null]);
    const preload = path.join(w.root, 'assert-restore-options.cjs');
    fs.writeFileSync(preload, `const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const original = childProcess.spawnSync;
childProcess.spawnSync = (command, args, options) => {
  assert.equal(command, 'dotnet');
  assert.equal(options.timeout, undefined);
  return original(command, args, options);
};
require('node:module').syncBuiltinESMExports();
`);
    const result = spawnSync(process.execPath, ['--require', preload, SCRIPT, 'restore', '--state-dir', w.state], {
      encoding: 'utf8', env: { ...process.env, PATH: `${path.join(w.root, 'bin')}${path.delimiter}${process.env.PATH}` },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(w.calls(), ['restore']);
  });

  const dependencyPin = pin('Microsoft.Extensions.DependencyModel', '10.0.14');
  const frameworkImports = files['Directory.Packages.props'].replace('</Project>', `<Import Project="Frameworks/net8.props" Condition="'$(TargetFramework)' == 'net8.0'" />
<Import Project="Frameworks/net10.props" Condition="'$(TargetFramework)' == 'net10.0'" /></Project>`);
  for (const layout of [
    { name: 'multiple central files', candidates: ['Nested/Directory.Packages.props', 'Net8/Directory.Packages.props'],
      extra: { 'Net8/Directory.Packages.props': props(pin('Microsoft.Extensions.DependencyModel', '8.0.0')) } },
    { name: 'framework-specific imported pins', candidates: ['Frameworks/net8.props', 'Frameworks/net10.props'],
      extra: { 'Directory.Packages.props': frameworkImports, 'Nested/Directory.Packages.props': '<Project />',
        'Frameworks/net8.props': props(pin('Microsoft.Extensions.DependencyModel', '8.0.0')),
        'Frameworks/net10.props': props(dependencyPin) } },
    { name: 'a single conditional import', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Directory.Build.props': '<Project><Import Project="Nested/Directory.Packages.props" Condition="true" /></Project>' } },
    { name: 'a transitive conditional import', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Directory.Build.props': '<Project><Import Project="Bridge.props" Condition="true" /></Project>',
        'Bridge.props': '<Project><Import Project="Nested/Directory.Packages.props" /></Project>' } },
    { name: 'a conditional ItemGroup', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Nested/Directory.Packages.props': props(dependencyPin).replace('<ItemGroup>', '<ItemGroup Condition="true">') } },
    { name: 'duplicate declarations in one file', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Nested/Directory.Packages.props': props(dependencyPin, pin('Microsoft.Extensions.DependencyModel', '8.0.0')) } },
    { name: 'an unresolved import', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Directory.Build.props': '<Project><Import Project="$(UnknownPath)/Versions.props" /></Project>' } },
  ]) {
    it(`fails closed without editing any manifest for ${layout.name}`, () => {
      const w = repairWork([downgrade()], dependencyPin, layout.extra);
      const before = Object.fromEntries(Object.keys({ ...files, 'Nested/Directory.Packages.props': '', ...layout.extra })
        .map((file) => [file, w.read(file)]));
      const result = w.cli('restore', '--state-dir', w.state);
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /::error::Cannot safely raise central pin Microsoft.Extensions.DependencyModel/);
      assert.match(result.stderr, /expected exactly one unconditional literal PackageVersion pin/);
      for (const file of layout.candidates) assert.ok(result.stderr.includes(file), result.stderr);
      for (const [file, text] of Object.entries(before)) assert.equal(w.read(file), text);
      assert.deepEqual(w.calls(), ['restore']);
    });
  }

  it('repairs a unique literal pin in an unconditionally imported central file', () => {
    const w = repairWork([downgrade(), null], '', {
      'Directory.Packages.props': files['Directory.Packages.props'].replace('</Project>', '<Import Project="Frameworks/shared.props" /></Project>'),
      'Frameworks/shared.props': props(dependencyPin),
    });
    const result = w.cli('restore', '--state-dir', w.state);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(w.read('Frameworks/shared.props'), props(dependencyPin).replace('10.0.14', '10.0.15'));
    assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
    assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
    assert.equal(w.read('Frameworks/shared.props'), props(dependencyPin));
  });

  it('honors case-insensitive exclusions without writing any central pins', () => {
    const w = repairWork([downgrade()]);
    const before = w.read('Nested/Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state, '--exclusions', 'microsoft.extensions.dependencymodel');
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /NU1109[\s\S]*package is excluded/);
    assert.equal(w.read('Nested/Directory.Packages.props'), before);
    assert.deepEqual(w.calls(), ['restore']);
  });

  it('surfaces unrelated failures after a repair and retains the original before-image', () => {
    const w = repairWork([downgrade(), 'error NU1301: Unable to load the service index']);
    const before = w.read('Nested/Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /NU1301/);
    assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
    assert.equal(w.read('Nested/Directory.Packages.props'), before);
  });

  it('bounds repairs to three attempts and keeps the original pins for rollback', () => {
    const w = repairWork(['10.0.15', '10.0.16', '10.0.17', '10.0.18'].map((v) => downgrade(undefined, v)));
    const before = w.read('Nested/Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /failed after 3 central pin repair attempts/);
    assert.deepEqual(w.calls(), ['restore', 'restore', 'restore', 'restore']);
    assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
    assert.equal(w.read('Nested/Directory.Packages.props'), before);
  });

  it('refuses property, range, conditional and Update central pins without editing them', () => {
    for (const centralPin of [pin('Microsoft.Extensions.DependencyModel', '$(SharedVersion)'),
      pin('Microsoft.Extensions.DependencyModel', '[10.0.14,11.0.0)'),
      '<PackageVersion Include="Microsoft.Extensions.DependencyModel" Version="10.0.14" Condition="true" />',
      '<PackageVersion Include="Microsoft.Extensions.DependencyModel"><Version Condition="true">10.0.14</Version></PackageVersion>',
      '<PackageVersion Update="Microsoft.Extensions.DependencyModel" Version="10.0.14" />']) {
      const w = repairWork([downgrade()], centralPin);
      const before = w.read('Nested/Directory.Packages.props');
      const result = w.cli('restore', '--state-dir', w.state);
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /expected exactly one unconditional literal PackageVersion pin/);
      assert.equal(w.read('Nested/Directory.Packages.props'), before);
    }
  });

  it('refuses an independent Cratis member repair', () => {
    const w = repairWork([downgrade('Cratis.Arc', '1.2.0', '1.1.0')]);
    const result = w.cli('restore', '--state-dir', w.state);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /cannot raise a Cratis family member independently/);
  });

  it('skips restore when no Cratis family changed', () => {
    const w = work({ 'Directory.Packages.props': props(pin('Serilog', '1.0.0')) }, {});
    assert.equal(w.cli('prepare', '--root', w.checkout, '--state-dir', w.state).status, 0);
    assert.equal(w.cli('restore', '--state-dir', w.state).status, 0);
  });

  it('does not write anything in dry-run mode', () => {
    const w = work(files, table);
    const result = w.cli('--dry-run', '--root', w.checkout);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /\+.*1\.1\.0/);
    assert.equal(w.read('Directory.Packages.props'), files['Directory.Packages.props']);
    assert.equal(fs.existsSync(w.state), false);
  });

  it('previews a manifest from stdin', () => {
    const w = work({}, table);
    const result = spawnSync(process.execPath, [SCRIPT, '--dry-run', '--stdin', '--label', 'x@y'], { encoding: 'utf8',
      input: files['Directory.Packages.props'], env: { ...process.env, CRATIS_NUGET_INDEX_DIR: path.join(w.root, 'index') } });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Single manifest only/);
  });

  it('prepares, verifies and rolls back, and refuses to roll back unexpected changes', () => {
    const w = work(files, table);
    assert.equal(w.cli('prepare', '--root', w.checkout, '--state-dir', w.state).status, 0);
    assert.match(w.read('Directory.Packages.props'), /Version="1\.1\.0"/);
    assert.equal(fs.readFileSync(path.join(w.state, 'protected-ids.txt'), 'utf8'), 'Cratis.Arc\nCratis.Arc.Core\n');
    assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
    // An SDK edit to a prepared pin is detected; so is an edit to any other manifest on rollback.
    fs.writeFileSync(path.join(w.checkout, 'Other.props'), '<Project />');
    assert.notEqual(w.cli('rollback', '--state-dir', w.state).status, 0);
    fs.rmSync(path.join(w.checkout, 'Other.props'));
    assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
    assert.equal(w.read('Directory.Packages.props'), files['Directory.Packages.props']);
    assert.equal(w.cli('prepare', '--root', w.checkout, '--state-dir', w.state).status, 0);
    fs.writeFileSync(path.join(w.checkout, 'Directory.Packages.props'), w.read('Directory.Packages.props').replace('1.1.0', '1.0.5'));
    assert.notEqual(w.cli('verify', '--state-dir', w.state).status, 0);
  });

  it('reports Cratis packages the manifests do not pin and fails for an edited family', () => {
    const w = work(files, table);
    w.cli('prepare', '--root', w.checkout, '--state-dir', w.state);
    const unknown = spawnSync(process.execPath, [SCRIPT, 'check-ids', '--state-dir', w.state], { encoding: 'utf8', input: 'Serilog\nCratis.Chronicle\nCratis.Arc\n' });
    assert.equal(unknown.status, 0);
    assert.equal(unknown.stdout, 'Cratis.Chronicle\n');
    const extra = spawnSync(process.execPath, [SCRIPT, 'check-ids', '--state-dir', w.state], { encoding: 'utf8', input: 'Cratis.Arc.Extra\n' });
    assert.equal(extra.status, 4);
  });

  it('fails without an index fixture instead of reaching the network, and refuses symlinked manifests', () => {
    const w = work(files, {});
    assert.notEqual(w.cli('--dry-run', '--root', w.checkout).status, 0);
    fs.symlinkSync(path.join(w.checkout, 'Directory.Packages.props'), path.join(w.checkout, 'Linked.props'));
    assert.match(w.cli('--dry-run', '--root', w.checkout).stderr, /symbolic link/);
  });

  it('reports zero families distinctly', () => {
    const w = work({ 'Directory.Packages.props': props(pin('Serilog', '1.0.0')) }, {});
    assert.match(w.cli('--dry-run', '--root', w.checkout).stdout, /No Cratis NuGet families found/);
  });
});
