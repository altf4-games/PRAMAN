#!/usr/bin/env python3
"""One-off generator for the synthetic photo fixtures in
`api/praman/seed/raw/`. These stand in for real vendor phone photos (which
we don't have) so the VLM extraction path has an image to actually read —
disclosed as synthetic in the README, never passed off as a real photo.
Run once; the output is committed, so this script isn't part of the app.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "api" / "praman" / "seed" / "raw"


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _italic_font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
        "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
        "/System/Library/Fonts/Supplemental/Noteworthy.ttc",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return _font(size)


def make_printed_price_list() -> None:
    img = Image.new("RGB", (900, 700), "white")
    draw = ImageDraw.Draw(img)
    title_font = _font(34)
    body_font = _font(26)
    draw.text((40, 30), "SHARMA JEWELLERS — PRICE LIST", fill="black", font=title_font)
    lines = [
        "Silver Chain (10g)                 Rs. 6,400",
        "Gold Ring - Plain Band (5g)         Rs. 32,000",
        "Engraved Silver Ring (custom)       Rs. 4,200 (made to order)",
        "Silver Anklet Pair                  Rs. 3,800",
        "Gold Earrings - Studs (3g)          Rs. 21,500",
        "Engraved Gold Pendant (custom)      Rs. 18,900 (made to order)",
        "Silver Bracelet                     Rs. 5,600",
        "Gold Bangle Set (2 pcs)             Rs. 68,000",
    ]
    y = 110
    for line in lines:
        draw.text((40, y), line, fill="black", font=body_font)
        y += 55
    img.save(OUT_DIR / "printed_price_list.png")


def make_handwritten_price_list() -> None:
    # Synthetic stand-in for a handwritten note: a script-like font, slight
    # per-character jitter, and a paper-yellow background to read as a hand
    # scrawled note rather than a clean printout.
    img = Image.new("RGB", (900, 700), (250, 244, 224))
    draw = ImageDraw.Draw(img)
    title_font = _italic_font(38)
    body_font = _italic_font(30)
    rng = random.Random(42)

    def jittered_text(pos: tuple[int, int], text: str, font: ImageFont.ImageFont) -> None:
        x, y = pos
        for ch in text:
            dy = rng.randint(-2, 2)
            draw.text((x, y + dy), ch, fill=(20, 20, 40), font=font)
            x += draw.textlength(ch, font=font)

    jittered_text((40, 30), "Ramesh Kirana Store - aaj ka rate", title_font)
    lines = [
        "Toor dal (1 kg) - 180",
        "Chawal (basmati, 1 kg) - 130",
        "Pyaz (1 kg) - 28",
        "Tamatar (1 kg) - 22 (stock kam hai)",
        "Aata (5 kg) - 245",
        "Chini (1 kg) - 46",
        "Doodh (1 L, amul) - 58",
    ]
    y = 110
    for line in lines:
        jittered_text((40, y), line, body_font)
        y += 60
    img.save(OUT_DIR / "handwritten_price_list.png")


def make_bare_product_photo() -> None:
    # A plain "product photo" with no price text at all — exercises the
    # case where the model correctly returns nothing extractable, or a very
    # low-confidence guess, rather than hallucinating a price.
    img = Image.new("RGB", (600, 600), (235, 225, 200))
    draw = ImageDraw.Draw(img)
    draw.rectangle([150, 150, 450, 450], fill=(180, 140, 60), outline="black", width=4)
    draw.ellipse([230, 230, 370, 370], fill=(220, 190, 90), outline="black", width=3)
    draw.text((160, 470), "(no price visible in frame)", fill="black", font=_font(20))
    img.save(OUT_DIR / "bare_product_photo.png")


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_printed_price_list()
    make_handwritten_price_list()
    make_bare_product_photo()
    print(f"wrote synthetic image fixtures to {OUT_DIR}")
