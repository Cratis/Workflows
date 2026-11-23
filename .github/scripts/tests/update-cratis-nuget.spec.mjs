// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { applyEdits, compareVersions, familyKey, parseExclusions, parseVersion, planUpdates, pruneConflicts, renderDiff, updateWithPruning } from '../update-cratis-nuget.mjs';

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

describe('SDK conflict pruning', () => {
  const conflict = (id, dependency = 'Microsoft.OpenApi', code = 'NU1109') => {
    const header = code === 'NU1107' ? `Version conflict detected for ${dependency}.` :
      `Detected package downgrade: ${dependency} from 3.0.0 to centrally defined 2.0.0.`;
    return `Project.csproj : error ${code}: ${header}\nProject.csproj : error ${code}: Project -> ${id} 11.0.0 -> ${dependency} (>= 3.0.0)\n`;
  };

  function fixture(t) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-pruning-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const file = path.join(root, 'Directory.Packages.props');
    const before = props(pin('Cratis.Fundamentals', '7.34.4'), pin('Kept', '1.0.0'));
    fs.writeFileSync(file, before);
    return { root, file, before, read: () => fs.readFileSync(file, 'utf8') };
  }

  it('attributes NU1107 and NU1109 to selected concrete ancestors case-insensitively', () => {
    for (const code of ['NU1107', 'NU1109']) {
      assert.deepEqual([...pruneConflicts(conflict('sWaGgEr', undefined, code), ['Swagger', 'Kept']).keys()], ['Swagger']);
    }
  });

  it('deduplicates repeated errors while preserving reasons for multiple conflicting packages', () => {
    const output = conflict('Swagger') + conflict('Swagger') + conflict('Runner', 'xunit.v3.common', 'NU1107');
    assert.deepEqual([...pruneConflicts(output, ['Swagger', 'Runner', 'Kept']).keys()], ['Swagger', 'Runner']);
  });

  it('never prunes an excluded, protected or otherwise unselected ancestor', () => {
    assert.throws(() => pruneConflicts(conflict('Cratis.Arc'), ['Kept']), /Cannot attribute NU1109/);
  });

  it('does not mistake the centrally pinned dependency for a selected updated ancestor', () => {
    assert.throws(() => pruneConflicts(conflict('Unselected'), ['Microsoft.OpenApi']), /Cannot attribute/);
  });

  const leafConflict = (id = 'MongoDB.Driver') => `error NU1109: Detected package downgrade: ${id} from 3.0.0 to centrally defined 2.0.0.
error: Sample -> Local.Project -> ${id} (>= 3.0.0)
error: Sample -> ${id} (>= 2.0.0)`;

  it('prunes a selected leaf only from a matching failed-attempt literal manifest upgrade', (t) => {
    const w = fixture(t);
    const project = path.join(w.root, 'MongoDB.csproj');
    const before = '<Project><PackageReference Include="MongoDB.Driver"><Version>2.0.0</Version></PackageReference></Project>';
    fs.writeFileSync(project, before);
    const calls = [];
    const notices = [];
    updateWithPruning({ root: w.root, packages: ['MongoDB.Driver', 'Kept'], log: (line) => notices.push(line), run: (_root, ids) => {
      calls.push([...ids]);
      assert.equal(fs.readFileSync(project, 'utf8'), before);
      if (calls.length === 1) {
        fs.writeFileSync(project, before.replace('2.0.0', '3.0.0'));
        return { status: 3, output: leafConflict('mongodb.driver') };
      }
      fs.writeFileSync(w.file, w.before.replace('Kept" Version="1.0.0', 'Kept" Version="2.0.0'));
      return { status: 0, output: '' };
    } });
    assert.deepEqual(calls, [['MongoDB.Driver', 'Kept'], ['Kept']]);
    assert.equal(fs.readFileSync(project, 'utf8'), before);
    assert.match(w.read(), /Kept" Version="2.0.0/);
    assert.deepEqual(notices.filter((line) => line.startsWith('::notice::')), ["::notice::Pruned NuGet package 'MongoDB.Driver': NU1109 conflicts with mongodb.driver"]);
  });

  const unchanged = '<Project><PackageReference Include="MongoDB.Driver" Version="2.0.0" /></Project>';
  const insufficientDeltas = [
    ['no manifest delta', unchanged, unchanged, ['MongoDB.Driver', 'Kept']],
    ['unselected leaf', unchanged, unchanged.replace('2.0.0', '3.0.0'), ['Kept']],
    ['wrong new version', unchanged, unchanged.replace('2.0.0', '2.5.0'), ['MongoDB.Driver', 'Kept']],
    ['only the lower requirement changed', unchanged.replace('2.0.0', '1.0.0'), unchanged, ['MongoDB.Driver', 'Kept']],
    ['new declaration', '<Project />', unchanged.replace('2.0.0', '3.0.0'), ['MongoDB.Driver', 'Kept']],
    ['conditional declaration', unchanged.replace('Version=', 'Condition="true" Version='), unchanged.replace('Version=', 'Condition="true" Version=').replace('2.0.0', '3.0.0'), ['MongoDB.Driver', 'Kept']],
    ['property declaration', unchanged.replace('2.0.0', '$(Pin)'), unchanged.replace('2.0.0', '3.0.0'), ['MongoDB.Driver', 'Kept']],
    ['duplicate declaration', unchanged.replace('</Project>', '<PackageReference Update="MongoDB.Driver" Version="2.0.0" /></Project>'), unchanged.replace('</Project>', '<PackageReference Update="MongoDB.Driver" Version="2.0.0" /></Project>').replace('Version="2.0.0"', 'Version="3.0.0"'), ['MongoDB.Driver', 'Kept']],
  ];
  for (const [label, before, after, packages] of insufficientDeltas) {
    it(`fails closed on leaf attribution with ${label}`, (t) => {
      const w = fixture(t);
      const project = path.join(w.root, 'MongoDB.csproj');
      fs.writeFileSync(project, before);
      let attempts = 0;
      assert.throws(() => updateWithPruning({ root: w.root, packages, log: () => {}, run: () => {
        attempts++;
        fs.writeFileSync(project, after);
        return { status: 3, output: leafConflict() };
      } }), /Cannot attribute NU1109 for MongoDB.Driver/);
      assert.equal(attempts, 1);
      assert.equal(fs.readFileSync(project, 'utf8'), before);
    });
  }

  it('uses only the current attempt delta, never a rolled-back earlier update as evidence', (t) => {
    const w = fixture(t);
    const project = path.join(w.root, 'Library.csproj');
    const before = '<Project><PackageReference Include="MongoDB.Driver" Version="2.0.0" /><PackageReference Include="Kept" Version="2.0.0" /></Project>';
    fs.writeFileSync(project, before);
    let attempts = 0;
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['MongoDB.Driver', 'Kept'], log: () => {}, run: () => {
      attempts++;
      assert.equal(fs.readFileSync(project, 'utf8'), before);
      if (attempts === 1) fs.writeFileSync(project, before.replaceAll('2.0.0', '3.0.0'));
      return { status: 3, output: leafConflict(attempts === 1 ? 'MongoDB.Driver' : 'Kept') };
    } }), /Cannot attribute NU1109 for Kept/);
    assert.equal(attempts, 2);
    assert.equal(fs.readFileSync(project, 'utf8'), before);
  });

  it('prunes a proven NU1107 leaf upgrade without raising its pin or retaining failed writes', (t) => {
    const w = fixture(t);
    const before = props(pin('xunit.v3.common', '2.0.0'));
    fs.writeFileSync(w.file, before);
    const calls = [];
    updateWithPruning({ root: w.root, packages: ['xunit.v3.common', 'Kept'], log: () => {}, run: (_root, ids) => {
      calls.push([...ids]);
      if (calls.length > 1) return { status: 0, output: '' };
      fs.writeFileSync(w.file, before.replace('2.0.0', '3.0.0'));
      return { status: 3, output: 'error NU1107: Version conflict detected for xunit.v3.common.\nerror: Project -> Unselected 1.0.0 -> xunit.v3.common (= 3.0.0)' };
    } });
    assert.deepEqual(calls, [['xunit.v3.common', 'Kept'], ['Kept']]);
    assert.equal(w.read(), before);
  });

  it('prefers selected concrete ancestors over leaf delta attribution', () => {
    const delta = [{ id: 'microsoft.openapi', version: parseVersion('3.0.0') }];
    assert.deepEqual([...pruneConflicts(conflict('Swagger'), ['Swagger', 'Microsoft.OpenApi'], delta).keys()], ['Swagger']);
  });

  it('fails closed on unrelated restore errors even alongside an attributable conflict', () => {
    assert.throws(() => pruneConflicts(conflict('Swagger') + 'error NU1301: Feed unavailable', ['Swagger']), /Cannot attribute SDK restore error/);
    assert.throws(() => pruneConflicts('SDK command is not supported', ['Swagger']), /No attributable/);
  });

  it('undoes every failed attempt, prunes incrementally and retains an unrelated successful update', (t) => {
    const w = fixture(t);
    const calls = [];
    const logs = [];
    updateWithPruning({ root: w.root, packages: ['Swagger', 'Runner', 'Kept'], log: (line) => logs.push(line),
      run: (_root, ids) => {
        assert.equal(w.read(), w.before, 'each attempt starts from the prepared family pins');
        calls.push([...ids]);
        fs.writeFileSync(w.file, w.before.replace('Kept" Version="1.0.0', 'Kept" Version="2.0.0'));
        if (calls.length === 1) return { status: 3, output: conflict('Swagger') };
        if (calls.length === 2) return { status: 3, output: conflict('Runner', 'xunit.v3.common', 'NU1107') };
        return { status: 0, output: 'Updated Kept' };
      } });
    assert.deepEqual(calls, [['Swagger', 'Runner', 'Kept'], ['Runner', 'Kept'], ['Kept']]);
    assert.match(w.read(), /Kept" Version="2.0.0/);
    assert.match(w.read(), /Cratis.Fundamentals" Version="7.34.4/);
    assert.equal(logs.filter((line) => line.includes('::notice::Pruned')).length, 2);
  });

  it('bounds retries to four SDK attempts and leaves no partial manifest changes', (t) => {
    const w = fixture(t);
    let attempts = 0;
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['A', 'B', 'C', 'D', 'Kept'], log: () => {},
      run: (_root, ids) => {
        attempts++;
        fs.writeFileSync(w.file, 'partial');
        return { status: 3, output: conflict(ids[0]) };
      } }), /exhausted 4 attempts/);
    assert.equal(attempts, 4);
    assert.equal(w.read(), w.before);
  });

  it('fails when pruning empties the selection rather than reporting an all-packages skip', (t) => {
    const w = fixture(t);
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['Swagger'], log: () => {},
      run: () => ({ status: 3, output: conflict('Swagger') }) }), /No eligible NuGet packages remain/);
    assert.equal(w.read(), w.before);
  });

  for (const status of [1, 2, 23, null]) {
    it(`does not retry a non-conflict failure (exit ${status}) and restores all manifests`, (t) => {
      const w = fixture(t);
      let calls = 0;
      assert.throws(() => updateWithPruning({ root: w.root, packages: ['Kept'], log: () => {}, run: () => {
        calls++;
        fs.writeFileSync(w.file, 'partial');
        return { status, output: 'Failed' };
      } }), /dotnet package update failed/);
      assert.equal(calls, 1);
      assert.equal(w.read(), w.before);
    });
  }

  it('rolls back an unattributable failure including existing lock files and newly created manifests', (t) => {
    const w = fixture(t);
    const lock = path.join(w.root, 'packages.lock.json');
    const added = path.join(w.root, 'Added.csproj');
    fs.writeFileSync(lock, '{}\n');
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['Kept'], log: () => {}, run: () => {
      fs.writeFileSync(w.file, 'partial');
      fs.writeFileSync(lock, '{"partial":true}');
      fs.writeFileSync(added, '<Project />');
      return { status: 3, output: conflict('Unselected') };
    } }), /Cannot attribute/);
    assert.equal(w.read(), w.before);
    assert.equal(fs.readFileSync(lock, 'utf8'), '{}\n');
    assert.equal(fs.existsSync(added), false);
  });

  it('undoes SDK writes if successful output fails the protected-pin verification', (t) => {
    const w = fixture(t);
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['Kept'], log: () => {},
      validate: () => { throw new Error('Protected pin changed'); }, run: () => {
        fs.writeFileSync(w.file, 'partial');
        return { status: 0, output: '' };
      } }), /Protected pin changed/);
    assert.equal(w.read(), w.before);
  });

  it('undoes SDK writes when launching the command throws', (t) => {
    const w = fixture(t);
    assert.throws(() => updateWithPruning({ root: w.root, packages: ['Kept'], log: () => {}, run: () => {
      fs.writeFileSync(w.file, 'partial');
      throw new Error('Cannot launch SDK');
    } }), /Cannot launch SDK/);
    assert.equal(w.read(), w.before);
  });

  it('preserves the unfiltered first call but retries with only surviving IDs', (t) => {
    const w = fixture(t);
    const calls = [];
    updateWithPruning({ root: w.root, packages: ['Swagger', 'Kept'], unfiltered: true, log: () => {}, run: (_root, ids) => {
      calls.push(ids);
      return calls.length === 1 ? { status: 3, output: conflict('Swagger') } : { status: 0, output: '' };
    } });
    assert.deepEqual(calls, [[], ['Kept']]);
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
    { name: 'a conditional Project', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Nested/Directory.Packages.props': props(dependencyPin).replace('<Project>', '<Project Condition="true">') } },
    { name: 'a Choose/When wrapper', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Nested/Directory.Packages.props': props(dependencyPin).replace('<Project>', '<Project><Choose><When Condition="true">')
        .replace('</Project>', '</When></Choose></Project>') } },
    { name: 'duplicate declarations in one file', candidates: ['Nested/Directory.Packages.props'],
      extra: { 'Nested/Directory.Packages.props': props(dependencyPin, pin('Microsoft.Extensions.DependencyModel', '8.0.0')) } },
    { name: 'two unconditional Includes across a central file and its import', candidates: ['Directory.Packages.props', 'Directory.Packages.NET11.props'],
      extra: { 'Directory.Packages.props': files['Directory.Packages.props'].replace('</ItemGroup>', `${dependencyPin}</ItemGroup>`)
        .replace('</Project>', '<Import Project="Directory.Packages.NET11.props" /></Project>'),
        'Directory.Packages.NET11.props': props(pin('Microsoft.Extensions.DependencyModel', '11.0.0')),
        'Nested/Directory.Packages.props': '<Project />' } },
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

  for (const layout of [
    { name: 'the Arc TFM-conditional NET11 Update',
      text: props('<PackageVersion Update="Microsoft.Extensions.DependencyModel" Version="11.0.0" />')
        .replace('<ItemGroup>', '<ItemGroup Condition="\'$(TargetFramework)\' == \'net11.0\'">') },
    { name: 'an unconditional Update', text: props('<PackageVersion Update="Microsoft.Extensions.DependencyModel" Version="11.0.0" />') },
    { name: 'a conditional Include ItemGroup', text: props(pin('Microsoft.Extensions.DependencyModel', '11.0.0'))
      .replace('<ItemGroup>', '<ItemGroup Condition="\'$(TargetFramework)\' == \'net11.0\'">') },
    { name: 'an Include with a Condition attribute',
      text: props('<PackageVersion Include="Microsoft.Extensions.DependencyModel" Version="11.0.0" Condition="\'$(TargetFramework)\' == \'net11.0\'" />') },
  ]) {
    it(`repairs the unconditional Include without changing ${layout.name}`, () => {
      const central = files['Directory.Packages.props'].replace('</ItemGroup>', `  ${dependencyPin}\n  </ItemGroup>`)
        .replace('</Project>', '<Import Project="$(MSBuildThisFileDirectory)/Directory.Packages.NET11.props" /></Project>');
      const w = repairWork([downgrade(), null], '', {
        'Directory.Packages.props': central,
        'Directory.Packages.NET11.props': layout.text,
      });
      const before = w.read('Directory.Packages.props');
      const result = w.cli('restore', '--state-dir', w.state);
      assert.equal(result.status, 0, result.stderr);
      assert.equal(w.read('Directory.Packages.props'), before.replace('10.0.14', '10.0.15'));
      assert.equal(w.read('Directory.Packages.NET11.props'), layout.text);
      assert.deepEqual(w.calls(), ['restore', 'restore']);
      assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
      assert.equal(w.cli('rollback', '--state-dir', w.state).status, 0);
      assert.equal(w.read('Directory.Packages.props'), central);
      assert.equal(w.read('Directory.Packages.NET11.props'), layout.text);
    });
  }

  it('fails closed when NuGet reports the stale NET11 Update rather than the unconditional Include', () => {
    const central = files['Directory.Packages.props'].replace('</ItemGroup>', `${dependencyPin}</ItemGroup>`)
      .replace('</Project>', '<Import Project="Directory.Packages.NET11.props" /></Project>');
    const net11 = props('<PackageVersion Update="Microsoft.Extensions.DependencyModel" Version="11.0.0" />')
      .replace('<ItemGroup>', '<ItemGroup Condition="\'$(TargetFramework)\' == \'net11.0\'">');
    const w = repairWork([downgrade(undefined, '11.0.1', '11.0.0')], '', {
      'Directory.Packages.props': central, 'Directory.Packages.NET11.props': net11,
    });
    const before = w.read('Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /::error::Cannot safely raise central pin Microsoft.Extensions.DependencyModel:.*centrally defined version 11\.0\.0.*candidate Directory\.Packages\.props.*literal Version 10\.0\.14/);
    assert.equal(w.read('Directory.Packages.props'), before);
    assert.equal(w.read('Directory.Packages.NET11.props'), net11);
    assert.deepEqual(w.calls(), ['restore']);
    assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
  });

  it('rejects conflicting centrally defined versions across NU1109 reports without writing any pins', () => {
    const w = repairWork([downgrade() + '\n' + downgrade(undefined, '11.0.1', '11.0.0')]);
    const before = w.read('Nested/Directory.Packages.props');
    const result = w.cli('restore', '--state-dir', w.state);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Conflicting NU1109 centrally defined versions for Microsoft.Extensions.DependencyModel: 10\.0\.14 \/ 11\.0\.0/);
    assert.equal(w.read('Nested/Directory.Packages.props'), before);
    assert.deepEqual(w.calls(), ['restore']);
    assert.equal(w.cli('verify', '--state-dir', w.state).status, 0);
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
    const w = repairWork(['10.0.15', '10.0.16', '10.0.17', '10.0.18']
      .map((v, i, versions) => downgrade(undefined, v, i === 0 ? '10.0.14' : versions[i - 1])));
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
      assert.match(result.stderr, /candidate files: Nested\/Directory\.Packages\.props/);
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
