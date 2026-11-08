#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Verify a minisign signature with a committed public key.
#
#   scripts/verify.sh <file> <signature> <expected trusted comment>
#       Checks against minisign.pub only, and requires the signed trusted comment
#       to equal the expected string exactly. Used after signing.
#
#   scripts/verify.sh --either <file> <signature>
#       Accepts a signature from minisign.pub or minisign-next.pub (a rotation).
#       Prints the signed trusted comment on stdout, for the caller to check.
#
# Fails closed: a missing or empty key file is an error, in both modes.
# MINISIGN_PUB overrides minisign.pub in the first mode (tests only).
set -euo pipefail

usage="usage: verify.sh <file> <signature> <expected comment> | verify.sh --either <file> <signature>"

either=false
if [ "${1:-}" = "--either" ]; then
  either=true
  shift
fi
file=${1:?$usage}
sig=${2:?$usage}
if [ "$either" = false ]; then
  expected=${3:?$usage}
fi

check_key() {
  if [ ! -s "$1" ]; then
    echo "error: $1 is missing or empty; refusing to continue" >&2
    exit 1
  fi
}

if [ "$either" = true ]; then
  for pub in minisign.pub minisign-next.pub; do
    check_key "$pub"
  done
  for pub in minisign.pub minisign-next.pub; do
    # -Q prints only the trusted comment, and only after the signature verified
    if got=$(minisign -V -p "$pub" -m "$file" -x "$sig" -Q 2>/dev/null); then
      echo "verified $file with $pub" >&2
      echo "$got"
      exit 0
    fi
  done
  echo "error: signature on $file does not verify with minisign.pub or minisign-next.pub" >&2
  exit 1
fi

pub=${MINISIGN_PUB:-minisign.pub}
check_key "$pub"

# -Q prints only the trusted comment, after both signatures verified
if ! got=$(minisign -V -p "$pub" -m "$file" -x "$sig" -Q); then
  echo "error: signature on $file does not verify with $pub" >&2
  exit 1
fi
if [ "$got" != "$expected" ]; then
  echo "error: trusted comment mismatch" >&2
  echo "  expected: $expected" >&2
  echo "  got:      $got" >&2
  exit 1
fi
echo "verified $file: $got"
