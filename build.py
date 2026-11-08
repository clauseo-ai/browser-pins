#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the Clauseo browser pins v1 (see SPEC.md).

  build.py CURRENT --out-dir OUT [--allow-downgrade] [--run-id N]
  build.py --live-check
  build.py --check-comment MANIFEST COMMENT

CURRENT is the path of the current verified manifest, or "none" on the first run.
A publishing run writes OUT/browser-pins-v1.json and reports publish, version and
trusted_comment (to $GITHUB_OUTPUT in Actions, else to stdout). publish=false means
the current manifest is still fresh: nothing is written.

Python 3 standard library only. Signing and verifying are the workflow's job.

Exit status: 0 on success (including publish=false), 1 on any failure. Nothing is
written to OUT unless every check passed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib

CFT_URL = ("https://googlechromelabs.github.io/chrome-for-testing/"
           "last-known-good-versions-with-downloads.json")
URL_TEMPLATE = ("https://storage.googleapis.com/chrome-for-testing-public/"
                "{version}/{platform}/{name}-{platform}.zip")
NAMES = ("chrome-headless-shell", "chrome")          # output order
PLATFORMS = ("win64", "mac-arm64", "mac-x64", "linux64")  # output order; others are ignored
SCHEMA = 1
COMMENT_PREFIX = "clauseo-browser-pins v1"
MANIFEST_NAME = "browser-pins-v1.json"
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

MAX_DOWNLOAD_BYTES = 700 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
CHUNK = 1024 * 1024
REPUBLISH_AFTER = _dt.timedelta(hours=20)
FUTURE_SLACK = _dt.timedelta(minutes=5)

USER_AGENT = "clauseo-browser-pins/1 (+https://github.com/clauseo-ai/browser-pins)"
TIMEOUT = 120

VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){3}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
PUBLISHED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
RUN_RE = re.compile(r"[0-9]+")
COMMENT_RE = re.compile(r"clauseo-browser-pins v1 published=(\S+) version=(\S+) run=([0-9]+)")

ACTION_SKIP = "skip"          # same version, current manifest still fresh
ACTION_REBUILD = "rebuild"    # same version, stale: fresh published, current hashes
ACTION_DOWNLOAD = "download"  # new version (or first run): download and inspect all 8 zips


class BuildError(Exception):
    """A failure: no output, non-zero exit."""


# --------------------------------------------------------------------------
# Network: the one function tests replace
# --------------------------------------------------------------------------

