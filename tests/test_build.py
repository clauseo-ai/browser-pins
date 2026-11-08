# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for build.py. No network: open_url is replaced by a fake that serves
the responses built here and fails on anything it was not given."""

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import build  # noqa: E402

REAL_OPEN_HEAD = build.open_head  # Base replaces build.open_head; these tests need the real one

NAMES = ("chrome-headless-shell", "chrome")
PLATFORMS = ("win64", "mac-arm64", "mac-x64", "linux64")
TEMPLATE = ("https://storage.googleapis.com/chrome-for-testing-public/"
            "{version}/{platform}/{name}-{platform}.zip")
CFT = build.CFT_URL
NOW = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
NOW_S = "2026-11-08T12:00:00Z"
RUN = "12345678"
NEW = "149.0.7.0"
OLD = "148.0.7778.1"


def url_for(version, name, platform):
    return TEMPLATE.format(version=version, platform=platform, name=name)


def make_zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def good_zip(version, name, platform):
    prefix = f"{name}-{platform}/"
    exe = build.exe_member(name, platform)
    return make_zip_bytes({
        prefix + "LICENSE": b"license text",
        exe: f"{version} {name} {platform} ".encode() * 40,
    })


def listing_doc(version, urls=None, drop=None):
    """A CfT-shaped Stable list. urls: {(name, platform): url} overrides.
    drop: (name, platform) to leave out. win32 is always listed (real CfT has it)."""
    urls = urls or {}
    downloads = {}
    for name in NAMES:
        entries = []
        for p in PLATFORMS + ("win32",):
            if drop == (name, p):
                continue
            u = urls.get((name, p), url_for(version, name, p))
            entries.append({"platform": p, "url": u})
        downloads[name] = entries
    return {
        "timestamp": "2026-11-08T00:00:00.000Z",
        "channels": {"Stable": {"channel": "Stable", "version": version,
                                "revision": "1", "downloads": downloads}},
    }


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class FakeHead:
    """Stands in for build.open_head. Statuses >= 400 raise, as the real one does."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def serve(self, url, status=200, headers=None):
        self.routes[url] = (status, headers if headers is not None else {})

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise build.BuildError(f"HEAD {url}: not served by the test")
        status, headers = self.routes[url]
        if status >= 400:
            raise build.BuildError(f"HEAD {url}: HTTP {status}")
        resp = FakeResponse(b"", headers)
        resp.status = status
        return resp


