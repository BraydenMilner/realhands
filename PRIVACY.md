# Privacy

`realhands` is local-first. The bridge and extension do not include telemetry,
analytics, or a hosted control service.

## Local Data

The Chrome extension stores local configuration in `chrome.storage.local`:

- Bridge URL.
- Optional bridge token.
- Browser ID.
- Recent connection/status metadata shown in the popup.

The bridge stores spawned browser profile data under the configured profiles
directory when persistent swarm profiles are used. Ephemeral profiles are removed
when the spawned browser closes.

The optional vision tier stores a local JSONL audit log and content-addressed
screenshots under `~/.local/share/realhands-vision/` by default. Audit rows redact
common secret patterns and sensitive URL query/fragment values, but screenshots can
still contain page content visible to the browser.

## Credential Vault

The optional vault stores credential records locally. Vault encryption uses a local
key managed by the bridge vault implementation and the operating system keyring
when available. The `/credentials/read` API is disabled by default because it can
return secret values to a caller with bridge access.

## Network Activity

The core bridge and extension communicate over loopback and do not phone home.
If you use the optional vision tier or bring your own model endpoint, screenshots
and task context are sent to the model service you configure. Cloud escalation is
controlled by your environment and model settings.

The side panel microphone button uses Chrome's Web Speech API when available. Use
of speech recognition is opt-in per click and may involve browser/platform speech
services depending on your Chrome configuration.

## User Control

You control which Chrome profile loads the extension, which pages are open, which
bridge URL the extension connects to, and whether a bridge token or vault API is
enabled.
