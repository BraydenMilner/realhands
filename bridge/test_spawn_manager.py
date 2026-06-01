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
    in the env.
  - spawn() tracks the browser; list() reflects it with alive=True.
  - close() kills the process group (os.killpg) and untracks.
  - a non-persistent (ephemeral) profile dir is created on spawn and removed on
    close; a persistent profile dir is created and KEPT after close.
  - spawn() auto-generates a browser_id when none is given.
  - list() alive reflects a dead process.

Run from this directory:
    ./.venv/bin/python -m pytest test_spawn_manager.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import spawn_manager
from spawn_manager import SwarmSpawner


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

    # Spawner never calls these on the happy path, but define them so any
    # incidental use is harmless.
    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class KillRecorder:
    """Records os.killpg / os.getpgid calls; controls os.kill(pid, 0) liveness."""

    def __init__(self):
        self.killpg_calls: list[tuple[int, int]] = []
        # pids considered alive for os.kill(pid, 0). Default: everything alive.
        self.dead_pids: set[int] = set()

    def getpgid(self, pid):
        # In tests the "group id" is just the pid.
        return pid

    def killpg(self, pgid, sig):
        self.killpg_calls.append((pgid, sig))

    def kill(self, pid, sig):
        # Emulate os.kill(pid, 0) liveness probe.
        if pid in self.dead_pids:
            raise ProcessLookupError(pid)
        return None


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

    # chrome binary first.
    assert argv[0] == "/opt/chrome/chrome"

    # user-data-dir points at profiles_dir/<browser_id> for a persistent spawn.
    expected_udd = str(spawner.profiles_dir / "alpha")
    assert f"--user-data-dir={expected_udd}" in argv

    # the three pinned flags are present.
    assert "--silent-debugger-extension-api" in argv
    assert "--no-first-run" in argv
    assert "--no-default-browser-check" in argv

    # the launch URL is the /register?browser_id= URL on the bridge port, and it
    # is the LAST arg (Chrome treats the trailing positional as the URL to open).
    assert argv[-1] == "http://localhost:7878/register?browser_id=alpha"


@pytest.mark.asyncio
async def test_spawn_launches_detached_with_display_env(spawner, fake_popen):
    await spawner.spawn(browser_id="beta", persistent=True)
    inst = fake_popen.instances[0]

    # Detached: new session so the bridge does not own Chrome.
    assert inst.kwargs.get("start_new_session") is True

    # DISPLAY + XAUTHORITY are injected into the child env.
    assert inst.env is not None
    assert inst.env["DISPLAY"] == ":10"
    assert inst.env["XAUTHORITY"] == "/home/realhands/.Xauthority"


@pytest.mark.asyncio
async def test_spawn_writes_per_browser_log(spawner, fake_popen):
    await spawner.spawn(browser_id="gamma", persistent=True)
    # The log file is created under profiles_dir as <browser_id>.log.
    log_path = spawner.profiles_dir / "gamma.log"
    assert log_path.exists()
    # And it's wired into Popen's stdout.
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
    # tracked under the generated id.
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

    # process group was killed (group id == pid in the recorder).
    assert any(call[0] == pid for call in kill_recorder.killpg_calls)

    # untracked.
    assert spawner.list() == []


@pytest.mark.asyncio
async def test_close_unknown_returns_false(spawner):
    assert await spawner.close("nope") is False


# ---------- profile dir lifecycle ----------


@pytest.mark.asyncio
async def test_ephemeral_profile_dir_created_then_removed(spawner):
    res = await spawner.spawn(browser_id="ephem", persistent=False)
    # find the tracked profile dir via list -> not exposed, so reach into state.
    info = spawner._browsers["ephem"]
    profile_dir = Path(info["profile_dir"])

    # ephemeral dir was created (mkdtemp) and lives outside profiles_dir.
    assert profile_dir.exists()
    assert info["persistent"] is False

    await spawner.close("ephem")
    # removed on close.
    assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_persistent_profile_dir_kept_after_close(spawner):
    await spawner.spawn(browser_id="keepme", profile="acct1", persistent=True)
    profile_dir = spawner.profiles_dir / "acct1"
    assert profile_dir.exists()

    await spawner.close("keepme")
    # persistent profile survives close so cookies/sessions persist.
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
    # nothing tracked after a failed launch.
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

    # the ephemeral dir created before the failed launch was cleaned up.
    assert "dir" in created
    assert not os.path.exists(created["dir"])


# ---------- construction ----------


def test_init_creates_profiles_dir(tmp_path):
    target = tmp_path / "nested" / "realhands-swarm"
    assert not target.exists()
    SwarmSpawner(profiles_dir=str(target))
    assert target.exists()


def test_init_defaults_match_pins():
    sp = SwarmSpawner.__new__(SwarmSpawner)
    # don't run __init__ (would mkdir under HOME); just check default constants.
    # Verify the default signature values via a real construct under tmp HOME is
    # overkill; assert the documented defaults on a freshly built instance with
    # an explicit profiles_dir so we don't touch ~/.config.
    import inspect

    sig = inspect.signature(SwarmSpawner.__init__)
    assert sig.parameters["chrome_bin"].default == "google-chrome"
    assert sig.parameters["bridge_port"].default == 7878
    assert sig.parameters["display"].default == ":10"