def open_url(url: str):
    """Open a URL and return a readable response. The only network access in this file."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        raise BuildError(f"fetch {url}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise BuildError(f"fetch {url}: {e}") from None


def _read_capped(resp, cap: int, what: str) -> bytes:
    try:
        data = resp.read(cap + 1)
    except (OSError, http.client.HTTPException) as e:
        raise BuildError(f"fetch {what}: {e}") from None
    if len(data) > cap:
        raise BuildError(f"fetch {what}: more than {cap} bytes")
    return data


def fetch_json(url: str):
    with open_url(url) as resp:
        body = _read_capped(resp, MAX_JSON_BYTES, url)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BuildError(f"fetch {url}: not JSON") from None


def _declared_length(resp) -> int | None:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Content-Length")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def download(url: str, dest: str, cap: int) -> tuple[str, int]:
    """Stream url to dest, refusing more than cap bytes. Return (sha256 hex, size)."""
    with open_url(url) as resp:
        declared = _declared_length(resp)
        if declared is not None and declared > cap:
            raise BuildError(f"download {url}: declares {declared} bytes, over the {cap} cap")
        digest = hashlib.sha256()
        size = 0
        with open(dest, "wb") as f:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except (OSError, http.client.HTTPException) as e:
                    raise BuildError(f"download {url}: {e}") from None
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise BuildError(f"download {url}: more than {cap} bytes; refusing")
                digest.update(chunk)
                f.write(chunk)
    return digest.hexdigest(), size


# --------------------------------------------------------------------------
# Versions, times, URLs, executables
# --------------------------------------------------------------------------

def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def expected_url(version: str, name: str, platform: str) -> str:
    return URL_TEMPLATE.format(version=version, platform=platform, name=name)


def exe_member(name: str, platform: str) -> str:
    """The executable inside the zip, which must be present."""
    prefix = f"{name}-{platform}/"
    if name == "chrome":
        if platform == "win64":
            return prefix + "chrome.exe"
        if platform == "linux64":
            return prefix + "chrome"
        return prefix + "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    if platform == "win64":
        return prefix + "chrome-headless-shell.exe"
    return prefix + "chrome-headless-shell"


def parse_published(value) -> _dt.datetime:
    if not isinstance(value, str) or not PUBLISHED_RE.fullmatch(value):
        raise BuildError(f"published {value!r} is not YYYY-MM-DDTHH:MM:SSZ")
    try:
        return _dt.datetime.strptime(value, TIME_FMT).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        raise BuildError(f"published {value!r} is not a real UTC time") from None


def format_time(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime(TIME_FMT)


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def trusted_comment(published: str, version: str, run_id: str) -> str:
    return f"{COMMENT_PREFIX} published={published} version={version} run={run_id}"


# --------------------------------------------------------------------------
# Google's listing
# --------------------------------------------------------------------------

def parse_listing(doc) -> tuple[str, dict[str, dict[str, str]]]:
    """Return (Stable version, {name: {platform: url}}) for our 8 downloads."""
    try:
        stable = doc["channels"]["Stable"]
        version = stable["version"]
        downloads = stable["downloads"]
    except (KeyError, TypeError):
        raise BuildError("Chrome for Testing JSON: no channels.Stable version and downloads") from None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise BuildError(f"Chrome for Testing JSON: Stable version {version!r} is not a.b.c.d")
    if not isinstance(downloads, dict):
        raise BuildError("Chrome for Testing JSON: downloads is not an object")
    urls: dict[str, dict[str, str]] = {}
    for name in NAMES:
        entries = downloads.get(name)
        if not isinstance(entries, list):
            raise BuildError(f"Chrome for Testing JSON: no downloads.{name} list")
        found: dict[str, str] = {}
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("platform"), str)
                    or not isinstance(entry.get("url"), str)):
                raise BuildError(f"Chrome for Testing JSON: malformed downloads.{name} entry")
            platform = entry["platform"]
            if platform not in PLATFORMS:
                continue  # win32, linux-arm64 and the like are not ours
            if platform in found:
                raise BuildError(f"downloads.{name}: platform {platform} is listed twice")
            found[platform] = entry["url"]
        missing = [p for p in PLATFORMS if p not in found]
        if missing:
            raise BuildError(f"downloads.{name}: no {', '.join(missing)} download listed")
        urls[name] = found
    return version, urls


def url_mismatches(version: str, urls: dict[str, dict[str, str]]) -> list[str]:
    bad = []
    for name in NAMES:
        for platform in PLATFORMS:
            want = expected_url(version, name, platform)
            got = urls[name][platform]
            if got != want:
                bad.append(f"{name} {platform}: Google lists {got}, expected {want}")
    return bad


def check_urls(version: str, urls: dict[str, dict[str, str]]) -> None:
    bad = url_mismatches(version, urls)
    if bad:
        raise BuildError("listed URL does not match the template: " + "; ".join(bad))


# --------------------------------------------------------------------------
# Manifest: read, validate, write
# --------------------------------------------------------------------------

def _keys(obj, keys: list[str], where: str) -> None:
    if not isinstance(obj, dict) or list(obj) != keys:
        raise BuildError(f"{where}: keys must be exactly {', '.join(keys)}, in that order")


def validate_manifest(doc):
    """Raise BuildError unless doc has exactly the SPEC shape, keys in SPEC order."""
    _keys(doc, ["schema", "published", "stable"], "manifest")
    if type(doc["schema"]) is not int or doc["schema"] != SCHEMA:
        raise BuildError("schema must be 1")
    parse_published(doc["published"])
    stable = doc["stable"]
    _keys(stable, ["version", "downloads"], "stable")
    if not isinstance(stable["version"], str) or not VERSION_RE.fullmatch(stable["version"]):
        raise BuildError(f"stable.version {stable['version']!r} is not a.b.c.d")
    downloads = stable["downloads"]
    _keys(downloads, list(NAMES), "stable.downloads")
    for name in NAMES:
        _keys(downloads[name], list(PLATFORMS), f"stable.downloads.{name}")
        for platform in PLATFORMS:
            entry = downloads[name][platform]
            where = f"stable.downloads.{name}.{platform}"
            _keys(entry, ["sha256", "size"], where)
            sha = entry["sha256"]
            if not isinstance(sha, str) or not HEX64_RE.fullmatch(sha):
                raise BuildError(f"{where}.sha256 must be 64 lowercase hex digits")
            size = entry["size"]
            if type(size) is not int or size <= 0:
                raise BuildError(f"{where}.size must be a positive integer")
    return doc


def read_manifest(path: str):
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_MANIFEST_BYTES + 1)
    except OSError as e:
        raise BuildError(f"read {path}: {e}") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BuildError(f"{path}: larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BuildError(f"{path}: not JSON") from None
    return validate_manifest(doc)


def build_manifest(published: str, version: str, downloads: dict) -> dict:
    return {
        "schema": SCHEMA,
        "published": published,
        "stable": {
            "version": version,
            "downloads": {
                name: {p: {"sha256": downloads[name][p]["sha256"],
                           "size": downloads[name][p]["size"]}
                       for p in PLATFORMS}
                for name in NAMES
            },
        },
    }


def write_manifest(out_dir: str, manifest: dict) -> str:
    """Write OUT/browser-pins-v1.json via a temp file, read it back and validate it."""
    data = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, MANIFEST_NAME)
    tmp = final + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        with open(tmp, "rb") as f:
            back = f.read(MAX_MANIFEST_BYTES + 1)
        if back != data:
            raise BuildError(f"{tmp}: read back differs from what was written")
        try:
            validate_manifest(json.loads(back.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            raise BuildError(f"{tmp}: not JSON") from None
        os.replace(tmp, final)
    except OSError as e:
        raise BuildError(f"write {final}: {e}") from None
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return final


# --------------------------------------------------------------------------
# Zip inspection
# --------------------------------------------------------------------------

def check_member(member: str, prefix: str, label: str) -> None:
    segments = re.split(r"[/\\]", member)
    if (not member.startswith(prefix) or member.startswith(("/", "\\"))
            or re.match(r"[A-Za-z]:", member) or ".." in segments):
        raise BuildError(f"{label}: zip entry {member!r} is not under {prefix} "
                         "(stray top-level, absolute or .. path)")


def check_zip(path: str, name: str, platform: str) -> None:
    label = f"{name} {platform}"
    prefix = f"{name}-{platform}/"
    exe = exe_member(name, platform)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BuildError(f"{label}: not a readable zip: {e}") from None
    with zf:
        names = zf.namelist()
        for member in names:
            check_member(member, prefix, label)
        if exe not in names:
            raise BuildError(f"{label}: zip has no {exe}")
        try:
            bad = zf.testzip()
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError,
                NotImplementedError, zlib.error) as e:
            raise BuildError(f"{label}: zip is corrupt: {e}") from None
        if bad is not None:
            raise BuildError(f"{label}: zip member {bad!r} is corrupt")


def inspect_one(version: str, name: str, platform: str, tmpdir: str) -> dict:
    url = expected_url(version, name, platform)
    dest = os.path.join(tmpdir, f"{name}-{platform}.zip")
    try:
        sha, size = download(url, dest, MAX_DOWNLOAD_BYTES)
        check_zip(dest, name, platform)
    finally:
        with contextlib.suppress(OSError):
            os.remove(dest)
    return {"sha256": sha, "size": size}


def inspect_downloads(version: str, tmpdir: str) -> dict:
    return {name: {p: inspect_one(version, name, p, tmpdir) for p in PLATFORMS}
            for name in NAMES}


# --------------------------------------------------------------------------
# Deciding and running
# --------------------------------------------------------------------------

def plan(current, version: str, allow_downgrade: bool, now: _dt.datetime) -> str:
    if current is None:
        return ACTION_DOWNLOAD
    cur_version = current["stable"]["version"]
    if version_key(version) == version_key(cur_version):
        age = now - parse_published(current["published"])
        if age < -FUTURE_SLACK:
            raise BuildError(f"current published {current['published']} is in the future")
        return ACTION_SKIP if age < REPUBLISH_AFTER else ACTION_REBUILD
    if version_key(version) < version_key(cur_version) and not allow_downgrade:
        raise BuildError(f"Google's Stable is {version}, lower than the published {cur_version}; "
                         "refusing (allow_downgrade is off)")
    return ACTION_DOWNLOAD


def run_build(current_arg: str, out_dir: str, allow_downgrade: bool, run_id: str) -> tuple[dict, str]:
    """Return (outputs, summary text) or raise BuildError."""
    if not run_id or not RUN_RE.fullmatch(run_id):
        raise BuildError("run id is missing or not all digits (--run-id, or $GITHUB_RUN_ID)")
    current = None if current_arg == "none" else read_manifest(current_arg)

    version, urls = parse_listing(fetch_json(CFT_URL))
    check_urls(version, urls)

    now = now_utc()
    action = plan(current, version, allow_downgrade, now)
    if action == ACTION_SKIP:
        published = current["published"]
        return ({"publish": "false"},
                f"Stable {version} is unchanged and was published at {published}, less than "
                f"{int(REPUBLISH_AFTER.total_seconds() // 3600)} h ago. Nothing was published.")
    if current is not None and now <= parse_published(current["published"]):
        raise BuildError(f"published {format_time(now)} is not later than the current "
                         f"{current['published']}")

    if action == ACTION_REBUILD:
        downloads = current["stable"]["downloads"]
        how = "Same Stable version, republished with the current verified hashes (no downloads)."
    else:
        with tempfile.TemporaryDirectory(prefix="browser-pins-",
                                         dir=os.environ.get("RUNNER_TEMP") or None) as tmp:
            downloads = inspect_downloads(version, tmp)
        how = ("New Stable version, all 8 downloads inspected." if current is None or
               version_key(version) != version_key(current["stable"]["version"])
               else "Same version, re-downloaded.")
        if current is not None and version_key(version) < version_key(current["stable"]["version"]):
            how = "DOWNGRADE: Google lists an older Stable; published because allow_downgrade is set."

    published = format_time(now)
    manifest = validate_manifest(build_manifest(published, version, downloads))
    write_manifest(out_dir, manifest)
    comment = trusted_comment(published, version, run_id)

    lines = ["## Clauseo browser pins build", "", how, "", f"Trusted comment: `{comment}`", "",
             "| name | platform | sha256 | size |", "|---|---|---|---|"]
    for name in NAMES:
        for platform in PLATFORMS:
            e = downloads[name][platform]
            lines.append(f"| {name} | {platform} | `{e['sha256']}` | {e['size']} |")
    return ({"publish": "true", "version": version, "trusted_comment": comment},
            "\n".join(lines))


def check_comment(manifest_path: str, comment: str) -> None:
    doc = read_manifest(manifest_path)
    m = COMMENT_RE.fullmatch(comment)
    if not m:
        raise BuildError("trusted comment is not 'clauseo-browser-pins v1 published=<p> "
                         "version=<V> run=<digits>'")
    if m.group(1) != doc["published"] or m.group(2) != doc["stable"]["version"]:
        raise BuildError("trusted comment's published or version does not match the manifest body")


def live_check() -> None:
    """Fetch the Stable list, print V and the 8 URLs, check them against the template. No downloads."""
    version, urls = parse_listing(fetch_json(CFT_URL))
    print(f"Stable version: {version}")
    for name in NAMES:
        for platform in PLATFORMS:
            print(f"{name} {platform}: {urls[name][platform]}")
    check_urls(version, urls)
    print("template check: all 8 listed URLs match the template")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def emit(outputs: dict[str, str]) -> None:
    text = "".join(f"{k}={v}\n" for k, v in outputs.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("current", nargs="?",
                   help='path of the current verified manifest, or "none" on the first run')
    p.add_argument("--out-dir", help="where browser-pins-v1.json goes")
    p.add_argument("--allow-downgrade", action="store_true",
                   help="publish a lower Stable version (for Google pulling a release)")
    p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""),
                   help="GitHub Actions run ID for the trusted comment (default: $GITHUB_RUN_ID)")
    p.add_argument("--live-check", action="store_true",
                   help="fetch the Stable list, print it and check the URLs; download nothing")
    p.add_argument("--check-comment", nargs=2, metavar=("MANIFEST", "COMMENT"),
                   help="check that COMMENT is the trusted comment of MANIFEST")
    args = p.parse_args(argv)

    try:
        if args.live_check:
            live_check()
            return 0
        if args.check_comment:
            check_comment(*args.check_comment)
            print("trusted comment matches the manifest body")
            return 0
        if args.current is None or args.out_dir is None:
            p.error("CURRENT and --out-dir are required")
        outputs, summary = run_build(args.current, args.out_dir, args.allow_downgrade, args.run_id)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        write_summary("## Clauseo browser pins build FAILED\n\n"
                      f"**Reason:** {e}\n\nNothing was published; the current assets stay.")
        return 1

    write_summary(summary)
    print(summary, file=sys.stderr)
    emit(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
