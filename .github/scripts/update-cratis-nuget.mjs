// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
// Keep every locally pinned member of a Cratis NuGet family on one stable version.
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const MANIFEST_EXTENSIONS = ['.csproj', '.props', '.targets'];
const SKIPPED_DIRECTORIES = new Set(['.git', '.ai-work', 'node_modules', 'bin', 'obj']);
const PACKAGE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const VERSION = /^(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?(?:\+[0-9A-Za-z.-]+)?$/;
const PROPERTY_REFERENCE = /^\$\(([A-Za-z_][A-Za-z0-9_.-]*)\)$/;
const NUGET_TIMEOUT_MS = 20000;
const BLOCKING_UNRESOLVED_IMPORT = 'an MSBuild import could not be resolved to a scanned file';

export function parseVersion(text) {
  const match = VERSION.exec(text);
  if (!match) return null;
  const numbers = match[1].split('.').map(Number);
  while (numbers.length < 4) numbers.push(0);
  return { numbers, pre: match[2] ?? null, text };
}

function comparePre(left, right) {
  if (left === right) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  const a = left.split('.');
  const b = right.split('.');
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] === undefined) return -1;
    if (b[i] === undefined) return 1;
    const numeric = /^\d+$/.test(a[i]) && /^\d+$/.test(b[i]);
    if (numeric && Number(a[i]) !== Number(b[i])) return Number(a[i]) < Number(b[i]) ? -1 : 1;
    if (!numeric && a[i] !== b[i]) {
      if (/^\d+$/.test(a[i])) return -1;
      if (/^\d+$/.test(b[i])) return 1;
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

export function compareVersions(left, right) {
  for (let i = 0; i < 4; i++) {
    if (left.numbers[i] !== right.numbers[i]) return left.numbers[i] < right.numbers[i] ? -1 : 1;
  }
  return comparePre(left.pre, right.pre);
}

function versionKey(version) {
  const parts = version.numbers[3] === 0 ? version.numbers.slice(0, 3) : version.numbers;
  return parts.join('.') + (version.pre === null ? '' : `-${version.pre}`);
}

export function familyKey(id) {
  const parts = id.toLowerCase().split('.');
  if (parts[0] !== 'cratis') return null;
  return `cratis.${parts[1] ?? 'arc'}`;
}

function familyName(id) {
  const parts = id.split('.');
  return `Cratis.${parts[1] ?? 'Arc'}`;
}

function skipPast(text, terminator, from, label) {
  const index = text.indexOf(terminator, from);
  if (index < 0) throw new Error(`Malformed XML: unterminated ${label}`);
  return index + terminator.length;
}

function readAttributes(text, start, element) {
  const word = /[^\s=/>]+/y;
  let at = start;
  for (;;) {
    while (/\s/.test(text[at] ?? '')) at++;
    if (text[at] === '>') return at + 1;
    if (text.startsWith('/>', at)) {
      element.selfClosing = true;
      return at + 2;
    }
    word.lastIndex = at;
    const name = word.exec(text);
    if (!name) throw new Error(`Malformed XML: bad attribute in <${element.name}>`);
    at += name[0].length;
    while (/\s/.test(text[at] ?? '')) at++;
    if (text[at] !== '=') throw new Error(`Malformed XML: attribute ${name[0]} has no value`);
    at++;
    while (/\s/.test(text[at] ?? '')) at++;
    const quote = text[at];
    if (quote !== '"' && quote !== "'") throw new Error(`Malformed XML: attribute ${name[0]} is not quoted`);
    const close = text.indexOf(quote, at + 1);
    if (close < 0) throw new Error(`Malformed XML: unterminated attribute ${name[0]}`);
    element.attrs.set(name[0].toLowerCase(), { name: name[0], value: text.slice(at + 1, close), start: at + 1, end: close });
    at = close + 1;
  }
}

// A bounded tokenizer: it records element and value spans so edits never reserialize the document.
export function parseXml(text) {
  const elements = [];
  const stack = [];
  let at = 0;
  while (at < text.length) {
    const lt = text.indexOf('<', at);
    if (lt < 0) break;
    const top = stack[stack.length - 1];
    if (text.startsWith('<!--', lt)) {
      if (top) top.hasMarkup = true;
      at = skipPast(text, '-->', lt + 4, 'comment');
    } else if (text.startsWith('<![CDATA[', lt)) {
      if (top) top.hasMarkup = true;
      at = skipPast(text, ']]>', lt + 9, 'CDATA section');
    } else if (text.startsWith('<?', lt)) {
      at = skipPast(text, '?>', lt + 2, 'processing instruction');
    } else if (text.startsWith('<!', lt)) {
      at = skipPast(text, '>', lt + 2, 'declaration');
    } else if (text[lt + 1] === '/') {
      const close = skipPast(text, '>', lt + 2, 'closing tag');
      const name = text.slice(lt + 2, close - 1).trim();
      if (!top || top.name !== name) throw new Error(`Malformed XML: unexpected </${name}>`);
      top.contentEnd = lt;
      top.end = close;
      stack.pop();
      at = close;
    } else {
      const named = /[^\s/>]+/y;
      named.lastIndex = lt + 1;
      const name = named.exec(text);
      if (!name) throw new Error('Malformed XML: element without a name');
      const element = { name: name[0], lname: name[0].toLowerCase(), attrs: new Map(), parent: top ?? null,
        children: [], start: lt, selfClosing: false, hasMarkup: false };
      element.openEnd = readAttributes(text, lt + 1 + name[0].length, element);
      if (top) top.children.push(element);
      elements.push(element);
      if (element.selfClosing) {
        element.contentEnd = element.openEnd;
        element.end = element.openEnd;
      } else {
        stack.push(element);
      }
      at = element.openEnd;
    }
  }
  if (stack.length > 0) throw new Error(`Malformed XML: <${stack[stack.length - 1].name}> is not closed`);
  return elements;
}

function textValue(text, element) {
  if (element.selfClosing || element.children.length > 0 || element.hasMarkup) return null;
  const content = text.slice(element.openEnd, element.contentEnd);
  const value = content.trim();
  const start = element.openEnd + (content.length - content.trimStart().length);
  return { value, start, end: start + value.length };
}

function isConditional(element) {
  for (let node = element; node; node = node.parent) {
    if (node.attrs.has('condition') || ['choose', 'when', 'otherwise'].includes(node.lname)) return true;
  }
  return false;
}

function versionSources(text, element) {
  const sources = [];
  for (const key of ['version', 'versionoverride']) {
    const attribute = element.attrs.get(key);
    if (attribute) sources.push({ kind: `attribute ${attribute.name}`, raw: attribute.value, start: attribute.start,
      end: attribute.end, conditional: isConditional(element) });
  }
  for (const child of element.children.filter((c) => c.lname === 'version' || c.lname === 'versionoverride')) {
    const value = textValue(text, child);
    sources.push({ kind: `element ${child.name}`, raw: value?.value ?? null, start: value?.start, end: value?.end,
      conditional: isConditional(child) });
  }
  return sources;
}

function analyzeFile(file, text) {
  const elements = parseXml(text);
  const result = { file, text, properties: [], declarations: [], imports: [] };
  for (const element of elements) {
    if (element.parent?.lname === 'propertygroup') {
      const value = textValue(text, element);
      result.properties.push({ name: element.lname, file, value, conditional: isConditional(element) });
    } else if (element.lname === 'import') {
      result.imports.push({ project: element.attrs.get('project')?.value ?? null, sdk: element.attrs.has('sdk') });
    } else if (element.lname === 'packageversion' || element.lname === 'packagereference') {
      const id = (element.attrs.get('include') ?? element.attrs.get('update'))?.value;
      if (!id || !PACKAGE_ID.test(id) || !familyKey(id)) continue;
      for (const source of versionSources(text, element)) result.declarations.push({ file, id, ...source });
    }
  }
  return result;
}

function hasUnresolvedImport(analyses, files) {
  for (const analysis of analyses) {
    for (const entry of analysis.imports.filter((i) => !i.sdk)) {
      const project = (entry.project ?? '$(').replace(/\$\((?:MSBuildThisFileDirectory|MSBuildProjectDirectory)\)[\\/]?/gi, '');
      if (project.includes('$(')) return true;
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(analysis.file), project.replaceAll('\\', '/')));
      if (!files.has(target)) return true;
    }
  }
  return false;
}

