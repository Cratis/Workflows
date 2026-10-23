#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
set -euo pipefail

version=1.7.12
case "$(uname -m)" in
    x86_64|amd64)
        arch=amd64
        sha256=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
        ;;
    aarch64|arm64)
        arch=arm64
        sha256=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
        ;;
    *)
        echo "Unsupported actionlint architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

archive="$RUNNER_TEMP/actionlint.tar.gz"
curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${version}/actionlint_${version}_linux_${arch}.tar.gz" -o "$archive"
echo "$sha256  $archive" | sha256sum --check --strict
mkdir -p "$RUNNER_TEMP/bin"
tar -xzf "$archive" -C "$RUNNER_TEMP/bin" actionlint
echo "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