class FakeWeb:
    """Stands in for build.open_url. Records every URL; unknown URLs fail."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def serve(self, url, data, headers=None):
        self.routes[url] = (data, headers)

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise build.BuildError(f"fetch {url}: not served by the test")
        data, headers = self.routes[url]
        return FakeResponse(data, headers)

    def zip_calls(self):
        return [u for u in self.calls if u.endswith(".zip")]


def manifest_doc(version=OLD, published="2026-11-07T06:00:00Z", size=1000):
    downloads = {}
    for name in NAMES:
        downloads[name] = {}
        for p in PLATFORMS:
            sha = hashlib.sha256(f"{version}{name}{p}".encode()).hexdigest()
            downloads[name][p] = {"sha256": sha, "size": size}
    return {"schema": 1, "published": published,
            "stable": {"version": version, "downloads": downloads}}


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=2) + "\n")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pins-test-")
        self.out = os.path.join(self.tmp, "out")
        self.web = FakeWeb()
        self.gh_output = os.path.join(self.tmp, "gh_output")
        self.summary = os.path.join(self.tmp, "summary.md")
        env = {"GITHUB_OUTPUT": self.gh_output, "GITHUB_STEP_SUMMARY": self.summary,
               "RUNNER_TEMP": self.tmp}
        self.env_patch = mock.patch.dict(os.environ, env)
        self.env_patch.start()
        os.environ.pop("GITHUB_RUN_ID", None)
        self.web_patch = mock.patch.object(build, "open_url", self.web)
        self.web_patch.start()
        self.heads = FakeHead()
        self.head_patch = mock.patch.object(build, "open_head", self.heads)
        self.head_patch.start()

    def tearDown(self):
        self.head_patch.stop()
        self.web_patch.stop()
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve_listing(self, version, **kw):
        self.web.serve(CFT, json.dumps(listing_doc(version, **kw)).encode())

    def serve_zips(self, version, overrides=None):
        """Serve good zips for all 8, except overrides {(name, platform): bytes}."""
        overrides = overrides or {}
        zips = {}
        for name in NAMES:
            for p in PLATFORMS:
                data = overrides.get((name, p), good_zip(version, name, p))
                zips[(name, p)] = data
                self.web.serve(url_for(version, name, p), data)
        return zips

    def serve_new(self, version=NEW, **kw):
        self.serve_listing(version, **kw)
        return self.serve_zips(version)

    def current(self, doc=None, name="current.json"):
        return write_json(os.path.join(self.tmp, name), doc or manifest_doc())

    def main(self, args, now=NOW):
        with mock.patch.object(build, "now_utc", return_value=now):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                rc = build.main(args)
        return rc, out.getvalue(), err.getvalue()

    def gh_outputs(self):
        if not os.path.exists(self.gh_output):
            return {}
        with open(self.gh_output, encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if line.strip())

    def written(self):
        path = os.path.join(self.out, build.MANIFEST_NAME)
        with open(path, "rb") as f:
            return f.read()

    def written_doc(self):
        return json.loads(self.written())

    def summary_text(self):
        if not os.path.exists(self.summary):
            return ""
        with open(self.summary, encoding="utf-8") as f:
            return f.read()


# --------------------------------------------------------------------------

class VersionTest(unittest.TestCase):
    def test_numeric_compare_not_lexical(self):
        self.assertLess(build.version_key("149.0.7.9"), build.version_key("149.0.7.10"))
        self.assertLess(build.version_key("148.9.9.9"), build.version_key("149.0.0.0"))
        self.assertEqual(build.version_key("149.0.7.0"), build.version_key("149.0.7.0"))

    def test_template_is_the_spec_template(self):
        self.assertEqual(build.URL_TEMPLATE, TEMPLATE)

    def test_executable_paths(self):
        want = {
            ("chrome", "win64"): "chrome-win64/chrome.exe",
            ("chrome", "linux64"): "chrome-linux64/chrome",
            ("chrome", "mac-arm64"): "chrome-mac-arm64/Google Chrome for Testing.app/"
                                     "Contents/MacOS/Google Chrome for Testing",
            ("chrome", "mac-x64"): "chrome-mac-x64/Google Chrome for Testing.app/"
                                   "Contents/MacOS/Google Chrome for Testing",
            ("chrome-headless-shell", "win64"): "chrome-headless-shell-win64/chrome-headless-shell.exe",
            ("chrome-headless-shell", "mac-arm64"): "chrome-headless-shell-mac-arm64/chrome-headless-shell",
            ("chrome-headless-shell", "mac-x64"): "chrome-headless-shell-mac-x64/chrome-headless-shell",
            ("chrome-headless-shell", "linux64"): "chrome-headless-shell-linux64/chrome-headless-shell",
        }
        for (name, p), path in want.items():
            self.assertEqual(build.exe_member(name, p), path)


class PlanTest(unittest.TestCase):
    def cur(self, version, published):
        return manifest_doc(version=version, published=published)

    def test_first_run_downloads(self):
        self.assertEqual(build.plan(None, NEW, False, NOW), build.ACTION_DOWNLOAD)

    def test_newer_downloads(self):
        c = self.cur(OLD, "2026-11-07T06:00:00Z")
        self.assertEqual(build.plan(c, NEW, False, NOW), build.ACTION_DOWNLOAD)

    def test_same_version_under_20h_skips(self):
        c = self.cur(NEW, "2026-11-08T00:00:01Z")  # 11 h 59 min 59 s old
        self.assertEqual(build.plan(c, NEW, False, NOW), build.ACTION_SKIP)

    def test_same_version_exactly_20h_rebuilds(self):
        c = self.cur(NEW, "2026-11-07T16:00:00Z")
        self.assertEqual(build.plan(c, NEW, False, NOW), build.ACTION_REBUILD)

    def test_same_version_over_20h_rebuilds(self):
        c = self.cur(NEW, "2026-11-07T06:00:00Z")
        self.assertEqual(build.plan(c, NEW, False, NOW), build.ACTION_REBUILD)

    def test_downgrade_refused_then_allowed(self):
        c = self.cur(NEW, "2026-11-07T06:00:00Z")
        with self.assertRaisesRegex(build.BuildError, "lower than"):
            build.plan(c, OLD, False, NOW)
        self.assertEqual(build.plan(c, OLD, True, NOW), build.ACTION_DOWNLOAD)

    def test_same_version_lexically_lower_still_compares_numerically(self):
        c = self.cur("149.0.7.10", "2026-11-07T06:00:00Z")
        with self.assertRaises(build.BuildError):
            build.plan(c, "149.0.7.9", False, NOW)

    def test_future_published_refused(self):
        c = self.cur(NEW, "2026-11-08T12:30:00Z")
        with self.assertRaisesRegex(build.BuildError, "future"):
            build.plan(c, NEW, False, NOW)

    def test_slightly_future_published_skips(self):
        c = self.cur(NEW, "2026-11-08T12:02:00Z")
        self.assertEqual(build.plan(c, NEW, False, NOW), build.ACTION_SKIP)


class ListingTest(Base):
    def test_parse_ignores_extra_platforms(self):
        version, urls = build.parse_listing(listing_doc(NEW))
        self.assertEqual(version, NEW)
        self.assertEqual(set(urls["chrome"]), set(PLATFORMS))
        self.assertEqual(build.url_mismatches(version, urls), [])

    def test_url_mismatch_is_refused(self):
        doc = listing_doc(NEW, urls={("chrome", "linux64"): "https://evil.example/chrome.zip"})
        version, urls = build.parse_listing(doc)
        self.assertEqual(len(build.url_mismatches(version, urls)), 1)
        with self.assertRaisesRegex(build.BuildError, "does not match the template"):
            build.check_urls(version, urls)

    def test_missing_platform_is_refused(self):
        with self.assertRaisesRegex(build.BuildError, "linux64"):
            build.parse_listing(listing_doc(NEW, drop=("chrome-headless-shell", "linux64")))

    def test_duplicate_platform_is_refused(self):
        doc = listing_doc(NEW)
        doc["channels"]["Stable"]["downloads"]["chrome"].append(
            {"platform": "linux64", "url": url_for(NEW, "chrome", "linux64")})
        with self.assertRaisesRegex(build.BuildError, "listed twice"):
            build.parse_listing(doc)

    def test_bad_version_refused(self):
        doc = listing_doc(NEW)
        doc["channels"]["Stable"]["version"] = "149.0.7"
        with self.assertRaisesRegex(build.BuildError, "a.b.c.d"):
            build.parse_listing(doc)

    def test_url_mismatch_run_publishes_nothing(self):
        self.serve_listing(NEW, urls={("chrome", "win64"): "https://evil.example/x.zip"})
        self.serve_zips(NEW)
        rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        self.assertEqual(self.web.zip_calls(), [])
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("FAILED", self.summary_text())

    def test_missing_platform_run_publishes_nothing(self):
        self.web.serve(CFT, json.dumps(listing_doc(NEW, drop=("chrome", "mac-x64"))).encode())
        rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        self.assertEqual(self.web.zip_calls(), [])
        self.assertFalse(os.path.exists(self.out))


class ZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pins-zip-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def zip_file(self, members):
        path = os.path.join(self.tmp, "x.zip")
        with open(path, "wb") as f:
            f.write(make_zip_bytes(members))
        return path

    def exe(self, name="chrome", platform="linux64"):
        return build.exe_member(name, platform)

    def test_good_zip_passes_for_every_pair(self):
        for name in NAMES:
            for p in PLATFORMS:
                path = os.path.join(self.tmp, "g.zip")
                with open(path, "wb") as f:
                    f.write(good_zip(NEW, name, p))
                build.check_zip(path, name, p)  # no exception

    def test_stray_top_level_entry(self):
        path = self.zip_file({"README.txt": b"x", self.exe(): b"x"})
        with self.assertRaisesRegex(build.BuildError, "README.txt"):
            build.check_zip(path, "chrome", "linux64")

    def test_dotdot_entry(self):
        path = self.zip_file({"chrome-linux64/../x": b"x", self.exe(): b"x"})
        with self.assertRaisesRegex(build.BuildError, r"\.\./x"):
            build.check_zip(path, "chrome", "linux64")

    def test_dotdot_inside_prefix_backslash(self):
        path = self.zip_file({"chrome-linux64/a\\..\\..\\x": b"x", self.exe(): b"x"})
        with self.assertRaises(build.BuildError):
            build.check_zip(path, "chrome", "linux64")

    def test_absolute_entry(self):
        path = self.zip_file({"/abs/chrome-linux64/x": b"x", self.exe(): b"x"})
        with self.assertRaises(build.BuildError):
            build.check_zip(path, "chrome", "linux64")

    def test_backslash_absolute_entry(self):
        path = self.zip_file({"\\chrome-linux64\\x": b"x", self.exe(): b"x"})
        with self.assertRaises(build.BuildError):
            build.check_zip(path, "chrome", "linux64")

    def test_drive_letter_entry(self):
        path = self.zip_file({"C:chrome-linux64/x": b"x", self.exe(): b"x"})
        with self.assertRaises(build.BuildError):
            build.check_zip(path, "chrome", "linux64")

    def test_missing_executable(self):
        path = self.zip_file({"chrome-linux64/LICENSE": b"x"})
        with self.assertRaisesRegex(build.BuildError, "has no"):
            build.check_zip(path, "chrome", "linux64")

    def test_executable_in_wrong_place(self):
        path = self.zip_file({"chrome-linux64/sub/chrome": b"x"})
        with self.assertRaises(build.BuildError):
            build.check_zip(path, "chrome", "linux64")

    def test_not_a_zip(self):
        path = os.path.join(self.tmp, "bad.zip")
        with open(path, "wb") as f:
            f.write(b"not a zip at all")
        with self.assertRaisesRegex(build.BuildError, "not a readable zip"):
            build.check_zip(path, "chrome", "linux64")

    def test_corrupt_member_detected(self):
        # a stored (uncompressed) member: flip one byte of its data, so only the CRC catches it
        payload = b"A" * 5000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr(self.exe(), payload)
        data = bytearray(buf.getvalue())
        pos = data.find(payload)
        self.assertGreater(pos, 0)
        data[pos + 100] ^= 0xFF
        path = os.path.join(self.tmp, "c.zip")
        with open(path, "wb") as f:
            f.write(bytes(data))
        with self.assertRaisesRegex(build.BuildError, "corrupt"):
            build.check_zip(path, "chrome", "linux64")


class ScenarioTest(Base):
    def test_first_run_publishes_all_eight(self):
        zips = self.serve_new(NEW)
        rc, out, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.web.zip_calls()), 8)
        outs = self.gh_outputs()
        self.assertEqual(outs["publish"], "true")
        self.assertEqual(outs["version"], NEW)
        doc = self.written_doc()
        self.assertEqual(doc["published"], NOW_S)
        self.assertEqual(doc["stable"]["version"], NEW)
        for name in NAMES:
            for p in PLATFORMS:
                data = zips[(name, p)]
                self.assertEqual(doc["stable"]["downloads"][name][p],
                                 {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        self.assertEqual(os.listdir(self.out), [build.MANIFEST_NAME])

    def test_new_version_replaces_older(self):
        self.serve_new(NEW)
        cur = self.current()
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertEqual(self.written_doc()["stable"]["version"], NEW)
        self.assertEqual(self.gh_outputs()["publish"], "true")

    def test_same_version_under_20h_does_not_publish(self):
        self.serve_listing(OLD)
        cur = self.current(manifest_doc(version=OLD, published="2026-11-08T00:30:00Z"))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertEqual(self.web.zip_calls(), [])
        self.assertEqual(self.gh_outputs(), {"publish": "false"})
        self.assertFalse(os.path.exists(self.out))

    def test_same_version_over_20h_rebuilt_without_downloads(self):
        self.serve_listing(OLD)
        cur_doc = manifest_doc(version=OLD, published="2026-11-07T06:00:00Z")
        cur = self.current(cur_doc)
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertEqual(self.web.zip_calls(), [])
        doc = self.written_doc()
        self.assertEqual(doc["published"], NOW_S)
        self.assertEqual(doc["stable"]["version"], OLD)
        self.assertEqual(doc["stable"]["downloads"], cur_doc["stable"]["downloads"])
        self.assertEqual(self.gh_outputs()["publish"], "true")
        self.assertEqual(self.gh_outputs()["version"], OLD)

    def test_downgrade_refused(self):
        self.serve_listing(OLD)
        cur = self.current(manifest_doc(version=NEW, published="2026-11-07T06:00:00Z"))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        self.assertEqual(self.web.zip_calls(), [])
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("refusing", self.summary_text())

    def test_downgrade_allowed_publishes(self):
        self.serve_new(OLD)
        cur = self.current(manifest_doc(version=NEW, published="2026-11-07T06:00:00Z"))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN, "--allow-downgrade"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.written_doc()["stable"]["version"], OLD)
        self.assertIn("DOWNGRADE", self.summary_text())

    def test_published_must_increase_on_download(self):
        self.serve_new(NEW)
        cur = self.current(manifest_doc(version=OLD, published="2026-11-08T12:00:00Z"))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        self.assertEqual(self.web.zip_calls(), [])
        self.assertFalse(os.path.exists(self.out))

    def test_same_version_published_equal_to_now_is_skip(self):
        # age 0 is under 20 h: no rebuild, so published never has to move
        self.serve_listing(OLD)
        cur = self.current(manifest_doc(version=OLD, published=NOW_S))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)  # age 0 is under 20 h: skip, not a rebuild
        self.assertEqual(self.gh_outputs(), {"publish": "false"})

    def test_published_strictly_later_than_current(self):
        self.serve_new(NEW)
        cur = self.current(manifest_doc(version=OLD, published="2026-11-08T12:00:01Z"))
        rc, _, _ = self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)

    def test_bad_zip_via_run_publishes_nothing(self):
        # one of the 8 zips has a stray top-level entry: nothing is written, temp files are gone
        self.serve_listing(NEW)
        self.serve_zips(NEW, overrides={
            ("chrome", "mac-arm64"): make_zip_bytes({
                "stray.txt": b"x",
                build.exe_member("chrome", "mac-arm64"): b"x"})})
        rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        # it stops at the first failing zip (chrome mac-arm64, the sixth download)
        self.assertEqual(self.web.zip_calls()[-1], url_for(NEW, "chrome", "mac-arm64"))
        self.assertEqual(len(self.web.zip_calls()), 6)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".zip")], [])

    def test_size_cap(self):
        self.serve_new(NEW)
        with mock.patch.object(build, "MAX_DOWNLOAD_BYTES", 10):
            rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".zip")], [])

    def test_declared_length_over_cap_refused_before_reading(self):
        url = url_for(NEW, "chrome", "linux64")
        self.web.serve(url, b"x" * 10, headers={"Content-Length": "999999"})
        with mock.patch.object(build, "MAX_DOWNLOAD_BYTES", 100):
            with self.assertRaisesRegex(build.BuildError, "declares"):
                build.download(url, os.path.join(self.tmp, "d.zip"), 100)

    def test_cap_is_700_mib(self):
        self.assertEqual(build.MAX_DOWNLOAD_BYTES, 700 * 1024 * 1024)

    def test_exact_json_shape_and_key_order(self):
        self.serve_new(NEW)
        rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        text = self.written().decode("utf-8")
        self.assertTrue(text.endswith("}\n") and not text.endswith("\n\n"))
        order = []

        def hook(pairs):
            order.append([k for k, _ in pairs])
            return dict(pairs)

        doc = json.loads(text, object_pairs_hook=hook)
        self.assertEqual(order[-1], ["schema", "published", "stable"])
        top = [o for o in order if o == ["schema", "published", "stable"]]
        self.assertEqual(len(top), 1)
        self.assertIn(["version", "downloads"], order)
        self.assertIn(["chrome-headless-shell", "chrome"], order)
        self.assertIn(["win64", "mac-arm64", "mac-x64", "linux64"], order)
        self.assertIn(["sha256", "size"], order)
        # byte-exact: json.dumps indent=2 of the same content, trailing newline
        self.assertEqual(text, json.dumps(doc, indent=2) + "\n")
        self.assertEqual(list(doc["stable"]["downloads"]), ["chrome-headless-shell", "chrome"])

    def test_trusted_comment(self):
        self.serve_new(NEW)
        rc, _, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh_outputs()["trusted_comment"],
                         f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run={RUN}")
        self.assertEqual(self.gh_outputs()["trusted_comment"],
                         build.trusted_comment(NOW_S, NEW, RUN))

    def test_check_comment_round_trip(self):
        self.serve_new(NEW)
        self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        path = os.path.join(self.out, build.MANIFEST_NAME)
        comment = self.gh_outputs()["trusted_comment"]
        build.check_comment(path, comment)
        with self.assertRaises(build.BuildError):
            build.check_comment(path, comment.replace("version=", "version=9."))
        with self.assertRaises(build.BuildError):
            build.check_comment(path, comment.replace(f"run={RUN}", "run=abc"))

    def test_run_id_from_environment(self):
        self.serve_new(NEW)
        os.environ["GITHUB_RUN_ID"] = "777"
        rc, _, _ = self.main(["none", "--out-dir", self.out])
        self.assertEqual(rc, 0)
        self.assertTrue(self.gh_outputs()["trusted_comment"].endswith("run=777"))

    def test_run_id_missing_or_not_digits_fails(self):
        for args in (["none", "--out-dir", self.out],
                     ["none", "--out-dir", self.out, "--run-id", ""],
                     ["none", "--out-dir", self.out, "--run-id", "12a"],
                     ["none", "--out-dir", self.out, "--run-id", "-1"],
                     ["none", "--out-dir", self.out, "--run-id", "1 2"]):
            rc, _, _ = self.main(args)
            self.assertEqual(rc, 1, args)
        self.assertEqual(self.web.calls, [])

    def test_missing_current_file_fails(self):
        rc, _, _ = self.main([os.path.join(self.tmp, "nope.json"), "--out-dir", self.out,
                              "--run-id", RUN])
        self.assertEqual(rc, 1)

    def test_no_output_on_publish_false(self):
        self.serve_listing(OLD)
        cur = self.current(manifest_doc(version=OLD, published="2026-11-08T11:00:00Z"))
        self.main([cur, "--out-dir", self.out, "--run-id", RUN])
        self.assertFalse(os.path.exists(self.out))

    def test_local_run_prints_outputs_to_stdout(self):
        del os.environ["GITHUB_OUTPUT"]
        self.serve_new(NEW)
        rc, out, _ = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 0)
        self.assertIn("publish=true\n", out)
        self.assertIn(f"trusted_comment=clauseo-browser-pins v1 published={NOW_S} version={NEW} run={RUN}", out)

    def test_live_check_downloads_nothing(self):
        self.serve_listing(NEW)
        rc, out, _ = self.main(["--live-check"])
        self.assertEqual(rc, 0)
        self.assertIn(f"Stable version: {NEW}", out)
        self.assertEqual(out.count("https://storage.googleapis.com/"), 8)
        self.assertEqual(self.web.calls, [CFT])

    def test_live_check_mismatch_fails(self):
        self.web.serve(CFT, json.dumps(listing_doc(NEW, urls={
            ("chrome", "linux64"): "https://elsewhere.example/c.zip"})).encode())
        rc, _, _ = self.main(["--live-check"])
        self.assertEqual(rc, 1)

    def test_failure_writes_summary_and_nothing_else(self):
        rc, _, err = self.main(["none", "--out-dir", self.out, "--run-id", RUN])
        self.assertEqual(rc, 1)  # CFT not served -> fetch fails
        self.assertIn("FAILED", self.summary_text())
        self.assertFalse(os.path.exists(self.out))


class ValidateTest(Base):
    def valid(self):
        return manifest_doc(version=NEW, published=NOW_S)

    def test_valid_passes(self):
        build.validate_manifest(self.valid())

    def assertBad(self, doc, pattern=None):
        with self.assertRaises(build.BuildError) as ctx:
            build.validate_manifest(doc)
        if pattern:
            self.assertRegex(str(ctx.exception), pattern)

    def test_bad_cases(self):
        def mutate(f):
            d = self.valid()
            f(d)
            return d

        self.assertBad(mutate(lambda d: d.update(schema=2)), "schema")
        self.assertBad(mutate(lambda d: d.update(schema=True)), "schema")
        self.assertBad(mutate(lambda d: d.update(published="2026-11-08 12:00:00")), "published")
        self.assertBad(mutate(lambda d: d.update(published="2026-02-30T12:00:00Z")), "real UTC")
        self.assertBad(mutate(lambda d: d["stable"].update(version="149.0.7")), "a.b.c.d")
        self.assertBad(mutate(lambda d: d.update(extra=1)), "keys")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"].pop("linux64")), "keys")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            sha256="A" * 64)), "hex")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            sha256="a" * 63)), "hex")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            size=0)), "positive")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            size=True)), "positive")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            size="5")), "positive")
        self.assertBad(mutate(lambda d: d["stable"]["downloads"]["chrome"]["linux64"].update(
            url="https://x")), "keys")

    def test_wrong_key_order_refused(self):
        d = self.valid()
        d = {"published": d["published"], "schema": d["schema"], "stable": d["stable"]}
        self.assertBad(d, "order")

    def test_nested_wrong_order_refused(self):
        d = self.valid()
        e = d["stable"]["downloads"]["chrome"]["linux64"]
        d["stable"]["downloads"]["chrome"]["linux64"] = {"size": e["size"], "sha256": e["sha256"]}
        self.assertBad(d, "order")


class CommentTest(Base):
    def test_check_comment_accepts_the_exact_form(self):
        path = write_json(os.path.join(self.tmp, "m.json"), manifest_doc(version=NEW, published=NOW_S))
        build.check_comment(path, f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run=1")

    def test_check_comment_refuses(self):
        path = write_json(os.path.join(self.tmp, "m.json"), manifest_doc(version=NEW, published=NOW_S))
        for bad in (f"clauseo-browser-pins v1 published={NOW_S} version={NEW}",
                    f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run=",
                    f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run=1 ",
                    f"clauseo-browser-pins v1 published=2026-11-07T06:00:00Z version={NEW} run=1",
                    f"clauseo-browser-pins v1 published={NOW_S} version=148.0.0.0 run=1",
                    f"clauseo-blocklist v1 generated={NOW_S} entries=1"):
            with self.assertRaises(build.BuildError, msg=bad):
                build.check_comment(path, bad)

    def test_trusted_comment_format(self):
        self.assertEqual(build.trusted_comment(NOW_S, NEW, RUN),
                         f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run={RUN}")


class SigningScriptsTest(unittest.TestCase):
    """Only the failure paths: no key or signature is generated here."""

    def run_script(self, script, args, cwd, env=None, stdin=None):
        return subprocess.run(["bash", os.path.join(ROOT, "scripts", script)] + args,
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=60)

    def test_sign_refuses_without_secrets(self):
        tmp = tempfile.mkdtemp(prefix="pins-sign-")
        try:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("MINISIGN_SECRET_KEY", "MINISIGN_PASSWORD")}
            env["KEY_FILE"] = os.path.join(tmp, "k")
            r = self.run_script("sign.sh", ["f", "c"], tmp, env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("MINISIGN_SECRET_KEY is empty", r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("minisign"), "minisign is not installed")
    def test_verify_fails_closed_without_keys(self):
        tmp = tempfile.mkdtemp(prefix="pins-verify-")
        try:
            for f in ("m.json", "m.json.minisig"):
                open(os.path.join(tmp, f), "w").close()
            r = self.run_script("verify.sh", ["m.json", "m.json.minisig", "c"], tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing or empty", r.stderr)
            r = self.run_script("verify.sh", ["--either", "m.json", "m.json.minisig"], tmp)
            self.assertNotEqual(r.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("minisign"), "minisign is not installed")
    def test_verify_refuses_garbage_signature(self):
        tmp = tempfile.mkdtemp(prefix="pins-verify-")
        try:
            with open(os.path.join(tmp, "minisign.pub"), "w") as f:
                f.write("untrusted comment: test\nRWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
            with open(os.path.join(tmp, "minisign-next.pub"), "w") as f:
                f.write("untrusted comment: test\nRWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
            for f in ("m.json", "m.json.minisig"):
                with open(os.path.join(tmp, f), "w") as fh:
                    fh.write("junk\n")
            r = self.run_script("verify.sh", ["m.json", "m.json.minisig", "c"], tmp)
            self.assertNotEqual(r.returncode, 0)
            r = self.run_script("verify.sh", ["--either", "m.json", "m.json.minisig"], tmp)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ValidateModeTest(Base):
    """build.py --validate: the publish job's check. It must never touch the network."""

    MIB = 1024 * 1024

    def new_file(self, doc=None, name="new.json"):
        return write_json(os.path.join(self.tmp, name), doc or manifest_doc(
            version=NEW, published=NOW_S, size=5 * self.MIB))

    def validate(self, new, current="none", extra=(), now=NOW):
        # the local rules only: the Google checks have their own class
        args = ["--validate", new, current, "--run-id", RUN, "--no-google"] + list(extra)
        return self.main(args, now=now)

    def test_first_run_passes_and_computes_comment(self):
        rc, _, _ = self.validate(self.new_file())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh_outputs(), {
            "trusted_comment": f"clauseo-browser-pins v1 published={NOW_S} version={NEW} run={RUN}",
            "version": NEW})
        self.assertEqual(self.web.calls, [])  # no network, no zips
        self.assertFalse(os.path.exists(self.out))  # and it writes no manifest

    def test_newer_than_current_passes(self):
        cur = self.current(manifest_doc(version=OLD, published="2026-11-07T06:00:00Z"))
        rc, _, _ = self.validate(self.new_file(), cur)
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh_outputs()["version"], NEW)
        self.assertEqual(self.web.calls, [])

    def test_same_version_later_passes(self):
        cur = self.current(manifest_doc(version=NEW, published="2026-11-07T06:00:00Z"))
        rc, _, _ = self.validate(self.new_file(), cur)
        self.assertEqual(rc, 0)

    def test_size_bounds_are_inclusive(self):
        for size, want in ((self.MIB - 1, 1), (self.MIB, 0),
                           (700 * self.MIB, 0), (700 * self.MIB + 1, 1)):
            self.gh_outputs_reset()
            rc, _, _ = self.validate(self.new_file(manifest_doc(
                version=NEW, published=NOW_S, size=size)))
            self.assertEqual(rc, want, size)

    def test_one_bad_size_among_eight_fails(self):
        doc = manifest_doc(version=NEW, published=NOW_S, size=5 * self.MIB)
        doc["stable"]["downloads"]["chrome"]["mac-x64"]["size"] = 10
        rc, _, _ = self.validate(self.new_file(doc))
        self.assertEqual(rc, 1)
        self.assertEqual(self.gh_outputs(), {})

    def test_published_five_minutes_ahead_passes_six_fails(self):
        ok = manifest_doc(version=NEW, published="2026-11-08T12:05:00Z", size=5 * self.MIB)
        self.assertEqual(self.validate(self.new_file(ok))[0], 0)
        self.gh_outputs_reset()
        bad = manifest_doc(version=NEW, published="2026-11-08T12:05:01Z", size=5 * self.MIB)
        rc, _, _ = self.validate(self.new_file(bad))
        self.assertEqual(rc, 1)
        self.assertIn("future", self.summary_text())

    def test_schema_violation_fails(self):
        doc = manifest_doc(version=NEW, published=NOW_S, size=5 * self.MIB)
        doc["extra"] = 1
        self.assertEqual(self.validate(self.new_file(doc))[0], 1)

    def test_bad_hex_fails(self):
        doc = manifest_doc(version=NEW, published=NOW_S, size=5 * self.MIB)
        doc["stable"]["downloads"]["chrome"]["linux64"]["sha256"] = "F" * 64
        self.assertEqual(self.validate(self.new_file(doc))[0], 1)

    def test_missing_new_file_fails(self):
        self.assertEqual(self.validate(os.path.join(self.tmp, "absent.json"))[0], 1)

    def test_published_must_be_strictly_later_than_current(self):
        cur = self.current(manifest_doc(version=OLD, published=NOW_S))
        self.assertEqual(self.validate(self.new_file(), cur)[0], 1)  # equal
        cur2 = self.current(manifest_doc(version=OLD, published="2026-11-08T12:00:01Z"),
                            name="cur2.json")
        self.assertEqual(self.validate(self.new_file(), cur2)[0], 1)  # new is earlier
        cur3 = self.current(manifest_doc(version=OLD, published="2026-11-08T11:59:59Z"),
                            name="cur3.json")
        self.assertEqual(self.validate(self.new_file(), cur3)[0], 0)  # new is later

    def test_lower_version_refused_unless_allowed(self):
        cur = self.current(manifest_doc(version=NEW, published="2026-11-07T06:00:00Z"))
        new = self.new_file(manifest_doc(version=OLD, published=NOW_S, size=5 * self.MIB))
        self.assertEqual(self.validate(new, cur)[0], 1)
        self.gh_outputs_reset()
        self.assertEqual(self.validate(new, cur, extra=["--allow-downgrade"])[0], 0)
        self.assertEqual(self.gh_outputs()["version"], OLD)

    def test_downgrade_still_needs_later_published(self):
        cur = self.current(manifest_doc(version=NEW, published=NOW_S))
        new = self.new_file(manifest_doc(version=OLD, published=NOW_S, size=5 * self.MIB))
        self.assertEqual(self.validate(new, cur, extra=["--allow-downgrade"])[0], 1)

    def test_invalid_current_fails(self):
        bad = manifest_doc(version=OLD, published="2026-11-07T06:00:00Z")
        bad["schema"] = 2
        cur = self.current(bad)
        self.assertEqual(self.validate(self.new_file(), cur)[0], 1)

    def test_missing_current_fails(self):
        self.assertEqual(self.validate(self.new_file(), os.path.join(self.tmp, "nope.json"))[0], 1)

    def test_run_id_required(self):
        args = ["--validate", self.new_file(), "none"]
        self.assertEqual(self.main(args)[0], 1)
        self.assertEqual(self.main(args + ["--run-id", "x1"])[0], 1)

    def test_run_id_from_environment(self):
        os.environ["GITHUB_RUN_ID"] = "4242"
        rc, _, _ = self.main(["--validate", self.new_file(), "none", "--no-google"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.gh_outputs()["trusted_comment"].endswith("run=4242"))

    def test_comment_is_computed_from_manifest_fields(self):
        rc, _, _ = self.validate(self.new_file(manifest_doc(
            version="150.1.2.3", published="2026-11-08T10:00:00Z", size=5 * self.MIB)))
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh_outputs()["trusted_comment"],
                         f"clauseo-browser-pins v1 published=2026-11-08T10:00:00Z "
                         f"version=150.1.2.3 run={RUN}")

    def gh_outputs_reset(self):
        if os.path.exists(self.gh_output):
            os.remove(self.gh_output)


