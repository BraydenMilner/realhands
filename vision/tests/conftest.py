"""Pytest configuration.

Auto-generates the synthetic fixture PNGs if missing. Tests don't depend on
specific pixel content (LLM is mocked).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config) -> None:  # noqa: D401
    fixtures_dir = ROOT / "tests" / "fixtures"
    login_png = fixtures_dir / "login_page.png"
    if not login_png.exists():
        sys.path.insert(0, str(fixtures_dir))
        from make_fixtures import make_action_modal, make_login_page

        make_login_page()
        make_action_modal()
