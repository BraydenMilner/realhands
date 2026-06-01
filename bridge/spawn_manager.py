"""SwarmSpawner — launches/kills headed Chrome profiles on demand.

Layer-2 process manager for the realhands swarm. Runs on the machine where the
bridge runs (the VPS with the X display). Each spawned Chrome is a headed
browser pointed at the bridge's /register?browser_id=<ID> URL so the ONE
extension (force-installed via Chrome policy) learns its browser_id from the
tab URL via the service worker's tabs.onUpdated handler — no content script
needed on http://localhost.

Design notes (honoring SWARM PROTOCOL v1):
- Chrome is launched DETACHED (start_new_session=True / setsid) so the bridge
  process does not own it and a bridge restart never kills the swarm.
- The launch URL carries the id; the SW persists it to realhands_state.browser_id
  and (re)registers under <ID>.
- persistent=False -> ephemeral profile dir under the system temp area, removed
  on close. persistent=True -> a named reusable profile under profiles_dir kept
  on disk so logged-in sessions (cookies) survive close -> reopen.
- close() kills the whole process GROUP (os.killpg) because Chrome forks helper
  processes; killing only the launcher pid would orphan them.
- NO network calls are made here; this is pure process management.

Imported by bridge.py. Config is read from env in bridge.py and passed in.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("agent_bridge.spawn")

# Flags pinned by the SWARM PROTOCOL. --silent-debugger-extension-api keeps the
# CDP/debugger banner from appearing (the executor drives input via the debugger
# extension API for isTrusted=true). --no-first-run / --no-default-browser-check
# stop fresh profiles from showing onboarding chrome that would steal focus from
# the /register tab.
_CHROME_FLAGS = (
    "--silent-debugger-extension-api",
    "--no-first-run",
    "--no-default-browser-check",
)

# Prefix for ephemeral profile dirs so they're recognizable in the temp area and
# safe to rmtree (we only ever remove dirs we created with this prefix).
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


class SwarmSpawner:
    """Launch/kill headed Chrome profiles on demand for the bridge swarm."""

    def __init__(
        self,
        chrome_bin: str = "google-chrome",
        profiles_dir: Optional[str] = None,
        bridge_port: int = 7878,
        display: str = ":10",
        xauthority: Optional[str] = None,
    ) -> None:
        self.chrome_bin = chrome_bin
        self.profiles_dir = Path(profiles_dir) if profiles_dir else _default_profiles_dir()
        self.bridge_port = int(bridge_port)
        self.display = display
        self.xauthority = xauthority if xauthority is not None else _default_xauthority()
        # browser_id -> {pid, profile_dir, persistent, profile, popen, log_file}
        self._browsers: dict[str, dict] = {}
        # Ensure the profiles root exists (best-effort, but a hard failure here
        # means we can't write persistent profiles or logs, so let it raise).
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.profiles_dir.resolve()

    # ---------- argv construction ----------

    def _register_url(self, browser_id: str) -> str:
        return (
            f"http://localhost:{self.bridge_port}/register"
            f"?browser_id={browser_id}"
        )

    def _build_argv(self, user_data_dir: str, browser_id: str) -> list[str]:
        argv = [
            self.chrome_bin,
            f"--user-data-dir={user_data_dir}",
        ]
        argv.extend(_CHROME_FLAGS)
        # The register URL MUST be the launch URL — it carries the id for the SW.
        argv.append(self._register_url(browser_id))
        return argv

    # ---------- env ----------

    def _launch_env(self) -> dict:
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        if self.xauthority:
            env["XAUTHORITY"] = self.xauthority
        return env

    # ---------- spawn ----------

    async def spawn(
        self,
        browser_id: Optional[str] = None,
        profile: Optional[str] = None,
        persistent: bool = False,
        start_url: Optional[str] = None,
    ) -> dict:
        """Launch a headed Chrome for `browser_id`.

        Returns {"browser_id", "pid"}. Raises RuntimeError on launch failure.

        - user-data-dir = profiles_dir/<profile or browser_id> for persistent,
          else an ephemeral temp dir tracked for cleanup.
        - launch URL = http://localhost:<bridge_port>/register?browser_id=<id>.
        - start_url is accepted for forward-compat (the SW can navigate there
          after registering); for v1 registering is enough, so it's not added to
          the argv here.
        """
        if browser_id is None:
            browser_id = self._gen_id()
        browser_id = _validate_safe_name(str(browser_id), "browser_id")
        if profile is not None:
            profile = _validate_safe_name(str(profile), "profile")

        if browser_id in self._browsers and self._is_alive(self._browsers[browser_id]["pid"]):
            raise RuntimeError(f"browser_id {browser_id!r} already running")

        # Resolve the user-data-dir.
        if persistent:
            name = profile or browser_id
            profile_path = (self.profiles_dir / name).resolve()
            if not _inside_dir(profile_path, self.profiles_dir):
                raise ValueError("invalid profile: resolved path escapes profiles_dir")
            profile_path.mkdir(parents=True, exist_ok=True)
            profile_dir = str(profile_path)
        else:
            # Ephemeral: a fresh temp dir we own and will rmtree on close. If a
            # `profile` name was given alongside persistent=False we still use an
            # ephemeral dir (non-persistent never survives), but the requested
            # name is recorded for visibility.
            profile_dir = tempfile.mkdtemp(prefix=_EPHEMERAL_PREFIX)

        argv = self._build_argv(profile_dir, browser_id)

        # Per-browser log under profiles_dir so stdout/stderr don't pollute the
        # bridge's own streams and a crash can be inspected post-mortem.
        log_path = self.profiles_dir / f"{browser_id}.log"
        try:
            log_file = open(log_path, "ab", buffering=0)
        except OSError as exc:
            # Couldn't open the log — clean up an ephemeral dir we just made.
            if not persistent:
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError(
                f"failed to open spawn log {log_path}: {exc}"
            ) from exc

        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is built from config, not user shell input
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._launch_env(),
                # DETACHED: new session/process group so the bridge doesn't own
                # Chrome and we can killpg the whole tree on close.
                start_new_session=True,
                close_fds=True,
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

    # ---------- close ----------

    async def close(self, browser_id: str) -> bool:
        """Kill the process GROUP for `browser_id` and untrack it.

        For non-persistent browsers, also remove the ephemeral profile dir.
        Returns True if the browser_id was tracked, False otherwise.
        """
        browser_id = str(browser_id)
        info = self._browsers.pop(browser_id, None)
        if info is None:
            return False

        pid = info["pid"]
        self._killpg(pid)

        # Close the log fd we held open.
        log_file = info.get("log_file")
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

        # Remove the ephemeral profile dir; never delete a persistent one.
        if not info["persistent"]:
            shutil.rmtree(info["profile_dir"], ignore_errors=True)

        log.info("closed browser_id=%s pid=%s", browser_id, pid)
        return True

    # ---------- list ----------

    def list(self) -> list:
        """Return [{browser_id, pid, profile, persistent, alive}] for all tracked
        browsers. `alive` reflects whether the process is still running.
        """
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

    # ---------- helpers ----------

    @staticmethod
    def _gen_id() -> str:
        # Short uuid-based id — enough entropy to avoid collisions in a swarm,
        # short enough to be readable in logs and URLs.
        return "b-" + uuid.uuid4().hex[:8]

    @staticmethod
    def _is_alive(pid: int) -> bool:
        """True if a process with `pid` is still running.

        Uses os.kill(pid, 0): raises ProcessLookupError if gone, PermissionError
        if it exists but we can't signal it (still 'alive').
        """
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
        """Best-effort kill of the whole process group led by `pid`.

        Chrome spawns helper processes in the same session/group (we launched it
        with start_new_session=True), so SIGTERM to the group reaps the tree.
        Swallow ProcessLookupError (already dead) and PermissionError.
        """
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
