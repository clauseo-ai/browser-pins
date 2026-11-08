# Clauseo browser pins v1: the published format (agreed with stage E)

Agreed between the stage E build thread (iris) and stage F (boxbox) on 2026-11-08.

## What it is
A signed manifest that pins the Chrome for Testing builds Clauseo uses: `chrome` and
`chrome-headless-shell`, by version, sha256 and size, for each platform. Agents browse
untrusted sites and Chrome ships security fixes often, so the pin can't be frozen into an
app release. It is published here and refreshed by CI.

## Where
- **Repo:** `clauseo-ai/browser-pins` (public), owned by stage F.
- **Release:** fixed tag `current`. Each publish **replaces** its two assets:
  - `https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json`
  - `https://github.com/clauseo-ai/browser-pins/releases/download/current/browser-pins-v1.json.minisig`
- **Bundled snapshot:** Clauseo release builds carry a verified copy at
  `<resource_dir>/browser-pins/browser-pins-v1.json` and `.minisig`.

## The keys
Minisign keys of their own, separate from the block list key.
- **Current:** `minisign.pub`. Its secret key and password are secrets of the `signing`
  environment (`MINISIGN_SECRET_KEY`, `MINISIGN_PASSWORD`), which only `main` can use. They are
  also kept in an encrypted backup held by stage F.
- **Next:** `minisign-next.pub`. Its secret key exists only in that encrypted backup until a rotation.
- **The app compiles in both** and accepts either, so a rotation needs no emergency app release.

## The format
```json
{
  "schema": 1,
  "published": "<RFC3339 UTC, seconds, Z>",
  "stable": {
    "version": "<a.b.c.d>",
    "downloads": {
      "chrome-headless-shell": {
        "win64":     {"sha256": "<64 lowercase hex>", "size": <bytes>},
        "mac-arm64": {"sha256": "…", "size": 0},
        "mac-x64":   {"sha256": "…", "size": 0},
        "linux64":   {"sha256": "…", "size": 0}
      },
      "chrome": { "win64": {…}, "mac-arm64": {…}, "mac-x64": {…}, "linux64": {…} }
    }
  }
}
```
- **No URLs.** The app builds every download URL from one fixed template on Google's
  storage, so a manifest can't redirect a download:
  `https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/{name}-{platform}.zip`
- **No win-arm64:** Google publishes none. ARM64 Windows uses win64 under emulation.
- **The trusted comment,** which the signature covers, is exactly:
  `clauseo-browser-pins v1 published=<published> version=<version> run=<run_id>`
  The app checks that `published` and `version` match the body. `run` is the GitHub Actions run
  ID that signed the file (`https://github.com/clauseo-ai/browser-pins/actions/runs/<run_id>`), so
  any pins file can be traced to its run.

## What the app does (stage E)
- Verifies the signature and trusted comment with the compiled-in key.
- Downloads from the template URL, caps the download at `size`, and refuses a sha256 or size mismatch.
- Caches the newest verified manifest. Uses the newer of the bundled and cached manifests.
- **No rollback:** refuses a manifest whose `published` is older, or whose `stable.version`
  is lower, than its newest verified one.
- Fails closed if it has never verified a manifest.
- Refuses a browser more than one Stable major behind the manifest, and offers the update.
- Shows a non-blocking notice when its newest manifest is older than 14 days.

## What CI does (stage F)
- Runs every 3 hours and on manual dispatch.
- Reads `channels.Stable` from
  `https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json`.
- **A new version:** downloads all 8 zips (2 binaries × 4 platforms) from the template URL, and
  requires Google's listed URL to equal the template. Each zip must hold only entries under
  `{name}-{platform}/`, with no absolute or `..` paths, and must contain the executable.
  Records sha256 and size.
- **The same version:** republishes once a day with a fresh `published` (reusing the verified
  current hashes), so the app can tell a live feed from a frozen one.
- **A lower version:** refuses unless the manual input `allow_downgrade` is set (for Google pulling a release).
- **Any failed check:** nothing is published, and the current assets stay. No partial manifests.
- **The sha256 and size always come from our own download** from the template URL, never from a feed.

## Repo hardening
- `main` is protected (pull request, code owner review, no force push, no deletion).
- CODEOWNERS covers `.github/`, the sign and verify scripts and both public keys.
- The signing job runs only on `main` (`schedule`, `workflow_dispatch`) in the `signing`
  environment. Pull request workflows get no secrets, and nothing uses `pull_request_target`.
- Every action is pinned by full commit SHA.