function countUses(analyses, name) {
  const pattern = new RegExp(`\\$\\(${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\)`, 'gi');
  return analyses.reduce((total, analysis) => total + (analysis.text.match(pattern)?.length ?? 0), 0);
}

function resolveDeclaration(declaration, context) {
  const { raw } = declaration;
  if (declaration.conditional) return { reason: `${declaration.id} has a conditional version in ${declaration.file}` };
  if (raw === null) return { reason: `${declaration.id} has a version that is not plain text in ${declaration.file}` };
  if (parseVersion(raw)) return { version: raw, edit: { file: declaration.file, start: declaration.start, end: declaration.end, before: raw } };
  const property = PROPERTY_REFERENCE.exec(raw);
  if (!property) return { reason: `${declaration.id} uses the unsupported version '${raw}' in ${declaration.file}` };
  const name = property[1].toLowerCase();
  const definitions = context.properties.get(name) ?? [];
  if (context.unresolvedImports) return { reason: `$(${property[1]}) cannot be proven: ${BLOCKING_UNRESOLVED_IMPORT}` };
  if (definitions.length !== 1) return { reason: `$(${property[1]}) has ${definitions.length} definitions, not exactly one` };
  const [definition] = definitions;
  if (definition.conditional || !definition.value || !parseVersion(definition.value.value)) {
    return { reason: `$(${property[1]}) is conditional or not a plain version` };
  }
  return { version: definition.value.value, property: name,
    edit: { file: definition.file, start: definition.value.start, end: definition.value.end, before: definition.value.value } };
}

