#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Runs `pulumi "$@"`, holds all output back, and prints it only after a scan finds no key,
# certificate or token material. On a hit the output is withheld and the step fails; the
# annotation names the command and the pattern class, never the matched text. There are no
# exceptions: a value that must appear in output is marked as a Pulumi secret instead.
#
# Config reads, ESC plaintext reads and explicit secret output are refused before invocation.
# Exit status: 1 when admission or scanning fails (wins over Pulumi's status); otherwise
# Pulumi's own status. Pattern scanning cannot identify all unarmoured secrets.
# PULUMI_GUARD_OUTPUT_FILE optionally publishes clean stdout as a private file instead
# of logging it; stderr is scanned separately.
set -euo pipefail
umask 077

# Pulumi identifies commands by literal tokens: no prefixes, only the env/esc alias
# (verified in r2 against v3.256.0). Plaintext paths require these words, so scan all
# tokens without interpreting options or stopping at --. Values that literally use
# these command words can also be refused.
admit_command() {
    local config_seen=0 env_seen=0 token
    for token in "$@"; do
        case "$token" in
            --show-secrets|--show-secrets=*) return 1 ;;
            config) config_seen=1 ;;
            env|esc) env_seen=1 ;;
            get) [ "$config_seen" -eq 0 ] && [ "$env_seen" -eq 0 ] || return 1 ;;
            open|run) [ "$env_seen" -eq 0 ] || return 1 ;;
        esac
    done
    return 0
}

output="$(mktemp)"
temporary_files=("$output")
trap 'rm -f -- "${temporary_files[@]}"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

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
    echo "::error title=Invalid Pulumi output path::Use a workspace-relative file with an existing parent inside GITHUB_WORKSPACE, without .. components, directories or symlink targets." >&2
    exit 1
}

prepare_destination() {
    local requested="$1" parent root
    [[ "$requested" =~ [^[:space:]] ]] || invalid_path
    case "/$requested/" in
        //*|*/../*) invalid_path ;;
    esac
    [ -n "${GITHUB_WORKSPACE:-}" ] || invalid_path
    requested="$GITHUB_WORKSPACE/$requested"
    [ ! -d "$requested" ] && [ ! -L "$requested" ] || invalid_path
    root="$(cd -- "$GITHUB_WORKSPACE" 2>/dev/null && pwd -P)" || invalid_path
    parent="$(cd -- "$(dirname -- "$requested")" 2>/dev/null && pwd -P)" || invalid_path
    case "$parent/" in
        "$root/"*) ;;
        *) invalid_path ;;
    esac
    workspace_root="$root"
    destination_parent="$parent"
    destination="$parent/$(basename -- "$requested")"
    rm -f -- "$destination"
}

publication_path_is_safe() {
    local parent
    parent="$(cd -- "$destination_parent" 2>/dev/null && pwd -P)" || return 1
    [ "$parent" = "$destination_parent" ] || return 1
    case "$parent/" in
        "$workspace_root/"*) ;;
        *) return 1 ;;
    esac
    [ ! -L "$destination" ] && [ ! -d "$destination" ]
}

publication_failed() {
    echo "::error title=Pulumi output withheld::Pulumi output file could not be published." >&2
    exit 1
}

publish_destination() {
    local staged
    publication_path_is_safe || publication_failed
    staged="$(mktemp "$destination_parent/.pulumi-guarded.XXXXXXXX" 2>/dev/null)" || publication_failed
    temporary_files+=("$staged")
    cat "$output" > "$staged" || publication_failed
    chmod 0600 "$staged" 2>/dev/null || publication_failed
    publication_path_is_safe || publication_failed
    mv -f -T -- "$staged" "$destination" 2>/dev/null || publication_failed
}

destination=""
if [ -n "${PULUMI_GUARD_OUTPUT_FILE:-}" ]; then
    prepare_destination "$PULUMI_GUARD_OUTPUT_FILE"
fi

# Prepare the destination and remove stale output before admission, so refused
# commands cannot leave a previous result behind. Admission itself is unchanged.
if ! admit_command "$@"; then
    echo "::error title=Pulumi output withheld::Pulumi invocation refused."
    exit 1
fi

command_label="${1:-}"
if ! [[ "$command_label" =~ ^[a-zA-Z][a-zA-Z0-9-]*$ ]]; then
    command_label="command"
fi
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
        publish_destination
    fi
else
    cat "$output"
fi
exit "$status"
