# Contributing

## Development Setup

Bridge:

```bash
cd bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Vision:

```bash
cd vision
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m pytest -q
```

Extension syntax checks:

```bash
node --check extension/background.js
node --check extension/content.js
node --check extension/popup/popup.js
```

## Loading The Extension

Open `chrome://extensions`, enable Developer mode, choose Load unpacked, and select
the `extension/` directory. The popup lets you set the bridge URL, optional bridge
token, and browser ID.

## Pull Requests

- Keep bridge and vision test suites green.
- Add tests for behavior changes, especially auth, vault, spawn, and guard logic.
- Keep extension permissions least-privilege and document any permission changes.
- Do not include secrets, browser profiles, vault data, or private audit files.
- For security-sensitive changes, describe the threat model and compatibility
  impact in the PR.
