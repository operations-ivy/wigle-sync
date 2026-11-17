"""The e-ink screen's art: a circuit-board border and the mascot, an original
sunglasses robot with one sprite for each of pwnagotchi's 25 face names (the
user, 2026-11-17; the names from PWNAGOTCHI-CUSTOM-FACES-MOD, the art our own).
Pure drawing on a PIL ImageDraw, so it's tested without a panel."""

from __future__ import annotations

from typing import Literal

BORDER = 10  # px: the circuit border's band round the edge
CHIP = 11  # px: the chip in each corner


def _via(pen, x: int, y: int) -> None:
    pen.ellipse((x - 2, y - 2, x + 2, y + 2), fill=0)
    pen.point((x, y), fill=255)


def _pad(pen, x: int, y: int) -> None:
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


def circuit_border(pen, width: int, height: int) -> None:
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
        body = [at(2, 2), at(8, 8)]
        pen.rectangle(
            (min(p[0] for p in body), min(p[1] for p in body), max(p[0] for p in body), max(p[1] for p in body)), fill=0
        )
        pen.point(at(3, 3), fill=255)  # pin 1, towards the corner
        for k in (3, 5, 7):  # pins on its two inner sides, out to the edge traces
            pen.line([at(9, k), at(CHIP, k)], fill=0)
            pen.line([at(k, 9), at(k, CHIP)], fill=0)
        pen.line([at(CHIP, 1), at(CHIP, 3)], fill=0)  # the edge traces meet its outer pins
        pen.line([at(1, CHIP), at(3, CHIP)], fill=0)


GRID_W, GRID_H = 22, 20  # the robot's grid, in its own pixels

Emotion = Literal[
    "look_r",
    "look_l",
    "look_r_happy",
    "look_l_happy",
    "sleep",
    "sleep2",
    "awake",
    "bored",
    "intense",
    "cool",
    "happy",
    "excited",
    "grateful",
    "motivated",
    "demotivated",
    "smart",
    "lonely",
    "sad",
    "angry",
    "friend",
    "broken",
    "debug",
    "upload",
    "upload1",
    "upload2",
]

EMOTIONS: tuple[Emotion, ...] = (
    "look_r",
    "look_l",
    "look_r_happy",
    "look_l_happy",
    "sleep",
    "sleep2",
    "awake",
    "bored",
    "intense",
    "cool",
    "happy",
    "excited",
    "grateful",
    "motivated",
    "demotivated",
    "smart",
    "lonely",
    "sad",
    "angry",
    "friend",
    "broken",
    "debug",
    "upload",
    "upload1",
    "upload2",
)

# Each emotion: (antenna, shades, eyes, mouth, extra)
PARTS = {
    "look_r": ("up", "low", "right", "flat", None),
    "look_l": ("up", "low", "left", "flat", None),
    "look_r_happy": ("up", "low", "right", "smile", None),
    "look_l_happy": ("up", "low", "left", "smile", None),
    "sleep": ("droop", "on", None, "flat", "zz"),
    "sleep2": ("droop", "on", None, "o_small", "zzz"),
    "awake": ("up", "forehead", "open", "o", None),
    "bored": ("droop", "on", None, "flat", "dots"),
    "intense": ("up", "forehead", "narrow", "flat", "sweat"),
    "cool": ("up", "on", None, "smirk", "glint"),
    "happy": ("up", "on", None, "smile", None),
    "excited": ("spark", "on", None, "grin", "sparkles"),
    "grateful": ("up", "on", None, "smile", "heart"),
    "motivated": ("spark", "on", None, "grin", "bang"),
    "demotivated": ("droop", "low", "half", "frown", None),
    "smart": ("up", "on", None, "smirk", "bulb"),
    "lonely": ("droop", "on", None, "flat", "question"),
    "sad": ("droop", "low", "half", "frown", "tear"),
    "angry": ("spark", "angry", None, "zigzag", "steam"),
    "friend": ("up", "hearts", None, "smile", None),
    "broken": ("bent", "cracked", None, "zigzag", None),
    "debug": ("up", "forehead", "open", "flat", "bug"),
    "upload": ("spark", "on", None, "o_small", "arrow1"),
    "upload1": ("spark", "on", None, "o_small", "arrow2"),
    "upload2": ("spark", "on", None, "o_small", "arrow3"),
}


