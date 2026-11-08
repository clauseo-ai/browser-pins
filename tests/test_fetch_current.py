# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/fetch-current.sh: the rules for the release lookup and the
incomplete-pair handling. curl and gh are replaced by fakes on PATH; no network."""

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "fetch-current.sh")
MANIFEST = "browser-pins-v1.json"

FAKE_CURL = r'''#!/usr/bin/env bash
# fake curl: the body goes to the -o file, the HTTP code to stdout (-w)
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ] && cp "$FAKE_RELEASE" "$out"
printf '%s' "$FAKE_HTTP_CODE"
'''

FAKE_GH = r'''#!/usr/bin/env bash
# fake gh: only "release download" is expected; it copies the fake assets into --dir
if [ "$1 $2" != "release download" ]; then
  echo "unexpected gh call: $*" >&2
  exit 2
fi
shift 2
dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) dir=$2; shift 2 ;;
    --pattern) shift 2 ;;
    *) shift ;;
  esac
done
cp "$FAKE_ASSETS"/* "$dir"/
'''


# fake minisign: succeeds only when the test sets FAKE_COMMENT, and then prints it
# (-Q prints the signed trusted comment); otherwise it fails like a bad signature
FAKE_MINISIGN = r'''#!/usr/bin/env bash
if [ -z "${FAKE_COMMENT:-}" ]; then
  exit 1
fi
printf '%s\n' "$FAKE_COMMENT"
'''

OLD_VERSION = "148.0.7778.1"


def manifest_body(published, version=OLD_VERSION):
    names = ("chrome-headless-shell", "chrome")
    platforms = ("win64", "mac-arm64", "mac-x64", "linux64")
    downloads = {}
    for name in names:
        downloads[name] = {}
        for p in platforms:
            sha = hashlib.sha256(f"{version}{name}{p}".encode()).hexdigest()
            downloads[name][p] = {"sha256": sha, "size": 1000}
    doc = {"schema": 1, "published": published,
           "stable": {"version": version, "downloads": downloads}}
    return json.dumps(doc, indent=2) + "\n"


def days_ago(days):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return when.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def release(*names):
    assets = ",".join('{"name": "%s"}' % n for n in names)
    return '{"tag_name": "current", "assets": [%s]}' % assets


class FetchCurrentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pins-fetch-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("curl", FAKE_CURL), ("gh", FAKE_GH), ("minisign", FAKE_MINISIGN)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets)
        self.release_file = os.path.join(self.tmp, "release.json")
        self.summary = os.path.join(self.tmp, "summary.md")
        self.dest = os.path.join(self.tmp, "current")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fetch(self, code, release_json=None, assets=(), recover=False, contents=None,
                  comment=""):
        contents = contents or {}
        with open(self.release_file, "w") as f:
            f.write(release_json or release())
        for name in assets:
            with open(os.path.join(self.assets, name), "w") as f:
                f.write(contents.get(name, "junk\n"))
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "GH_TOKEN": "test-token",
            "GH_REPO": "clauseo-ai/browser-pins",
            "RECOVER_INCOMPLETE": "true" if recover else "false",
            "FAKE_HTTP_CODE": str(code),
            "FAKE_RELEASE": self.release_file,
            "FAKE_ASSETS": self.assets,
            "FAKE_COMMENT": comment,
            "GITHUB_STEP_SUMMARY": self.summary,
            "RUNNER_TEMP": self.tmp,
        }
        return subprocess.run(["bash", SCRIPT, self.dest], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=120)

    def summary_text(self):
        if not os.path.exists(self.summary):
            return ""
        with open(self.summary, encoding="utf-8") as f:
            return f.read()

    def test_404_is_a_first_run(self):
        r = self.run_fetch(404)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "none\n")

    def test_other_http_codes_fail(self):
        for code in (500, 403, 401, 302):
            r = self.run_fetch(code)
            self.assertNotEqual(r.returncode, 0, code)
            self.assertEqual(r.stdout, "", code)
            self.assertIn(f"HTTP {code}", r.stderr)
            self.assertIn("**Failed:**", self.summary_text())

    def test_release_with_no_assets_fails(self):
        r = self.run_fetch(200, release())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("lacks", r.stderr)

    def test_only_manifest_fails(self):
        r = self.run_fetch(200, release(MANIFEST), assets=[MANIFEST])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(MANIFEST + ".minisig", r.stderr)

    def test_only_signature_fails(self):
        r = self.run_fetch(200, release(MANIFEST + ".minisig"), assets=[MANIFEST + ".minisig"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(MANIFEST, r.stderr)

    def test_incomplete_pair_with_recover_is_first_run_with_warning(self):
        r = self.run_fetch(200, release(MANIFEST), assets=[MANIFEST], recover=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "none\n")
        self.assertIn("Warning", self.summary_text())
        self.assertIn("recover_incomplete", self.summary_text())

    def test_recover_does_not_fail_a_complete_pair_that_is_bad(self):
        # a complete pair still has to verify: junk signatures fail even with recover set
        r = self.run_fetch(200, release(MANIFEST, MANIFEST + ".minisig"),
                           assets=[MANIFEST, MANIFEST + ".minisig"], recover=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("do not verify", r.stderr)

    def test_complete_pair_with_bad_signature_fails(self):
        r = self.run_fetch(200, release(MANIFEST, MANIFEST + ".minisig"),
                           assets=[MANIFEST, MANIFEST + ".minisig"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("do not verify", r.stderr)
        self.assertIn("**Failed:**", self.summary_text())

    def run_verified_current(self, published):
        """A current release whose manifest and signature verify (fake minisign), published at `published`."""
        body = manifest_body(published)
        comment = f"clauseo-browser-pins v1 published={published} version={OLD_VERSION} run=1"
        return self.run_fetch(200, release(MANIFEST, MANIFEST + ".minisig"),
                              assets=[MANIFEST, MANIFEST + ".minisig"],
                              contents={MANIFEST: body}, comment=comment)

    def test_current_published_2_days_ago_has_no_warning(self):
        r = self.run_verified_current(days_ago(2))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), os.path.join(self.dest, MANIFEST))
        self.assertNotIn("::warning::", r.stderr)
        self.assertNotIn("Warning", self.summary_text())
        self.assertIn("Verified the current assets", self.summary_text())

    def test_current_published_4_days_ago_warns(self):
        published = days_ago(4)
        r = self.run_verified_current(published)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), os.path.join(self.dest, MANIFEST))
        self.assertIn("::warning::", r.stderr)
        self.assertIn("published 4 days ago", r.stderr)
        self.assertIn("**Warning:** the current manifest was published 4 days ago "
                      f"({published}), more than 3: recent publishes have not succeeded.",
                      self.summary_text())


if __name__ == "__main__":
    unittest.main()
