"""Render the existing compass as a transparent Windows multiresolution ICO."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)


def compass(size: int = 256) -> Image.Image:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    def bounds(values):
        return tuple(round(value * scale) for value in values)
    draw.ellipse(bounds((2, 2, 61, 61)), fill=(18, 96, 77, 255))
    draw.ellipse(bounds((10, 10, 53, 53)), outline="white", width=max(1, round(2 * scale)))
    draw.polygon([bounds(point) for point in ((32, 14), (39, 34), (32, 30), (25, 34))], fill=(235, 96, 84, 255))
    draw.polygon([bounds(point) for point in ((32, 50), (25, 30), (32, 34), (39, 30))], fill="white")
    return image


def build(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    compass().save(path, format="ICO", sizes=[(size, size) for size in SIZES])
    return path


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parents[1] / "app" / "static" / "tripfinder.ico"))
