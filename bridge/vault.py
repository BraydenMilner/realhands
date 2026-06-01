"""Local encrypted credential vault for the realhands Bridge.

Stores per-platform credentials (username, password, 2FA method, notes) in a
SQLite database with each field encrypted via Fernet. The Fernet key lives in
the OS keychain via the `keyring` library — never on disk next to the DB.

Hard rules:
- This module is a *reference* store. The Bridge never auto-types passwords.
- No plaintext is ever logged or returned by `list()`.
- No network I/O.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

KEYRING_SERVICE = "realhands-agent-bridge"
KEYRING_USERNAME = "vault-key"

FIELDS = ("username", "password", "twofa_method", "notes")


def _default_db_path() -> str:
    env = os.environ.get("REALHANDS_VAULT_PATH")
    if env:
        return env
    return str(Path.home() / ".local" / "share" / "realhands-agent-bridge" / "vault.db")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_keyring():
    """Import keyring lazily and surface a useful error if the backend is broken."""
    try:
        import keyring  # type: ignore
        from keyring.errors import KeyringError  # type: ignore
    except ImportError as e:  # pragma: no cover - import guard
        raise RuntimeError(
            "The 'keyring' package is required for the realhands vault. "
            "Install it with: pip install keyring"
        ) from e
    return keyring, KeyringError


def _get_or_create_key() -> bytes:
    keyring, KeyringError = _load_keyring()
    try:
        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError as e:
        raise RuntimeError(
            f"OS keyring backend is unavailable ({e}). "
            "On Linux install a Secret Service provider (e.g. gnome-keyring or "
            "KeePassXC's freedesktop integration). The vault refuses to fall "
            "back to a less secure key store."
        ) from e
    except Exception as e:  # backends like the null/fail backend
        raise RuntimeError(
            f"OS keyring backend is unavailable ({e}). "
            "Install gnome-keyring / KeePassXC / Apple Keychain access."
        ) from e

    if existing:
        return existing.encode("ascii")

    new_key = Fernet.generate_key()
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, new_key.decode("ascii"))
    except Exception as e:
        raise RuntimeError(
            f"Failed to persist the new vault key to the OS keyring: {e}"
        ) from e
    return new_key


def _store_key(new_key: bytes) -> None:
    keyring, KeyringError = _load_keyring()
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, new_key.decode("ascii"))
    except Exception as e:
        raise RuntimeError(f"Failed to persist rotated vault key: {e}") from e


class VaultManager:
    """Encrypted local credential store. One instance per process is fine."""

    def __init__(self, db_path: str | None = None) -> None:
        path = db_path or _default_db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self._key = _get_or_create_key()
        self._fernet = Fernet(self._key)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # ---- schema / lifecycle ------------------------------------------------

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                platform_slug TEXT PRIMARY KEY,
                username TEXT,
                password TEXT,
                twofa_method TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "VaultManager":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- crypto helpers ----------------------------------------------------

    def _encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        # Fernet tokens are already base64; storing as TEXT directly is fine.
        return token.decode("ascii")

    def _decrypt(self, token_text: str | None) -> str | None:
        if token_text is None:
            return None
        return self._fernet.decrypt(token_text.encode("ascii")).decode("utf-8")

    # ---- public API --------------------------------------------------------

    def set(
        self,
        platform: str,
        *,
        username: str | None = None,
        password: str | None = None,
        twofa_method: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Upsert credentials for `platform`.

        Per-field semantics:
          - None: leave the existing value alone (or NULL if new row)
          - "" (empty string): clear the field (store SQL NULL)
          - any other str: encrypt and store
        """
        if not platform:
            raise ValueError("platform slug must be a non-empty string")

        incoming = {
            "username": username,
            "password": password,
            "twofa_method": twofa_method,
            "notes": notes,
        }

        existing = self._conn.execute(
            "SELECT username, password, twofa_method, notes FROM credentials "
            "WHERE platform_slug = ?",
            (platform,),
        ).fetchone()

        merged: dict[str, str | None] = {}
        for field in FIELDS:
            value = incoming[field]
            if value is None:
                merged[field] = existing[field] if existing else None
            elif value == "":
                merged[field] = None
            else:
                merged[field] = self._encrypt(value)

        now = _utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO credentials
              (platform_slug, username, password, twofa_method, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform_slug) DO UPDATE SET
              username = excluded.username,
              password = excluded.password,
              twofa_method = excluded.twofa_method,
              notes = excluded.notes,
              updated_at = excluded.updated_at
            """,
            (
                platform,
                merged["username"],
                merged["password"],
                merged["twofa_method"],
                merged["notes"],
                now,
            ),
        )
        self._conn.commit()

    def get(
        self, platform: str, field: str | None = None
    ) -> str | dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT platform_slug, username, password, twofa_method, notes, updated_at "
            "FROM credentials WHERE platform_slug = ?",
            (platform,),
        ).fetchone()

        if row is None:
            return None

        if field is not None:
            if field not in FIELDS:
                raise ValueError(
                    f"unknown field {field!r}; expected one of {FIELDS}"
                )
            return self._decrypt(row[field])

        return {
            "platform": row["platform_slug"],
            "username": self._decrypt(row["username"]),
            "password": self._decrypt(row["password"]),
            "twofa_method": self._decrypt(row["twofa_method"]),
            "notes": self._decrypt(row["notes"]),
            "updated_at": row["updated_at"],
        }

    def list(self) -> list[dict[str, Any]]:  # noqa: A003 - matches spec
        """Return metadata only — never plaintext."""
        rows = self._conn.execute(
            "SELECT platform_slug, username, password, twofa_method, notes, updated_at "
            "FROM credentials ORDER BY platform_slug"
        ).fetchall()
        return [
            {
                "platform": r["platform_slug"],
                "has_username": r["username"] is not None,
                "has_password": r["password"] is not None,
                "has_twofa_method": r["twofa_method"] is not None,
                "has_notes": r["notes"] is not None,
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def remove(self, platform: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM credentials WHERE platform_slug = ?", (platform,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def rotate_key(self) -> None:
        """Re-encrypt every field with a fresh Fernet key, atomically."""
        new_key = Fernet.generate_key()
        new_fernet = Fernet(new_key)

        rows = self._conn.execute(
            "SELECT platform_slug, username, password, twofa_method, notes "
            "FROM credentials"
        ).fetchall()

        # Decrypt-then-encrypt under the new key in memory first so a bad
        # ciphertext aborts before we touch the DB.
        reencrypted: list[tuple[str, str | None, str | None, str | None, str | None]] = []
        for row in rows:
            new_fields: list[str | None] = []
            for field in FIELDS:
                token = row[field]
                if token is None:
                    new_fields.append(None)
                else:
                    plaintext = self._fernet.decrypt(token.encode("ascii"))
                    new_fields.append(
                        new_fernet.encrypt(plaintext).decode("ascii")
                    )
            reencrypted.append(
                (row["platform_slug"], new_fields[0], new_fields[1], new_fields[2], new_fields[3])
            )

        try:
            with self._conn:  # single transaction
                for slug, u, p, t, n in reencrypted:
                    self._conn.execute(
                        "UPDATE credentials SET username = ?, password = ?, "
                        "twofa_method = ?, notes = ? WHERE platform_slug = ?",
                        (u, p, t, n, slug),
                    )
        except Exception:
            # DB write failed — leave the old key in place so vault still opens.
            raise

        # DB is now fully on the new key — flip the in-memory cipher and persist.
        self._fernet = new_fernet
        self._key = new_key
        _store_key(new_key)


__all__ = ["VaultManager", "KEYRING_SERVICE", "KEYRING_USERNAME", "FIELDS"]
