#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Runs `pulumi "$@"`, holds all output back, and prints it only after a scan finds no key,
# certificate or token material. On a hit the output is withheld and the step fails; the
# annotation names the command and the pattern class, never the matched text. There are no
# exceptions: a value that must appear in output is marked as a Pulumi secret instead.
#
# Exit status: 1 when the scan matched or could not run (fail closed, wins over Pulumi's
# status); otherwise Pulumi's own status. PULUMI_GUARD_OUTPUT_FILE optionally publishes
# clean stdout as a private file instead of logging it; stderr is scanned separately.
set -euo pipefail
umask 077

output="$(mktemp)"
temporary_files=("$output")
trap 'rm -f -- "${temporary_files[@]}"' EXIT

# name|extended regex
CLASSES=(
    'private-key|-----BEGIN [A-Z0-9 ]*PRIVATE KEY'
    'certificate|-----BEGIN [A-Z0-9 ]*CERTIFICATE'
    'kubeconfig-credential|client-key-data|client-certificate-data'
    'base64-armour|LS0tLS1CRUdJTi'
    'github-token|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_'
    'jwt|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
    'aws-access-key|AKIA[0-9A-Z]{16}'
)

# grep exit: 0 match, 1 none, anything else = not checked. Prints the status.
match_status() {
    local rc=0
    grep -Eq -e "$1" "$2" || rc=$?
    echo "$rc"
}

invalid_path() {
    echo "::error title=Invalid Pulumi output path::Use a file with an existing parent under GITHUB_WORKSPACE or RUNNER_TEMP, not a directory or symlink." >&2
    exit 1
}

prepare_destination() {
    local requested="$1" parent root allowed=0
    [[ "$requested" =~ [^[:space:]] ]] || invalid_path
    if [[ "$requested" != /* ]]; then
        [ -n "${GITHUB_WORKSPACE:-}" ] || invalid_path
        requested="$GITHUB_WORKSPACE/$requested"
    fi
    [ ! -d "$requested" ] && [ ! -L "$requested" ] || invalid_path
    parent="$(cd -- "$(dirname -- "$requested")" 2>/dev/null && pwd -P)" || invalid_path
    for root in "${GITHUB_WORKSPACE:-}" "${RUNNER_TEMP:-}"; do
        [ -n "$root" ] || continue
        root="$(cd -- "$root" 2>/dev/null && pwd -P)" || continue
        case "$parent/" in "$root/"*) allowed=1 ;; esac
    done
    [ "$allowed" -eq 1 ] || invalid_path
    destination="$parent/$(basename -- "$requested")"
    rm -f -- "$destination"
}

destination=""
if [ -n "${PULUMI_GUARD_OUTPUT_FILE:-}" ]; then
    prepare_destination "$PULUMI_GUARD_OUTPUT_FILE"
fi
for arg in "$@"; do
    case "$arg" in
        --show-secrets|--show-secrets=*)
            echo "::error title=Unsafe Pulumi arguments::--show-secrets is not permitted." >&2
            exit 1 ;;
    esac
done

command_label="${1:-}"
status=0
scan_files=("$output")
if [ -n "$destination" ]; then
    stderr="$(mktemp)"
    temporary_files+=("$stderr")
    scan_files+=("$stderr")
    pulumi "$@" > "$output" 2> "$stderr" || status=$?
else
    pulumi "$@" > "$output" 2>&1 || status=$?
fi

matched=()
unchecked=0
for entry in "${CLASSES[@]}"; do
    name="${entry%%|*}"
    found=0
    for file in "${scan_files[@]}"; do
        case "$(match_status "${entry#*|}" "$file")" in
            0) found=1 ;;
            1) ;;
            *) unchecked=1 ;;
        esac
    done
    if [ "$found" -eq 1 ]; then matched+=("$name"); fi
done

if [ "${#matched[@]}" -gt 0 ] || [ "$unchecked" -ne 0 ]; then
    detail="could not be checked"
    if [ "${#matched[@]}" -gt 0 ]; then
        detail="matched: $(IFS=,; echo "${matched[*]}")"
    fi
    echo "::error title=Pulumi output withheld::The output of 'pulumi ${command_label}' was withheld (${detail}). Mark the value that carries it as a Pulumi secret."
    exit 1
fi

if [ -n "$destination" ]; then
    cat "$stderr" >&2
    if [ "$status" -eq 0 ]; then
        staged="$(mktemp "$(dirname -- "$destination")/.pulumi-guarded.XXXXXXXX")"
        temporary_files+=("$staged")
        cat "$output" > "$staged"
        chmod 0600 "$staged"
        mv -f -- "$staged" "$destination"
    fi
else
    cat "$output"
fi
exit "$status"
