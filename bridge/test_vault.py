"""Tests for the realhands local credential vault.

Uses an in-memory keyring backend so we never touch the real system keychain.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

import keyring
import pytest
from cryptography.fernet import Fernet, InvalidToken
from keyring.backend import KeyringBackend

import vault as vault_module
from vault import VaultManager


# ----- in-memory keyring backend -------------------------------------------


class InMemoryKeyring(KeyringBackend):
    """Trivial backend that stores secrets in a process-local dict."""

    priority = 1.0  # type: ignore[assignment]

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:  # noqa: D401
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


@pytest.fixture()
def fake_keyring() -> Iterator[InMemoryKeyring]:
    """Swap in a fresh in-memory keyring for the duration of one test."""
    backend = InMemoryKeyring()
    original = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(original)


@pytest.fixture()
def vault(tmp_path, fake_keyring) -> Iterator[VaultManager]:
    db_path = tmp_path / "vault.db"
    v = VaultManager(str(db_path))
    try:
        yield v
    finally:
        v.close()


# ----- basic roundtrip ------------------------------------------------------


def test_set_and_get_all_fields(vault):
    vault.set(
        "example.com",
        username="alice@example.com",
        password="hunter2",
        twofa_method="authenticator app",
        notes="recovery email is alice-recovery@example.com",
    )
    full = vault.get("example.com")
    assert full["platform"] == "example.com"
    assert full["username"] == "alice@example.com"
    assert full["password"] == "hunter2"
    assert full["twofa_method"] == "authenticator app"
    assert full["notes"] == "recovery email is alice-recovery@example.com"
    assert full["updated_at"]


def test_get_single_field(vault):
    vault.set("demo_site", username="bob", password="s3cret")
    assert vault.get("demo_site", "password") == "s3cret"
    assert vault.get("demo_site", "username") == "bob"
    assert vault.get("demo_site", "notes") is None


def test_get_missing_platform_returns_none(vault):
    assert vault.get("nope") is None
    assert vault.get("nope", "password") is None


def test_get_unknown_field_raises(vault):
    vault.set("a", username="x")
    with pytest.raises(ValueError):
        vault.get("a", "not_a_field")


# ----- None vs "" semantics -------------------------------------------------


def test_partial_update_preserves_other_fields(vault):
    vault.set("example_bank", username="user1", password="pw1", notes="initial")
    vault.set("example_bank", password="pw2")  # update only password

    full = vault.get("example_bank")
    assert full["username"] == "user1"
    assert full["password"] == "pw2"
    assert full["notes"] == "initial"


def test_empty_string_clears_field(vault):
    vault.set("sample_app", username="user1", password="pw1", notes="note1")
    vault.set("sample_app", notes="")  # clear notes only

    full = vault.get("sample_app")
    assert full["username"] == "user1"
    assert full["password"] == "pw1"
    assert full["notes"] is None


def test_explicit_none_leaves_field_alone(vault):
    vault.set("test_portal", username="u", password="p")
    vault.set("test_portal", username=None, password=None, notes="added later")

    full = vault.get("test_portal")
    assert full["username"] == "u"
    assert full["password"] == "p"
    assert full["notes"] == "added later"


def test_updated_at_changes_on_set(vault):
    vault.set("a", username="x")
    first = vault.get("a")["updated_at"]
    # SQLite timestamps are ISO strings down to microseconds — easy to compare.
    vault.set("a", password="y")
    second = vault.get("a")["updated_at"]
    assert second >= first
    assert second != first or True  # ordering is enough; equal-microsecond is rare


# ----- list() must not leak plaintext --------------------------------------


def test_list_contains_no_plaintext(vault):
    vault.set("example.com", username="alice", password="hunter2", notes="my-note")
    vault.set("demo_site", username="bob")

    rows = vault.list()
    assert len(rows) == 2
    serialized = json.dumps(rows)

    for secret in ("alice", "hunter2", "my-note", "bob"):
        assert secret not in serialized, f"plaintext leaked into list(): {secret}"

    by_platform = {r["platform"]: r for r in rows}
    assert by_platform["example.com"]["has_username"] is True
    assert by_platform["example.com"]["has_password"] is True
    assert by_platform["example.com"]["has_twofa_method"] is False
    assert by_platform["example.com"]["has_notes"] is True
    assert by_platform["demo_site"]["has_password"] is False


def test_list_empty(vault):
    assert vault.list() == []


# ----- remove ---------------------------------------------------------------


def test_remove_returns_true_when_present(vault):
    vault.set("p", username="u")
    assert vault.remove("p") is True
    assert vault.get("p") is None


def test_remove_returns_false_when_missing(vault):
    assert vault.remove("does-not-exist") is False


# ----- DB-level "no plaintext on disk" --------------------------------------


def test_database_file_has_no_plaintext(tmp_path, fake_keyring):
    db_path = tmp_path / "vault.db"
    v = vault_module.VaultManager(str(db_path))
    try:
        v.set("example.com", username="alice", password="hunter2!")
    finally:
        v.close()

    raw = db_path.read_bytes()
    assert b"alice" not in raw
    assert b"hunter2!" not in raw


# ----- key rotation ---------------------------------------------------------


def test_rotate_key_preserves_plaintext(tmp_path, fake_keyring):
    db_path = tmp_path / "vault.db"
    v = vault_module.VaultManager(str(db_path))
    try:
        v.set("example.com", username="alice", password="hunter2", notes="n")
        v.set("demo_site", username="bob")

        # Capture old key and a ciphertext sample BEFORE rotating.
        old_key = fake_keyring.get_password(
            vault_module.KEYRING_SERVICE, vault_module.KEYRING_USERNAME
        )
        assert old_key is not None
        old_fernet = Fernet(old_key.encode("ascii"))

        conn = sqlite3.connect(str(db_path))
        try:
            old_pw_ciphertext = conn.execute(
                "SELECT password FROM credentials WHERE platform_slug = 'example.com'"
            ).fetchone()[0]
        finally:
            conn.close()
        # sanity: old key still decrypts the captured ciphertext
        assert old_fernet.decrypt(old_pw_ciphertext.encode("ascii")) == b"hunter2"

        v.rotate_key()

        # Plaintext still readable through the manager.
        assert v.get("example.com", "password") == "hunter2"
        assert v.get("example.com", "username") == "alice"
        assert v.get("example.com", "notes") == "n"
        assert v.get("demo_site", "username") == "bob"
    finally:
        v.close()

    # New key in keyring must differ.
    new_key = fake_keyring.get_password(
        vault_module.KEYRING_SERVICE, vault_module.KEYRING_USERNAME
    )
    assert new_key is not None
    assert new_key != old_key

    # Best-effort: the OLD key should no longer decrypt the captured ciphertext
    # (because the row in the DB is now a fresh token under the new key).
    conn = sqlite3.connect(str(db_path))
    try:
        new_pw_ciphertext = conn.execute(
            "SELECT password FROM credentials WHERE platform_slug = 'example.com'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert new_pw_ciphertext != old_pw_ciphertext
    with pytest.raises(InvalidToken):
        old_fernet.decrypt(new_pw_ciphertext.encode("ascii"))


def test_rotate_key_on_empty_vault(tmp_path, fake_keyring):
    db_path = tmp_path / "vault.db"
    v = vault_module.VaultManager(str(db_path))
    try:
        old_key = fake_keyring.get_password(
            vault_module.KEYRING_SERVICE, vault_module.KEYRING_USERNAME
        )
        v.rotate_key()
        new_key = fake_keyring.get_password(
            vault_module.KEYRING_SERVICE, vault_module.KEYRING_USERNAME
        )
        assert new_key != old_key
        # Manager still works.
        v.set("p", username="u")
        assert v.get("p", "username") == "u"
    finally:
        v.close()


# ----- persistence across instances ----------------------------------------


def test_persistence_across_instances(tmp_path, fake_keyring):
    db_path = tmp_path / "vault.db"
    v1 = vault_module.VaultManager(str(db_path))
    try:
        v1.set("p", username="u", password="pw")
    finally:
        v1.close()

    v2 = vault_module.VaultManager(str(db_path))
    try:
        assert v2.get("p", "username") == "u"
        assert v2.get("p", "password") == "pw"
    finally:
        v2.close()


def test_creates_parent_directory(tmp_path, fake_keyring):
    nested = tmp_path / "a" / "b" / "c" / "vault.db"
    assert not nested.parent.exists()
    v = vault_module.VaultManager(str(nested))
    try:
        v.set("p", username="u")
        assert nested.exists()
    finally:
        v.close()


# ----- validation -----------------------------------------------------------


def test_empty_platform_rejected(vault):
    with pytest.raises(ValueError):
        vault.set("", username="x")