function stableCandidates(versions) {
  const candidates = new Map();
  for (const text of versions) {
    const version = typeof text === 'string' ? parseVersion(text) : null;
    if (version && version.pre === null) candidates.set(versionKey(version), version);
  }
  return candidates;
}

async function planFamily(family, context) {
  const members = [...family.members.keys()];
  const effective = [];
  for (const declaration of family.declarations) {
    const resolved = resolveDeclaration(declaration, context);
    if (resolved.reason) return { outcome: 'deferred', reason: resolved.reason, members };
    if (resolved.property) {
      const uses = family.declarations.filter((d) => d.raw?.toLowerCase() === `$(${resolved.property})`).length;
      if (countUses(context.analyses, resolved.property) !== uses) {
        return { outcome: 'deferred', reason: `$(${resolved.property}) is used outside this family's package versions`, members };
      }
    }
    effective.push(resolved);
  }
  const floor = effective.map((e) => parseVersion(e.version)).reduce((a, b) => (compareVersions(a, b) >= 0 ? a : b));
  let common = null;
  for (const id of members) {
    const stable = stableCandidates(await context.getVersions(id));
    common = common === null ? stable : new Map([...common].filter(([key]) => stable.has(key)));
  }
  const eligible = [...common.values()].filter((version) => compareVersions(version, floor) >= 0);
  if (eligible.length === 0) {
    return { outcome: 'deferred', reason: `no stable version at or above ${floor.text} exists for every member`, members };
  }
  const target = eligible.reduce((a, b) => (compareVersions(a, b) >= 0 ? a : b));
  const edits = new Map();
  for (const resolved of effective) {
    if (compareVersions(parseVersion(resolved.version), target) !== 0) {
      edits.set(`${resolved.edit.file}:${resolved.edit.start}`, { ...resolved.edit, text: target.text });
    }
  }
  return { outcome: edits.size > 0 ? 'updated' : 'unchanged', target: target.text, members, edits: [...edits.values()] };
}

export function parseExclusions(csv) {
  const ids = [];
  for (const part of (csv ?? '').split(',')) {
    const id = part.trim();
    if (!id) continue;
    if (!PACKAGE_ID.test(id)) throw new Error('Invalid NuGet exclusion ID');
    ids.push(id);
  }
  return ids;
}

