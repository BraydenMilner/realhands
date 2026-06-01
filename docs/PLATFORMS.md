# Platform Support

## What works where

| Feature | Linux | macOS | Windows |
|---|---|---|---|
| Multiplex driving (`/call`, `/sequence`, `/close` by `browser_id`) | Yes | Yes | Yes |
| Manual extension loading (Developer mode) | Yes | Yes | Yes |
| One-call `/spawn` auto-swarm | Yes | Yes | Implemented, unverified |
| Policy setup CLI (`policy_setup.py`) | Yes | Yes | Implemented, unverified |

Multiplex driving works everywhere today — it is pure Python (FastAPI + WebSocket
routing keyed by `browser_id`) with no platform-specific code.

## Auto-spawn (`POST /spawn`)

The `/spawn` endpoint launches a fresh Chrome profile that self-registers with the
bridge. This requires:

1. **Chrome installed** on the machine. The bridge auto-detects the binary per OS,
   or you can set `CHROME_BIN` to the full path.
2. **The RealHands extension force-installed via Chrome managed policy.** Modern
   Chrome ignores `--load-extension` on fresh profiles; a managed-policy
   `ExtensionInstallForcelist` is the only reliable way to get the extension into
   every new profile automatically. This policy must be installed **once** with
   admin/sudo privileges.

### One-time policy setup

```bash
# Linux / macOS (requires sudo)
sudo python3 bridge/policy_setup.py install --extension-id <YOUR_EXTENSION_ID> --yes

# Check status
python3 bridge/policy_setup.py status

# Remove later
sudo python3 bridge/policy_setup.py remove --yes
```

The `<YOUR_EXTENSION_ID>` comes from `chrome://extensions` in Developer mode with
the unpacked extension loaded. You also need a packed `.crx` file — see below.

#### Packing the extension

```bash
# From the repo root (produces realhands.crx + realhands.pem)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --pack-extension=extension/ --pack-extension-key=realhands.pem
# On Linux:
google-chrome --pack-extension=extension/ --pack-extension-key=realhands.pem
```

If you don't have a `.pem` key yet, omit `--pack-extension-key` on the first run
to generate one. Keep the `.pem` private.

### How the policy works per OS

**Linux:**
- Writes `/etc/opt/chrome/policies/managed/realhands.json` with
  `ExtensionInstallForcelist` and `ExtensionInstallSources` pointing at a local
  `file://` update XML.
- Requires `sudo`.

**macOS:**
- Writes managed preferences to `/Library/Managed Preferences/com.google.Chrome.plist`
  via `defaults write`, plus a fallback JSON policy to `/etc/chrome/policies/managed/realhands.json`.
- Requires `sudo`.
- The launcher (Chrome detection, env, process management) is verified on macOS. The
  managed-policy force-install itself follows Chrome's documented mechanism but has not
  yet been confirmed end-to-end on a real Mac (macOS managed preferences can require a
  `cfprefsd` refresh before Chrome picks them up).

**Windows (UNVERIFIED):**
- Writes registry keys under
  `HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist` and
  `ExtensionInstallSources`.
- Requires an elevated (Administrator) terminal.
- The registry paths and values follow Chrome's policy documentation but have **not
  been tested on a real Windows machine**.

### Chrome binary detection

The bridge searches for Chrome in this order:

1. `CHROME_BIN` environment variable (if set, used directly).
2. Platform-specific search:
   - **Linux:** `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser` (via `$PATH`).
   - **macOS:** `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
   - **Windows:** `chrome.exe` under `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%LocalAppData%\Google\Chrome\Application`.
3. If nothing is found, a clear error is raised.

### DISPLAY / X11

- **Linux:** `DISPLAY` and `XAUTHORITY` are passed to the Chrome subprocess (default
  `:10` and `~/.Xauthority`, configurable via env).
- **macOS / Windows:** These variables are **not** set — the real desktop is used.

### Process management

- **Linux / macOS:** Chrome is launched with `start_new_session=True`; on close,
  `os.killpg(SIGTERM)` kills the entire process group.
- **Windows:** Chrome is launched with `CREATE_NEW_PROCESS_GROUP`; on close,
  `taskkill /F /T /PID` kills the entire process tree.

## No-admin fallback

If you cannot or do not want to run the policy setup with admin/sudo:

1. Load the unpacked extension in Chrome Developer mode (`chrome://extensions`).
2. Use a **persistent profile** (your real Chrome profile or a named profile).
3. Drive it manually via `/call` and `/sequence`.

This works on all platforms but requires manual setup for each browser and does not
support the one-call `/spawn` auto-swarm flow (since the extension won't be in a
fresh profile).
