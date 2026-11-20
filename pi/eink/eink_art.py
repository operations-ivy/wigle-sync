"""The e-ink screen's art: a chip in each corner and the mascot, pwnagotchi's
stock text face for each of its 25 emotions (the user, 2026-11-20: they replace
our sunglasses robot). Pure drawing on a PIL ImageDraw, so it's tested without
a panel."""

from __future__ import annotations

from typing import get_args
from typing import Literal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.ImageDraw import ImageDraw

BORDER = 11  # px: the band round the edge kept for the corner chips
CHIP = 11  # px: a corner chip, pins included


def corner_chips(pen: ImageDraw, width: int, height: int) -> None:
    """A chip in each corner, in the outer BORDER px, its pin-1 notch turned to
    its corner (the user, 2026-11-20: the chips without the circuit traces)."""
    corners = (  # each corner's (x, y) from its own corner, mirrored
        lambda x, y: (x, y),
        lambda x, y: (width - 1 - x, y),
        lambda x, y: (width - 1 - x, height - 1 - y),
        lambda x, y: (x, height - 1 - y),
    )
    for at in corners:
        xs, ys = zip(at(2, 2), at(8, 8))  # the body, whichever way the corner mirrors it
        pen.rectangle((min(xs), min(ys), max(xs), max(ys)), fill=0)
        pen.point(at(3, 3), fill=255)  # pin 1, towards the corner
        for k in (3, 5, 7):  # pins on its two inner sides
            pen.line([at(9, k), at(CHIP - 1, k)], fill=0)
            pen.line([at(k, 9), at(k, CHIP - 1)], fill=0)


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
