"""JSONL audit log + content-addressed screenshot store.

One line per call to decide_action(). Screenshots are saved by SHA-256 hash so
identical screenshots only consume disk once and can be replayed by hash later.

We never upload screenshots anywhere except the LLM provider being called —
this module's job is local persistence only.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_screenshot(screenshot: bytes, screenshot_dir: str) -> tuple[str, str]:
    """Save the screenshot by content hash; idempotent.

    Returns (sha256_hex, absolute_path). If the file already exists (cache hit
    from a prior call with the same bytes) we skip the write.
    """
    digest = sha256_hex(screenshot)
    dir_path = Path(screenshot_dir)
    # Owner-only dir — screenshots can contain page contents / secrets.
    dir_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_mode(dir_path, 0o700)
    out = dir_path / f"{digest}.png"
    if not out.exists():
        # write atomically — temp file then rename. Avoids partial files if
        # the process crashes mid-write.
        tmp = out.with_suffix(".png.tmp")
        tmp.write_bytes(screenshot)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    return digest, str(out)


def _ensure_mode(path: Path, mode: int) -> None:
    """Best-effort chmod; ignore failures on filesystems that don't support it
    (e.g. some network/Windows mounts)."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def append_audit(audit_path: str, row: dict[str, Any]) -> None:
    """Append one JSONL row. Creates parent dirs and the file as needed.

    The audit log can hold task context / URLs; we keep the directory and file
    owner-only (0o700 / 0o600).
    """
    path = Path(audit_path)
    # Owner-only parent dir.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_mode(path.parent, 0o700)
    # Create the file with 0o600 BEFORE writing so secrets never land in a
    # world-readable file even momentarily.
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.close(fd)
    _ensure_mode(path, 0o600)
    # Open in append+text mode; newline="" so we control line endings.
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def now_iso() -> str:
    """UTC ISO-8601 with seconds precision. Matches the rest of realhands's logs."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
