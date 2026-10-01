# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
"""Snapshot manifests, then update only peer ranges that tracked an exact local pin."""
import json
from pathlib import Path
import re
import subprocess
import sys

PIN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
DECODER = json.JSONDecoder()


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
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others",
                                     "--exclude-standard", "--", "package.json", "**/package.json"])
    manifests = {}
    for name in sorted(set(names.decode().strip("\0").split("\0")) - {""}):
        path = Path(name)
        if "node_modules" in path.parts:
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Refusing symlinked manifest: " + name)
        manifests[name] = json.loads(path.read_text())
    destination.write_text(json.dumps(manifests))
    print(f"Snapshotted {len(manifests)} manifests")


def synchronize(destination):
    manifests = json.loads(destination.read_text())
    updated = 0
    for name, before in manifests.items():
        path = Path(name)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Refusing symlinked manifest: " + name)
        text = path.read_bytes().decode("utf-8")
        after = json.loads(text)
        replacements = {}
        for package, peer in before.get("peerDependencies", {}).items():
            if not isinstance(peer, str) or peer[:1] not in ("^", "~"):
                continue
            old = peer[1:]
            if not PIN.fullmatch(old) or after.get("peerDependencies", {}).get(package) != peer:
                continue
            pins = []
            for section in ("dependencies", "devDependencies"):
                previous = before.get(section, {}).get(package)
                if previous is not None:
                    # Ambiguous duplicate pins are not a compatibility policy to guess at.
                    if previous != old:
                        break
                    pins.append(after.get(section, {}).get(package))
            else:
                if pins and len(set(pins)) == 1 and isinstance(pins[0], str) and PIN.fullmatch(pins[0]) and pins[0] != old:
                    replacements[package] = peer[0] + pins[0]
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
        path.write_bytes(text.encode("utf-8"))
        updated += len(edits)
    print(f"Synchronized {updated} pin-tracking peer ranges across {len(manifests)} manifests")


if __name__ == "__main__":
    operation, destination = sys.argv[1:]
    {"snapshot": snapshot, "sync": synchronize}[operation](Path(destination))
