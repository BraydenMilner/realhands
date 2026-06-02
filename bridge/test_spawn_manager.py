"""Tests for SwarmSpawner (spawn_manager.py).

These tests NEVER launch a real Chrome and NEVER signal a real process. We
monkeypatch:
  - subprocess.Popen      -> a fake that records argv/env/kwargs and returns a
                              fake pid.
  - os.killpg / os.getpgid -> recorders (so close() doesn't touch a real PID).
  - os.kill (signal 0)    -> a controllable liveness oracle for list()/alive.

Covered:
  - spawn() builds the correct argv: --user-data-dir, the
    /register?browser_id=<id> launch URL, and the three pinned flags.
  - spawn() launches DETACHED (start_new_session=True) with DISPLAY/XAUTHORITY
    in the env on Linux.
  - spawn() tracks the browser; list() reflects it with alive=True.
  - close() kills the process group (os.killpg) and untracks.
  - a non-persistent (ephemeral) profile dir is created on spawn and removed on
    close; a persistent profile dir is created and KEPT after close.
  - spawn() auto-generates a browser_id when none is given.
  - list() alive reflects a dead process.
  - Cross-platform: per-OS Chrome binary detection, env vars, and kill strategy.

Run from this directory:
    ./.venv/bin/python -m pytest test_spawn_manager.py -q
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import spawn_manager
from spawn_manager import SwarmSpawner, _kill_process_tree


# ---------- fakes ----------


class FakePopen:
    """Records the argv/kwargs of every Popen call and returns a fake pid."""

    instances: list["FakePopen"] = []
    next_pid = 4242

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.env = kwargs.get("env")
        self.pid = FakePopen.next_pid
        FakePopen.next_pid += 1
        FakePopen.instances.append(self)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class KillRecorder:
    """Records os.killpg / os.getpgid calls; controls os.kill(pid, 0) liveness."""

    def __init__(self):
        self.killpg_calls: list[tuple[int, int]] = []
        self.dead_pids: set[int] = set()

    def getpgid(self, pid):
        return pid

    def killpg(self, pgid, sig):
        self.killpg_calls.append((pgid, sig))

    def kill(self, pid, sig):
        if pid in self.dead_pids:
            raise ProcessLookupError(pid)
        return None


class TaskkillRecorder:
    """Records taskkill subprocess calls on Windows."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return type("R", (), {"returncode": 0})()


# ---------- fixtures ----------


@pytest.fixture
def kill_recorder(monkeypatch):
    rec = KillRecorder()
    monkeypatch.setattr(spawn_manager.os, "killpg", rec.killpg)
    monkeypatch.setattr(spawn_manager.os, "getpgid", rec.getpgid)
    monkeypatch.setattr(spawn_manager.os, "kill", rec.kill)
    return rec


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.instances = []
    FakePopen.next_pid = 4242
    monkeypatch.setattr(spawn_manager.subprocess, "Popen", FakePopen)
    return FakePopen


@pytest.fixture
def spawner(tmp_path, fake_popen, kill_recorder):
    """A SwarmSpawner writing profiles/logs under tmp_path, fully mocked out."""
    return SwarmSpawner(
        chrome_bin="/opt/chrome/chrome",
        profiles_dir=str(tmp_path / "profiles"),
        bridge_port=7878,
        display=":10",
        xauthority="/home/realhands/.Xauthority",
    )


# ---------- argv / launch ----------


@pytest.mark.asyncio
async def test_spawn_builds_correct_argv(spawner, fake_popen):
    res = await spawner.spawn(browser_id="alpha", persistent=True)
    assert res == {"browser_id": "alpha", "pid": 4242}

    assert len(fake_popen.instances) == 1
    argv = fake_popen.instances[0].argv

    assert argv[0] == "/opt/chrome/chrome"

    expected_udd = str(spawner.profiles_dir / "alpha")
    assert f"--user-data-dir={expected_udd}" in argv

    assert "--silent-debugger-extension-api" in argv
    assert "--no-first-run" in argv
    assert "--no-default-browser-check" in argv

    assert argv[-1] == "http://localhost:7878/register?browser_id=alpha"


@pytest.mark.asyncio
async def test_spawn_launches_detached_with_display_env(monkeypatch, spawner, fake_popen):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    await spawner.spawn(browser_id="beta", persistent=True)
    inst = fake_popen.instances[0]

    assert inst.kwargs.get("start_new_session") is True

    assert inst.env is not None
    assert inst.env["DISPLAY"] == ":10"
    assert inst.env["XAUTHORITY"] == "/home/realhands/.Xauthority"


