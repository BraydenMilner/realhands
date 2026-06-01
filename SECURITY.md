# Security

## Reporting

Please report security issues privately before opening a public issue. If you do
not have a private contact for the maintainer, open a minimal public issue asking
for a security contact without including exploit details. Include the affected
component, reproduction steps, expected impact, and any logs that do not contain
secrets once a private channel is established.

## Trust Boundary

`realhands` is designed as a local control plane. The bridge binds to `127.0.0.1` by
default, and the extension connects to that loopback bridge over WebSocket. Treat
the bridge as a powerful local API: it can drive the browser profile that loaded
the extension.

Do not expose the bridge to a shared network without adding your own network-layer
controls. On shared or multi-user hosts, set `REALHANDS_BRIDGE_TOKEN`.

## Bridge Token

When `REALHANDS_BRIDGE_TOKEN` is set:

- REST calls require `X-RealHands-Token: <token>`.
- The extension WebSocket handshake includes `?token=<token>`.
- Missing or wrong tokens are rejected.

When the token is unset, localhost workflows remain compatible and the bridge logs
a startup warning. The token is a bearer secret; store it like a password.

## WebSocket Origin Checks

The executor WebSocket rejects normal `http://` and `https://` web origins. It
accepts absent or `null` origins and Chrome extension origins so that a hostile web
page cannot directly open `ws://localhost:7878/` and speak the executor protocol.

## Vault API

`/credentials/read` can return local credential values. It is disabled by default
and returns `vault_api_disabled` unless `REALHANDS_VAULT_API_ENABLED=1` is set. When
enabled, it requires `REALHANDS_BRIDGE_TOKEN` and the matching token header.

## Hardening Checklist

- Keep the bridge on loopback unless you have a reviewed deployment plan.
- Set `REALHANDS_BRIDGE_TOKEN` on shared machines.
- Enable `REALHANDS_VAULT_API_ENABLED=1` only for workflows that truly need it.
- Review extension permissions before release changes.
- Do not commit private audit notes, tokens, vault data, or browser profiles.
