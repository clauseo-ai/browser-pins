# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/replace-assets.sh: the upload-then-swap order and what a failure leaves.
gh is replaced by a fake on PATH that keeps a release in a directory; no network."""

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "replace-assets.sh")

# The release is $FAKE_REL: one directory per asset id holding `name` and `data`; the next id is
# in $FAKE_REL.id. Every call is logged to $FAKE_LOG. Renaming an asset to a name an asset has
# fails (HTTP 422), that asset's own name included, and a missing asset id is HTTP 404.
FAKE_GH = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
by_name() {
  local d
  for d in "$FAKE_REL"/*/; do
    [ -d "$d" ] || continue
    if [ "$(cat "$d/name")" = "$1" ]; then basename "$d"; fi
  done
  return 0
}
case "$1 $2" in
  "release upload")
    shift 3
    for f in "$@"; do
      [ "$f" = --clobber ] && continue
      if [ -n "${FAKE_FAIL_UPLOAD:-}" ]; then echo "HTTP 502" >&2; exit 1; fi
      n=$(basename "$f")
      old=$(by_name "$n")
      if [ -n "$old" ]; then rm -rf "${FAKE_REL:?}/$old"; fi
      id=$(cat "$FAKE_REL.id"); echo $((id + 1)) > "$FAKE_REL.id"
      mkdir "$FAKE_REL/$id"; printf '%s' "$n" > "$FAKE_REL/$id/name"; cp "$f" "$FAKE_REL/$id/data"
    done ;;
  "release view")
    for d in "$FAKE_REL"/*/; do
      [ -d "$d" ] || continue
      echo "$(cat "$d/name") $(stat -c %s "$d/data") https://api.github.test/assets/$(basename "$d")"
    done ;;
  "api https:"*)
    id=${2##*/}
    [ -d "$FAKE_REL/$id" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    cat "$FAKE_REL/$id/name"; echo ;;
  "api -X")
    method=$3 url=$4 id=${4##*/}
    [ -d "$FAKE_REL/$id" ] || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    case "$method" in
      DELETE)
        rm -rf "${FAKE_REL:?}/$id"
        if [ -n "${FAKE_LOSE_DELETE_ANSWER:-}" ]; then
          echo "HTTP 502" >&2; exit 1
        fi ;;
      PATCH)
        n=${6#name=}
        other=$(by_name "$n")
        if [ -n "$other" ]; then echo "HTTP 422" >&2; exit 1; fi
        printf '%s' "$n" > "$FAKE_REL/$id/name"
        if [ -n "${FAKE_LOSE_PATCH_ANSWER:-}" ]; then echo "HTTP 502" >&2; exit 1; fi ;;
    esac ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
'''


class ReplaceAssets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        gh = os.path.join(bin_dir, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, 0o755)
        self.rel = os.path.join(self.tmp, "release")
        os.mkdir(self.rel)
        with open(self.rel + ".id", "w") as f:
            f.write("1")
        self.log = os.path.join(self.tmp, "gh.log")
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                        FAKE_REL=self.rel, FAKE_LOG=self.log, REPLACE_ASSETS_RETRY_DELAY="0")
        self.files = [self.local("a.bin", "new data\n"), self.local("a.bin.minisig", "new sig\n")]

    def local(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def put(self, name, text):
        """Puts an asset on the fake release directly, as an earlier run left it."""
        with open(self.rel + ".id") as f:
            asset_id = int(f.read())
        with open(self.rel + ".id", "w") as f:
            f.write(str(asset_id + 1))
        d = os.path.join(self.rel, str(asset_id))
        os.mkdir(d)
        with open(os.path.join(d, "name"), "w") as f:
            f.write(name)
        with open(os.path.join(d, "data"), "w") as f:
            f.write(text)

    def release(self):
        """The release as {name: data}; a name twice fails the test."""
        out = {}
        for asset_id in os.listdir(self.rel):
            with open(os.path.join(self.rel, asset_id, "name")) as f:
                name = f.read()
            with open(os.path.join(self.rel, asset_id, "data")) as f:
                self.assertNotIn(name, out)
                out[name] = f.read()
        return out

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def run_script(self, **env):
        return subprocess.run(["bash", SCRIPT, "current", *self.files], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def test_first_run_publishes_both(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release(), {"a.bin": "new data\n", "a.bin.minisig": "new sig\n"})
        self.assertFalse([c for c in self.calls() if "DELETE" in c])

    def test_replace_uploads_first_then_swaps_each(self):
        self.put("a.bin", "old data\n")
        self.put("a.bin.minisig", "old sig\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release(), {"a.bin": "new data\n", "a.bin.minisig": "new sig\n"})
        kinds = [c.split()[0] + " " + c.split()[1] + (" " + c.split()[2] if c.startswith("api") else "")
                 for c in self.calls()]
        self.assertEqual(kinds[0], "release upload")
        self.assertEqual([k for k in kinds if k.startswith("api")],
                         ["api -X DELETE", "api -X PATCH", "api -X DELETE", "api -X PATCH"])

    def test_leftover_of_a_stopped_run_is_replaced(self):
        self.put("a.bin", "old data\n")
        self.put("a.bin.next", "stale\n")
        self.put("a.bin.minisig.next", "stale\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release(), {"a.bin": "new data\n", "a.bin.minisig": "new sig\n"})

    def test_failed_upload_leaves_the_release_as_it_was(self):
        self.put("a.bin", "old data\n")
        self.put("a.bin.minisig", "old sig\n")
        r = self.run_script(FAKE_FAIL_UPLOAD="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("its assets are as they were", r.stderr)
        self.assertEqual(self.release(), {"a.bin": "old data\n", "a.bin.minisig": "old sig\n"})
        self.assertFalse([c for c in self.calls() if c.startswith("api")])

    def test_lost_delete_answer_counts_as_deleted(self):
        self.put("a.bin", "old data\n")
        self.put("a.bin.minisig", "old sig\n")
        r = self.run_script(FAKE_LOSE_DELETE_ANSWER="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release(), {"a.bin": "new data\n", "a.bin.minisig": "new sig\n"})

    def test_lost_rename_answer_counts_as_renamed(self):
        self.put("a.bin", "old data\n")
        self.put("a.bin.minisig", "old sig\n")
        r = self.run_script(FAKE_LOSE_PATCH_ANSWER="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.release(), {"a.bin": "new data\n", "a.bin.minisig": "new sig\n"})

    def test_missing_file_fails_before_any_call(self):
        self.files.append(os.path.join(self.tmp, "nope"))
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
