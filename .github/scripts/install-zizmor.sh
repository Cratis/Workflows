#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
set -euo pipefail

version=1.31.1
case "$(uname -m)" in
    x86_64|amd64)
        arch=x86_64
        sha256=d185f726bdaf92df78273217dfdd24ebcd6b0ef48cf3ce776ff5762b6e138c99
        ;;
    aarch64|arm64)
        arch=aarch64
        sha256=37eebbd5762a44f262d94875431b14a6d2402991e90a685113ea97b8b41be833
        ;;
    *)
        echo "Unsupported zizmor architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

archive="$RUNNER_TEMP/zizmor.tar.gz"
curl -fsSL "https://github.com/zizmorcore/zizmor/releases/download/v${version}/zizmor-${arch}-unknown-linux-gnu.tar.gz" -o "$archive"
echo "$sha256  $archive" | sha256sum --check --strict
mkdir -p "$RUNNER_TEMP/bin"
tar -xzf "$archive" -C "$RUNNER_TEMP/bin" zizmor
echo "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