@pytest.mark.asyncio
async def test_spawn_writes_per_browser_log(spawner, fake_popen):
    await spawner.spawn(browser_id="gamma", persistent=True)
    log_path = spawner.profiles_dir / "gamma.log"
    assert log_path.exists()
    inst = fake_popen.instances[0]
    assert inst.kwargs.get("stdout") is not None


# ---------- tracking / list ----------


@pytest.mark.asyncio
async def test_spawn_tracks_and_list_reflects(spawner):
    await spawner.spawn(browser_id="alpha", profile="acct1", persistent=True)
    listing = spawner.list()
    assert len(listing) == 1
    entry = listing[0]
    assert entry["browser_id"] == "alpha"
    assert entry["pid"] == 4242
    assert entry["profile"] == "acct1"
    assert entry["persistent"] is True
    assert entry["alive"] is True


@pytest.mark.asyncio
async def test_list_alive_reflects_dead_process(spawner, kill_recorder):
    res = await spawner.spawn(browser_id="alpha", persistent=True)
    kill_recorder.dead_pids.add(res["pid"])
    entry = spawner.list()[0]
    assert entry["alive"] is False


@pytest.mark.asyncio
async def test_spawn_autogenerates_browser_id(spawner):
    res = await spawner.spawn(browser_id=None, persistent=True)
    bid = res["browser_id"]
    assert isinstance(bid, str) and bid
    assert spawner.list()[0]["browser_id"] == bid


@pytest.mark.asyncio
async def test_spawn_rejects_unsafe_browser_id(spawner, fake_popen):
    for browser_id in ("../x", "has/slash", "..", "a" * 65):
        with pytest.raises(ValueError):
            await spawner.spawn(browser_id=browser_id, persistent=False)
    assert fake_popen.instances == []


@pytest.mark.asyncio
async def test_spawn_rejects_unsafe_profile(spawner, fake_popen):
    for profile in ("../x", "has/slash", "..", "a" * 65):
        with pytest.raises(ValueError):
            await spawner.spawn(browser_id="alpha", profile=profile, persistent=True)
    assert fake_popen.instances == []


@pytest.mark.asyncio
async def test_spawn_rejects_profile_symlink_escape(spawner, tmp_path, fake_popen):
    outside = tmp_path / "outside"
    outside.mkdir()
    (spawner.profiles_dir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        await spawner.spawn(browser_id="alpha", profile="linked", persistent=True)

    assert fake_popen.instances == []
    assert not (outside / "Default").exists()


# ---------- close ----------


@pytest.mark.asyncio
async def test_close_killpgs_and_untracks(spawner, kill_recorder):
    res = await spawner.spawn(browser_id="alpha", persistent=True)
    pid = res["pid"]

    ok = await spawner.close("alpha")
    assert ok is True

    assert any(call[0] == pid for call in kill_recorder.killpg_calls)

    assert spawner.list() == []


@pytest.mark.asyncio
async def test_close_unknown_returns_false(spawner):
    assert await spawner.close("nope") is False


# ---------- profile dir lifecycle ----------


@pytest.mark.asyncio
async def test_ephemeral_profile_dir_created_then_removed(spawner):
    res = await spawner.spawn(browser_id="ephem", persistent=False)
    info = spawner._browsers["ephem"]
    profile_dir = Path(info["profile_dir"])

    assert profile_dir.exists()
    assert info["persistent"] is False

    await spawner.close("ephem")
    assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_persistent_profile_dir_kept_after_close(spawner):
    await spawner.spawn(browser_id="keepme", profile="acct1", persistent=True)
    profile_dir = spawner.profiles_dir / "acct1"
    assert profile_dir.exists()

    await spawner.close("keepme")
    assert profile_dir.exists()


@pytest.mark.asyncio
async def test_persistent_uses_browser_id_when_no_profile(spawner, fake_popen):
    await spawner.spawn(browser_id="solo", persistent=True)
    expected_udd = str(spawner.profiles_dir / "solo")
    argv = fake_popen.instances[0].argv
    assert f"--user-data-dir={expected_udd}" in argv
    assert (spawner.profiles_dir / "solo").exists()


# ---------- launch failure ----------


@pytest.mark.asyncio
async def test_spawn_launch_failure_raises_clear_error(tmp_path, monkeypatch, kill_recorder):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'google-chrome'")

    monkeypatch.setattr(spawn_manager.subprocess, "Popen", boom)
    sp = SwarmSpawner(
        chrome_bin="google-chrome",
        profiles_dir=str(tmp_path / "profiles"),
    )
    with pytest.raises(RuntimeError) as ei:
        await sp.spawn(browser_id="x", persistent=False)
    assert "failed to launch chrome" in str(ei.value)
    assert sp.list() == []


@pytest.mark.asyncio
async def test_spawn_launch_failure_cleans_ephemeral_dir(tmp_path, monkeypatch, kill_recorder):
    created = {}

    real_mkdtemp = spawn_manager.tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created["dir"] = path
        return path

    def boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(spawn_manager.tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(spawn_manager.subprocess, "Popen", boom)
    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))

    with pytest.raises(RuntimeError):
        await sp.spawn(browser_id="x", persistent=False)

    assert "dir" in created
    assert not os.path.exists(created["dir"])


