# -*- coding: utf-8 -*-
"""앱 아이콘(icon.ico)을 만듭니다. 빌드 전에 한 번만 실행하면 됩니다.

필요: Pillow (pip install pillow) — 아이콘이 이미 있으면 실행할 필요 없습니다.
디자인: 파란 라운드 사각형 위에 필지(폴리곤) 모양 + 분할선.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icon.ico"
SIZE = 256
TOP = (91, 141, 181)      # #5b8db5
BOTTOM = (60, 105, 145)   # 아래쪽 진한 파랑
PARCEL = (255, 255, 255)
LINE = (72, 122, 163)     # #487aa3


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def gradient(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), TOP)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / (size - 1)
        color = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def build() -> Path:
    base = gradient(SIZE).convert("RGBA")
    base.putalpha(rounded_mask(SIZE, 56))

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # 필지(폴리곤) 모양
    parcel = [(58, 150), (92, 62), (172, 52), (206, 116), (176, 206), (96, 210)]
    draw.polygon(parcel, fill=PARCEL + (240,))

    # 분할선 두 개(이 앱의 '분할·병합'을 상징)
    draw.line([(92, 62), (150, 210)], fill=LINE, width=9)
    draw.line([(58, 150), (206, 116)], fill=LINE, width=9)

    # 꼭짓점(버텍스) 점
    for x, y in parcel:
        draw.ellipse([x - 9, y - 9, x + 9, y + 9], fill=LINE)

    icon = Image.alpha_composite(base, layer)
    icon.save(OUT, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return OUT


if __name__ == "__main__":
    print("아이콘 생성:", build())