class GoogleCheckTest(Base):
    """build.py --validate's Google checks: the listed version, and each file's HEAD length."""

    MIB = 1024 * 1024

    def setUp(self):
        super().setUp()
        self.serve_listing(NEW)

    def serve_heads(self, version=NEW, size=5 * MIB, sizes=None, status=200, headers=None):
        """Serve HEAD answers for the 8 files of version. sizes: {(name, platform): length}."""
        sizes = sizes or {}
        for name in NAMES:
            for p in PLATFORMS:
                length = sizes.get((name, p), size)
                h = headers if headers is not None else {"Content-Length": str(length)}
                self.heads.serve(url_for(version, name, p), status=status, headers=h)

    def new_doc(self, version=NEW, size=5 * MIB, sizes=None):
        doc = manifest_doc(version=version, published=NOW_S, size=size)
        for (name, p), length in (sizes or {}).items():
            doc["stable"]["downloads"][name][p]["size"] = length
        return doc

    def validate_google(self, doc, current="none"):
        path = write_json(os.path.join(self.tmp, "new.json"), doc)
        return self.main(["--validate", path, current, "--run-id", RUN])

    def test_pass_checks_listing_and_all_eight_heads(self):
        self.serve_heads()
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh_outputs()["version"], NEW)
        self.assertEqual(self.web.calls, [CFT])
        want = sorted(url_for(NEW, n, p) for n in NAMES for p in PLATFORMS)
        self.assertEqual(sorted(self.heads.calls), want)
        self.assertEqual(self.web.zip_calls(), [])

    def test_version_not_listed_fails(self):
        # a higher version nobody lists: its files may even exist, the version still fails
        self.serve_heads(version="999.0.0.0")
        rc, _, _ = self.validate_google(self.new_doc(version="999.0.0.0"))
        self.assertEqual(rc, 1)
        self.assertIn("not Google's listed Stable", self.summary_text())
        self.assertEqual(self.gh_outputs(), {})
        self.assertEqual(self.heads.calls, [])

    def test_lower_listed_version_fails(self):
        self.serve_heads(version="156.0.0.0")
        rc, _, _ = self.validate_google(self.new_doc(version="156.0.0.0"))
        self.assertEqual(rc, 1)

    def test_listing_url_mismatch_fails(self):
        self.web.serve(CFT, json.dumps(listing_doc(NEW, urls={
            ("chrome", "win64"): "https://evil.example/c.zip"})).encode())
        self.serve_heads()
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)
        self.assertEqual(self.heads.calls, [])

    def test_404_on_a_file_fails(self):
        self.serve_heads()
        self.heads.serve(url_for(NEW, "chrome", "linux64"), status=404)
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 404", self.summary_text())
        self.assertEqual(self.gh_outputs(), {})

    def test_size_mismatch_fails(self):
        self.serve_heads(sizes={("chrome", "mac-arm64"): 5 * self.MIB + 1})
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)
        self.assertIn("chrome mac-arm64", self.summary_text())
        self.assertIn("bytes", self.summary_text())

    def test_wrong_size_from_the_manifest_fails(self):
        # the manifest's size is 5 MiB; Google's file is the length served
        self.serve_heads(size=6 * self.MIB)
        rc, _, _ = self.validate_google(self.new_doc(size=5 * self.MIB))
        self.assertEqual(rc, 1)

    def test_missing_content_length_fails(self):
        self.serve_heads(headers={})
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)
        self.assertIn("no Content-Length", self.summary_text())

    def test_non_numeric_content_length_fails(self):
        self.serve_heads(headers={"Content-Length": "lots"})
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)

    def test_non_200_success_status_fails(self):
        self.serve_heads(status=204)
        rc, _, _ = self.validate_google(self.new_doc())
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 204", self.summary_text())

    def test_no_google_skips_all_network(self):
        self.serve_heads(version="999.0.0.0")
        path = write_json(os.path.join(self.tmp, "new.json"), self.new_doc(version="999.0.0.0"))
        rc, _, _ = self.main(["--validate", path, "none", "--run-id", RUN, "--no-google"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.web.calls, [])
        self.assertEqual(self.heads.calls, [])

    def test_local_rules_still_apply_with_google(self):
        self.serve_heads()
        doc = self.new_doc()
        doc["stable"]["downloads"]["chrome"]["linux64"]["size"] = 10  # below 1 MiB
        rc, _, _ = self.validate_google(doc)
        self.assertEqual(rc, 1)
        self.assertEqual(self.heads.calls, [])

    def test_head_request_uses_head_method(self):
        with mock.patch.object(build.urllib.request, "urlopen") as uo:
            uo.return_value = io.BytesIO(b"")
            REAL_OPEN_HEAD("https://example.invalid/x.zip")
        req = uo.call_args[0][0]
        self.assertEqual(req.get_method(), "HEAD")
        self.assertEqual(req.full_url, "https://example.invalid/x.zip")

    def test_open_head_turns_http_errors_into_build_errors(self):
        import urllib.error
        err = urllib.error.HTTPError("https://example.invalid/x.zip", 404, "Not Found", {}, None)
        with mock.patch.object(build.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(build.BuildError, "HTTP 404"):
                REAL_OPEN_HEAD("https://example.invalid/x.zip")


if __name__ == "__main__":
    unittest.main()
