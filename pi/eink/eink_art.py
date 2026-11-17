"""The e-ink screen's art: a circuit-board border and the mascot, an original
sunglasses robot with one sprite for each of pwnagotchi's 25 face names (the
user, 2026-11-17; the names from PWNAGOTCHI-CUSTOM-FACES-MOD, the art our own).
Pure drawing on a PIL ImageDraw, so it's tested without a panel."""

from __future__ import annotations

from typing import get_args
from typing import Literal
from typing import NamedTuple
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


GRID_W, GRID_H = 22, 20  # the robot's grid, in its own pixels

Emotion = Literal[
    "look_r", "look_l", "look_r_happy", "look_l_happy", "sleep", "sleep2", "awake", "bored",
    "intense", "cool", "happy", "excited", "grateful", "motivated", "demotivated", "smart",
    "lonely", "sad", "angry", "friend", "broken", "debug", "upload", "upload1", "upload2",
]  # fmt: skip
EMOTIONS: tuple[Emotion, ...] = get_args(Emotion)

Pixel = tuple[int, int]


class Layer(NamedTuple):
    """One part of the robot: its black pixels, and white ones it punches
    through whatever else is drawn (glints, cracks, teeth)."""

    on: frozenset[Pixel]
    off: frozenset[Pixel] = frozenset()


def _rect(x0: int, y0: int, x1: int, y1: int) -> frozenset[Pixel]:
    return frozenset((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))


def _pts(*pixels: Pixel) -> frozenset[Pixel]:
    return frozenset(pixels)


# A rounded square outline, with bolts on its sides.
HEAD = Layer(
    frozenset((x, y) for x in range(4, 18) for y in (4, 17))
    | frozenset((x, y) for x in (2, 19) for y in range(6, 16))
    | _pts((3, 5), (18, 5), (3, 16), (18, 16))
    | _rect(0, 9, 1, 11)
    | _rect(20, 9, 21, 11)
)

_UP = _pts((10, 3), (10, 2)) | _rect(9, 0, 11, 1)
ANTENNAS = {
    "up": Layer(_UP),
    "spark": Layer(_UP | _pts((7, 0), (13, 0), (7, 2), (13, 2))),
    "droop": Layer(_pts((10, 3), (11, 2), (12, 2), (13, 3)) | _rect(13, 3, 14, 3)),
    "bent": Layer(_pts((10, 3), (9, 2), (8, 1)) | _rect(6, 0, 7, 1)),
}


def _eyes(kind: str | None, shades_low: bool) -> Layer:
    """The eyes, where the shades don't cover them: above shades slid down
    the nose (a row higher), or under shades pushed up."""
    y = 8 if shades_low else 9
    eyes = {
        "right": _rect(7, y, 8, y) | _rect(15, y, 16, y),
        "left": _rect(5, y, 6, y) | _rect(13, y, 14, y),
        "half": _rect(5, y, 8, y) | _rect(13, y, 16, y),
        "open": _rect(6, 10, 7, 11) | _rect(14, 10, 15, 11),
        "narrow": _rect(5, 10, 8, 10) | _rect(13, 10, 16, 10),
    }
    return Layer(eyes.get(kind, frozenset()))


def _lenses(y: int, glint: bool = True) -> Layer:
    """Two lenses with rounded bottoms, a bridge and the arms, top row at y."""
    on = _rect(4, y, 9, y + 2) | _rect(12, y, 17, y + 2) | _rect(10, y, 11, y) | _pts((3, y), (18, y))
    off = _pts((4, y + 2), (9, y + 2), (12, y + 2), (17, y + 2)) | (_pts((5, y), (13, y)) if glint else frozenset())
    return Layer(on, off)


SHADES = {
    "on": _lenses(8),
    "low": _lenses(10, glint=False),  # slid down the nose
    "forehead": _lenses(5, glint=False),  # pushed up
    "angry": Layer(  # slanted down to the middle
        _rect(4, 8, 9, 10) | _rect(12, 8, 17, 10) | _rect(10, 9, 11, 9),
        _pts((4, 8), (5, 8), (16, 8), (17, 8), (4, 9), (17, 9)),
    ),
    "cracked": Layer(
        _lenses(8, glint=False).on,
        _lenses(8, glint=False).off | _pts((5, 8), (6, 9), (7, 10), (14, 8), (15, 9), (14, 10)),
    ),
    "hearts": Layer(
        frozenset().union(
            *(
                _pts((cx - 2, 8), (cx - 1, 8), (cx + 1, 8), (cx + 2, 8), (cx, 11))
                | _rect(cx - 2, 9, cx + 2, 9)
                | _rect(cx - 1, 10, cx + 1, 10)
                for cx in (6, 14)
            )
        )
        | _rect(9, 9, 11, 9)
    ),
}

MOUTHS = {
    "smile": Layer(_pts((7, 13), (14, 13)) | _rect(8, 14, 13, 14)),
    "grin": Layer(_rect(7, 13, 14, 13) | _rect(8, 14, 13, 15), _rect(9, 14, 12, 14)),
    "smirk": Layer(_rect(8, 14, 12, 14) | _pts((13, 13))),
    "flat": Layer(_rect(8, 14, 13, 14)),
    "frown": Layer(_rect(8, 13, 13, 13) | _pts((7, 14), (14, 14))),
    "o": Layer(_rect(9, 13, 12, 15), _pts((10, 14), (11, 14))),
    "o_small": Layer(_rect(10, 13, 11, 14)),
    "zigzag": Layer(_pts((7, 14), (8, 13), (9, 14), (10, 13), (11, 14), (12, 13), (13, 14), (14, 13))),
}


