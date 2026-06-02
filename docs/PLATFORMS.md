# Platform Support

## What works where

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Single browser: load the extension + drive your real Chrome | ✅ | ✅ | ✅ |
| Multiplex driving (`/call`, `/sequence`, `/close` by `browser_id`) | ✅ | ✅ | ✅ |
| One-call `/spawn` auto-swarm | ✅ | ✅ (verified) | ✅ (same mechanism — confirm on a real Windows box) |

The **core** experience — load the extension into your own Chrome and drive it —
works everywhere with no admin and no extra downloads. The **swarm** is an
optional, advanced feature.

## The core (no admin, no download)

1. Load the unpacked extension once: `chrome://extensions` → Developer mode →
   **Load unpacked** → the `extension/` folder.
2. Run the bridge: double-click `start-mac.command` / `start-windows.bat`, or
   `cd bridge && uvicorn bridge:app --host 127.0.0.1 --port 7878`.
3. Hand your agent [`AGENTS.md`](../AGENTS.md) and drive your real, logged-in
   Chrome through the local API. That's it.

## Advanced: the swarm (`POST /spawn`)

`/spawn` launches a *fresh* browser that already has the extension and
self-registers with the bridge under its `browser_id`, so one agent can run many
browsers at once (address each by `browser_id` in `/call`; close with
`POST /browsers/<id>/close`).

**Why Chrome for Testing.** Branded Google Chrome 137+ **silently ignores**
`--load-extension`, so a freshly-spawned profile would never receive the
extension. The old workaround — a Chrome managed policy
(`ExtensionInstallForcelist`) — needs admin, shows a "Managed by your
organization" banner, can't be installed silently on modern macOS, and is
**blocked entirely on unmanaged consumer Windows machines**. So RealHands instead
uses **Chrome for Testing** (CfT): Google's official, versioned automation build
where `--load-extension` still works. It's a plain unzip — **no admin, no policy,
no prompts** — and it's the only mechanism that is `works=yes / admin=no` on both
macOS and Windows.

**How it works.** On the first `/spawn`, the bridge downloads + caches CfT
(~150 MB, once) under `~/.cache/realhands/chrome-for-testing/<version>/`, then
launches it with `--load-extension=extension/` plus the register URL. Subsequent
spawns reuse the cache. The download is lazy — it never blocks bridge startup and
never happens in the single-browser path.

The default release is pinned to CfT `149.0.7827.54` with built-in SHA-256 hashes
for `linux64`, `mac-arm64`, `mac-x64`, `win32`, and `win64`. If you opt into a
moving channel or a custom version, set `REALHANDS_CFT_SHA256` or the
platform-specific `REALHANDS_CFT_SHA256_<PLATFORM>` value to keep SHA-256 pinning;
otherwise RealHands falls back to the Google Cloud Storage object integrity header.

**Optional knobs (all env vars — no admin):**

| Variable | Effect |
|---|---|
| `CHROME_BIN` | Use an existing CfT/Chromium binary; skip the download |
| `REALHANDS_CFT_CHANNEL` | Opt into a moving channel: `Stable` / `Beta` / `Dev` / `Canary` |
| `REALHANDS_CFT_VERSION` | Pin an exact CfT version for deterministic swarms |
| `REALHANDS_CFT_SHA256` | Expected zip SHA-256 for a custom pinned version |
| `REALHANDS_CFT_SHA256_<PLATFORM>` | Per-platform zip SHA-256 override, e.g. `REALHANDS_CFT_SHA256_MAC_ARM64` |
| `REALHANDS_CFT_CACHE_DIR` | Relocate the cache |

Pre-warm (e.g. before an offline run): `python3 -m bridge.chrome_for_testing`
downloads CfT and prints the cached binary path.

**Notes.**
- CfT profiles are clean (no existing logins) — expected for a swarm, where each
  browser logs in per-instance. The single-browser path still drives your real,
  logged-in Chrome.
- macOS `/spawn` is verified end-to-end. Windows uses the identical mechanism
  (CfT `win64` + `--load-extension`) and should be confirmed on a real Windows box
  before being marked verified.
- The retired `policy_setup.py` (managed-policy force-install) is no longer used.

## Chrome detection (single-browser / manual)

For the core path you load the extension into whatever Chrome you already use.
If a branded Chrome binary is needed, the bridge searches `CHROME_BIN`, then
per-OS defaults (Linux `$PATH`: `google-chrome`/`chromium`; macOS
`/Applications/Google Chrome.app`; Windows `%ProgramFiles%`/`%LocalAppData%`).
