"""SwarmSpawner — launches/kills headed Chrome profiles on demand.

Layer-2 process manager for the realhands swarm. Runs on the machine where the
bridge runs. Each spawned Chrome is a headed browser pointed at the bridge's
/register?browser_id=<ID> URL so the ONE extension (force-installed via Chrome
policy) learns its browser_id from the tab URL via the service worker's
tabs.onUpdated handler — no content script needed on http://localhost.

Cross-platform: supports Linux (X11), macOS (native desktop), and Windows.
Chrome binary is auto-detected per OS (overridable via CHROME_BIN env).
DISPLAY/XAUTHORITY are only set on Linux. Process management uses POSIX
start_new_session+killpg on Linux/macOS and CREATE_NEW_PROCESS_GROUP+
taskkill on Windows.

Design notes (honoring SWARM PROTOCOL v1):
- Chrome is launched DETACHED so the bridge process does not own it and a bridge
  restart never kills the swarm.
- The launch URL carries the id; the SW persists it to realhands_state.browser_id
  and (re)registers under <ID>.
- persistent=False -> ephemeral profile dir under the system temp area, removed
  on close. persistent=True -> a named reusable profile under profiles_dir kept
  on disk so logged-in sessions (cookies) survive close -> reopen.
- close() kills the whole process GROUP/TREE because Chrome forks helper
  processes; killing only the launcher pid would orphan them.
- NO network calls are made here; this is pure process management.

Imported by bridge.py. Config is read from env in bridge.py and passed in.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("agent_bridge.spawn")

_CHROME_FLAGS = (
    "--silent-debugger-extension-api",
    "--no-first-run",
    "--no-default-browser-check",
)

# The unpacked extension dir, force-loaded into every spawned browser.
_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"

# Chrome for Testing resolver: /spawn launches CfT (not branded Chrome) because
# branded Chrome 137+ silently ignores --load-extension, so a spawned profile
# would never receive the extension. CfT keeps the flag working — no admin, no
# managed policy, no "Managed by your organization" banner.
try:
    from chrome_for_testing import ensure_chrome_for_testing, find_cached_binary
except ImportError:  # keep spawn_manager importable even if the resolver is absent
    ensure_chrome_for_testing = None  # type: ignore[assignment]
    find_cached_binary = None  # type: ignore[assignment]

_EPHEMERAL_PREFIX = "realhands-swarm-"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validate_safe_name(value: str, field_name: str) -> str:
    if not _SAFE_NAME_RE.fullmatch(value) or ".." in value:
        raise ValueError(
            f"invalid {field_name}: must match {_SAFE_NAME_RE.pattern} "
            "and not contain '..'"
        )
    return value


def _inside_dir(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _default_profiles_dir() -> Path:
    return Path.home() / ".config" / "realhands-swarm"


def _default_xauthority() -> str:
    return str(Path.home() / ".Xauthority")


def _current_os() -> str:
    return platform.system()


def _windows_program_dirs() -> list[str]:
    return [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("LocalAppData", ""),
    ]


def _find_chrome_binary() -> str:
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin:
        return env_bin

    os_name = _current_os()

    if os_name == "Linux":
        candidates = [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ]
        for name in candidates:
            found = shutil.which(name)
            if found:
                return found
    elif os_name == "Darwin":
        mac_path = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        if Path(mac_path).exists():
            return mac_path
    elif os_name == "Windows":
        for base in _windows_program_dirs():
            if not base:
                continue
            candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.exists():
                return str(candidate)

    raise RuntimeError(
        "Chrome binary not found. Install Google Chrome or set CHROME_BIN "
        "to the full path of the Chrome executable."
    )


def _kill_process_tree(pid: int) -> None:
    os_name = _current_os()
    if os_name == "Windows":
        _kill_process_tree_windows(pid)
    else:
        _kill_process_tree_posix(pid)


def _kill_process_tree_posix(pid: int) -> None:
    if pid is None or pid <= 0:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError:
        pgid = pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        log.warning("no permission to kill process group %s", pgid)
    except OSError as exc:
        log.warning("killpg(%s) failed: %s", pgid, exc)


def _kill_process_tree_windows(pid: int) -> None:
    if pid is None or pid <= 0:
        return
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    except Exception as exc:
        log.warning("taskkill failed for pid %s: %s", pid, exc)


class SwarmSpawner:
    """Launch/kill headed Chrome profiles on demand for the bridge swarm."""

    def __init__(
        self,
        chrome_bin: Optional[str] = None,
        profiles_dir: Optional[str] = None,
        bridge_port: int = 7878,
        display: str = ":10",
        xauthority: Optional[str] = None,
    ) -> None:
        # Explicit override (CHROME_BIN / constructor) wins and may point at any
        # --load-extension-capable binary (Chrome for Testing, Chromium). When
        # unset, the launch binary resolves LAZILY on first spawn to a cached or
        # downloaded Chrome for Testing — so bridge startup never blocks on a
        # download, and branded Chrome's dead --load-extension is never used.
        self.chrome_bin = chrome_bin
        self._cft_bin: Optional[str] = None
        self.profiles_dir = Path(profiles_dir) if profiles_dir else _default_profiles_dir()
        self.bridge_port = int(bridge_port)
        self.display = display
        self.xauthority = xauthority if xauthority is not None else _default_xauthority()
        self._browsers: dict[str, dict] = {}
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.profiles_dir.resolve()

    def _register_url(self, browser_id: str) -> str:
        return (
            f"http://localhost:{self.bridge_port}/register"
            f"?browser_id={browser_id}"
        )

    def _resolve_launch_binary(self) -> str:
        """Binary used for /spawn. An explicit override wins; otherwise a cached
        or freshly-downloaded Chrome for Testing (branded Chrome can't
        --load-extension, so it is never used here)."""
        if self.chrome_bin:
            return self.chrome_bin
        if self._cft_bin:
            return self._cft_bin
        if find_cached_binary is None or ensure_chrome_for_testing is None:
            raise RuntimeError(
                "chrome_for_testing resolver unavailable; set CHROME_BIN to a "
                "Chrome for Testing or Chromium binary that supports --load-extension"
            )
        self._cft_bin = find_cached_binary() or ensure_chrome_for_testing()
        return self._cft_bin

    def _build_argv(self, user_data_dir: str, browser_id: str) -> list[str]:
        argv = [
            self._resolve_launch_binary(),
            f"--user-data-dir={user_data_dir}",
        ]
        argv.extend(_CHROME_FLAGS)
        # Force-load the unpacked extension into the fresh profile. Verified on
        # Chrome for Testing 149: loads+enables the extension, and the register
        # tab opens so the service worker self-identifies by browser_id.
        argv.extend(
            (
                f"--load-extension={_EXTENSION_DIR}",
                f"--disable-extensions-except={_EXTENSION_DIR}",
                "--test-type",
            )
        )
        argv.append(self._register_url(browser_id))
        return argv

    def _launch_env(self) -> dict:
        env = dict(os.environ)
        if _current_os() == "Linux":
            env["DISPLAY"] = self.display
            if self.xauthority:
                env["XAUTHORITY"] = self.xauthority
        return env

    def _popen_kwargs(self) -> dict:
        os_name = _current_os()
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": self._launch_env(),
        }
        if os_name == "Windows":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
        else:
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True
        return kwargs

    async def spawn(
        self,
        browser_id: Optional[str] = None,
        profile: Optional[str] = None,
        persistent: bool = False,
        start_url: Optional[str] = None,
    ) -> dict:
        if browser_id is None:
            browser_id = self._gen_id()
        browser_id = _validate_safe_name(str(browser_id), "browser_id")
        if profile is not None:
            profile = _validate_safe_name(str(profile), "profile")

        if browser_id in self._browsers and self._is_alive(self._browsers[browser_id]["pid"]):
            raise RuntimeError(f"browser_id {browser_id!r} already running")

        if persistent:
            name = profile or browser_id
            profile_path = (self.profiles_dir / name).resolve()
            if not _inside_dir(profile_path, self.profiles_dir):
                raise ValueError("invalid profile: resolved path escapes profiles_dir")
            profile_path.mkdir(parents=True, exist_ok=True)
            profile_dir = str(profile_path)
        else:
            profile_dir = tempfile.mkdtemp(prefix=_EPHEMERAL_PREFIX)

        argv = self._build_argv(profile_dir, browser_id)

        log_path = self.profiles_dir / f"{browser_id}.log"
        try:
            log_file = open(log_path, "ab", buffering=0)
        except OSError as exc:
            if not persistent:
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError(
                f"failed to open spawn log {log_path}: {exc}"
            ) from exc

        popen_kwargs = self._popen_kwargs()
        popen_kwargs["stdout"] = log_file
        popen_kwargs["stderr"] = subprocess.STDOUT

        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            log_file.close()
            if not persistent:
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError(
                f"failed to launch chrome ({self.chrome_bin!r}) for "
                f"browser_id {browser_id!r}: {exc}"
            ) from exc

        self._browsers[browser_id] = {
            "pid": proc.pid,
            "profile_dir": profile_dir,
            "persistent": bool(persistent),
            "profile": (profile or browser_id) if persistent else profile,
            "start_url": start_url,
            "popen": proc,
            "log_file": log_file,
            "started_at": time.time(),
        }
        log.info(
            "spawned browser_id=%s pid=%s persistent=%s profile_dir=%s",
            browser_id,
            proc.pid,
            persistent,
            profile_dir,
        )
        return {"browser_id": browser_id, "pid": proc.pid}

    async def close(self, browser_id: str) -> bool:
        browser_id = str(browser_id)
        info = self._browsers.pop(browser_id, None)
        if info is None:
            return False

        pid = info["pid"]
        _kill_process_tree(pid)

        log_file = info.get("log_file")
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

        if not info["persistent"]:
            shutil.rmtree(info["profile_dir"], ignore_errors=True)

        log.info("closed browser_id=%s pid=%s", browser_id, pid)
        return True

    def list(self) -> list:
        out: list[dict] = []
        for browser_id, info in self._browsers.items():
            out.append(
                {
                    "browser_id": browser_id,
                    "pid": info["pid"],
                    "profile": info.get("profile"),
                    "persistent": info["persistent"],
                    "alive": self._is_alive(info["pid"]),
                }
            )
        return out

    @staticmethod
    def _gen_id() -> str:
        return "b-" + uuid.uuid4().hex[:8]

    @staticmethod
    def _is_alive(pid: int) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _killpg(pid: int) -> None:
        _kill_process_tree(pid)