def _arrows(*xs: int) -> Layer:
    return Layer(frozenset().union(*(_pts((x, 0), (x - 1, 1), (x, 1), (x + 1, 1), (x, 2), (x, 3)) for x in xs)))


# Extras round the head (and the glint, which only marks the shades).
EXTRAS = {
    "glint": Layer(frozenset(), _pts((6, 9), (14, 9))),
    "zz": Layer(_pts((15, 0), (16, 0), (17, 0), (16, 1), (15, 2), (16, 2), (17, 2))),
    "zzz": Layer(
        _pts((15, 1), (16, 1), (15, 2), (15, 3), (16, 3), (18, 0), (19, 0), (20, 0), (19, 1), (18, 2), (19, 2), (20, 2))
    ),
    "dots": Layer(_pts((14, 14), (16, 14))),
    "sweat": Layer(_pts((20, 5), (20, 6), (21, 6))),
    "sparkles": Layer(_pts((2, 1), (1, 2), (3, 2), (2, 3), (19, 1), (18, 2), (20, 2), (19, 3))),
    "heart": Layer(_pts((17, 0), (19, 0), (18, 3)) | _rect(16, 1, 20, 1) | _rect(17, 2, 19, 2)),
    "bang": Layer(_pts((19, 0), (19, 1), (19, 3))),
    "bulb": Layer(_rect(17, 0, 19, 2) | _pts((18, 3)), _pts((18, 1))),
    "question": Layer(_pts((17, 0), (18, 0), (19, 1), (18, 2), (18, 3))),
    "tear": Layer(_pts((16, 13), (15, 14), (16, 14), (17, 14), (16, 15))),
    "steam": Layer(_pts((1, 1), (2, 2), (1, 3), (20, 1), (19, 2), (20, 3))),
    "bug": Layer(_rect(17, 1, 19, 3) | _pts((16, 0), (20, 0), (16, 2), (20, 2))),
    "arrow1": _arrows(18),  # one more arrow each upload step
    "arrow2": _arrows(2, 18),
    "arrow3": _arrows(2, 6, 18),
}


class Parts(NamedTuple):
    antenna: str
    shades: str
    eyes: str | None
    mouth: str
    extra: str | None = None


PARTS: dict[Emotion, Parts] = {
    "look_r": Parts("up", "low", "right", "flat"),
    "look_l": Parts("up", "low", "left", "flat"),
    "look_r_happy": Parts("up", "low", "right", "smile"),
    "look_l_happy": Parts("up", "low", "left", "smile"),
    "sleep": Parts("droop", "on", None, "flat", "zz"),
    "sleep2": Parts("droop", "on", None, "o_small", "zzz"),
    "awake": Parts("up", "forehead", "open", "o"),
    "bored": Parts("droop", "on", None, "flat", "dots"),
    "intense": Parts("up", "forehead", "narrow", "flat", "sweat"),
    "cool": Parts("up", "on", None, "smirk", "glint"),
    "happy": Parts("up", "on", None, "smile"),
    "excited": Parts("spark", "on", None, "grin", "sparkles"),
    "grateful": Parts("up", "on", None, "smile", "heart"),
    "motivated": Parts("spark", "on", None, "grin", "bang"),
    "demotivated": Parts("droop", "low", "half", "frown"),
    "smart": Parts("up", "on", None, "smirk", "bulb"),
    "lonely": Parts("droop", "on", None, "flat", "question"),
    "sad": Parts("droop", "low", "half", "frown", "tear"),
    "angry": Parts("spark", "angry", None, "zigzag", "steam"),
    "friend": Parts("up", "hearts", None, "smile"),
    "broken": Parts("bent", "cracked", None, "zigzag"),
    "debug": Parts("up", "forehead", "open", "flat", "bug"),
    "upload": Parts("spark", "on", None, "o_small", "arrow1"),
    "upload1": Parts("spark", "on", None, "o_small", "arrow2"),
    "upload2": Parts("spark", "on", None, "o_small", "arrow3"),
}


def robot(emotion: Emotion) -> frozenset[Pixel]:
    """The black pixels of the robot for an emotion, as (x, y) on its grid:
    every part's black, less every part's white."""
    p = PARTS[emotion]
    layers = [HEAD, ANTENNAS[p.antenna], _eyes(p.eyes, p.shades == "low"), SHADES[p.shades], MOUTHS[p.mouth]]
    if p.extra:
        layers.append(EXTRAS[p.extra])
    on = frozenset().union(*(layer.on for layer in layers))
    off = frozenset().union(*(layer.off for layer in layers))
    return on - off


def draw_robot(pen: ImageDraw, emotion: Emotion, centre_x: int, top: int, scale: int = 3) -> None:
    """The robot for an emotion, each of its pixels a scale x scale square."""
    left = centre_x - GRID_W * scale // 2
    for x, y in robot(emotion):
        pen.rectangle(
            (left + x * scale, top + y * scale, left + (x + 1) * scale - 1, top + (y + 1) * scale - 1), fill=0
        )


COLUMN = 70  # px: the robot's column on the right of the screen, divider included

# Short labels under the robot, for the names that don't fit its column.
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
    """The text under the robot: its emotion, shortened where it wouldn't fit."""
    return LABELS.get(emotion, emotion)
