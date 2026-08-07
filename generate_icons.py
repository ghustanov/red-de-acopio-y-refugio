"""Genera los íconos de la app (PWA) en web/. Correr una sola vez."""
from PIL import Image, ImageDraw, ImageFont
from config import WEB_DIR

NAVY = (11, 42, 74)
WHITE = (255, 255, 255)
RED = (225, 29, 72)

FUENTES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(size):
    for ruta in FUENTES:
        try:
            return ImageFont.truetype(ruta, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make(size, nombre):
    img = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(img)
    # Letra "A" centrada
    font = _font(int(size * 0.56))
    texto = "A"
    bbox = d.textbbox((0, 0), texto, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1] - size * 0.04
    d.text((x, y), texto, font=font, fill=WHITE)
    # Barra roja de acento (emergencia)
    bw, bh = int(size * 0.34), max(3, int(size * 0.05))
    bx, by = (size - bw) // 2, int(size * 0.76)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=RED)
    salida = WEB_DIR / nombre
    img.save(salida)
    print("  ✓", salida)


if __name__ == "__main__":
    WEB_DIR.mkdir(exist_ok=True)
    make(192, "icon-192.png")
    make(512, "icon-512.png")
    make(180, "apple-touch-icon.png")
    print("Íconos listos.")
