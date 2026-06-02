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
import re
import shutil
import stat
import tempfile
import time
import uuid
import hashlib
import base64
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import PurePosixPath
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
_SHA256_ENV = "REALHANDS_CFT_SHA256"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_CHANNELS = frozenset({"Stable", "Beta", "Dev", "Canary"})
_CFT_HOST = "storage.googleapis.com"
_CFT_PATH_PREFIX = "/chrome-for-testing-public/"
_MANIFEST_NAME = ".realhands-cft.json"


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


def _validate_version(version: str) -> str:
    if not _VERSION_RE.fullmatch(version or ""):
        raise CftError(f"invalid CfT version {version!r}")
    return version


def _chrome_zip_name(platform_key: str) -> str:
    return f"chrome-{platform_key}.zip"


def _construct_download_url(version: str, platform_key: str) -> str:
    version = _validate_version(version)
    return (
        f"https://{_CFT_HOST}/chrome-for-testing-public/"
        f"{version}/{platform_key}/{_chrome_zip_name(platform_key)}"
    )


def _validate_download_url(url: str, *, version: str, platform_key: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    expected_path = (
        f"{_CFT_PATH_PREFIX}{version}/{platform_key}/{_chrome_zip_name(platform_key)}"
    )
    if parsed.scheme != "https" or parsed.netloc != _CFT_HOST or parsed.path != expected_path:
        raise CftError(f"unexpected CfT download URL: {url!r}")
    if parsed.query or parsed.fragment:
        raise CftError(f"unexpected CfT download URL parameters: {url!r}")
    return url


def _resolve_download(platform_key: str) -> tuple[str, str]:
    """Return (version, zip_url) for the desired CfT build.

    Honors REALHANDS_CFT_VERSION (exact pin) else REALHANDS_CFT_CHANNEL
    (default Stable) from the official versions-with-downloads JSON.
    """
    channel = os.environ.get(_CHANNEL_ENV, "Stable")
    pinned = os.environ.get(_PINNED_VERSION_ENV)

    if pinned:
        version = _validate_version(pinned)
        return version, _construct_download_url(version, platform_key)

    if channel not in _CHANNELS:
        raise CftError(f"unknown CfT channel {channel!r}")

    with urllib.request.urlopen(_CFT_VERSIONS_URL, timeout=30) as resp:  # noqa: S310
        data = json.load(resp)

    chan = data["channels"].get(channel)
    if chan is None:
        raise CftError(f"unknown CfT channel {channel!r}")

    version = _validate_version(chan["version"])
    downloads = chan["downloads"]["chrome"]
    for entry in downloads:
        if entry["platform"] == platform_key:
            url = _validate_download_url(entry["url"], version=version, platform_key=platform_key)
            return version, url
    raise CftError(f"no CfT chrome download for platform {platform_key!r}")


def _expected_sha256(platform_key: str) -> Optional[str]:
    platform_env = f"{_SHA256_ENV}_{platform_key.upper().replace('-', '_')}"
    expected = os.environ.get(platform_env) or os.environ.get(_SHA256_ENV)
    if not expected:
        return None
    expected = expected.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CftError(f"invalid SHA-256 in {platform_env} / {_SHA256_ENV}")
    return expected


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_md5_header(headers) -> Optional[str]:
    values = []
    if hasattr(headers, "get_all"):
        values.extend(headers.get_all("x-goog-hash") or [])
    one = headers.get("x-goog-hash")
    if one:
        values.append(one)
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part.startswith("md5="):
                return part[4:]
    return None


def _download_verified(
    zip_url: str,
    zip_path: Path,
    *,
    version: str,
    platform_key: str,
    expected_sha256: Optional[str],
) -> str:
    _validate_download_url(zip_url, version=version, platform_key=platform_key)
    log.info("downloading Chrome for Testing: %s", zip_url)
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()  # noqa: S324 - GCS object integrity header, not security auth.
    request = urllib.request.Request(zip_url)
    with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
        final_url = getattr(resp, "url", zip_url)
        _validate_download_url(final_url, version=version, platform_key=platform_key)
        expected_md5 = _extract_md5_header(resp.headers)
        if expected_sha256 is None and expected_md5 is None:
            raise CftError("CfT download did not include an integrity hash")
        with zip_path.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
                md5.update(chunk)
                out.write(chunk)
    digest = sha256.hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise CftError("CfT zip SHA-256 mismatch")
    if expected_md5 is not None:
        actual_md5 = base64.b64encode(md5.digest()).decode("ascii")
        if actual_md5 != expected_md5:
            raise CftError("CfT zip MD5 integrity mismatch")
    return digest


def _inside_dir(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _safe_member_parts(info: zipfile.ZipInfo, platform_key: str) -> tuple[str, ...]:
    raw_name = info.filename
    pure = PurePosixPath(raw_name)
    if pure.is_absolute() or any(part in ("", "..") for part in pure.parts):
        raise CftError(f"unsafe CfT zip member path: {raw_name!r}")
    expected_prefix = f"chrome-{platform_key}"
    if not pure.parts or pure.parts[0] != expected_prefix:
        raise CftError(f"unexpected CfT zip member path: {raw_name!r}")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO):
        raise CftError(f"unsafe CfT zip member type: {raw_name!r}")
    return tuple(pure.parts)


def _safe_extract(zf: zipfile.ZipFile, dest_dir: Path, platform_key: str) -> None:
    dest_root = dest_dir.resolve()
    for info in zf.infolist():
        parts = _safe_member_parts(info, platform_key)
        extracted = dest_root.joinpath(*parts)
        if not _inside_dir(extracted, dest_root):
            raise CftError(f"unsafe CfT zip member escaped destination: {info.filename!r}")
        mode = info.external_attr >> 16
        if info.is_dir():
            extracted.mkdir(parents=True, exist_ok=True)
            if platform.system() != "Windows":
                extracted.chmod(0o755)
            continue
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            if platform.system() == "Windows":
                raise CftError(f"unsafe CfT zip symlink on Windows: {info.filename!r}")
            target = zf.read(info).decode("utf-8", errors="strict")
            target_path = PurePosixPath(target)
            if target_path.is_absolute() or any(part in ("", "..") for part in target_path.parts):
                raise CftError(f"unsafe CfT zip symlink target: {info.filename!r}")
            resolved_target = (extracted.parent / target).resolve()
            if not _inside_dir(resolved_target, dest_root):
                raise CftError(f"unsafe CfT zip symlink escaped destination: {info.filename!r}")
            extracted.parent.mkdir(parents=True, exist_ok=True)
            if extracted.exists() or extracted.is_symlink():
                extracted.unlink()
            os.symlink(target, extracted)
            continue
        extracted.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, extracted.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        if platform.system() != "Windows":
            safe_mode = 0o755 if (mode & 0o111) else 0o644
            extracted.chmod(safe_mode)


def _write_manifest(
    dest_dir: Path,
    *,
    version: str,
    platform_key: str,
    zip_url: str,
    zip_sha256: str,
    binary: Path,
) -> None:
    manifest = {
        "version": version,
        "platform": platform_key,
        "url": zip_url,
        "zip_sha256": zip_sha256,
        "binary_relpath": str(binary.relative_to(dest_dir)),
        "binary_sha256": _sha256_file(binary),
    }
    path = dest_dir / _MANIFEST_NAME
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    if platform.system() != "Windows":
        path.chmod(0o600)


def _manifest_valid(dest_dir: Path, rel: Path, *, version: str, platform_key: str) -> bool:
    binary = dest_dir / rel
    manifest_path = dest_dir / _MANIFEST_NAME
    if not binary.exists() or not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if manifest.get("version") != version or manifest.get("platform") != platform_key:
        return False
    if manifest.get("binary_relpath") != str(rel):
        return False
    expected_binary_hash = manifest.get("binary_sha256")
    if not isinstance(expected_binary_hash, str):
        return False
    try:
        return _sha256_file(binary) == expected_binary_hash
    except OSError:
        return False


def _download_and_extract(zip_url: str, dest_dir: Path, *, version: str, platform_key: str) -> None:
    root = dest_dir.parent
    root.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        root.chmod(0o700)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=str(root)))
    zip_path = tmp_dir / "cft.zip"
    try:
        zip_sha256 = _download_verified(
            zip_url,
            zip_path,
            version=version,
            platform_key=platform_key,
            expected_sha256=_expected_sha256(platform_key),
        )
        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir(mode=0o755)
        rel = _binary_relpath(platform_key)
        binary = extract_dir / rel
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, extract_dir, platform_key)
        if not binary.exists():
            raise CftError(f"CfT extract did not contain expected binary at {binary}")
        _repair_bundle_perms(extract_dir)
        _ensure_executable(binary)
        _write_manifest(
            extract_dir,
            version=version,
            platform_key=platform_key,
            zip_url=zip_url,
            zip_sha256=zip_sha256,
            binary=binary,
        )
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        os.replace(extract_dir, dest_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    return Path(os.environ.get(_CACHE_DIR_ENV) or _DEFAULT_CACHE).expanduser()


def _version_sort_key(path: Path) -> tuple[int, int, int, int]:
    parts = path.name.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return (0, 0, 0, 0)
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


@contextmanager
def _install_lock(root: Path, version: str):
    root.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        root.chmod(0o700)
    lock_path = root / f".{version}.lock"
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + 300
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise CftError(f"timed out waiting for CfT install lock {lock_path}")
            time.sleep(0.2)
    try:
        os.write(fd, token.encode("ascii"))
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def find_cached_binary() -> Optional[str]:
    """Return a cached CfT binary path without any network access, or None."""
    platform_key = _platform_key()
    rel = _binary_relpath(platform_key)
    root = cache_dir()
    if not root.exists():
        return None
    pinned = os.environ.get(_PINNED_VERSION_ENV)
    if pinned:
        version = _validate_version(pinned)
        ver_dir = root / version
        if _manifest_valid(ver_dir, rel, version=version, platform_key=platform_key):
            return str(ver_dir / rel)
        return None

    candidates = sorted(
        [p for p in root.glob("*/") if _VERSION_RE.fullmatch(p.name)],
        key=_version_sort_key,
        reverse=True,
    )
    for ver_dir in candidates:
        if _manifest_valid(ver_dir, rel, version=ver_dir.name, platform_key=platform_key):
            binary = ver_dir / rel
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
    root = cache_dir().resolve()
    ver_dir = (root / version).resolve()
    if not _inside_dir(ver_dir, root):
        raise CftError("CfT install directory escapes cache root")
    binary = ver_dir / rel

    with _install_lock(root, version):
        if _manifest_valid(ver_dir, rel, version=version, platform_key=platform_key):
            _ensure_executable(binary)
            log.info("using cached Chrome for Testing %s at %s", version, binary)
            return str(binary)
        _download_and_extract(zip_url, ver_dir, version=version, platform_key=platform_key)
        if not _manifest_valid(ver_dir, rel, version=version, platform_key=platform_key):
            raise CftError("CfT install manifest validation failed")
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
