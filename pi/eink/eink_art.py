"""The e-ink screen's art: a circuit-board border and the mascot, pwnagotchi's
stock text face for each of its 25 emotions (the user, 2026-11-20: they replace
our sunglasses robot). Pure drawing on a PIL ImageDraw, so it's tested without
a panel."""

from __future__ import annotations

from typing import get_args
from typing import Literal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.ImageDraw import ImageDraw

BORDER = 11  # px: the circuit border's band round the edge (its inner pads reach 10 px in)
CHIP = 11  # px: the chip in each corner


def _via(pen: ImageDraw, x: int, y: int) -> None:
    pen.ellipse((x - 2, y - 2, x + 2, y + 2), fill=0)
    pen.point((x, y), fill=255)


def _pad(pen: ImageDraw, x: int, y: int) -> None:
    pen.rectangle((x - 1, y - 1, x + 1, y + 1), fill=0)


def _side_runs(length: int) -> list[tuple[int, int, int]]:
    """Trace runs along a side, as (lane, start, end) in px along it, clear of
    the corner chips. Lane 4 runs drop out to the edge trace; lane 7 runs, set
    between them, bend in to a pad. Fixed, so every screen has the same board."""
    runs, u, n = [], CHIP + 4, 0
    while u + 14 < length - CHIP - 4:
        span = (14, 9, 20, 11, 16, 8, 12)[n % 7]
        runs.append((4, u, min(u + span, length - CHIP - 6)))
        if u + span + 13 < length - CHIP - 4:
            runs.append((7, u + span // 2 + 1, u + span // 2 + 7))
        u += span + 8
        n += 1
    return runs


def circuit_border(pen: ImageDraw, width: int, height: int) -> None:
    """A circuit-board border in the outer BORDER px: an edge trace, two lanes
    of short traces with vias and pads, and a chip in each corner, its pin-1
    notch turned to its corner."""
    sides = {  # (u along the side, v in from its edge) to (x, y), and the side's length
        "top": (lambda u, v: (u, v), width),
        "right": (lambda u, v: (width - 1 - v, u), height),
        "bottom": (lambda u, v: (width - 1 - u, height - 1 - v), width),
        "left": (lambda u, v: (v, height - 1 - u), height),
    }
    for at, length in sides.values():
        pen.line([at(CHIP, 1), at(length - 1 - CHIP, 1)], fill=0)  # the edge trace, chip to chip
        for lane, a, b in _side_runs(length):
            _via(pen, *at(a, lane))
            pen.line([at(a + 3, lane), at(b, lane)], fill=0)
            if lane == 4:  # out to the edge trace
                pen.line([at(b, lane), at(b, 1)], fill=0)
            else:  # in on a 45, to a pad
                pen.line([at(b, lane), at(b + 2, lane + 2)], fill=0)
                _pad(pen, *at(b + 3, lane + 2))
    corners = {  # each corner's (x, y) from its own corner, mirrored
        "top left": lambda x, y: (x, y),
        "top right": lambda x, y: (width - 1 - x, y),
        "bottom right": lambda x, y: (width - 1 - x, height - 1 - y),
        "bottom left": lambda x, y: (x, height - 1 - y),
    }
    for at in corners.values():
        xs, ys = zip(at(2, 2), at(8, 8))  # the body, whichever way the corner mirrors it
        pen.rectangle((min(xs), min(ys), max(xs), max(ys)), fill=0)
        pen.point(at(3, 3), fill=255)  # pin 1, towards the corner
        for k in (3, 5, 7):  # pins on its two inner sides, out to the edge traces
            pen.line([at(9, k), at(CHIP, k)], fill=0)
            pen.line([at(k, 9), at(k, CHIP)], fill=0)
        pen.line([at(CHIP, 1), at(CHIP, 3)], fill=0)  # the edge traces meet its outer pins
        pen.line([at(1, CHIP), at(3, CHIP)], fill=0)


FACE_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"  # pwnagotchi's face font
FACE_SIZE = 18  # px: pwnagotchi's is 25, which won't fit the column

Emotion = Literal[
    "look_r", "look_l", "look_r_happy", "look_l_happy", "sleep", "sleep2", "awake", "bored",
    "intense", "cool", "happy", "excited", "grateful", "motivated", "demotivated", "smart",
    "lonely", "sad", "angry", "friend", "broken", "debug", "upload", "upload1", "upload2",
]  # fmt: skip
EMOTIONS: tuple[Emotion, ...] = get_args(Emotion)

# pwnagotchi's stock faces (pwnagotchi/ui/faces.py), one per emotion.
FACES: dict[Emotion, str] = {
    "look_r": "( ⚆_⚆)",
    "look_l": "(☉_☉ )",
    "look_r_happy": "( ◕‿◕)",
    "look_l_happy": "(◕‿◕ )",
    "sleep": "(⇀‿‿↼)",
    "sleep2": "(≖‿‿≖)",
    "awake": "(◕‿‿◕)",
    "bored": "(-__-)",
    "intense": "(°▃▃°)",
    "cool": "(⌐■_■)",
    "happy": "(•‿‿•)",
    "excited": "(ᵔ◡◡ᵔ)",
    "grateful": "(^‿‿^)",
    "motivated": "(☼‿‿☼)",
    "demotivated": "(≖__≖)",
    "smart": "(✜‿‿✜)",
    "lonely": "(ب__ب)",
    "sad": "(╥☁╥ )",
    "angry": "(-_-')",
    "friend": "(♥‿‿♥)",
    "broken": "(☓‿‿☓)",
    "debug": "(#__#)",
    "upload": "(1__0)",
    "upload1": "(1__1)",
    "upload2": "(0__1)",
}


def draw_face(pen: ImageDraw, emotion: Emotion, centre_x: int, middle_y: int, font=None) -> None:
    """The face for an emotion, centred on (centre_x, middle_y)."""
    if font is None:
        from PIL import ImageFont

        font = ImageFont.truetype(FACE_FONT, FACE_SIZE)
    pen.text((centre_x, middle_y), FACES[emotion], font=font, fill=0, anchor="mm")


COLUMN = 70  # px: the face's column on the right of the screen, divider included

# Short labels under the face, for the names that don't fit its column.
LABELS: dict[Emotion, str] = {
    "look_r": "look >",
    "look_l": "< look",
    "look_r_happy": "look > :)",
    "look_l_happy": ":) < look",
    "sleep2": "sleep",
    "upload1": "upload",
    "upload2": "upload",
}


def label(emotion: Emotion) -> str:
    """The text under the face: its emotion, shortened where it wouldn't fit."""
    return LABELS.get(emotion, emotion)
