"""A drawn parking spot for the demo camera: a carport wall with a wallbox, a parking bay and, when one is
parked, a car seen from the front. Everything is drawn here with Pillow; no photograph and no number plate."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter

WIDTH, HEIGHT = 1280, 720

#: Body colours by car (the entity id prefixes of the demo Kia cars), clearly different from each other.
COLOURS = {
    "family_car": (40, 82, 140),
    "city_car": (178, 44, 40),
}


def _shade(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(channel * factor))) for channel in colour)


def _car(draw: ImageDraw.ImageDraw, colour: tuple[int, int, int], cx: int, ground: int, scale: float) -> None:
    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[int, int, int, int]:
        return (round(cx + x0 * scale), round(ground - y1 * scale), round(cx + x1 * scale), round(ground - y0 * scale))

    tyre = (24, 24, 26)
    glass = (58, 72, 86)
    # Wheels under the body, the shadow on the ground.
    draw.ellipse(box(-260, -22, 260, 22), fill=(52, 52, 55))
    draw.rounded_rectangle(box(-230, 0, -170, 70), radius=round(10 * scale), fill=tyre)
    draw.rounded_rectangle(box(170, 0, 230, 70), radius=round(10 * scale), fill=tyre)
    # Lower body, bonnet line, cabin and windscreen.
    draw.rounded_rectangle(box(-250, 40, 250, 170), radius=round(36 * scale), fill=colour)
    draw.polygon(
        [box(-215, 165, -215, 165)[:2], box(215, 165, 215, 165)[:2], box(165, 290, 165, 290)[:2], box(-165, 290, -165, 290)[:2]],
        fill=_shade(colour, 0.92),
    )
    draw.polygon(
        [box(-190, 172, -190, 172)[:2], box(190, 172, 190, 172)[:2], box(150, 272, 150, 272)[:2], box(-150, 272, -150, 272)[:2]],
        fill=glass,
    )
    draw.line([box(-120, 262, -120, 262)[:2], box(-40, 190, -40, 190)[:2]], fill=(110, 126, 140), width=max(2, round(4 * scale)))
    # Mirrors, lights, the grille and the bumper (no plate).
    draw.rounded_rectangle(box(-262, 150, -222, 176), radius=round(6 * scale), fill=_shade(colour, 0.8))
    draw.rounded_rectangle(box(222, 150, 262, 176), radius=round(6 * scale), fill=_shade(colour, 0.8))
    draw.rounded_rectangle(box(-232, 118, -140, 146), radius=round(10 * scale), fill=(232, 236, 240))
    draw.rounded_rectangle(box(140, 118, 232, 146), radius=round(10 * scale), fill=(232, 236, 240))
    draw.rounded_rectangle(box(-110, 92, 110, 132), radius=round(12 * scale), fill=(30, 32, 36))
    draw.rounded_rectangle(box(-245, 40, 245, 72), radius=round(14 * scale), fill=_shade(colour, 0.7))


def render(vehicle: str | None, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """The parking spot with `vehicle` (one of `COLOURS`, or `None`: empty) as a JPEG."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (150, 160, 168))
    draw = ImageDraw.Draw(image)
    horizon = 300
    # Carport wall (boards), a hedge on the left, the ground.
    draw.rectangle((0, 0, WIDTH, horizon), fill=(176, 168, 152))
    for x in range(0, WIDTH, 46):
        draw.line([(x, 0), (x, horizon)], fill=(156, 148, 134), width=3)
    draw.rectangle((0, 120, 230, horizon), fill=(62, 96, 58))
    for x in range(0, 230, 18):
        draw.ellipse((x - 14, 100 + (x * 7) % 30, x + 20, 150 + (x * 7) % 30), fill=(70, 108, 64))
    draw.polygon([(0, horizon), (WIDTH, horizon), (WIDTH, HEIGHT), (0, HEIGHT)], fill=(98, 100, 104))
    # The bay's lines, in perspective.
    for top, bottom in ((360, 150), (920, 1130)):
        draw.line([(top, horizon + 4), (bottom, HEIGHT)], fill=(226, 226, 220), width=9)
    draw.line([(360, horizon + 4), (920, horizon + 4)], fill=(226, 226, 220), width=6)
    # The wallbox on the wall.
    draw.rounded_rectangle((990, 150, 1070, 250), radius=12, fill=(232, 234, 236))
    draw.ellipse((1018, 178, 1042, 202), fill=(40, 170, 90))
    draw.line([(1030, 250), (1030, 288), (1000, 300)], fill=(30, 30, 30), width=6)
    if vehicle in COLOURS:
        _car(draw, COLOURS[vehicle], cx=640, ground=610, scale=1.05)
    image = image.filter(ImageFilter.GaussianBlur(0.8))
    if (width, height) != (WIDTH, HEIGHT):
        image = image.resize((width, height))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    return out.getvalue()


if __name__ == "__main__":
    import sys

    sys.stdout.buffer.write(render(sys.argv[1] if len(sys.argv) > 1 else "family_car"))
