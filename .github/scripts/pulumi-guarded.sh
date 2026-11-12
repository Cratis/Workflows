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

if ! admit_command "$@"; then
    echo "::error title=Pulumi output withheld::Pulumi invocation refused."
    exit 1
fi

output="$(mktemp)"
trap 'rm -f "$output"' EXIT
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
    grep -Eq -e "$1" "$output" || rc=$?
    echo "$rc"
}

command_label="${1:-}"
if ! [[ "$command_label" =~ ^[a-zA-Z][a-zA-Z0-9-]*$ ]]; then
    command_label="command"
fi
status=0
pulumi "$@" > "$output" 2>&1 || status=$?

matched=()
unchecked=0
for entry in "${CLASSES[@]}"; do
    name="${entry%%|*}"
    case "$(match_status "${entry#*|}")" in
        0) matched+=("$name") ;;
        1) ;;
        *) unchecked=1 ;;
    esac
done

if [ "${#matched[@]}" -gt 0 ] || [ "$unchecked" -ne 0 ]; then
    detail="could not be checked"
    if [ "${#matched[@]}" -gt 0 ]; then
        detail="matched: $(IFS=,; echo "${matched[*]}")"
    fi
    echo "::error title=Pulumi output withheld::The output of 'pulumi ${command_label}' was withheld (${detail}). Mark the value that carries it as a Pulumi secret."
    exit 1
fi

cat "$output"
exit "$status"