# ---------- construction ----------


def test_init_creates_profiles_dir(tmp_path):
    target = tmp_path / "nested" / "realhands-swarm"
    assert not target.exists()
    SwarmSpawner(profiles_dir=str(target))
    assert target.exists()


def test_init_defaults_match_pins():
    import inspect

    sig = inspect.signature(SwarmSpawner.__init__)
    assert sig.parameters["chrome_bin"].default is None
    assert sig.parameters["bridge_port"].default == 7878
    assert sig.parameters["display"].default == ":10"


# ============================================================
# Cross-platform tests
# ============================================================


@pytest.mark.asyncio
async def test_linux_sets_display_env(monkeypatch, fake_popen, tmp_path):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    monkeypatch.setattr(spawn_manager, "_find_chrome_binary", lambda: "/usr/bin/google-chrome")

    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    await sp.spawn(browser_id="linux1", persistent=True)
    inst = fake_popen.instances[-1]
    assert inst.env["DISPLAY"] == ":10"
    assert "XAUTHORITY" in inst.env
    assert inst.kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_macos_no_display_env(monkeypatch, fake_popen, tmp_path):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Darwin")
    monkeypatch.setattr(
        spawn_manager,
        "_find_chrome_binary",
        lambda: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )

    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    await sp.spawn(browser_id="mac1", persistent=True)
    inst = fake_popen.instances[-1]
    assert "DISPLAY" not in inst.env
    assert "XAUTHORITY" not in inst.env
    assert inst.kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_windows_no_display_env(monkeypatch, fake_popen, tmp_path):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Windows")
    monkeypatch.setattr(
        spawn_manager,
        "_find_chrome_binary",
        lambda: r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )

    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    await sp.spawn(browser_id="win1", persistent=True)
    inst = fake_popen.instances[-1]
    assert "DISPLAY" not in inst.env
    assert "XAUTHORITY" not in inst.env
    flags = inst.kwargs.get("creationflags", 0)
    assert flags != 0
    assert "start_new_session" not in inst.kwargs


def test_find_chrome_linux(monkeypatch):
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    monkeypatch.setattr(spawn_manager.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "google-chrome" else None)

    result = spawn_manager._find_chrome_binary()
    assert result == "/usr/bin/google-chrome"


def test_find_chrome_linux_chromium_fallback(monkeypatch):
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")

    def fake_which(name):
        if name == "google-chrome":
            return None
        if name == "google-chrome-stable":
            return None
        if name == "chromium":
            return "/usr/bin/chromium"
        return None

    monkeypatch.setattr(spawn_manager.shutil, "which", fake_which)
    result = spawn_manager._find_chrome_binary()
    assert result == "/usr/bin/chromium"


def test_find_chrome_macos(monkeypatch, tmp_path):
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Darwin")
    mac_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    exists_map = {str(mac_path): True}

    original_exists = Path.exists

    def patched_exists(self):
        return exists_map.get(str(self), original_exists(self))

    monkeypatch.setattr(Path, "exists", patched_exists)

    result = spawn_manager._find_chrome_binary()
    assert result == str(mac_path)


def test_find_chrome_windows(monkeypatch):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Windows")
    monkeypatch.delenv("CHROME_BIN", raising=False)

    expected_str = "C:/Program Files/Google/Chrome/Application/chrome.exe"
    expected = Path(expected_str)
    exists_map = {str(expected): True, expected_str: True}
    original_exists = Path.exists

    def patched_exists(self):
        return exists_map.get(str(self), original_exists(self))

    monkeypatch.setattr(Path, "exists", patched_exists)
    monkeypatch.setattr(
        spawn_manager,
        "_windows_program_dirs",
        lambda: ["C:/Program Files", "C:/Program Files (x86)", "C:/Users/test/AppData/Local"],
    )

    result = spawn_manager._find_chrome_binary()
    assert "chrome.exe" in result


