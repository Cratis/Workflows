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

# Identify the command before accepting command-specific flags. These value-taking global
# options come from the installed `pulumi --help`: --color, --cwd (-C), --memprofilerate,
# --otel-traces, --profiling, --tracing, --verbose (-v). All other globals listed there
# are boolean. Unknown options before identity is known fail closed; ordinary commands'
# flags and positional values are otherwise left untouched.
admit_command() {
    local options_ended=0 token
    command_label=""
    for token in "$@"; do
        case "$token" in
            --show-secrets|--show-secrets=*) return 1 ;;
        esac
    done
    while [ "$#" -gt 0 ]; do
        if [ "$options_ended" -eq 0 ]; then
            case "$1" in
                --) options_ended=1; shift; continue ;;
                --color|--cwd|-C|--memprofilerate|--otel-traces|--profiling|--tracing|--verbose|-v)
                    [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
                    shift 2; continue ;;
                --color=*|--cwd=*|-C=*|--memprofilerate=*|--otel-traces=*|--profiling=*|--tracing=*|--verbose=*|-v=*)
                    shift; continue ;;
                --disable-integrity-checking|--emoji|-e|--fully-qualify-stack-names|-Q|--help|-h|--logflow|--logtostderr|--non-interactive|--version)
                    shift; continue ;;
                --disable-integrity-checking=*|--emoji=*|--fully-qualify-stack-names=*|--help=*|--logflow=*|--logtostderr=*|--non-interactive=*|--version=*)
                    shift; continue ;;
                --stack|-s)
                    # The composite inserts this flag between config/env and their subcommand.
                    [ -n "$command_label" ] && [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
                    shift 2; continue ;;
                --stack=*|-s=*)
                    [ -n "$command_label" ] || return 1
                    shift; continue ;;
                -*) return 1 ;;
            esac
        fi
        [[ "$1" =~ ^[a-zA-Z][a-zA-Z0-9-]*$ ]] || return 1
        if [ -z "$command_label" ]; then
            command_label="$1"
            case "$command_label" in
                config|env) shift; continue ;;
                *) return 0 ;;
            esac
        fi
        case "$command_label:$1" in
            config:get|env:open|env:get) return 1 ;;
            *) return 0 ;;
        esac
    done
    [ -n "$command_label" ]
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
