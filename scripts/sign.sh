#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Sign a file with minisign, non-interactively.
#
#   scripts/sign.sh <file> <trusted comment>
#
# Environment (never printed):
#   MINISIGN_SECRET_KEY  the whole minisign secret key file
#   MINISIGN_PASSWORD    its password
#   KEY_FILE             where to write the key (the workflow uses $RUNNER_TEMP;
#                        the caller deletes it afterwards)
#
# minisign 0.11 reads the password from stdin when stdin is not a terminal
# (it disables echo only if tcgetattr succeeds, then reads one line), so the
# password goes in through a pipe from printf, a shell builtin: it never
# appears in argv, in the environment of minisign, or in the log.
#
# Signing always uses the current secret key; the workflow then checks the
# signature with minisign.pub (scripts/verify.sh).
set -euo pipefail
set +x

file=${1:?usage: sign.sh <file> <trusted comment>}
comment=${2:?usage: sign.sh <file> <trusted comment>}
: "${KEY_FILE:?KEY_FILE is not set}"

if [ -z "${MINISIGN_SECRET_KEY:-}" ]; then
  echo "error: MINISIGN_SECRET_KEY is empty" >&2
  exit 1
fi
if [ -z "${MINISIGN_PASSWORD:-}" ]; then
  echo "error: MINISIGN_PASSWORD is empty" >&2
  exit 1
fi

umask 077
printf '%s\n' "$MINISIGN_SECRET_KEY" > "$KEY_FILE"
password=$MINISIGN_PASSWORD
unset MINISIGN_SECRET_KEY MINISIGN_PASSWORD

printf '%s\n' "$password" | minisign -S -s "$KEY_FILE" -m "$file" -x "$file.minisig" -t "$comment" >/dev/null
unset password
echo "signed $file"
