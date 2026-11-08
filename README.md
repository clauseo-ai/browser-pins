# Clauseo browser pins

A signed manifest that pins the [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
builds the [Clauseo](https://clauseo.chat) app uses: `chrome` and `chrome-headless-shell`, by
version, sha256 and size, for each platform. A scheduled GitHub Actions workflow refreshes it every
3 hours from Google's Stable channel. Each publish is signed with
[minisign](https://jedisct1.github.io/minisign/) and replaces the two assets on the release tagged
`current`.

The format, the keys and what the app does with the manifest are in [SPEC.md](SPEC.md). That file is the
contract; this README only describes how to use it.

## What the app fetches

- https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json
- https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json.minisig

The app checks the signature with a public key compiled into it, and it never replaces its pins with
older ones.

## Verifying by hand

The public keys are `minisign.pub` (current) and `minisign-next.pub` (next, used after a rotation),
both at the root of this repo. The app accepts either.

```sh
curl -LO https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json
curl -LO https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json.minisig
minisign -V -p minisign.pub -m browser-pins-v1.json -x browser-pins-v1.json.minisig
# -> Signature and comment signature verified
# -> Trusted comment: clauseo-browser-pins v1 published=... version=... run=...
```

The trusted comment is exactly
`clauseo-browser-pins v1 published=<published> version=<version> run=<run_id>`. Its `published` and
`version` must match the body, which this repo's build script checks:

```sh
python3 build.py --check-comment browser-pins-v1.json "<the trusted comment above>"
# -> trusted comment matches the manifest body
```

`run` is the GitHub Actions run that signed the file:
`https://github.com/clauseo-ai/browser-pins/actions/runs/<run_id>`.

## How a publish works

[`build.py`](build.py) uses only the Python 3 standard library. On each run it:

1. reads Google's Stable version and download list, and checks that all four platforms are listed for both
   binaries and that each URL equals the fixed template
   `https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/{name}-{platform}.zip`;
2. for a **new version**, streams all 8 zips from that template (at most 700 MiB each), computes sha256 and
   size, and checks each zip: every entry is under `{name}-{platform}/` with no absolute or `..` path, and the
   executable is present;
3. for the **same version**, republishes once the current manifest is 20 hours old, with a fresh
   `published` and the current verified hashes, with no downloads;
4. for a **lower version**, stops unless the manual `allow_downgrade` input is set;
5. writes `browser-pins-v1.json`, reads it back and checks it against the format.

Nothing is published, and the current assets stay, when any check fails. The workflow
([`.github/workflows/publish.yml`](.github/workflows/publish.yml)) signs the file, verifies the signature
with `minisign.pub` and the exact trusted comment, and then uploads both assets.

The unit tests need no network:

```sh
python3 -m unittest -v
```

The two signing-script tests that need `minisign` are skipped unless it is installed.

## Licence

The code in this repo is licensed under `GPL-3.0-or-later` (see [LICENSE](LICENSE)), like Clauseo.
The pins are Google's Chrome for Testing builds, under Google's terms.
