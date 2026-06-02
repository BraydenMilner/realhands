"""Offline tests for Chrome for Testing supply-chain safeguards."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

import chrome_for_testing as cft


def _zip_bytes(members: dict[str, bytes], modes: dict[str, int] | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            if modes and name in modes:
                info.external_attr = modes[name] << 16
            zf.writestr(info, content)
    return out.getvalue()


def _fake_response(payload: bytes, headers: dict[str, str] | None = None, url: str | None = None):
    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(payload)
            self.headers = headers or {"x-goog-hash": "md5="}
            self.url = url or "https://storage.googleapis.com/chrome-for-testing-public/1.2.3.4/linux64/chrome-linux64.zip"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

    return Response()


def test_pinned_version_constructs_exact_google_url(monkeypatch):
    monkeypatch.setenv("REALHANDS_CFT_VERSION", "1.2.3.4")
    version, url = cft._resolve_download("linux64")
    assert version == "1.2.3.4"
    assert url == "https://storage.googleapis.com/chrome-for-testing-public/1.2.3.4/linux64/chrome-linux64.zip"


def test_default_release_is_pinned_with_sha256(monkeypatch):
    monkeypatch.delenv("REALHANDS_CFT_VERSION", raising=False)
    monkeypatch.delenv("REALHANDS_CFT_CHANNEL", raising=False)
    version, url = cft._resolve_download("linux64")
    assert version == "149.0.7827.54"
    assert url == "https://storage.googleapis.com/chrome-for-testing-public/149.0.7827.54/linux64/chrome-linux64.zip"
    assert cft._expected_sha256(version, "linux64") == (
        "a77e3ba8fa8cf299a1a980313a162ea1a5d33297f632d5d3c7f553f7cf6780d4"
    )
    assert cft._expected_sha256(version, "mac-arm64") == (
        "376b22526a92345db188f8750be8a5abc45d1c8dda3805bfe89bf3327a10062f"
    )
    assert cft._expected_sha256(version, "mac-x64") == (
        "6f1e53ba52ae85dc5b6f1c60b169098b7dbabfc7ada5fffd17256c231825479b"
    )
    assert cft._expected_sha256(version, "win32") == (
        "fda6f2cf7272830ecb6706e2124724c0d7313f35bea8d29452ba95cb33d7338e"
    )
    assert cft._expected_sha256(version, "win64") == (
        "accd64b002d538cc7261f5bdb753c0d4b62ef78ea2a3c9d8dd8e65c9cdd9b9b4"
    )


def test_env_sha256_overrides_builtin_pin(monkeypatch):
    override = "0" * 64
    monkeypatch.setenv("REALHANDS_CFT_SHA256_LINUX64", override)
    assert cft._expected_sha256("149.0.7827.54", "linux64") == override


def test_invalid_version_rejected(monkeypatch):
    monkeypatch.setenv("REALHANDS_CFT_VERSION", "../../evil")
    with pytest.raises(cft.CftError):
        cft._resolve_download("linux64")


def test_remote_url_host_is_allowlisted(monkeypatch):
    monkeypatch.setenv("REALHANDS_CFT_CHANNEL", "Stable")
    payload = {
        "channels": {
            "Stable": {
                "version": "1.2.3.4",
                "downloads": {
                    "chrome": [
                        {"platform": "linux64", "url": "https://evil.example/chrome.zip"}
                    ]
                },
            }
        }
    }

    def fake_urlopen(*_args, **_kwargs):
        class Response(io.StringIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.close()

        return Response(json.dumps(payload))

    monkeypatch.setattr(cft.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(cft.CftError, match="unexpected CfT download URL"):
        cft._resolve_download("linux64")


def test_safe_extract_rejects_traversal(tmp_path):
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(_zip_bytes({"chrome-linux64/../evil": b"x"}))
    with zipfile.ZipFile(zip_path) as zf:
        with pytest.raises(cft.CftError, match="unsafe CfT zip member"):
            cft._safe_extract(zf, tmp_path / "out", "linux64")


def test_safe_extract_rejects_symlink_escape(tmp_path):
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(
            _zip_bytes(
                {"chrome-linux64/chrome": b"../evil"},
                modes={"chrome-linux64/chrome": 0o120777},
            )
        )
    with zipfile.ZipFile(zip_path) as zf:
        with pytest.raises(cft.CftError, match="unsafe CfT zip symlink target"):
            cft._safe_extract(zf, tmp_path / "out", "linux64")


def test_download_sha256_mismatch_rejected(monkeypatch, tmp_path):
    zip_payload = _zip_bytes({"chrome-linux64/chrome": b"#!/bin/sh\n"})
    good = hashlib.sha256(zip_payload).hexdigest()
    bad = "0" * 64 if good != "0" * 64 else "1" * 64
    monkeypatch.setattr(
        cft.urllib.request,
        "urlopen",
        lambda *_a, **_k: _fake_response(zip_payload, headers={}, url=cft._construct_download_url("1.2.3.4", "linux64")),
    )
    with pytest.raises(cft.CftError, match="SHA-256 mismatch"):
        cft._download_verified(
            cft._construct_download_url("1.2.3.4", "linux64"),
            tmp_path / "cft.zip",
            version="1.2.3.4",
            platform_key="linux64",
            expected_sha256=bad,
        )


def test_manifest_required_for_cached_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("REALHANDS_CFT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("REALHANDS_CFT_VERSION", "1.2.3.4")
    binary = tmp_path / "1.2.3.4" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"not verified")
    assert cft.find_cached_binary() is None