def robot(emotion: Emotion) -> set[tuple[int, int]]:
    """The black pixels of the robot for an emotion, as (x, y) on its grid."""
    antenna, shades, eyes, mouth, extra = PARTS[emotion]
    on: set[tuple[int, int]] = set()
    off: set[tuple[int, int]] = set()  # white pixels drawn over black (glints, cracks)

    def rect(x0, y0, x1, y1):
        on.update((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def px(*pts):
        on.update(pts)

    # head: a rounded square outline, bolts on the sides
    for x in range(4, 18):
        px((x, 4), (x, 17))
    for y in range(6, 16):
        px((2, y), (19, y))
    px((3, 5), (18, 5), (3, 16), (18, 16))
    rect(0, 9, 1, 11)
    rect(20, 9, 21, 11)
    # antenna
    if antenna == "droop":
        px((10, 3), (11, 2), (12, 2), (13, 3))
        rect(13, 3, 14, 3)
    elif antenna == "bent":
        px((10, 3), (9, 2), (8, 1))
        rect(6, 0, 7, 1)
    else:
        px((10, 3), (10, 2))
        rect(9, 0, 11, 1)
        if antenna == "spark":
            px((7, 0), (13, 0), (7, 2), (13, 2))
    # eyes (shown when the shades aren't over them)
    eye_y = 9
    if shades == "low":
        eye_y = 8
    if eyes == "right":
        rect(7, eye_y, 8, eye_y)
        rect(15, eye_y, 16, eye_y)
    elif eyes == "left":
        rect(5, eye_y, 6, eye_y)
        rect(13, eye_y, 14, eye_y)
    elif eyes == "open":
        rect(6, 10, 7, 11)
        rect(14, 10, 15, 11)
    elif eyes == "narrow":
        rect(5, 10, 8, 10)
        rect(13, 10, 16, 10)
    elif eyes == "half":
        rect(5, eye_y, 8, eye_y)
        rect(13, eye_y, 16, eye_y)

    # shades
    def lenses(y, glint=True):
        rect(4, y, 9, y + 2)
        rect(12, y, 17, y + 2)
        rect(10, y, 11, y)  # bridge
        off.update({(4, y + 2), (9, y + 2), (12, y + 2), (17, y + 2)})  # rounded bottoms
        rect(3, y, 3, y)
        rect(18, y, 18, y)  # arms
        if glint:
            off.update({(5, y), (13, y)})

    if shades == "on":
        lenses(8)
        if extra == "glint":
            off.update({(6, 9), (14, 9)})
    elif shades == "low":
        lenses(10, glint=False)
    elif shades == "forehead":
        lenses(5, glint=False)
    elif shades == "angry":
        rect(4, 8, 9, 10)
        rect(12, 8, 17, 10)
        off.update({(4, 8), (5, 8), (16, 8), (17, 8), (4, 9)})  # slanted down to the middle
        off.update({(17, 9)})
        rect(10, 9, 11, 9)
    elif shades == "cracked":
        lenses(8, glint=False)
        off.update({(5, 8), (6, 9), (7, 10), (14, 8), (15, 9), (14, 10)})
    elif shades == "hearts":
        for cx in (6, 14):
            px((cx - 2, 8), (cx - 1, 8), (cx + 1, 8), (cx + 2, 8))
            rect(cx - 2, 9, cx + 2, 9)
            rect(cx - 1, 10, cx + 1, 10)
            px((cx, 11))
        rect(9, 9, 11, 9)
    # mouth
    if mouth == "smile":
        px((7, 13), (14, 13))
        rect(8, 14, 13, 14)
    elif mouth == "grin":
        rect(7, 13, 14, 13)
        rect(8, 14, 13, 15)
        off.update({(9, 14), (10, 14), (11, 14), (12, 14)})
    elif mouth == "smirk":
        rect(8, 14, 12, 14)
        px((13, 13))
    elif mouth == "flat":
        rect(8, 14, 13, 14)
    elif mouth == "frown":
        rect(8, 13, 13, 13)
        px((7, 14), (14, 14))
    elif mouth == "o":
        rect(9, 13, 12, 15)
        off.update({(10, 14), (11, 14)})
    elif mouth == "o_small":
        rect(10, 13, 11, 14)
    elif mouth == "zigzag":
        px((7, 14), (8, 13), (9, 14), (10, 13), (11, 14), (12, 13), (13, 14), (14, 13))
    # extras, in the corners round the head
    if extra == "zz":
        px((15, 0), (16, 0), (17, 0), (16, 1), (15, 2), (16, 2), (17, 2))
    elif extra == "zzz":
        px((15, 1), (16, 1), (15, 2), (15, 3), (16, 3))
        px((18, 0), (19, 0), (20, 0), (19, 1), (18, 2), (19, 2), (20, 2))
    elif extra == "dots":
        px((14, 14), (16, 14))
    elif extra == "sweat":
        px((20, 5), (20, 6), (21, 6))
    elif extra == "sparkles":
        px((2, 1), (1, 2), (3, 2), (2, 3), (19, 1), (18, 2), (20, 2), (19, 3))
    elif extra == "heart":
        px((17, 0), (19, 0), (16, 1), (17, 1), (18, 1), (19, 1), (20, 1), (17, 2), (18, 2), (19, 2), (18, 3))
    elif extra == "bang":
        px((19, 0), (19, 1), (19, 3))
    elif extra == "bulb":
        rect(17, 0, 19, 2)
        px((18, 3))
        off.add((18, 1))
    elif extra == "question":
        px((17, 0), (18, 0), (19, 1), (18, 2), (18, 3))
    elif extra == "tear":
        px((16, 13), (15, 14), (16, 14), (17, 14), (16, 15))
    elif extra == "steam":
        px((1, 1), (2, 2), (1, 3), (20, 1), (19, 2), (20, 3))
    elif extra == "bug":
        rect(17, 1, 19, 3)
        px((16, 0), (20, 0), (16, 2), (20, 2))
    elif extra in ("arrow1", "arrow2", "arrow3"):
        for x in ((18,), (2, 18), (2, 6, 18))[int(extra[-1]) - 1]:  # one more arrow per step
            px((x, 0), (x - 1, 1), (x, 1), (x + 1, 1), (x, 2), (x, 3))
    return on - off


def draw_robot(pen, emotion: Emotion, centre_x: int, top: int, scale: int = 3) -> None:
    """The robot for an emotion, each of its pixels a scale x scale square."""
    left = centre_x - GRID_W * scale // 2
    for x, y in robot(emotion):
        pen.rectangle(
            (left + x * scale, top + y * scale, left + x * scale + scale - 1, top + y * scale + scale - 1), fill=0
        )


# Short labels under the robot, for the names that don't fit its column.
LABELS: dict[str, str] = {
    "look_r": "look >",
    "look_l": "< look",
    "look_r_happy": "look > :)",
    "look_l_happy": ":) < look",
    "sleep2": "sleep",
    "upload1": "upload",
    "upload2": "upload",
}


def label(emotion: Emotion) -> str:
    return LABELS.get(emotion, emotion)
