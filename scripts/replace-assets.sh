#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Replace assets of a release with new files, keeping the moment without them short.
#
#   scripts/replace-assets.sh <tag> <file>...
#
# `gh release upload --clobber` deletes the old asset and then uploads the new one, so a download
# of that name fails for the whole upload, and a failed upload leaves the release without it.
# This script uploads every file under a temporary name first (if that fails, the release's
# assets are as they were), then, file by file, deletes the old asset and renames the new one
# into place: two API calls. The swap is still not atomic: for a moment a reader can meet a
# missing asset, or a new file with the old signature, so readers retry until the pair verifies.
# A run that stops during the swap leaves `<name>.next` assets; the next run replaces them.
#
# Environment: GH_TOKEN, GH_REPO; REPLACE_ASSETS_RETRY_DELAY (seconds between the 4 tries of
# each API call, default 5).
set -euo pipefail

usage="usage: replace-assets.sh <tag> <file>..."
tag=${1:?$usage}
shift
[ "$#" -ge 1 ] || { echo "$usage" >&2; exit 2; }
suffix=.next
delay=${REPLACE_ASSETS_RETRY_DELAY:-5}

fail() {
  echo "::error::$1" >&2
  exit 1
}
# Runs a command up to 4 times, $delay seconds apart.
retry() {
  local i
  for i in 1 2 3 4; do
    "$@" && return 0
    [ "$i" -eq 4 ] || sleep "$delay"
  done
  return 1
}
# Deletes the asset at API URL $1. One already gone (HTTP 404) counts as deleted, since a retry
# after a delete whose answer was lost gets that.
delete_asset() {
  local out
  out=$(gh api -X DELETE "$1" 2>&1) && return 0
  grep -q 'HTTP 404' <<<"$out" && return 0
  echo "$out" >&2
  return 1
}
# Prints "<name> <size> <api url>" for each asset of the release.
assets() {
  gh release view "$tag" --json assets --jq '.assets[] | "\(.name) \(.size) \(.apiUrl)"'
}
field() { # field <listing> <name> <column>
  awk -v n="$2" -v c="$3" '$1 == n { print $c }' <<<"$1"
}

for f in "$@"; do
  [ -f "$f" ] || fail "$f is not a file."
done

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
for f in "$@"; do
  cp "$f" "$staging/$(basename "$f")$suffix"
done

# 1. Upload under the temporary names; --clobber replaces a leftover of a run that stopped.
retry gh release upload "$tag" "$staging"/* --clobber ||
  fail "could not upload the new assets to release $tag; its assets are as they were."

# 2. Swap, file by file.
listing=$(retry assets) || fail "could not list the assets of release $tag."
for f in "$@"; do
  name=$(basename "$f")
  new=$(field "$listing" "$name$suffix" 3)
  old=$(field "$listing" "$name" 3)
  [ -n "$new" ] || fail "$name$suffix is not on release $tag after the upload."
  if [ -n "$old" ]; then
    retry delete_asset "$old" || fail "could not delete the old $name from release $tag."
  fi
  retry gh api -X PATCH "$new" -f name="$name" --silent ||
    fail "could not rename $name$suffix to $name on release $tag."
done

# 3. Check: each name is there once, with the size of its file.
listing=$(retry assets) || fail "could not list the assets of release $tag."
for f in "$@"; do
  name=$(basename "$f")
  sizes=$(field "$listing" "$name" 2)
  [ "$sizes" = "$(stat -c %s "$f")" ] ||
    fail "release $tag has $name with size(s) '${sizes//$'\n'/ }', not $(stat -c %s "$f")."
done
