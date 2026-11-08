#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Fetch the current release assets and verify them. Both jobs of publish.yml run it.
#
#   scripts/fetch-current.sh <dest dir>
#
# Prints on stdout the path of the verified current manifest, or "none" for a first run.
# Diagnostics go to stderr. Environment:
#   GH_TOKEN, GH_REPO   the release lookup and download
#   RECOVER_INCOMPLETE  "true" lets a release with an incomplete asset pair count as a
#                       first run, with a warning (owner only: the workflow checks the actor)
#   GITHUB_STEP_SUMMARY optional, for the summary lines
#
# Rules:
#   - only HTTP 404 on release `current` is a first run;
#   - a release that exists but lacks either asset fails, unless RECOVER_INCOMPLETE=true;
#   - when both assets are there, the signature must verify with minisign.pub or
#     minisign-next.pub, and the signed trusted comment must match the manifest body.
set -euo pipefail

usage="usage: fetch-current.sh <dest dir>"
dest=${1:?$usage}
mkdir -p "$dest"
dest=$(cd "$dest" && pwd)
# minisign.pub and build.py are relative to the repo root
cd "$(dirname "$0")/.."

manifest_name=browser-pins-v1.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

note() { echo "$1" >&2; }
summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}
fail() {
  echo "::error::$1" >&2
  summary "**Failed:** $1 Nothing was published."
  exit 1
}

code=$(curl -sS --max-time 60 -o "$tmp/release.json" -w '%{http_code}' \
  -H "Authorization: Bearer ${GH_TOKEN:-}" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${GH_REPO:?GH_REPO is not set}/releases/tags/current") \
  || fail "could not ask GitHub for release current."

case "$code" in
  404)
    note "No release current yet: first run."
    echo none
    exit 0 ;;
  200) ;;
  *) fail "GitHub answered HTTP $code for release current." ;;
esac

names=$(jq -r '.assets[].name' "$tmp/release.json") || fail "release current has no readable asset list."
missing=""
for asset in "$manifest_name" "$manifest_name.minisig"; do
  if ! grep -Fxq "$asset" <<<"$names"; then
    missing="$missing $asset"
  fi
done
if [ -n "$missing" ]; then
  if [ "${RECOVER_INCOMPLETE:-false}" = true ]; then
    note "Release current lacks:$missing; recover_incomplete is set, so this is a first run."
    summary "**Warning:** release \`current\` lacks:$missing. \`recover_incomplete\` is set, so this run is treated as a first run and replaces what is there."
    echo none
    exit 0
  fi
  fail "release current exists but lacks:$missing. Only the owner can republish over it (recover_incomplete)."
fi

gh release download current --pattern "$manifest_name" --pattern "$manifest_name.minisig" \
  --dir "$dest" --clobber >&2 || fail "could not download the current assets."
manifest="$dest/$manifest_name"
comment=$(scripts/verify.sh --either "$manifest" "$manifest.minisig") \
  || fail "the current assets do not verify with minisign.pub or minisign-next.pub."
python3 build.py --check-comment "$manifest" "$comment" >&2 \
  || fail "the current assets' trusted comment does not match their body."
summary "Verified the current assets: \`$comment\`"
echo "$manifest"
