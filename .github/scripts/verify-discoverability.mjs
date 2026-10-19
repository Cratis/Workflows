// Copyright (c) Cratis. All rights reserved.
// Licensed under the MIT license. See LICENSE file in the project root for full license information.

// Checks a built static site for search (SEO), answer-engine (AEO) and generative (GEO) discoverability.
// Node built-ins only. Usage:
//   node verify-discoverability.mjs --root <dir> --base-url <url> [--fail-on none|error] [--ignore <glob,glob>]
// Exit codes: 0 no failing findings, 1 errors with --fail-on error, 2 could not run.
import { existsSync, readFileSync, readdirSync, statSync, appendFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const maxTitle = 60;
const maxDescription = 160;
const maxSummaryRows = 200;

class UsageError extends Error { }

function parseArguments(argv) {
    const options = { root: undefined, baseUrl: undefined, failOn: 'none', ignore: [] };
    for (let index = 0; index < argv.length; index += 2) {
        const [name, value] = [argv[index], argv[index + 1]];
        if (value === undefined) throw new UsageError(`${name} needs a value`);
        if (name === '--root') options.root = value;
        else if (name === '--base-url') options.baseUrl = value;
        else if (name === '--fail-on') options.failOn = value;
        else if (name === '--ignore') options.ignore = value.split(/[,\n]/).map(part => part.trim()).filter(Boolean);
        else throw new UsageError(`unknown argument ${name}`);
    }
    if (!options.root || !existsSync(options.root) || !statSync(options.root).isDirectory()) throw new UsageError(`--root must be an existing directory (got ${options.root})`);
    if (!['none', 'error'].includes(options.failOn)) throw new UsageError(`--fail-on must be none or error (got ${options.failOn})`);
    try {
        options.base = new URL(options.baseUrl);
    } catch {
        throw new UsageError(`--base-url must be an absolute URL (got ${options.baseUrl})`);
    }
    return options;
}

// Glob: ** crosses directories, * and ? stay within one path segment. Patterns match the path relative to the root.
function globToRegExp(glob) {
    let source = '';
    for (let index = 0; index < glob.length; index++) {
        const character = glob[index];
        if (character === '*' && glob[index + 1] === '*') {
            index++;
            if (glob[index + 1] === '/') { index++; source += '(?:.*/)?'; } else source += '.*';
        } else if (character === '*') source += '[^/]*';
        else if (character === '?') source += '[^/]';
        else source += character.replace(/[.+^${}()|[\]\\]/g, '\\$&');
    }
    return new RegExp(`^${source}$`);
}

// Attribute values may be double-quoted, single-quoted or unquoted (minified HTML).
const attribute = (tag, name) => tag.match(new RegExp(`(?<=[\\s"'/])${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\`]+))`, 'i'))?.slice(1).find(value => value !== undefined);
const isFile = path => { try { return statSync(path).isFile(); } catch { return false; } };
const isDirectory = path => { try { return statSync(path).isDirectory(); } catch { return false; } };
// Reads a file; an unreadable one becomes a finding instead of a crash.
const readText = (path, file, add) => {
    try { return readFileSync(path, 'utf8'); } catch (error) { add('error', file, `cannot read file: ${error.code ?? error.message}`); return undefined; }
};
const tagsOf = (html, name) => [...html.matchAll(new RegExp(`<${name}\\b[^>]*>`, 'gi'))].map(match => match[0]);
const metaContent = (metas, key, value) => metas.filter(tag => attribute(tag, key)?.toLowerCase() === value).map(tag => attribute(tag, 'content') ?? '');

function* htmlFiles(directory, root, ignored, add) {
    let names;
    try { names = readdirSync(directory).sort(); } catch (error) { add('error', relative(root, directory) || '.', `cannot read directory: ${error.code ?? error.message}`); return; }
    for (const name of names) {
        const path = join(directory, name);
        const relativePath = relative(root, path).split(sep).join('/');
        if (ignored.some(pattern => pattern.test(relativePath))) continue;
        if (isDirectory(path)) yield* htmlFiles(path, root, ignored, add);
        else if (name.endsWith('.html')) yield relativePath;
    }
}

// Where a published path lives in the built output.
const candidates = path => {
    const clean = decodeURIComponent(path).replace(/^\/+/, '');
    if (clean === '') return ['index.html'];
    return clean.endsWith('/') ? [`${clean}index.html`] : [`${clean}.html`, `${clean}/index.html`, clean];
};

function checkPage(relativePath, html, base) {
    const findings = [];
    const add = (level, message) => findings.push({ level, file: relativePath, message });
    const metas = tagsOf(html, 'meta');
    if (metaContent(metas, 'name', 'robots').some(content => /noindex/i.test(content))) return { findings, indexable: false };
    if (/http-equiv\s*=\s*["']?refresh/i.test(html)) return { findings, indexable: false };

    if (!attribute(html.match(/<html\b[^>]*>/i)?.[0] ?? '', 'lang')) add('error', 'missing <html lang>');
    // Only <title> elements of the document head count; inline <svg> titles do not.
    const head = (html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1] ?? html.split(/<body\b/i)[0]).replace(/<svg\b[\s\S]*?<\/svg>/gi, '');
    const titles = [...head.matchAll(/<title\b[^>]*>([\s\S]*?)<\/title>/gi)].map(match => match[1].trim());
    if (titles.length === 0 || !titles[0]) add('error', 'missing or empty <title>');
    else if (titles.length > 1) add('error', `${titles.length} <title> elements; expected one`);
    else if ([...titles[0]].length > maxTitle) add('warning', `title is ${[...titles[0]].length} characters; keep it within ${maxTitle}`);

    const [description] = metaContent(metas, 'name', 'description');
    if (!description) add('warning', 'missing meta description');
    else if ([...description].length > maxDescription) add('warning', `meta description is ${[...description].length} characters; keep it within ${maxDescription}`);

    const canonical = tagsOf(html, 'link').find(tag => attribute(tag, 'rel')?.toLowerCase().split(/\s+/).includes('canonical'));
    const href = canonical && attribute(canonical, 'href');
    if (!href) add('error', 'missing <link rel="canonical">');
    else {
        try {
            const url = new URL(href);
            if (url.host !== base.host) add('error', `canonical points at ${url.host}, expected ${base.host}`);
        } catch {
            add('error', `canonical is not an absolute URL: ${href}`);
        }
    }

    for (const block of html.matchAll(/(<script\b[^>]*>)([\s\S]*?)<\/script>/gi)) {
        if (attribute(block[1], 'type')?.toLowerCase() !== 'application/ld+json') continue;
        try {
            JSON.parse(block[2]);
        } catch (error) {
            add('error', `JSON-LD does not parse: ${error.message}`);
        }
    }

    const [image] = metaContent(metas, 'property', 'og:image');
    if (!image) add('warning', 'missing og:image');
    else if (!metaContent(metas, 'property', 'og:image:alt').some(Boolean)) add('warning', 'og:image without og:image:alt');
    return { findings, indexable: true };
}

function checkSite(root, base, ignore = []) {
    const ignored = ignore.map(globToRegExp);
    const findings = [];
    const add = (level, file, message) => findings.push({ level, file, message });
    const pages = new Set();
    for (const path of htmlFiles(root, root, ignored, add)) {
        const html = readText(join(root, path), path, add);
        if (html === undefined) continue;
        const result = checkPage(path, html, base);
        findings.push(...result.findings);
        if (result.indexable) pages.add(path);
    }

    const robotsPath = join(root, 'robots.txt');
    if (!isFile(robotsPath)) add('error', 'robots.txt', 'missing robots.txt');
    else {
        const robots = readText(robotsPath, 'robots.txt', add);
        if (robots !== undefined && !/^\s*sitemap\s*:\s*\S/im.test(robots)) add('error', 'robots.txt', 'robots.txt has no Sitemap line');
    }

    const sitemaps = [];
    const sitemapPaths = ['sitemap.xml', 'sitemap-index.xml'].filter(name => isFile(join(root, name)));
    if (sitemapPaths.length === 0) add('error', 'sitemap.xml', 'no sitemap.xml or sitemap-index.xml at the site root');
    const queue = [...sitemapPaths];
    const seen = new Set(queue);
    while (queue.length > 0) {
        const name = queue.shift();
        const xml = readText(join(root, name), name, add);
        if (xml === undefined) continue;
        const urls = [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/gi)].map(match => match[1].replaceAll('&amp;', '&'));
        const isIndex = /<sitemapindex\b/i.test(xml);
        for (const location of urls) {
            let url;
            try { url = new URL(location); } catch { add('error', name, `sitemap entry is not an absolute URL: ${location}`); continue; }
            if (url.host !== base.host) { add('error', name, `sitemap entry ${location} is not on ${base.host}`); continue; }
            const local = url.pathname.startsWith(base.pathname) ? url.pathname.slice(base.pathname.length) : url.pathname;
            let found;
            try { found = candidates(local).find(candidate => isFile(join(root, candidate))); } catch { add('error', name, `sitemap entry ${location} is not a valid URL path`); continue; }
            if (isIndex) {
                if (found && !seen.has(found)) { seen.add(found); queue.push(found); } else if (!found) add('error', name, `sitemap index entry ${location} has no built file`);
            } else if (!found) add('error', name, `sitemap URL ${location} does not map to a built page`);
            else sitemaps.push(found);
        }
    }
    const llmsPath = join(root, 'llms.txt');
    if (!isFile(llmsPath)) add('warning', 'llms.txt', 'missing llms.txt');
    return { findings, pages: pages.size, sitemapUrls: sitemaps.length };
}

function renderSummary(result, failOn) {
    const errors = result.findings.filter(finding => finding.level === 'error').length;
    const warnings = result.findings.length - errors;
    const lines = [
        '## Discoverability check',
        '',
        `${result.pages} indexable page(s), ${result.sitemapUrls} sitemap URL(s): **${errors} error(s)**, **${warnings} warning(s)** (fail-on: \`${failOn}\`).`,
        '',
    ];
    if (result.findings.length > 0) {
        lines.push('| Level | File | Finding |', '| --- | --- | --- |');
        for (const finding of result.findings.slice(0, maxSummaryRows)) {
            lines.push(`| ${finding.level} | \`${finding.file}\` | ${finding.message.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ')} |`);
        }
        if (result.findings.length > maxSummaryRows) lines.push('', `${result.findings.length - maxSummaryRows} more finding(s) not shown.`);
    }
    return `${lines.join('\n')}\n`;
}

const escapeData = value => value.replace(/%/g, '%25').replace(/\r/g, '%0D').replace(/\n/g, '%0A');
const escapeProperty = value => escapeData(value).replace(/:/g, '%3A').replace(/,/g, '%2C');

function main() {
    let options;
    try {
        options = parseArguments(process.argv.slice(2));
    } catch (error) {
        if (!(error instanceof UsageError)) throw error;
        console.error(`could not run: ${error.message}`);
        return 2;
    }
    const result = checkSite(options.root, options.base, options.ignore);
    for (const finding of result.findings) {
        console.log(`::${finding.level} file=${escapeProperty(finding.file)}::${escapeData(finding.message)}`);
    }
    const summary = renderSummary(result, options.failOn);
    console.log(summary);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
    const errors = result.findings.some(finding => finding.level === 'error');
    return errors && options.failOn === 'error' ? 1 : 0;
}

process.exitCode = main();
