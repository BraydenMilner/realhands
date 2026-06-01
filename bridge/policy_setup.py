#!/usr/bin/env python3
"""RealHands extension force-install policy setup.

Installs/removes Chrome's managed-policy ExtensionInstallForcelist so that the
RealHands extension is force-installed into EVERY new Chrome profile — which is
required for the /spawn auto-swarm flow. Without this policy, Chrome ignores
--load-extension on fresh profiles.

This script needs admin/sudo privileges and must be run ONCE per machine.

Per-OS locations:
  Linux:   /etc/opt/chrome/policies/managed/realhands.json
  macOS:   /Library/Managed Preferences/com.google.Chrome.plist
  Windows: HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist (UNVERIFIED)

Usage:
  sudo python3 bridge/policy_setup.py install --yes
  sudo python3 bridge/policy_setup.py remove --yes
  python3 bridge/policy_setup.py status
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"
UPDATE_XML_TEMPLATE = EXTENSION_DIR / "update.xml"
MANIFEST_PATH = EXTENSION_DIR / "manifest.json"


def _current_os() -> str:
    return platform.system()


def _read_manifest() -> dict:
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def _get_extension_version() -> str:
    return _read_manifest().get("version", "0.0.0")


def _crx_path() -> Path:
    return EXTENSION_DIR.parent / "realhands.crx"


def _update_xml_path() -> Path:
    return EXTENSION_DIR / "update.xml"


def _render_update_xml(extension_id: str, crx_path: Path, version: str) -> str:
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{extension_id}'>\n"
        f"    <updatecheck codebase='file://{crx_path}' version='{version}' />\n"
        f"  </app>\n"
        "</gupdate>\n"
    )
    return xml


def _check_admin() -> bool:
    os_name = _current_os()
    if os_name == "Windows":
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
        except Exception:
            return False
    return os.geteuid() == 0


def _status_linux() -> None:
    policy_file = Path("/etc/opt/chrome/policies/managed/realhands.json")
    if policy_file.exists():
        print(f"[Linux] Policy file exists: {policy_file}")
        print(json.dumps(json.loads(policy_file.read_text()), indent=2))
    else:
        print(f"[Linux] No policy file at {policy_file}")


def _status_macos() -> None:
    plist_path = "/Library/Managed Preferences/com.google.Chrome.plist"
    if os.path.exists(plist_path):
        print(f"[macOS] Managed plist exists: {plist_path}")
        try:
            result = subprocess.run(
                ["defaults", "read", plist_path],
                capture_output=True, text=True,
            )
            print(result.stdout)
        except Exception as exc:
            print(f"  (could not read plist: {exc})")
    else:
        print(f"[macOS] No managed plist at {plist_path}")
    print("[macOS] Note: also check profiles via 'profiles show' command.")


def _status_windows() -> None:
    print("[Windows] Checking registry HKLM\\SOFTWARE\\Policies\\Google\\Chrome...")
    try:
        result = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("  (no ExtensionInstallForcelist key found)")
    except FileNotFoundError:
        print("  (reg command not available)")


def cmd_status() -> None:
    os_name = _current_os()
    print(f"OS: {os_name}")
    crx = _crx_path()
    print(f"CRX file: {crx} {'(exists)' if crx.exists() else '(MISSING)'}")
    ux = _update_xml_path()
    print(f"Update XML: {ux} {'(exists)' if ux.exists() else '(not yet generated)'}")
    print(f"Extension version: {_get_extension_version()}")
    print()
    if os_name == "Linux":
        _status_linux()
    elif os_name == "Darwin":
        _status_macos()
    elif os_name == "Windows":
        _status_windows()
    else:
        print(f"Unsupported OS: {os_name}")


def cmd_install(extension_id: str, yes: bool) -> None:
    os_name = _current_os()
    version = _get_extension_version()
    crx = _crx_path()
    update_xml = _update_xml_path()

    if not crx.exists():
        print(
            f"ERROR: packed extension not found at {crx}\n"
            "Pack it first:\n"
            "  chrome --pack-extension=extension/ --pack-extension-key=realhands.pem\n"
            "Or use the unpacked extension ID from chrome://extensions in Developer mode."
        )
        sys.exit(1)

    xml_content = _render_update_xml(extension_id, crx, version)
    print(f"Will write update XML to: {update_xml}")
    print(f"Extension ID: {extension_id}")
    print(f"CRX path: {crx}")
    print(f"Version: {version}")
    print()

    if os_name == "Linux":
        print(f"Will write policy to: /etc/opt/chrome/policies/managed/realhands.json")
    elif os_name == "Darwin":
        print(f"Will write managed policy for com.google.Chrome via /Library/Managed Preferences/")
    elif os_name == "Windows":
        print("Will write registry keys under HKLM\\SOFTWARE\\Policies\\Google\\Chrome")
    else:
        print(f"Unsupported OS: {os_name}")
        sys.exit(1)

    if not yes:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    update_xml.write_text(xml_content)
    print(f"Wrote {update_xml}")

    if os_name == "Linux":
        _install_linux(extension_id, update_xml)
    elif os_name == "Darwin":
        _install_macos(extension_id, update_xml)
    elif os_name == "Windows":
        _install_windows(extension_id, update_xml)

    print("\nDone. Restart Chrome for the policy to take effect.")


def _install_linux(extension_id: str, update_xml: Path) -> None:
    policy_dir = Path("/etc/opt/chrome/policies/managed")
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy_file = policy_dir / "realhands.json"
    xml_url = f"file://{update_xml.resolve()}"
    policy = {
        "ExtensionInstallForcelist": {
            "1": f"{extension_id};{xml_url}"
        },
        "ExtensionInstallSources": [
            "file:////*"
        ],
    }
    policy_file.write_text(json.dumps(policy, indent=2) + "\n")
    print(f"Wrote {policy_file}")


def _install_macos(extension_id: str, update_xml: Path) -> None:
    xml_url = f"file://{update_xml.resolve()}"

    plist_dir = Path("/Library/Managed Preferences")
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.google.Chrome.plist"

    subprocess.run(
        [
            "defaults", "write", str(plist_path),
            "ExtensionInstallForcelist",
            "-dict", "1", f"{extension_id};{xml_url}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "defaults", "write", str(plist_path),
            "ExtensionInstallSources",
            "-array", "file:////*",
        ],
        check=True,
    )
    print(f"Wrote managed preferences to {plist_path}")

    profiles_dir = Path("/etc/chrome/policies/managed")
    profiles_dir.mkdir(parents=True, exist_ok=True)
    policy_file = profiles_dir / "realhands.json"
    policy = {
        "ExtensionInstallForcelist": {
            "1": f"{extension_id};{xml_url}"
        },
        "ExtensionInstallSources": [
            "file:////*"
        ],
    }
    policy_file.write_text(json.dumps(policy, indent=2) + "\n")
    print(f"Wrote fallback policy to {policy_file}")


def _install_windows(extension_id: str, update_xml: Path) -> None:
    # UNVERIFIED on Windows — registry-based policy approach.
    xml_url = f"file://{update_xml.resolve()}"

    reg_key = r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
    subprocess.run(
        ["reg", "add", reg_key, "/v", "1", "/t", "REG_SZ", "/d", f"{extension_id};{xml_url}", "/f"],
        check=True,
    )

    sources_key = r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallSources"
    subprocess.run(
        ["reg", "add", sources_key, "/v", "1", "/t", "REG_SZ", "/d", "file:////*", "/f"],
        check=True,
    )
    print("Wrote registry keys for ExtensionInstallForcelist and ExtensionInstallSources")
    print("NOTE: Windows support is UNVERIFIED — registry paths and values are based on")
    print("Chrome policy documentation but have not been tested on this platform.")


def cmd_remove(yes: bool) -> None:
    os_name = _current_os()
    if os_name == "Linux":
        policy_file = Path("/etc/opt/chrome/policies/managed/realhands.json")
        print(f"Will remove: {policy_file}")
    elif os_name == "Darwin":
        plist_path = "/Library/Managed Preferences/com.google.Chrome.plist"
        fallback = Path("/etc/chrome/policies/managed/realhands.json")
        print(f"Will remove keys from: {plist_path}")
        print(f"Will remove fallback: {fallback}")
    elif os_name == "Windows":
        print("Will remove registry keys under HKLM\\SOFTWARE\\Policies\\Google\\Chrome")
    else:
        print(f"Unsupported OS: {os_name}")
        sys.exit(1)

    if not yes:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    if os_name == "Linux":
        _remove_linux()
    elif os_name == "Darwin":
        _remove_macos()
    elif os_name == "Windows":
        _remove_windows()

    print("Done.")


def _remove_linux() -> None:
    policy_file = Path("/etc/opt/chrome/policies/managed/realhands.json")
    if policy_file.exists():
        policy_file.unlink()
        print(f"Removed {policy_file}")
    else:
        print(f"No policy file at {policy_file}")


def _remove_macos() -> None:
    plist_path = "/Library/Managed Preferences/com.google.Chrome.plist"
    if os.path.exists(plist_path):
        subprocess.run(
            ["defaults", "delete", plist_path, "ExtensionInstallForcelist"],
            capture_output=True,
        )
        subprocess.run(
            ["defaults", "delete", plist_path, "ExtensionInstallSources"],
            capture_output=True,
        )
        print(f"Removed extension keys from {plist_path}")
    else:
        print(f"No plist at {plist_path}")

    fallback = Path("/etc/chrome/policies/managed/realhands.json")
    if fallback.exists():
        fallback.unlink()
        print(f"Removed fallback {fallback}")


def _remove_windows() -> None:
    # UNVERIFIED on Windows
    reg_key = r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
    subprocess.run(["reg", "delete", reg_key, "/va", "/f"], capture_output=True)

    sources_key = r"HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallSources"
    subprocess.run(["reg", "delete", sources_key, "/va", "/f"], capture_output=True)
    print("Removed registry keys")
    print("NOTE: Windows support is UNVERIFIED.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install/remove Chrome managed policy for RealHands extension force-install. "
                    "Requires admin/sudo."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current policy status")

    install_p = sub.add_parser("install", help="Install the force-install policy")
    install_p.add_argument(
        "--extension-id", required=True,
        help="Chrome extension ID (from chrome://extensions in Developer mode)",
    )
    install_p.add_argument("--yes", action="store_true", help="Skip confirmation")

    remove_p = sub.add_parser("remove", help="Remove the force-install policy")
    remove_p.add_argument("--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        cmd_status()
    elif args.command == "install":
        if not _check_admin():
            print("ERROR: This command requires admin/sudo privileges.")
            print("Re-run with: sudo python3 bridge/policy_setup.py install --yes")
            sys.exit(1)
        cmd_install(args.extension_id, args.yes)
    elif args.command == "remove":
        if not _check_admin():
            print("ERROR: This command requires admin/sudo privileges.")
            sys.exit(1)
        cmd_remove(args.yes)


if __name__ == "__main__":
    main()