export async function planUpdates({ files, exclusions = [], getVersions, checkImports = true }) {
  const analyses = [...files].map(([file, text]) => {
    try {
      return analyzeFile(file, text);
    } catch (error) {
      throw new Error(`${file}: ${error.message}`);
    }
  });
  const properties = new Map();
  for (const analysis of analyses) {
    for (const property of analysis.properties) properties.set(property.name, [...(properties.get(property.name) ?? []), property]);
  }
  const context = { analyses, properties, getVersions, unresolvedImports: checkImports && hasUnresolvedImport(analyses, files) };
  const held = new Set(exclusions.map(familyKey).filter(Boolean));
  const families = new Map();
  for (const declaration of analyses.flatMap((a) => a.declarations)) {
    const key = familyKey(declaration.id);
    const family = families.get(key) ?? { key, name: familyName(declaration.id), declarations: [], members: new Map() };
    family.declarations.push(declaration);
    if (!family.members.has(declaration.id.toLowerCase())) family.members.set(declaration.id.toLowerCase(), declaration.id);
    families.set(key, family);
  }
  const results = [];
  const edits = new Map();
  for (const family of [...families.values()].sort((a, b) => a.key.localeCompare(b.key))) {
    const members = [...family.members.values()];
    const planned = held.has(family.key)
      ? { outcome: 'held', reason: 'a member is excluded', members }
      : { ...(await planFamily({ ...family, members: family.members }, context)), members };
    results.push({ key: family.key, name: family.name, ...planned, members });
    for (const edit of planned.edits ?? []) edits.set(edit.file, [...(edits.get(edit.file) ?? []), edit]);
  }
  const protectedIds = [...new Map(analyses.flatMap((a) => a.declarations).map((d) => [d.id.toLowerCase(), d.id])).values()];
  return { families: results, edits, protectedIds };
}

export function applyEdits(text, edits) {
  let result = text;
  for (const edit of [...edits].sort((a, b) => b.start - a.start)) {
    if (result.slice(edit.start, edit.end) !== edit.before) throw new Error('A pin changed while the update was being prepared');
    result = result.slice(0, edit.start) + edit.text + result.slice(edit.end);
  }
  return result;
}

// Pins that must not drift once prepared: used to verify the generic updater left families alone.
export function snapshotPins(files) {
  const analyses = [...files].map(([file, text]) => analyzeFile(file, text));
  const referenced = new Set();
  const lines = [];
  for (const declaration of analyses.flatMap((a) => a.declarations)) {
    lines.push(`pin|${declaration.file}|${declaration.id.toLowerCase()}|${declaration.raw}|${declaration.conditional}`);
    const property = declaration.raw && PROPERTY_REFERENCE.exec(declaration.raw);
    if (property) referenced.add(property[1].toLowerCase());
  }
  for (const property of analyses.flatMap((a) => a.properties).filter((p) => referenced.has(p.name))) {
    lines.push(`property|${property.file}|${property.name}|${property.value?.value}`);
  }
  return lines.sort().join('\n');
}

export function renderDiff(files, edits) {
  const output = [];
  for (const [file, fileEdits] of [...edits].sort(([a], [b]) => a.localeCompare(b))) {
    const before = files.get(file).split('\n');
    const after = applyEdits(files.get(file), fileEdits).split('\n');
    output.push(`--- a/${file}`, `+++ b/${file}`);
    after.forEach((line, index) => {
      if (line !== before[index]) output.push(`@@ line ${index + 1} @@`, `-${before[index]}`, `+${line}`);
    });
  }
  return output.join('\n');
}

const escapeAnnotation = (value) => value.replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A');