def test_find_chrome_env_override(monkeypatch):
    monkeypatch.setattr(os.environ, "get", lambda k, d=None: "/my/custom/chrome" if k == "CHROME_BIN" else d)
    result = spawn_manager._find_chrome_binary()
    assert result == "/my/custom/chrome"


def test_find_chrome_not_found_raises(monkeypatch):
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    monkeypatch.setattr(spawn_manager.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="Chrome binary not found"):
        spawn_manager._find_chrome_binary()


def test_kill_tree_posix_uses_killpg(monkeypatch):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    killpg_calls = []

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(spawn_manager.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(spawn_manager.os, "killpg", fake_killpg)

    _kill_process_tree(999)
    assert killpg_calls == [(999, spawn_manager.signal.SIGTERM)]


def test_kill_tree_posix_dead_process(monkeypatch):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")

    def fake_getpgid(pid):
        raise ProcessLookupError

    monkeypatch.setattr(spawn_manager.os, "getpgid", fake_getpgid)
    _kill_process_tree(999)


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Windows")
    recorder = TaskkillRecorder()
    monkeypatch.setattr(spawn_manager.subprocess, "call", recorder)

    _kill_process_tree(888)
    assert len(recorder.calls) == 1
    assert recorder.calls[0] == ["taskkill", "/F", "/T", "/PID", "888"]


def test_kill_tree_windows_dead_process(monkeypatch):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Windows")

    def boom(*a, **k):
        raise FileNotFoundError("no taskkill")

    monkeypatch.setattr(spawn_manager.subprocess, "call", boom)

    kill_calls = []
    monkeypatch.setattr(spawn_manager.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

    _kill_process_tree(777)
    assert kill_calls == [(777, spawn_manager.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_close_uses_kill_tree_on_linux(monkeypatch, fake_popen, tmp_path):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Linux")
    monkeypatch.setattr(spawn_manager, "_find_chrome_binary", lambda: "/usr/bin/chrome")

    killpg_calls = []

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(spawn_manager.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(spawn_manager.os, "killpg", fake_killpg)
    monkeypatch.setattr(spawn_manager.os, "kill", lambda pid, sig: None)

    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    await sp.spawn(browser_id="lx", persistent=True)
    await sp.close("lx")
    assert len(killpg_calls) == 1
    assert killpg_calls[0][0] == 4242


@pytest.mark.asyncio
async def test_close_uses_taskkill_on_windows(monkeypatch, fake_popen, tmp_path):
    monkeypatch.setattr(spawn_manager, "_current_os", lambda: "Windows")
    monkeypatch.setattr(
        spawn_manager,
        "_find_chrome_binary",
        lambda: r"C:\chrome\chrome.exe",
    )

    taskkill_recorder = TaskkillRecorder()
    monkeypatch.setattr(spawn_manager.subprocess, "call", taskkill_recorder)
    monkeypatch.setattr(spawn_manager.os, "kill", lambda pid, sig: None)

    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    await sp.spawn(browser_id="wn", persistent=True)
    await sp.close("wn")
    assert len(taskkill_recorder.calls) == 1
    assert "888" in taskkill_recorder.calls[0] or "4242" in taskkill_recorder.calls[0]


def test_spawn_resolves_chrome_for_testing(monkeypatch, tmp_path):
    # /spawn launches Chrome for Testing (branded Chrome can't --load-extension).
    # chrome_bin unset -> NOT resolved at construction (no download on startup);
    # resolved lazily to a (mocked) cached CfT binary on first use.
    monkeypatch.setattr(spawn_manager, "find_cached_binary", lambda: "/fake/cft")
    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    assert sp.chrome_bin is None
    assert sp._resolve_launch_binary() == "/fake/cft"


def test_spawn_argv_force_loads_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(spawn_manager, "find_cached_binary", lambda: "/fake/cft")
    sp = SwarmSpawner(profiles_dir=str(tmp_path / "profiles"))
    argv = sp._build_argv(str(tmp_path / "p1"), "swarm-1")
    ext = str(spawn_manager._EXTENSION_DIR)
    assert argv[0] == "/fake/cft"
    assert f"--load-extension={ext}" in argv
    assert f"--disable-extensions-except={ext}" in argv
    assert "--test-type" in argv
    assert argv[-1].endswith("/register?browser_id=swarm-1")


def test_chrome_bin_explicit_skips_finder(tmp_path):
    sp = SwarmSpawner(
        chrome_bin="/custom/chrome",
        profiles_dir=str(tmp_path / "profiles"),
    )
    assert sp.chrome_bin == "/custom/chrome"
    assert sp._resolve_launch_binary() == "/custom/chrome"  # override wins over CfT
