// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.
// Keep every locally pinned member of a Cratis NuGet family on one stable version.
import { spawnSync } from 'node:child_process';
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
const NUMERIC = /^\d+$/;
const NUGET_TIMEOUT_MS = 20000;
const UNRESOLVED_IMPORT = 'an MSBuild import could not be resolved to a scanned file';

// ---------------------------------------------------------------- versions

export function parseVersion(text) {
  const match = VERSION.exec(text);
  if (!match) return null;
  const numbers = match[1].split('.').map(Number);
  while (numbers.length < 4) numbers.push(0);
  return { numbers, pre: match[2] ?? null, text };
}

function compareIdentifier(a, b) {
  if (a === b) return 0;
  const aNumeric = NUMERIC.test(a);
  const bNumeric = NUMERIC.test(b);
  if (aNumeric && bNumeric) return Number(a) < Number(b) ? -1 : 1;
  if (aNumeric !== bNumeric) return aNumeric ? -1 : 1;
  return a < b ? -1 : 1;
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
    const result = compareIdentifier(a[i], b[i]);
    if (result !== 0) return result;
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

const highest = (versions) => versions.reduce((a, b) => (compareVersions(a, b) >= 0 ? a : b));

export function familyKey(id) {
  const parts = id.toLowerCase().split('.');
  if (parts[0] !== 'cratis') return null;
  return `cratis.${parts[1] ?? 'arc'}`;
}

const familyName = (id) => `Cratis.${id.split('.')[1] ?? 'Arc'}`;

// ------------------------------------------------------------ XML tokenizer

function skipPast(text, terminator, from, label) {
  const index = text.indexOf(terminator, from);
  if (index < 0) throw new Error(`Malformed XML: unterminated ${label}`);
  return index + terminator.length;
}

function skipSpace(text, at) {
  let next = at;
  while (/\s/.test(text[next] ?? '')) next++;
  return next;
}

function readAttribute(text, at, element) {
  const word = /[^\s=/>]+/y;
  word.lastIndex = at;
  const name = word.exec(text);
  if (!name) throw new Error(`Malformed XML: bad attribute in <${element.name}>`);
  const equals = skipSpace(text, at + name[0].length);
  if (text[equals] !== '=') throw new Error(`Malformed XML: attribute ${name[0]} has no value`);
  const { start, end } = readQuoted(text, skipSpace(text, equals + 1), name[0]);
  element.attrs.set(name[0].toLowerCase(), { name: name[0], value: text.slice(start, end), start, end });
  return end + 1;
}

function readQuoted(text, open, name) {
  const quote = text[open];
  if (quote !== '"' && quote !== "'") throw new Error(`Malformed XML: attribute ${name} is not quoted`);
  const close = text.indexOf(quote, open + 1);
  if (close < 0) throw new Error(`Malformed XML: unterminated attribute ${name}`);
  return { start: open + 1, end: close };
}

function readAttributes(text, start, element) {
  let at = skipSpace(text, start);
  while (text[at] !== '>' && !text.startsWith('/>', at)) {
    at = skipSpace(text, readAttribute(text, at, element));
  }
  element.selfClosing = text[at] === '/';
  return at + (element.selfClosing ? 2 : 1);
}

function readOpenTag(text, lt, parent) {
  const named = /[^\s/>]+/y;
  named.lastIndex = lt + 1;
  const name = named.exec(text);
  if (!name) throw new Error('Malformed XML: element without a name');
  const element = { name: name[0], lname: name[0].toLowerCase(), attrs: new Map(), parent, children: [], start: lt,
    selfClosing: false, hasMarkup: false };
  element.openEnd = readAttributes(text, lt + 1 + name[0].length, element);
  if (element.selfClosing) {
    element.contentEnd = element.openEnd;
    element.end = element.openEnd;
  }
  return element;
}

function readCloseTag(text, lt, top) {
  const close = skipPast(text, '>', lt + 2, 'closing tag');
  const name = text.slice(lt + 2, close - 1).trim();
  if (!top || top.name !== name) throw new Error(`Malformed XML: unexpected </${name}>`);
  top.contentEnd = lt;
  top.end = close;
  return close;
}

// Comments, CDATA, processing instructions and declarations: returns the next index, or null for a tag.
const NON_ELEMENTS = [
  { open: '<!--', close: '-->', label: 'comment', markup: true },
  { open: '<![CDATA[', close: ']]>', label: 'CDATA section', markup: true },
  { open: '<?', close: '?>', label: 'processing instruction', markup: false },
  { open: '<!', close: '>', label: 'declaration', markup: false },
];

function skipNonElement(text, lt, top) {
  const kind = NON_ELEMENTS.find((candidate) => text.startsWith(candidate.open, lt));
  if (!kind) return null;
  if (top && kind.markup) top.hasMarkup = true;
  return skipPast(text, kind.close, lt + kind.open.length, kind.label);
}

function readTag(text, lt, open) {
  const top = open.stack[open.stack.length - 1];
  if (text[lt + 1] === '/') {
    const next = readCloseTag(text, lt, top);
    open.stack.pop();
    return next;
  }
  const element = readOpenTag(text, lt, top ?? null);
  top?.children.push(element);
  open.elements.push(element);
  if (!element.selfClosing) open.stack.push(element);
  return element.openEnd;
}

// A bounded tokenizer: it records element and value spans so edits never reserialize the document.
export function parseXml(text) {
  const open = { elements: [], stack: [] };
  let at = 0;
  for (let lt = text.indexOf('<', at); lt >= 0; lt = text.indexOf('<', at)) {
    at = skipNonElement(text, lt, open.stack[open.stack.length - 1]) ?? readTag(text, lt, open);
  }
  if (open.stack.length > 0) throw new Error(`Malformed XML: <${open.stack[open.stack.length - 1].name}> is not closed`);
  return open.elements;
}

// ------------------------------------------------------------ manifest model

function textValue(text, element) {
  const hasContent = !element.selfClosing && element.children.length === 0 && !element.hasMarkup;
  if (!hasContent) return null;
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

function attributeSource(element, key) {
  const attribute = element.attrs.get(key);
  if (!attribute) return [];
  return [{ kind: `attribute ${attribute.name}`, raw: attribute.value, start: attribute.start, end: attribute.end,
    conditional: isConditional(element) }];
}

function childSource(text, child) {
  const value = textValue(text, child);
  return { kind: `element ${child.name}`, raw: value?.value ?? null, start: value?.start, end: value?.end,
    conditional: isConditional(child) };
}

function versionSources(text, element) {
  const children = element.children.filter((c) => c.lname === 'version' || c.lname === 'versionoverride');
  return [...attributeSource(element, 'version'), ...attributeSource(element, 'versionoverride'),
    ...children.map((child) => childSource(text, child))];
}

const isCratisPackageId = (id) => Boolean(id) && PACKAGE_ID.test(id) && familyKey(id) !== null;

function packageDeclarations(file, text, element) {
  const id = (element.attrs.get('include') ?? element.attrs.get('update'))?.value;
  if (!isCratisPackageId(id)) return [];
  // An Update item overrides another declaration, so it is never a plain pin.
  const override = element.attrs.has('update') && !element.attrs.has('include');
  return versionSources(text, element).map((source) => ({ file, id, ...source, conditional: source.conditional || override }));
}

function analyzeFile(file, text) {
  const result = { file, text, properties: [], declarations: [], imports: [] };
  for (const element of parseXml(text)) {
    if (element.parent?.lname === 'propertygroup') {
      result.properties.push({ name: element.lname, file, value: textValue(text, element), conditional: isConditional(element) });
    } else if (element.lname === 'import') {
      result.imports.push({ project: element.attrs.get('project')?.value ?? null, sdk: element.attrs.has('sdk'),
        conditional: isConditional(element) });
    } else if (element.lname === 'packageversion' || element.lname === 'packagereference') {
      result.declarations.push(...packageDeclarations(file, text, element));
    }
  }
  return result;
}

const DIRECTORY_PROPERTY = /\$\((?:MSBuildThisFileDirectory|MSBuildProjectDirectory)\)[\\/]?/gi;

// The scanned file an import points at, or null when it is an SDK import or cannot be resolved.
function importTarget(analysis, entry, files) {
  if (entry.sdk) return null;
  const project = (entry.project ?? '$(').replace(DIRECTORY_PROPERTY, '');
  if (project.includes('$(')) return null;
  const target = path.posix.normalize(path.posix.join(path.posix.dirname(analysis.file), project.replaceAll('\\', '/')));
  return files.has(target) ? target : null;
}

const hasUnresolvedImport = (analyses, files) => analyses.some((analysis) =>
  analysis.imports.some((entry) => !entry.sdk && importTarget(analysis, entry, files) === null));

const importedFiles = (analysis, files, onlyConditional) => analysis.imports
  .filter((entry) => entry.conditional || !onlyConditional)
  .map((entry) => importTarget(analysis, entry, files))
  .filter(Boolean);

function importClosure(analyses, files, roots) {
  const byFile = new Map(analyses.map((analysis) => [analysis.file, analysis]));
  const pending = [...roots];
  const seen = new Set();
  while (pending.length > 0) {
    const file = pending.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    pending.push(...importedFiles(byFile.get(file), files, false));
  }
  return seen;
}

// Everything a conditional import brings in is conditional, transitively.
function markConditionalImports(analyses, files) {
  const roots = analyses.flatMap((analysis) => importedFiles(analysis, files, true));
  const seen = importClosure(analyses, files, roots);
  for (const analysis of analyses.filter((entry) => seen.has(entry.file))) {
    analysis.declarations.forEach((d) => { d.conditional = true; });
    analysis.properties.forEach((p) => { p.conditional = true; });
  }
  return seen;
}

function countUses(analyses, name) {
  const pattern = new RegExp(`\\$\\(${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\)`, 'gi');
  return analyses.reduce((total, analysis) => total + (analysis.text.match(pattern)?.length ?? 0), 0);
}

// ----------------------------------------------------------------- planning

const deferral = (reason) => ({ reason });

function resolveProperty(declaration, name, context) {
  const definitions = context.properties.get(name.toLowerCase()) ?? [];
  if (context.unresolvedImports) return deferral(`$(${name}) cannot be proven: ${UNRESOLVED_IMPORT}`);
  if (definitions.length !== 1) return deferral(`$(${name}) has ${definitions.length} definitions, not exactly one`);
  const [definition] = definitions;
  const plain = definition.value && parseVersion(definition.value.value);
  if (definition.conditional || !plain) return deferral(`$(${name}) is conditional or not a plain version`);
  return { version: definition.value.value, property: name.toLowerCase(),
    edit: { file: definition.file, start: definition.value.start, end: definition.value.end, before: definition.value.value } };
}

function resolveDeclaration(declaration, context) {
  const { raw } = declaration;
  if (declaration.conditional) return deferral(`${declaration.id} has a conditional version in ${declaration.file}`);
  if (raw === null) return deferral(`${declaration.id} has a version that is not plain text in ${declaration.file}`);
  if (parseVersion(raw)) {
    return { version: raw, edit: { file: declaration.file, start: declaration.start, end: declaration.end, before: raw } };
  }
  const property = PROPERTY_REFERENCE.exec(raw);
  if (!property) return deferral(`${declaration.id} uses the unsupported version '${raw}' in ${declaration.file}`);
  return resolveProperty(declaration, property[1], context);
}

function stableCandidates(versions) {
  const candidates = new Map();
  for (const text of versions) {
    const version = typeof text === 'string' ? parseVersion(text) : null;
    if (version && version.pre === null) candidates.set(versionKey(version), version);
  }
  return candidates;
}

function resolveFamilyPins(family, context) {
  const effective = [];
  for (const declaration of family.declarations) {
    const resolved = resolveDeclaration(declaration, context);
    if (resolved.reason) return resolved;
    const uses = resolved.property
      ? family.declarations.filter((d) => d.raw?.toLowerCase() === `$(${resolved.property})`).length : 0;
    if (resolved.property && countUses(context.analyses, resolved.property) !== uses) {
      return deferral(`$(${resolved.property}) is used outside this family's package versions`);
    }
    effective.push(resolved);
  }
  return { effective };
}

async function commonStableVersions(members, getVersions) {
  let common = null;
  for (const id of members) {
    const stable = stableCandidates(await getVersions(id));
    common = common === null ? stable : new Map([...common].filter(([key]) => stable.has(key)));
  }
  return [...common.values()];
}

function editsTowards(effective, target) {
  const edits = new Map();
  for (const resolved of effective) {
    if (compareVersions(parseVersion(resolved.version), target) === 0) continue;
    edits.set(`${resolved.edit.file}:${resolved.edit.start}`, { ...resolved.edit, text: target.text });
  }
  return [...edits.values()];
}

async function planFamily(family, context) {
  const members = [...family.members.keys()];
  const pins = resolveFamilyPins(family, context);
  if (pins.reason) return { outcome: 'deferred', reason: pins.reason, members };
  const floor = highest(pins.effective.map((e) => parseVersion(e.version)));
  const eligible = (await commonStableVersions(members, context.getVersions)).filter((v) => compareVersions(v, floor) >= 0);
  if (eligible.length === 0) {
    return { outcome: 'deferred', reason: `no stable version at or above ${floor.text} exists for every member`, members };
  }
  const target = highest(eligible);
  const edits = editsTowards(pins.effective, target);
  return { outcome: edits.length > 0 ? 'updated' : 'unchanged', target: target.text, members, edits };
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

function analyzeAll(files) {
  return [...files].map(([file, text]) => {
    try {
      return analyzeFile(file, text);
    } catch (error) {
      throw new Error(`${file}: ${error.message}`);
    }
  });
}

function collectProperties(analyses) {
  const properties = new Map();
  for (const property of analyses.flatMap((a) => a.properties)) {
    properties.set(property.name, [...(properties.get(property.name) ?? []), property]);
  }
  return properties;
}

function groupFamilies(analyses) {
  const families = new Map();
  for (const declaration of analyses.flatMap((a) => a.declarations)) {
    const key = familyKey(declaration.id);
    const family = families.get(key) ?? { key, name: familyName(declaration.id), declarations: [], members: new Map() };
    family.declarations.push(declaration);
    if (!family.members.has(declaration.id.toLowerCase())) family.members.set(declaration.id.toLowerCase(), declaration.id);
    families.set(key, family);
  }
  return [...families.values()].sort((a, b) => a.key.localeCompare(b.key));
}

export async function planUpdates({ files, exclusions = [], getVersions, checkImports = true }) {
  const analyses = analyzeAll(files);
  markConditionalImports(analyses, files);
  const context = { analyses, properties: collectProperties(analyses), getVersions,
    unresolvedImports: checkImports && hasUnresolvedImport(analyses, files) };
  const held = new Set(exclusions.map(familyKey).filter(Boolean));
  const results = [];
  const edits = new Map();
  for (const family of groupFamilies(analyses)) {
    const members = [...family.members.values()];
    const planned = held.has(family.key)
      ? { outcome: 'held', reason: 'a member is excluded' } : await planFamily(family, context);
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

// ------------------------------------------------------------------ output

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

function describeFamily(family) {
  const members = family.members.join(', ');
  if (family.outcome === 'deferred') {
    return `::warning::Cratis NuGet family ${family.name} was left unchanged: ${escapeAnnotation(family.reason)} (${escapeAnnotation(members)})`;
  }
  if (family.outcome === 'held') {
    return `::notice::Cratis NuGet family ${family.name} is held: ${family.reason} (${escapeAnnotation(members)})`;
  }
  return `Cratis NuGet family ${family.name}: ${family.outcome}${family.target ? ` at ${family.target}` : ''} (${members})`;
}

export function describeOutcomes(plan, manifestCount) {
  const count = (outcome) => plan.families.filter((f) => f.outcome === outcome).length;
  const summary = plan.families.length === 0
    ? `No Cratis NuGet families found in ${manifestCount} manifest file(s).`
    : `Cratis NuGet families: ${plan.families.length} found; ${count('updated')} updated, ${count('unchanged')} unchanged, ` +
      `${count('deferred')} deferred, ${count('held')} held.`;
  return [...plan.families.map(describeFamily), summary];
}

// ----------------------------------------------------------- package index

function readFixtureIndex(id, indexDir) {
  const file = path.join(indexDir, `${id.toLowerCase()}.json`);
  if (!fs.existsSync(file)) throw new Error(`No package index fixture for ${id}`);
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

async function readNuGetIndex(id) {
  const response = await fetch(`https://api.nuget.org/v3-flatcontainer/${id.toLowerCase()}/index.json`,
    { signal: AbortSignal.timeout(NUGET_TIMEOUT_MS), headers: { accept: 'application/json' } });
  if (!response.ok) throw new Error(`NuGet index for ${id} answered HTTP ${response.status}`);
  return response.json();
}

const isVersionList = (body) => Array.isArray(body?.versions) && body.versions.length > 0 && body.versions.every((v) => typeof v === 'string');

async function fetchVersions(id, indexDir) {
  const body = indexDir ? readFixtureIndex(id, indexDir) : await readNuGetIndex(id);
  if (!isVersionList(body)) throw new Error(`NuGet index for ${id} is missing or malformed`);
  return body.versions;
}

// ------------------------------------------------------- files and state

const isManifest = (name) => MANIFEST_EXTENSIONS.includes(path.extname(name).toLowerCase());

function readManifest(files, top, full) {
  if (!fs.realpathSync(full).startsWith(top + path.sep)) throw new Error(`Refusing manifest outside the checkout: ${full}`);
  files.set(path.relative(top, full).split(path.sep).join('/'), fs.readFileSync(full, 'utf8'));
}

function visitEntry(scan, directory, entry) {
  const { files, top, include } = scan;
  const full = path.join(directory, entry.name);
  const manifest = include(entry.name);
  if (entry.isSymbolicLink() && manifest) throw new Error(`Refusing symbolic link manifest ${path.relative(top, full)}`);
  if (entry.isDirectory() && !SKIPPED_DIRECTORIES.has(entry.name)) walk(scan, full);
  if (entry.isFile() && manifest) readManifest(files, top, full);
}

function walk(scan, directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) visitEntry(scan, directory, entry);
}

function collectFiles(root, include = isManifest) {
  const files = new Map();
  const top = fs.realpathSync(root);
  walk({ files, top, include }, top);
  return files;
}

const sha = (text) => createHash('sha256').update(text).digest('hex');

function writeChecked(root, file, expected, next) {
  const target = path.join(root, file);
  if (fs.readFileSync(target, 'utf8') !== expected) throw new Error(`${file} changed while preparing the update`);
  fs.writeFileSync(target, next);
}

function writeEdits(root, changed, files, after) {
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
  writeEdits(root, changed, files, after);
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

function matchesPrepared(state, current) {
  const names = Object.keys(state.hashes);
  const sameFiles = current.size === names.length && names.every((file) => current.has(file));
  return sameFiles && names.every((file) => sha(current.get(file)) === state.hashes[file]);
}

function rollback(stateDir) {
  const state = readState(stateDir);
  if (!matchesPrepared(state, collectFiles(state.root))) {
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
  for (const id of input.split('\n').map((line) => line.trim()).filter(Boolean)) {
    const key = familyKey(id);
    if (!key || known.has(id.toLowerCase())) continue;
    if (state.edited.includes(key)) {
      console.error(`::error::${escapeAnnotation(id)} belongs to an edited Cratis family but is not pinned in the scanned manifests`);
      process.exitCode = 4;
    }
    console.log(id);
  }
}

// ---------------------------------------------------------- restore repair

function requiredDowngradeVersion(id, required, current) {
  const version = parseVersion(required);
  const pinned = parseVersion(current);
  const message = `Unsupported NU1109 versions for ${id}: ${required} / ${current}`;
  if (!version || !pinned) throw new Error(message);
  if (compareVersions(version, pinned) <= 0) throw new Error(message);
  return version;
}

function downgradeRequirements(output) {
  const requirements = new Map();
  const pattern = /\bNU1109: Detected package downgrade: ([A-Za-z0-9_.-]+) from (\S+) to centrally defined (\S+)\./g;
  for (const [, id, required, current] of output.matchAll(pattern)) {
    const version = requiredDowngradeVersion(id, required, current);
    const previous = requirements.get(id.toLowerCase());
    if (previous && previous.current !== current) throw new Error(`Conflicting NU1109 centrally defined versions for ${id}: ${previous.current} / ${current}`);
    if (!previous || compareVersions(version, previous.version) > 0) requirements.set(id.toLowerCase(), { id, version, current });
  }
  return [...requirements.values()];
}

function literalPinSource(element, text, message) {
  const sources = versionSources(text, element);
  if (element.attrs.has('update')) throw new Error(message);
  if (sources.length !== 1) throw new Error(message);
  const [source] = sources;
  if (source.conditional) throw new Error(message);
  if (!parseVersion(source.raw)) throw new Error(message);
  return source;
}

function centralPinEdit(candidate, requirement, message) {
  const { element, text } = candidate;
  const source = literalPinSource(element, text, message);
  const id = element.attrs.get('include').value;
  if (source.raw !== requirement.current) {
    throw new Error(`Cannot safely raise central pin ${id}: NuGet reported centrally defined version ${requirement.current}, but candidate ${candidate.file} has literal Version ${source.raw}`);
  }
  if (compareVersions(parseVersion(source.raw), requirement.version) >= 0) return null;
  return { ...source, id, before: source.raw, text: requirement.version.text };
}

function centralPinCandidates(file, text, id) {
  return parseXml(text)
    .filter((element) => element.lname === 'packageversion')
    .filter((element) => (element.attrs.get('include') ?? element.attrs.get('update'))?.value.toLowerCase() === id.toLowerCase())
    .map((element) => ({ file, text, element }));
}

function centralRepairCandidate(files, id) {
  const analyses = analyzeAll(files);
  const conditional = markConditionalImports(analyses, files);
  const roots = analyses.filter((entry) => path.posix.basename(entry.file).toLowerCase() === 'directory.packages.props');
  const scope = importClosure(analyses, files, roots.map((entry) => entry.file));
  const matches = analyses.filter((entry) => scope.has(entry.file))
    .flatMap((entry) => centralPinCandidates(entry.file, entry.text, id));
  const candidates = matches.filter(({ element }) => !element.attrs.has('update') && !isConditional(element));
  const names = [...new Set(matches.map((entry) => entry.file))].sort().join(', ') || '(none)';
  const message = `Cannot safely raise central pin ${id}: expected exactly one unconditional literal PackageVersion pin outside conditional imports; candidate files: ${names}`;
  if (candidates.length !== 1) throw new Error(message);
  const [candidate] = candidates;
  if (conditional.has(candidate.file)) throw new Error(message);
  if (hasUnresolvedImport(analyses, files)) throw new Error(`${message}; imports could not be resolved`);
  return { candidate, message };
}

function centralRepairsFor(files, requirement) {
  const { candidate, message } = centralRepairCandidate(files, requirement.id);
  const edit = centralPinEdit(candidate, requirement, message);
  return edit ? [[candidate.file, [edit]]] : [];
}

function planCentralRepairs(files, requirements, exclusions) {
  const edits = new Map();
  const excluded = new Set(exclusions.map((id) => id.toLowerCase()));
  for (const requirement of requirements) {
    const { id, version } = requirement;
    if (excluded.has(id.toLowerCase())) throw new Error(`NU1109 requires ${id} ${version.text}, but the package is excluded`);
    if (familyKey(id)) throw new Error(`NU1109 requires ${id} ${version.text}; cannot raise a Cratis family member independently`);
    const changes = centralRepairsFor(files, requirement);
    if (changes.length === 0) throw new Error(`NU1109 requires ${id} ${version.text}, but no repairable central pin was found`);
    for (const [file, next] of changes) edits.set(file, [...(edits.get(file) ?? []), ...next]);
  }
  return edits;
}

function rememberBeforeImage(stateDir, state, file, text) {
  if (state.changed[file]) return;
  state.changed[file] = `before/${Object.keys(state.changed).length}`;
  fs.writeFileSync(path.join(stateDir, state.changed[file]), text);
}

function saveCentralRepairs(stateDir, state, files, edits) {
  const after = new Map(files);
  for (const [file, changes] of edits) {
    after.set(file, applyEdits(files.get(file), changes));
    rememberBeforeImage(stateDir, state, file, files.get(file));
  }
  writeEdits(state.root, Object.fromEntries([...edits.keys()].map((file) => [file, true])), files, after);
  state.hashes = Object.fromEntries([...after].map(([file, text]) => [file, sha(text)]));
  fs.writeFileSync(path.join(stateDir, 'state.json'), JSON.stringify(state, null, 2));
  for (const [file, changes] of edits) {
    for (const change of changes) console.log(`Raised central NuGet pin ${change.id} in ${file}: ${change.before} -> ${change.text}`);
  }
}

function restoreOutput(root) {
  const result = spawnSync('dotnet', ['restore'], { cwd: root, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, DOTNET_CLI_UI_LANGUAGE: 'en-US' } });
  if (result.status === 0) return null;
  const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;
  console.error(output);
  if (result.error) throw result.error;
  return output;
}

function restoreWithRepairs(stateDir, exclusions) {
  const state = readState(stateDir);
  if (state.edited.length === 0) return;
  const maxRepairs = 3;
  for (let attempt = 0; attempt <= maxRepairs; attempt++) {
    const files = collectFiles(state.root);
    if (!matchesPrepared(state, files)) throw new Error('Manifests changed before central pin repair; refusing to continue');
    const output = restoreOutput(state.root);
    if (output === null) return;
    if (attempt === maxRepairs) throw new Error(`dotnet restore failed after ${maxRepairs} central pin repair attempts`);
    const requirements = downgradeRequirements(output);
    if (requirements.length === 0) throw new Error('dotnet restore failed after Cratis family prepare; see NuGet diagnostics above');
    const edits = planCentralRepairs(files, requirements, exclusions);
    saveCentralRepairs(stateDir, state, files, edits);
  }
}

// ------------------------------------------------------ SDK prune and retry

const MAX_UPDATE_ATTEMPTS = 4;

function conflictPackage(block) {
  const match = /\bNU1107: Version conflict detected for ([A-Za-z0-9_.-]+)\./.exec(block) ??
    /\bNU1109: Detected package downgrade: ([A-Za-z0-9_.-]+) from /.exec(block);
  if (!match) throw new Error('Cannot attribute SDK restore error; refusing to prune');
  return match[1];
}

function selectedNode(node, selected) {
  // Only concrete package/version nodes, never the project name or a central pin.
  const match = /^([A-Za-z0-9_.-]+)\s+(\S+)$/.exec(node.trim());
  if (!match || !parseVersion(match[2])) return null;
  return selected.get(match[1].toLowerCase());
}

function selectedAncestors(block, conflict, selected) {
  const chains = block.split('\n').map((line) => line.split(' -> '));
  const matches = chains.filter((chain) => {
    const leaf = /^([A-Za-z0-9_.-]+)\s+\(/.exec(chain.at(-1).trim());
    return leaf?.[1].toLowerCase() === conflict.toLowerCase();
  });
  return matches.flatMap((chain) => chain.slice(1, -1).map((node) => selectedNode(node, selected))).filter(Boolean);
}

function conflictBlocks(output) {
  const errors = output.split('\n').filter((line) => !/\bwarning:?\s+NU\d{4}:/i.test(line)).join('\n');
  if ([...errors.matchAll(/\berror:?\s+(NU\d{4}):/gi)].some((match) => !['NU1107', 'NU1109'].includes(match[1].toUpperCase()))) {
    throw new Error('Cannot attribute SDK restore error; refusing to prune');
  }
  const headers = [...errors.matchAll(/\berror:?\s+(NU1107): Version conflict detected for |\berror:?\s+(NU1109): Detected package downgrade: /g)];
  if (headers.length === 0) throw new Error('No attributable NU1107/NU1109 errors in SDK output');
  return headers.map((header, index) => ({ code: header[1] ?? header[2],
    text: errors.slice(header.index, headers[index + 1]?.index) }));
}

function literalPackagePin(text, element) {
  if (!['packagereference', 'packageversion'].includes(element.lname)) return null;
  const id = (element.attrs.get('include') ?? element.attrs.get('update'))?.value;
  if (!id || !PACKAGE_ID.test(id)) return null;
  const sources = versionSources(text, element);
  const literal = sources.length === 1 && !sources[0].conditional && !element.attrs.has('update');
  return { id: id.toLowerCase(), kind: element.lname, version: literal ? parseVersion(sources[0].raw) : null };
}

function literalPins(text) {
  const pins = parseXml(text).map((element) => literalPackagePin(text, element)).filter(Boolean);
  const unique = new Map();
  for (const pin of pins) unique.set(pin.id, unique.has(pin.id) ? null : pin);
  return unique;
}

function filePackageDelta(before, after) {
  const previous = literalPins(before);
  const changed = [...literalPins(after).values()].filter(Boolean);
  return changed.filter((pin) => {
    const old = previous.get(pin.id);
    return old?.version && pin.version && old.kind === pin.kind && compareVersions(old.version, pin.version) < 0;
  });
}

function attemptPackageDelta(before, after) {
  return [...before].filter(([file, text]) => isManifest(file) && after.has(file) && after.get(file) !== text)
    .flatMap(([file, text]) => filePackageDelta(text, after.get(file)));
}

function deltaLeaf(block, conflict, context) {
  const id = context.selected.get(conflict.toLowerCase());
  if (!id) return [];
  const escaped = conflict.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const requirements = [...block.text.matchAll(new RegExp(` -> ${escaped} \\(\\s*(?:>=|=)\\s+(\\S+?)\\s*\\)`, 'gi'))]
    .map((match) => parseVersion(match[1])).filter(Boolean).map(versionKey);
  const downgrade = /\bNU1109: .* from (\S+) to /.exec(block.text);
  const required = downgrade ? requirements.filter((version) => version === versionKey(parseVersion(downgrade[1]))) : requirements;
  const proven = context.delta.some((pin) => pin.id === id.toLowerCase() && required.includes(versionKey(pin.version)));
  return proven ? [id] : [];
}

function matchesPreviewVersions(requirement, versions) {
  const target = parseVersion(versions.target ?? '');
  const current = parseVersion(versions.current ?? '');
  return target && current && compareVersions(requirement.version, target) === 0 &&
    compareVersions(parseVersion(requirement.current), current) === 0;
}

function previewLeaf(block, conflict, context) {
  if (block.code !== 'NU1109') return [];
  const id = context.selected.get(conflict.toLowerCase());
  const versions = context.versions.filter((entry) => entry.id.toLowerCase() === conflict.toLowerCase());
  if (!id || versions.length === 0) return [];
  const [requirement] = downgradeRequirements(block.text);
  const matches = requirement && versions.every((entry) => matchesPreviewVersions(requirement, entry));
  return matches ? [id] : [];
}

function conflictLeaves(block, conflict, context) {
  const proven = deltaLeaf(block, conflict, context);
  return proven.length > 0 ? proven : previewLeaf(block, conflict, context);
}

function conflictReasons(block, context) {
  const conflict = conflictPackage(block.text);
  const ancestors = selectedAncestors(block.text, conflict, context.selected);
  const ids = ancestors.length > 0 ? ancestors : conflictLeaves(block, conflict, context);
  if (ids.length === 0) throw new Error(`Cannot attribute ${block.code} for ${conflict} to a selected package`);
  return ids.map((id) => [id, `${block.code} conflicts with ${conflict}`]);
}

export function pruneConflicts(output, packages, delta = [], versions = []) {
  const selected = new Map(packages.map((id) => [id.toLowerCase(), id]));
  return new Map(conflictBlocks(output).flatMap((block) => conflictReasons(block, { selected, delta, versions })));
}

const isSdkFile = (name) => isManifest(name) || name.toLowerCase() === 'packages.lock.json';

function undoSdkAttempt(root, before) {
  const current = collectFiles(root, isSdkFile);
  for (const file of current.keys()) {
    if (!before.has(file)) fs.unlinkSync(path.join(root, file));
  }
  for (const [file, text] of before) fs.writeFileSync(path.join(root, file), text);
}

function sdkUpdate(root, packages) {
  const result = spawnSync('dotnet', ['package', 'update', ...packages], { cwd: root, encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024, env: { ...process.env, DOTNET_CLI_UI_LANGUAGE: 'en-US' } });
  if (result.error) throw result.error;
  return { status: result.status, output: `${result.stdout ?? ''}${result.stderr ?? ''}` };
}

function sdkAttempt(options, before, packages) {
  try {
    const result = options.run(options.root, packages);
    options.log(result.output);
    const delta = result.status === 3 ? attemptPackageDelta(before, collectFiles(options.root)) : [];
    if (result.status === 0) options.validate();
    else undoSdkAttempt(options.root, before);
    return { ...result, delta };
  } catch (error) {
    undoSdkAttempt(options.root, before);
    throw error;
  }
}

function unfilteredNoop(result, unfiltered) {
  if (!unfiltered || result.status !== 2) return false;
  return result.output.includes('All packages are up to date');
}

function requireRetryable(result, attempt) {
  if (result.status !== 3) throw new Error(`dotnet package update failed (exit ${result.status})`);
  if (attempt === MAX_UPDATE_ATTEMPTS) throw new Error(`NuGet SDK update exhausted ${MAX_UPDATE_ATTEMPTS} attempts`);
}

function selectionVersions(files, targets) {
  return targets.map(({ id, version }) => {
    try {
      const { candidate, message } = centralRepairCandidate(files, id);
      const current = literalPinSource(candidate.element, candidate.text, message).raw;
      return { id, current, target: version };
    } catch {
      return { id, current: null, target: version };
    }
  });
}

function pruneSelection(result, selected, versions, log) {
  const reasons = pruneConflicts(result.output, selected, result.delta, versions);
  for (const [id, reason] of reasons) log(`::notice::Pruned NuGet package '${escapeAnnotation(id)}': ${escapeAnnotation(reason)}`);
  const remaining = selected.filter((id) => !reasons.has(id));
  if (remaining.length === 0) throw new Error('No eligible NuGet packages remain after conflict pruning');
  return remaining;
}

export function updateWithPruning({ root, packages, targets = [], unfiltered = false, run = sdkUpdate, validate = () => {}, log = console.log }) {
  const before = collectFiles(root, isSdkFile);
  const versions = selectionVersions(before, targets);
  const options = { root, run, validate, log };
  let selected = [...packages];
  let attemptIds = unfiltered ? [] : selected;
  for (let attempt = 1; attempt <= MAX_UPDATE_ATTEMPTS; attempt++) {
    log(`NuGet SDK update attempt ${attempt}/${MAX_UPDATE_ATTEMPTS}`);
    const result = sdkAttempt(options, before, attemptIds);
    if (result.status === 0) return;
    if (unfilteredNoop(result, unfiltered)) return;
    requireRetryable(result, attempt);
    selected = pruneSelection(result, selected, versions, log);
    attemptIds = selected;
  }
}

function runSdkUpdate(values, packages) {
  if (packages.some((id) => !PACKAGE_ID.test(id))) throw new Error('Invalid selected NuGet package ID');
  const stateDir = values['state-dir'];
  const state = readState(stateDir);
  if (!matchesPrepared(state, collectFiles(state.root))) throw new Error('Manifests changed before SDK update');
  const targets = values.targets ? JSON.parse(fs.readFileSync(values.targets, 'utf8')) : [];
  updateWithPruning({ root: state.root, packages, targets, unfiltered: values.unfiltered, validate: () => verify(stateDir) });
}

// ----------------------------------------------------------------- commands

function loadInput(values) {
  if (values.stdin) {
    const files = new Map([[values['manifest-path'] ?? 'Directory.Packages.props', fs.readFileSync(0, 'utf8')]]);
    return { files, root: null, fromStdin: true };
  }
  if (!values.root) throw new Error('Pass --root or --stdin');
  const root = fs.realpathSync(values.root);
  return { files: collectFiles(root), root, fromStdin: false };
}

function versionLookup(values) {
  const indexDir = values['index-dir'] ?? process.env.CRATIS_NUGET_INDEX_DIR;
  const cache = new Map();
  return (id) => {
    const key = id.toLowerCase();
    if (!cache.has(key)) cache.set(key, fetchVersions(id, indexDir));
    return cache.get(key);
  };
}

async function planFromInput(values) {
  const input = loadInput(values);
  const plan = await planUpdates({ files: input.files, exclusions: parseExclusions(values.exclusions),
    getVersions: versionLookup(values), checkImports: !input.fromStdin });
  return { ...input, plan };
}

async function runPreview(values) {
  const { files, plan, fromStdin } = await planFromInput(values);
  if (values.label) console.log(`Preview for ${values.label}`);
  const diff = renderDiff(files, plan.edits);
  if (diff) console.log(diff);
  if (fromStdin) console.log('Single manifest only: imports and property consumers outside it were not evaluated.');
  for (const line of describeOutcomes(plan, files.size)) console.log(line);
}

async function runPrepare(values) {
  if (values.stdin || !values['state-dir']) throw new Error('prepare needs --root and --state-dir');
  const { files, plan, root } = await planFromInput(values);
  for (const line of describeOutcomes(plan, files.size)) console.log(line);
  prepare(root, path.resolve(values['state-dir']), plan, files);
}

const COMMANDS = {
  preview: runPreview,
  prepare: runPrepare,
  restore: (values) => restoreWithRepairs(values['state-dir'], parseExclusions(values.exclusions)),
  verify: (values) => verify(values['state-dir']),
  rollback: (values) => rollback(values['state-dir']),
  'check-ids': (values) => checkIds(values['state-dir'], fs.readFileSync(0, 'utf8')),
};

async function main(argv) {
  const { values, positionals } = parseArgs({ args: argv, allowPositionals: true, options: {
    'dry-run': { type: 'boolean' }, root: { type: 'string' }, stdin: { type: 'boolean' }, 'manifest-path': { type: 'string' },
    label: { type: 'string' }, exclusions: { type: 'string' }, 'state-dir': { type: 'string' }, 'index-dir': { type: 'string' },
    unfiltered: { type: 'boolean' }, targets: { type: 'string' } } });
  const name = values['dry-run'] ? 'preview' : positionals[0] ?? 'preview';
  if (name === 'update') return runSdkUpdate(values, positionals.slice(1));
  const command = COMMANDS[name];
  if (!command) throw new Error(`Unknown command ${name}`);
  await command(values);
}

if (process.argv[1] && import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href) {
  main(process.argv.slice(2)).catch((error) => {
    console.error(`::error::${escapeAnnotation(error.message)}`);
    process.exitCode = 1;
  });
}