export function describeOutcomes(plan, manifestCount) {
  const lines = [];
  const count = (outcome) => plan.families.filter((f) => f.outcome === outcome).length;
  for (const family of plan.families) {
    const members = family.members.join(', ');
    if (family.outcome === 'deferred') {
      lines.push(`::warning::Cratis NuGet family ${family.name} was left unchanged: ${escapeAnnotation(family.reason)} (${escapeAnnotation(members)})`);
    } else if (family.outcome === 'held') {
      lines.push(`::notice::Cratis NuGet family ${family.name} is held: ${family.reason} (${escapeAnnotation(members)})`);
    } else {
      lines.push(`Cratis NuGet family ${family.name}: ${family.outcome}${family.target ? ` at ${family.target}` : ''} (${members})`);
    }
  }
  lines.push(plan.families.length === 0
    ? `No Cratis NuGet families found in ${manifestCount} manifest file(s).`
    : `Cratis NuGet families: ${plan.families.length} found; ${count('updated')} updated, ${count('unchanged')} unchanged, ` +
      `${count('deferred')} deferred, ${count('held')} held.`);
  return lines;
}

async function fetchVersions(id, indexDir) {
  const name = id.toLowerCase();
  let body;
  if (indexDir) {
    const file = path.join(indexDir, `${name}.json`);
    if (!fs.existsSync(file)) throw new Error(`No package index fixture for ${id}`);
    body = JSON.parse(fs.readFileSync(file, 'utf8'));
  } else {
    const response = await fetch(`https://api.nuget.org/v3-flatcontainer/${name}/index.json`,
      { signal: AbortSignal.timeout(NUGET_TIMEOUT_MS), headers: { accept: 'application/json' } });
    if (!response.ok) throw new Error(`NuGet index for ${id} answered HTTP ${response.status}`);
    body = await response.json();
  }
  if (!body || !Array.isArray(body.versions) || body.versions.length === 0 || !body.versions.every((v) => typeof v === 'string')) {
    throw new Error(`NuGet index for ${id} is missing or malformed`);
  }
  return body.versions;
}

function collectFiles(root) {
  const files = new Map();
  const top = fs.realpathSync(root);
  const walk = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(directory, entry.name);
      const isManifest = MANIFEST_EXTENSIONS.includes(path.extname(entry.name).toLowerCase());
      if (entry.isSymbolicLink()) {
        if (isManifest) throw new Error(`Refusing symbolic link manifest ${path.relative(top, full)}`);
      } else if (entry.isDirectory()) {
        if (!SKIPPED_DIRECTORIES.has(entry.name)) walk(full);
      } else if (entry.isFile() && isManifest) {
        if (!fs.realpathSync(full).startsWith(top + path.sep)) throw new Error(`Refusing manifest outside the checkout: ${full}`);
        files.set(path.relative(top, full).split(path.sep).join('/'), fs.readFileSync(full, 'utf8'));
      }
    }
  };
  walk(top);
  return files;
}

const sha = (text) => createHash('sha256').update(text).digest('hex');

function writeChecked(root, file, expected, next) {
  const target = path.join(root, file);
  if (fs.readFileSync(target, 'utf8') !== expected) throw new Error(`${file} changed while preparing the update`);
  fs.writeFileSync(target, next);
}

function prepare(root, stateDir, plan, files) {
  fs.mkdirSync(path.join(stateDir, 'before'), { recursive: true });
  const after = new Map(files);
  const changed = {};
  let index = 0;
  for (const [file, edits] of plan.edits) {
    after.set(file, applyEdits(files.get(file), edits));
    changed[file] = `before/${index++}`;
    fs.writeFileSync(path.join(stateDir, changed[file]), files.get(file));
  }
  const written = [];
  try {
    for (const file of Object.keys(changed)) {
      writeChecked(root, file, files.get(file), after.get(file));
      written.push(file);
    }
  } catch (error) {
    for (const file of written) fs.writeFileSync(path.join(root, file), files.get(file));
    throw error;
  }
  const hashes = Object.fromEntries([...after].map(([file, text]) => [file, sha(text)]));
  const edited = plan.families.filter((f) => f.outcome === 'updated').map((f) => f.key);
  fs.writeFileSync(path.join(stateDir, 'state.json'), JSON.stringify({ root, hashes, changed, edited, snapshot: snapshotPins(after),
    protectedIds: plan.protectedIds }, null, 2));
  fs.writeFileSync(path.join(stateDir, 'protected-ids.txt'), plan.protectedIds.map((id) => `${id}\n`).join(''));
}

const readState = (stateDir) => JSON.parse(fs.readFileSync(path.join(stateDir, 'state.json'), 'utf8'));

