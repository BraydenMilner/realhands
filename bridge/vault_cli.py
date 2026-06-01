"""CLI for the realhands local credential vault.

Invocation: `python -m vault_cli <subcommand> [args]`

Subcommands:
  add        --platform=X                  Prompt for each field.
  set        --platform=X --field=F [--stdin]
                                           Prompt (or read stdin) for one field.
  show       --platform=X [--field=F]      y/N confirm, then print plaintext.
  list                                     Tabulated metadata, no plaintext.
  remove     --platform=X                  y/N confirm, then delete.
  rotate-key                               y/N confirm, then rotate.

Hard rule: plaintext is only ever emitted by `show` after explicit confirmation.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Sequence

from vault import FIELDS, VaultManager


# ----- prompt helpers -------------------------------------------------------


def _confirm(question: str) -> bool:
    """y/N prompt. Defaults to No on EOF or anything that isn't y/yes."""
    try:
        reply = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def _prompt_field(field: str) -> str:
    """Return user-supplied value for `field`. Empty string == clear."""
    if field == "password":
        return getpass.getpass("password (empty = clear, no echo): ")
    label = field.replace("_", " ")
    try:
        return input(f"{label} (empty = clear): ")
    except EOFError:
        return ""


# ----- subcommands ----------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    with VaultManager() as v:
        kwargs: dict[str, str] = {}
        print(f"Adding/updating credentials for {args.platform}.")
        print("Press Enter to skip a field (leaves it unchanged). Type '' to clear it.")
        for field in FIELDS:
            try:
                if field == "password":
                    raw = getpass.getpass(f"{field} [skip=Enter, clear=''] (no echo): ")
                else:
                    raw = input(f"{field} [skip=Enter, clear='']: ")
            except EOFError:
                raw = ""
            if raw == "":
                # treat blank as "skip" to avoid accidental wipes during `add`
                continue
            if raw == "''":
                kwargs[field] = ""
            else:
                kwargs[field] = raw
        if not kwargs:
            # Still call set() so an empty row is created with updated_at.
            v.set(args.platform)
            print(f"No fields supplied; ensured row for {args.platform} exists.")
        else:
            v.set(args.platform, **kwargs)
            print(f"Saved {len(kwargs)} field(s) for {args.platform}.")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if args.field not in FIELDS:
        print(f"error: --field must be one of {FIELDS}", file=sys.stderr)
        return 2

    if args.stdin:
        # read all of stdin, strip trailing newline only (preserve internal whitespace)
        data = sys.stdin.read()
        if data.endswith("\n"):
            data = data[:-1]
        value = data
    else:
        value = _prompt_field(args.field)

    with VaultManager() as v:
        v.set(args.platform, **{args.field: value})
    if value == "":
        print(f"Cleared {args.field} for {args.platform}.")
    else:
        print(f"Updated {args.field} for {args.platform}.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    target = f"{args.field} for {args.platform}" if args.field else f"all fields for {args.platform}"
    if not _confirm(f"Reveal plaintext {target}?"):
        print("Aborted.")
        return 1

    with VaultManager() as v:
        if args.field:
            value = v.get(args.platform, args.field)
            if value is None:
                # Could mean "platform missing" or "field empty" — disambiguate.
                full = v.get(args.platform)
                if full is None:
                    print(f"No such platform: {args.platform}", file=sys.stderr)
                    return 1
                print(f"{args.field}: <not set>")
            else:
                print(value)
        else:
            full = v.get(args.platform)
            if full is None:
                print(f"No such platform: {args.platform}", file=sys.stderr)
                return 1
            print(f"platform:     {full['platform']}")
            for f in FIELDS:
                val = full[f]
                print(f"{f + ':':<14}{'<not set>' if val is None else val}")
            print(f"updated_at:   {full['updated_at']}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    with VaultManager() as v:
        rows = v.list()
    if not rows:
        print("(vault is empty)")
        return 0
    headers = ("platform", "user", "pass", "2fa", "notes", "updated_at")
    widths = [
        max(len(headers[0]), *(len(r["platform"]) for r in rows)),
        len(headers[1]),
        len(headers[2]),
        len(headers[3]),
        len(headers[4]),
        max(len(headers[5]), *(len(r["updated_at"]) for r in rows)),
    ]

    def _fmt_row(values: Sequence[str]) -> str:
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    print(_fmt_row(headers))
    print(_fmt_row(["-" * w for w in widths]))
    for r in rows:
        print(
            _fmt_row(
                (
                    r["platform"],
                    "y" if r["has_username"] else "-",
                    "y" if r["has_password"] else "-",
                    "y" if r["has_twofa_method"] else "-",
                    "y" if r["has_notes"] else "-",
                    r["updated_at"],
                )
            )
        )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if not _confirm(f"Delete all credentials for {args.platform}?"):
        print("Aborted.")
        return 1
    with VaultManager() as v:
        ok = v.remove(args.platform)
    if ok:
        print(f"Removed {args.platform}.")
        return 0
    print(f"No such platform: {args.platform}", file=sys.stderr)
    return 1


def cmd_rotate_key(_args: argparse.Namespace) -> int:
    if not _confirm("Generate a new vault key and re-encrypt all credentials?"):
        print("Aborted.")
        return 1
    with VaultManager() as v:
        v.rotate_key()
    print("Key rotated. All credentials re-encrypted.")
    return 0


# ----- argparse wiring ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="realhands-vault", description="realhands local credential vault")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="prompt for each field (skip with Enter)")
    p_add.add_argument("--platform", required=True)
    p_add.set_defaults(func=cmd_add)

    p_set = sub.add_parser("set", help="set or clear a single field")
    p_set.add_argument("--platform", required=True)
    p_set.add_argument("--field", required=True, choices=FIELDS)
    p_set.add_argument("--stdin", action="store_true", help="read value from stdin (no prompt)")
    p_set.set_defaults(func=cmd_set)

    p_show = sub.add_parser("show", help="reveal plaintext (requires confirmation)")
    p_show.add_argument("--platform", required=True)
    p_show.add_argument("--field", choices=FIELDS, default=None)
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="metadata-only listing")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("remove", help="delete one platform's row")
    p_rm.add_argument("--platform", required=True)
    p_rm.set_defaults(func=cmd_remove)

    p_rot = sub.add_parser("rotate-key", help="re-encrypt everything under a new key")
    p_rot.set_defaults(func=cmd_rotate_key)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
