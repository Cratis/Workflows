#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Prints the staggered cron entry for a repository that opts in to batched releases
# (Cratis/Workflows#189). The minute offset 0-14 is derived from the repository name, so repositories
# do not all tick at the same minutes; each ticks four times an hour at offset, +15, +30 and +45.
#
# Usage: batched-release-cron.sh <repository-name>

set -euo pipefail

batched_release_cron() {
  local checksum remainder offset
  read -r checksum remainder < <(printf '%s' "$1" | cksum)
  offset=$((checksum % 15))
  printf '%d,%d,%d,%d * * * *' "$offset" "$((offset + 15))" "$((offset + 30))" "$((offset + 45))"
}

if [ "${1:-}" = "" ] || [ "$#" -ne 1 ]; then
  echo "usage: ${0##*/} <repository-name>" >&2
  exit 2
fi

batched_release_cron "$1"
printf '\n'
