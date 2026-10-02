# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Synchronize local pin-tracking peers without crossing their existing compatibility range."""
import json
from pathlib import Path
import re
import subprocess
import sys

NUMBER = r"(?:0|[1-9][0-9]*)"
IDENTIFIER = rf"(?:{NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
PIN = re.compile(rf"({NUMBER})\.({NUMBER})\.({NUMBER})(?:-({IDENTIFIER}(?:\.{IDENTIFIER})*))?"
                 r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
DECODER = json.JSONDecoder()


def annotation(level, message):
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{message}")


def section(manifest, name):
    value = manifest.get(name)
    return value if isinstance(value, dict) else {}


def check_path(path):
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Refusing manifest outside checkout: " + str(path))
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Refusing symlinked manifest: " + str(path))


def object_manifest(value, path):
    if isinstance(value, dict):
        return True
    annotation("notice", f"Skipping non-object manifest: {path}")
    return False


def satisfies(marker, old, new):
    """SemVer satisfaction for the only supported range shapes: ^pin and ~pin."""
    old_match, new_match = PIN.fullmatch(old), PIN.fullmatch(new)
    old_core = tuple(map(int, old_match.group(1, 2, 3)))
    new_core = tuple(map(int, new_match.group(1, 2, 3)))
    width = 2 if marker == "~" else 1 if old_core[0] else 2 if old_core[1] else 3
    if new_core < old_core or new_core[:width] != old_core[:width]:
        return False
    old_pre, new_pre = old_match[4], new_match[4]
    if not new_pre:
        return True
    # npm ranges admit prereleases only on the explicitly opted-in version tuple.
    if not old_pre or new_core != old_core:
        return False

    def identifiers(value):
        return tuple((0, int(part)) if part.isdecimal() else (1, part) for part in value.split("."))

    return identifiers(new_pre) >= identifiers(old_pre)


def members(text):
    """Yield JSON object values and their spans without reformatting the document."""
    index = text.index("{") + 1
    while True:
        while text[index].isspace() or text[index] == ",":
            index += 1
        if text[index] == "}":
            return
        key, index = DECODER.raw_decode(text, index)
        while text[index].isspace():
            index += 1
        if text[index] != ":":
            raise ValueError("Expected JSON member separator")
        index += 1
        while text[index].isspace():
            index += 1
        start = index
        value, index = DECODER.raw_decode(text, index)
        yield key, value, start, index


def snapshot(destination):
    manifests = {}

    def capture(path):
        check_path(path)
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if object_manifest(value, path):
            manifests[path.as_posix()] = value
            return value
        return {}

    root = Path("package.json")
    check_path(root)
    if root.exists():
        manifest = capture(root)
        # ncu -w resolves package.json's workspaces array (or workspaces.packages),
        # appending /package.json to each glob. Do not scan unrelated fixtures.
        workspaces = manifest.get("workspaces", [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages", [])
        patterns = []
        for workspace in workspaces if isinstance(workspaces, list) else []:
            if not isinstance(workspace, str):
                continue
            pattern = Path(workspace.removeprefix("!"))
            check_path(pattern)
            patterns.append(("!" if workspace.startswith("!") else "") + str(pattern))
        if patterns:
            # Node 22 is already installed by the workflow. Its native glob handles
            # npm-style braces/extglobs; Python glob would silently miss workspaces.
            # Match directories first so glob cannot silently hide a symlinked workspace.
            names = json.loads(subprocess.check_output(["node", "-e", """
                const { globSync } = require('node:fs');
                const patterns = JSON.parse(process.argv[1]);
                const exclude = ['**/node_modules/**', '**/.pnpm-store/**',
                    ...patterns.filter(p => p.startsWith('!')).map(p => p.slice(1))];
                console.log(JSON.stringify(globSync(patterns.filter(p => !p.startsWith('!')), { exclude })));
            """, json.dumps(patterns)], text=True))
            for name in sorted(set(names)):
                directory = Path(name)
                if {"node_modules", ".pnpm-store"}.intersection(directory.parts):
                    continue
                check_path(directory)
                if directory.is_dir():
                    path = directory / "package.json"
                    check_path(path)
                    if path.is_file():
                        capture(path)
    destination.write_text(json.dumps(manifests))
    print(f"Snapshotted {len(manifests)} manifests")


def synchronize(destination):
    manifests = json.loads(destination.read_text())
    updated = 0
    for name, before in manifests.items():
        path = Path(name)
        check_path(path)
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        after = json.loads(text)
        if not object_manifest(after, path):
            continue
        replacements = {}
        for package, peer in section(before, "peerDependencies").items():
            if not isinstance(peer, str) or peer[:1] not in ("^", "~"):
                continue
            old = peer[1:]
            if not PIN.fullmatch(old) or section(after, "peerDependencies").get(package) != peer:
                continue
            pins = []
            for dependency_section in ("dependencies", "devDependencies"):
                previous = section(before, dependency_section).get(package)
                if previous is not None:
                    # Ambiguous duplicate pins are not a compatibility policy to guess at.
                    if previous != old:
                        break
                    pins.append(section(after, dependency_section).get(package))
            else:
                if (pins and isinstance(pins[0], str) and PIN.fullmatch(pins[0]) and pins[0] != old
                        and all(pin == pins[0] for pin in pins)):
                    if satisfies(peer[0], old, pins[0]):
                        replacements[package] = peer[0] + pins[0]
                    else:
                        annotation("warning", f"{name}: {package} peer {peer} unchanged; new pin {pins[0]} "
                                   "is outside the existing peer range. Review compatibility manually.")
        if not replacements:
            continue
        edits = []
        for key, value, start, end in members(text):
            if key == "peerDependencies":
                for package, peer, value_start, value_end in members(text[start:end]):
                    if package in replacements:
                        edits.append((start + value_start, start + value_end, json.dumps(replacements[package])))
        for start, end, replacement in reversed(edits):
            text = text[:start] + replacement + text[end:]
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        path.write_bytes(text.encode(encoding))
        updated += len(edits)
    print(f"Synchronized {updated} pin-tracking peer ranges across {len(manifests)} manifests")


if __name__ == "__main__":
    operation, destination = sys.argv[1:]
    {"snapshot": snapshot, "sync": synchronize}[operation](Path(destination))
