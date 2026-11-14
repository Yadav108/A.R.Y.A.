"""Generate ARYA's app icon (arya/static/arya.png + arya.ico): a glowing orb."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "arya" / "static"
S = 1024  # draw large, downsample for crisp edges


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def orb() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # outer glow
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((90, 90, S - 90, S - 90), fill=(99, 102, 241, 150))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))
    # sphere: radial gradient indigo -> teal, light from top-left
    sphere = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = sphere.load()
    cx, cy, r = S / 2, S / 2, S * 0.30
    hx, hy = cx - r * 0.35, cy - r * 0.42
    indigo, teal, deep = (99, 102, 241), (20, 184, 166), (23, 20, 66)
    for y in range(int(cy - r), int(cy + r) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d > r:
                continue
            h = min(1.0, (((x - hx) ** 2 + (y - hy) ** 2) ** 0.5) / (r * 1.6))
            diag = min(1.0, max(0.0, ((x - (cx - r)) + (y - (cy - r))) / (4 * r)))
            base = lerp(indigo, teal, diag)
            col = lerp(lerp((224, 231, 255), base, min(1.0, h * 2.2)), deep, max(0.0, h - 0.5) * 1.2)
            edge = 255 if d < r - 2 else int(255 * (r - d + 1) / 3)
            px[x, y] = (*col, max(0, min(255, edge)))
    img.alpha_composite(sphere)
    # orbit ring
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((96, 96, S - 96, S - 96), outline=(129, 140, 248, 230), width=22)
    img.alpha_composite(ring.filter(ImageFilter.GaussianBlur(1.2)))
    return img


def main() -> None:
    big = orb()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "arya.png")
    big.resize((256, 256), Image.LANCZOS).save(
        OUT / "arya.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "arya.png", OUT / "arya.ico")


if __name__ == "__main__":
    main()
