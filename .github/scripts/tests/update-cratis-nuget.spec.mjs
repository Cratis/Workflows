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
      env: { ...process.env, CRATIS_NUGET_INDEX_DIR: path.join(root, 'index') } });
    return { root, checkout, state: path.join(root, 'state'), cli, read: (f) => fs.readFileSync(path.join(checkout, f), 'utf8') };
  };
  const files = { 'Directory.Packages.props': props(pin('Cratis.Arc', '1.0.0'), pin('Cratis.Arc.Core', '1.0.0')) };
  const table = { 'cratis.arc': ['1.0.0', '1.1.0'], 'cratis.arc.core': ['1.0.0', '1.1.0'] };

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
