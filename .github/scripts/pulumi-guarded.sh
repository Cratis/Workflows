#!/usr/bin/env bash
# Copyright (c) Cratis. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.
#
# Runs `pulumi "$@"`, holds all output back, and prints it only after a scan finds no key,
# certificate or token material. On a hit the output is withheld and the step fails; the
# annotation names the command and the pattern class, never the matched text.
#
# Exit status: 1 when the scan matched or could not run (fail closed, wins over Pulumi's
# status); otherwise Pulumi's own status.
#
# PULUMI_GUARD_ALLOW_FILE (optional): file with one SHA-256 per line (blank lines and
# '#' comments ignored), the fingerprint of a certificate body (base64 lines, whitespace
# removed) that may appear in output. It never tolerates private keys, kubeconfig
# credential fields, base64 armour or tokens.
set -euo pipefail
umask 077

output="$(mktemp)"
trap 'rm -f "$output"' EXIT

CERT_BEGIN='-----BEGIN [A-Z0-9 ]*CERTIFICATE'
# name|extended regex; the certificate class is handled separately (allow-list).
CLASSES=(
    'private-key|-----BEGIN [A-Z0-9 ]*PRIVATE KEY'
    'kubeconfig-credential|client-key-data|client-certificate-data'
    'base64-armour|LS0tLS1CRUdJTi'
    'github-token|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_'
    'jwt|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
    'aws-access-key|AKIA[0-9A-Z]{16}'
)

# Prints a SHA-256 hex digest of stdin.
sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

# grep exit: 0 match, 1 none, anything else = not checked. Prints the status.
match_status() {
    local rc=0
    grep -Eq -e "$1" "$output" || rc=$?
    echo "$rc"
}

# Prints the body of every certificate block, one per line; UNTERMINATED for an open block.
certificate_bodies() {
    awk -v begin="$CERT_BEGIN" -v end='-----END [A-Z0-9 ]*CERTIFICATE' '
        $0 ~ begin { if (inside) { print "UNTERMINATED"; inside = 0; next } inside = 1; body = ""; next }
        inside && $0 ~ end { print body; inside = 0; next }
        inside { gsub(/[ \t\r]/, ""); body = body $0 }
        END { if (inside) print "UNTERMINATED" }
    ' "$output"
}

# 0 = a certificate that is not allow-listed, 1 = none or all allow-listed, 2 = error.
certificate_status() {
    local rc
    rc="$(match_status "$CERT_BEGIN")"
    [ "$rc" = 1 ] && { echo 1; return; }
    [ "$rc" = 0 ] || { echo 2; return; }
    local allow="${PULUMI_GUARD_ALLOW_FILE:-}"
    if [ -z "$allow" ] || [ ! -r "$allow" ]; then
        echo 0
        return
    fi
    local bodies body fingerprint
    bodies="$(certificate_bodies)" || { echo 2; return; }
    while IFS= read -r body; do
        [ -n "$body" ] || { echo 0; return; }
        [ "$body" != UNTERMINATED ] || { echo 0; return; }
        fingerprint="$(printf '%s' "$body" | sha256_of)" || { echo 2; return; }
        grep -Fxq -e "$fingerprint" "$allow" 2> /dev/null || { echo 0; return; }
    done <<< "$bodies"
    echo 1
}

command_label="${1:-}"
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
case "$(certificate_status)" in
    0) matched+=("certificate") ;;
    1) ;;
    *) unchecked=1 ;;
esac

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
