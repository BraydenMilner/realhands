#!/usr/bin/env python3
"""RETIRED — Chrome managed-policy force-install is no longer used.

`/spawn` now launches **Chrome for Testing** with `--load-extension` (no admin,
no managed policy, no `.crx`, no extension ID). Branded Chrome 137+ silently
ignores `--load-extension`, and off-store force-install is blocked on unmanaged
consumer machines, so the old `ExtensionInstallForcelist` approach this file used
to implement does not work for the swarm.

See `docs/PLATFORMS.md`. This stub remains only so old references don't break.
"""
from __future__ import annotations

import sys

_NOTICE = (
    "policy_setup.py is retired.\n"
    "The swarm (/spawn) now uses Chrome for Testing with --load-extension —\n"
    "no admin, no managed policy, no .crx, no extension ID. Nothing to install.\n"
    "See docs/PLATFORMS.md.\n"
)


def main() -> int:
    sys.stderr.write(_NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
