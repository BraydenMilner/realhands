"""Generate synthetic test screenshots using PIL.

Run this once to (re)create the PNGs in this directory. The PNGs are committed
so tests don't need to generate them at runtime; this script exists so anyone
can rebuild them if the layout changes.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FIXTURES_DIR = Path(__file__).parent


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try a common system font; fall back to PIL's default bitmap font."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Ubuntu
        "/usr/share/fonts/TTF/DejaVuSans.ttf",  # Arch
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def make_login_page() -> Path:
    """1024x768 PNG: 'Email' and 'Password' inputs with a 'Sign in' button.

    Layout is intentionally obvious so the vision tier should hit high
    confidence with no clever interpretation.
    """
    width, height = 1024, 768
    img = Image.new("RGB", (width, height), color=(245, 247, 250))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(36)
    label_font = _load_font(20)
    field_font = _load_font(18)
    button_font = _load_font(22)

    # Page title.
    draw.text((width // 2 - 100, 80), "Example Login", fill=(20, 20, 30), font=title_font)

    # Form card.
    card_x, card_y, card_w, card_h = 312, 200, 400, 360
    draw.rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        fill=(255, 255, 255),
        outline=(210, 215, 225),
        width=2,
    )

    # Email label + field.
    email_label_y = card_y + 40
    draw.text((card_x + 30, email_label_y), "Email", fill=(60, 65, 80), font=label_font)
    email_field_y = email_label_y + 30
    draw.rectangle(
        [card_x + 30, email_field_y, card_x + card_w - 30, email_field_y + 44],
        fill=(252, 253, 255),
        outline=(180, 188, 200),
        width=1,
    )
    draw.text(
        (card_x + 42, email_field_y + 12),
        "you@example.com",
        fill=(170, 175, 185),
        font=field_font,
    )

    # Password label + field.
    pwd_label_y = email_field_y + 74
    draw.text((card_x + 30, pwd_label_y), "Password", fill=(60, 65, 80), font=label_font)
    pwd_field_y = pwd_label_y + 30
    draw.rectangle(
        [card_x + 30, pwd_field_y, card_x + card_w - 30, pwd_field_y + 44],
        fill=(252, 253, 255),
        outline=(180, 188, 200),
        width=1,
    )
    # Password placeholder dots — never the literal password.
    draw.text((card_x + 42, pwd_field_y + 12), "*" * 12, fill=(170, 175, 185), font=field_font)

    # Sign in button.
    btn_y = pwd_field_y + 78
    draw.rectangle(
        [card_x + 30, btn_y, card_x + card_w - 30, btn_y + 50],
        fill=(46, 110, 220),
        outline=(40, 95, 200),
        width=1,
    )
    # Center "Sign in" within the button.
    draw.text((card_x + card_w // 2 - 40, btn_y + 12), "Sign in", fill=(255, 255, 255), font=button_font)

    out = FIXTURES_DIR / "login_page.png"
    img.save(out, format="PNG")
    return out


def make_action_modal() -> Path:
    """1024x768 PNG: a confirmation modal with a single 'Submit' button."""
    width, height = 1024, 768
    img = Image.new("RGB", (width, height), color=(30, 35, 50))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(28)
    body_font = _load_font(18)
    button_font = _load_font(22)

    # Modal card.
    mw, mh = 480, 280
    mx, my = (width - mw) // 2, (height - mh) // 2
    draw.rectangle([mx, my, mx + mw, my + mh], fill=(245, 247, 250), outline=(0, 0, 0), width=2)
    draw.text((mx + 100, my + 30), "Confirm", fill=(20, 30, 50), font=title_font)
    draw.text((mx + 60, my + 100), "Submit your request to continue.", fill=(60, 65, 80), font=body_font)

    btn_x, btn_y, btn_w, btn_h = mx + 160, my + 180, 160, 56
    draw.rectangle(
        [btn_x, btn_y, btn_x + btn_w, btn_y + btn_h],
        fill=(40, 170, 80),
        outline=(30, 130, 60),
        width=1,
    )
    draw.text((btn_x + 44, btn_y + 14), "Submit", fill=(255, 255, 255), font=button_font)

    out = FIXTURES_DIR / "action_modal.png"
    img.save(out, format="PNG")
    return out


if __name__ == "__main__":
    print("wrote", make_login_page())
    print("wrote", make_action_modal())
