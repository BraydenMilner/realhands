#!/usr/bin/env python3
"""Chrome for Testing (CfT) resolver/installer for the RealHands swarm.

WHY THIS EXISTS
---------------
As of Chrome 137 (June 2025) the ``--load-extension`` command-line flag is
permanently removed from *branded* Google Chrome on macOS and Windows (all
channels). On branded Chrome 138-150 the flag is silently ignored — a spawned
profile never gets the extension, so the swarm's ``/register`` self-identify
flow can never start. This was confirmed empirically on Chrome 148 (the
extension's service worker target is absent under branded Chrome, present under
CfT).

Chrome for Testing is Google's officially-sanctioned, versioned, NON-auto-updating
build that *keeps* ``--load-extension`` working on macOS and Windows. It is a
plain unzip (no installer, no admin, no managed policy, no "Managed by your
organization" banner, no scary prompts). This module resolves a cached CfT
binary or downloads one on first use, so ``/spawn`` can launch
``<cft> --load-extension=<ext> --user-data-dir=<profile> <register-url>``.

The download is done ONCE per machine and cached under
``~/.cache/realhands/chrome-for-testing/<version>/``. Subsequent spawns reuse it.

No third-party deps: uses urllib + zipfile from the stdlib. If
``@puppeteer/browsers`` (npx) happens to be present it is NOT required.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import stat
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("agent_bridge.cft")

# Official CfT discovery endpoint (JSON, public, no auth).
_CFT_VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)

_DEFAULT_CACHE = Path.home() / ".cache" / "realhands" / "chrome-for-testing"

# Pin a known-good version by default for deterministic swarms. Override with
# REALHANDS_CFT_VERSION=<full version> or REALHANDS_CFT_CHANNEL=Stable to track
# the latest. Empirically verified working on this version family.
_PINNED_VERSION_ENV = "REALHANDS_CFT_VERSION"
_CHANNEL_ENV = "REALHANDS_CFT_CHANNEL"  # Stable | Beta | Dev | Canary
_CACHE_DIR_ENV = "REALHANDS_CFT_CACHE_DIR"


class CftError(RuntimeError):
    """Raised when CfT cannot be resolved or installed."""


def _platform_key() -> str:
    """Map this OS/arch to a CfT download platform key."""
    sysname = platform.system()
    machine = platform.machine().lower()
    if sysname == "Darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if sysname == "Windows":
        # CfT publishes win32 and win64; default to 64-bit.
        return "win64" if machine.endswith("64") else "win32"
    if sysname == "Linux":
        return "linux64"
    raise CftError(f"unsupported platform for Chrome for Testing: {sysname}/{machine}")


def _binary_relpath(platform_key: str) -> Path:
    """Path to the chrome binary INSIDE an extracted CfT zip, per platform."""
    if platform_key.startswith("mac"):
        app = f"chrome-{platform_key}/Google Chrome for Testing.app"
        return Path(app) / "Contents" / "MacOS" / "Google Chrome for Testing"
    if platform_key.startswith("win"):
        return Path(f"chrome-{platform_key}") / "chrome.exe"
    return Path(f"chrome-{platform_key}") / "chrome"


def _resolve_download(platform_key: str) -> tuple[str, str]:
    """Return (version, zip_url) for the desired CfT build.

    Honors REALHANDS_CFT_VERSION (exact pin) else REALHANDS_CFT_CHANNEL
    (default Stable) from the official versions-with-downloads JSON.
    """
    channel = os.environ.get(_CHANNEL_ENV, "Stable")
    pinned = os.environ.get(_PINNED_VERSION_ENV)

    with urllib.request.urlopen(_CFT_VERSIONS_URL, timeout=30) as resp:  # noqa: S310
        data = json.load(resp)

    chan = data["channels"].get(channel)
    if chan is None:
        raise CftError(f"unknown CfT channel {channel!r}")

    version = pinned or chan["version"]
    downloads = chan["downloads"]["chrome"]
    for entry in downloads:
        if entry["platform"] == platform_key:
            return version, entry["url"]
    raise CftError(f"no CfT chrome download for platform {platform_key!r}")


def _download_and_extract(zip_url: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "cft.zip"
    log.info("downloading Chrome for Testing: %s", zip_url)
    urllib.request.urlretrieve(zip_url, zip_path)  # noqa: S310
    with zipfile.ZipFile(zip_path) as zf:
        # zipfile.extractall() drops Unix permissions, which leaves the CfT
        # helper executables (chrome_crashpad_handler, the GPU/Renderer Helper
        # .app binaries) NON-executable -> "Permission denied" -> Chrome crashes
        # on launch. Preserve each entry's original mode from external_attr.
        for info in zf.infolist():
            extracted = Path(zf.extract(info, dest_dir))
            mode = (info.external_attr >> 16) & 0o7777
            if mode and platform.system() != "Windows":
                try:
                    extracted.chmod(mode)
                except OSError:
                    pass
    zip_path.unlink(missing_ok=True)
    _repair_bundle_perms(dest_dir)


def _repair_bundle_perms(root: Path) -> None:
    """Belt-and-suspenders: ensure every Mach-O launcher under the extract is
    executable, even if a zip entry lacked a stored mode. Targets files inside
    any */MacOS/ dir (the .app bundle executables) and the crashpad handler.
    """
    if platform.system() == "Windows":
        return
    add_x = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.parent.name == "MacOS" or "crashpad_handler" in path.name:
            try:
                path.chmod(path.stat().st_mode | add_x)
            except OSError:
                pass


def _ensure_executable(binary: Path) -> None:
    """zipfile drops the +x bit on macOS/Linux; restore it on the main binary
    (full-bundle repair is done at extract time by _repair_bundle_perms)."""
    if platform.system() == "Windows":
        return
    try:
        mode = binary.stat().st_mode
        binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def cache_dir() -> Path:
    return Path(os.environ.get(_CACHE_DIR_ENV) or _DEFAULT_CACHE)


def find_cached_binary() -> Optional[str]:
    """Return a cached CfT binary path without any network access, or None."""
    platform_key = _platform_key()
    rel = _binary_relpath(platform_key)
    root = cache_dir()
    if not root.exists():
        return None
    # Prefer the pinned/requested version dir if it resolves; else any cached.
    candidates = sorted(root.glob("*/"), reverse=True)
    for ver_dir in candidates:
        binary = ver_dir / rel
        if binary.exists():
            return str(binary)
    return None


def ensure_chrome_for_testing(offline_ok: bool = False) -> str:
    """Resolve a usable CfT binary path, downloading+caching on first use.

    Returns the absolute path to the CfT chrome binary. Idempotent: a second
    call with the same version reuses the cached extract. Set offline_ok=True
    to return a cached binary without ever hitting the network (raises if none
    is cached).
    """
    platform_key = _platform_key()
    rel = _binary_relpath(platform_key)

    if offline_ok:
        cached = find_cached_binary()
        if cached:
            return cached
        raise CftError("no cached Chrome for Testing and offline_ok=True")

    version, zip_url = _resolve_download(platform_key)
    ver_dir = cache_dir() / version
    binary = ver_dir / rel

    if binary.exists():
        _ensure_executable(binary)
        log.info("using cached Chrome for Testing %s at %s", version, binary)
        return str(binary)

    # Try cache for ANY version before downloading (a pin bump shouldn't force
    # a redownload if the box is offline mid-operation).
    _download_and_extract(zip_url, ver_dir)
    if not binary.exists():
        raise CftError(
            f"CfT extract did not contain expected binary at {binary}"
        )
    _ensure_executable(binary)
    log.info("installed Chrome for Testing %s at %s", version, binary)
    return str(binary)


if __name__ == "__main__":  # tiny CLI: `python3 -m bridge.chrome_for_testing`
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "path":
        cached = find_cached_binary()
        print(cached or "(none cached)")
    else:
        print(ensure_chrome_for_testing())