function verify(stateDir) {
  const state = readState(stateDir);
  if (snapshotPins(collectFiles(state.root)) !== state.snapshot) {
    throw new Error('A Cratis family pin changed after it was prepared; refusing to continue');
  }
  console.log('Cratis NuGet family pins match the prepared outcome.');
}

function rollback(stateDir) {
  const state = readState(stateDir);
  const current = collectFiles(state.root);
  const names = Object.keys(state.hashes);
  if (current.size !== names.length || names.some((file) => !current.has(file) || sha(current.get(file)) !== state.hashes[file])) {
    throw new Error('Manifests changed beyond the prepared Cratis edits; refusing to roll back');
  }
  for (const [file, image] of Object.entries(state.changed)) {
    fs.writeFileSync(path.join(state.root, file), fs.readFileSync(path.join(stateDir, image), 'utf8'));
  }
  console.log(`Rolled back ${Object.keys(state.changed).length} Cratis family manifest edit(s).`);
}

function checkIds(stateDir, input) {
  const state = readState(stateDir);
  const known = new Set(state.protectedIds.map((id) => id.toLowerCase()));
  let incomplete = false;
  for (const id of input.split('\n').map((line) => line.trim()).filter(Boolean)) {
    const key = familyKey(id);
    if (!key || known.has(id.toLowerCase())) continue;
    if (state.edited.includes(key)) {
      console.error(`::error::${escapeAnnotation(id)} belongs to an edited Cratis family but is not pinned in the scanned manifests`);
      incomplete = true;
    }
    console.log(id);
  }
  if (incomplete) process.exitCode = 4;
}

async function main(argv) {
  const { values, positionals } = parseArgs({ args: argv, allowPositionals: true, options: {
    'dry-run': { type: 'boolean' }, root: { type: 'string' }, stdin: { type: 'boolean' }, 'manifest-path': { type: 'string' },
    label: { type: 'string' }, exclusions: { type: 'string' }, 'state-dir': { type: 'string' }, 'index-dir': { type: 'string' } } });
  const command = values['dry-run'] ? 'preview' : positionals[0] ?? 'preview';
  const stateDir = values['state-dir'];
  if (command === 'verify') return verify(stateDir);
  if (command === 'rollback') return rollback(stateDir);
  if (command === 'check-ids') return checkIds(stateDir, fs.readFileSync(0, 'utf8'));
  if (command !== 'prepare' && command !== 'preview') throw new Error(`Unknown command ${command}`);
  const fromStdin = Boolean(values.stdin);
  const root = values.root ? fs.realpathSync(values.root) : null;
  if (!fromStdin && !root) throw new Error('Pass --root or --stdin');
  if (command === 'prepare' && (fromStdin || !stateDir)) throw new Error('prepare needs --root and --state-dir');
  const files = fromStdin ? new Map([[values['manifest-path'] ?? 'Directory.Packages.props', fs.readFileSync(0, 'utf8')]]) : collectFiles(root);
  const indexDir = values['index-dir'] ?? process.env.CRATIS_NUGET_INDEX_DIR;
  const cache = new Map();
  const getVersions = (id) => {
    const key = id.toLowerCase();
    if (!cache.has(key)) cache.set(key, fetchVersions(id, indexDir));
    return cache.get(key);
  };
  const plan = await planUpdates({ files, exclusions: parseExclusions(values.exclusions), getVersions, checkImports: !fromStdin });
  if (command === 'preview') {
    if (values.label) console.log(`Preview for ${values.label}`);
    const diff = renderDiff(files, plan.edits);
    if (diff) console.log(diff);
    if (fromStdin) console.log('Single manifest only: imports and property consumers outside it were not evaluated.');
  }
  for (const line of describeOutcomes(plan, files.size)) console.log(line);
  if (command === 'prepare') prepare(root, path.resolve(stateDir), plan, files);
}

if (process.argv[1] && import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href) {
  main(process.argv.slice(2)).catch((error) => {
    console.error(`::error::${escapeAnnotation(error.message)}`);
    process.exitCode = 1;
  });
}
