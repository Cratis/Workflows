#!/usr/bin/env python3
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Pure-append Dependabot actions entry: 0 changed, 10 exists, 11 unsafe."""
import re
import sys

ENTRY = '''  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: monthly
    open-pull-requests-limit: 1
    groups:
      actions:
        patterns:
          - "*"
'''
CANONICAL = 'version: 2\nupdates:\n' + ENTRY


def entry_indent(text):
    """Accept only a final block-style updates sequence with consistent entries."""
    if re.search(r'[\t\r]|[&*][A-Za-z_]|<<\s*:', text):
        return None
    keys = [line for line in text.splitlines() if line and not line[0].isspace() and not line.startswith('#')]
    if not keys or not re.fullmatch(r'updates:\s*(?:#.*)?', keys[-1]):
        return None
    if sum(bool(re.match(r'updates:', line)) for line in keys) != 1:
        return None
    body = text.split(keys[-1], 1)[1]
    entries = re.findall(r'^( +)-\s+package-ecosystem:\s*\S+.*$', body, re.MULTILINE)
    if not entries or len(set(entries)) != 1:
        return None
    indent = entries[0]
    # Every sibling must be one of those ecosystem mappings; no flow-style entries.
    siblings = [line for line in body.splitlines() if line.strip() and not line.lstrip().startswith('#')
                and len(line) - len(line.lstrip()) <= len(indent)]
    if len(siblings) != len(entries):
        return None
    return indent


def valid_yaml(text):
    """Use PyYAML when already installed, but never require a third-party runtime."""
    try:
        import yaml
    except ImportError:
        return True
    try:
        data = yaml.safe_load(text)
        return isinstance(data, dict) and isinstance(data.get('updates'), list)
    except yaml.YAMLError:
        return False


def ensure(text):
    if not text:
        return 0, CANONICAL
    if re.search(r'''package-ecosystem:\s*["']?github-actions(?:["']|\s|$)''', text):
        return 10, ''
    indent = entry_indent(text)
    if indent is None:
        return 11, ''
    entry = '\n'.join(indent + line[2:] for line in ENTRY.splitlines()) + '\n'
    merged = text + ('\n' if text.endswith('\n') else '\n\n') + entry
    if not merged.startswith(text) or not valid_yaml(merged):
        return 11, ''
    return 0, merged


def main():
    # Reading bytes avoids universal-newline translation: CRLF must be rejected, not rewritten.
    try:
        text = sys.stdin.buffer.read().decode('utf-8')
    except UnicodeDecodeError:
        return 11
    status, output = ensure(text)
    sys.stdout.write(output)
    return status


if __name__ == '__main__':
    sys.exit(main())
