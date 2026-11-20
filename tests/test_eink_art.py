"""pi/eink/eink_art.py: the corner chips and pwnagotchi's stock faces."""

from __future__ import annotations

import typing
from pathlib import Path

import eink_art as art
import pytest

W, H = 250, 122  # the panel
TERMINUS_12_WIDTH = 6  # px a character


def test_every_emotion_has_its_face():
    assert set(art.FACES) == set(typing.get_args(art.Emotion)) == set(art.EMOTIONS)
    assert len(art.EMOTIONS) == 25  # pwnagotchi's face names


def test_the_faces_are_pwnagotchis_stock_ones():
    assert art.FACES["cool"] == "(⌐■_■)"
    assert art.FACES["awake"] == "(◕‿‿◕)"
    assert art.FACES["broken"] == "(☓‿‿☓)"
    assert len(set(art.FACES.values())) == len(art.FACES)


@pytest.mark.parametrize("emotion", art.EMOTIONS)
def test_each_label_fits_under_the_face(emotion):
    assert len(art.label(emotion)) * TERMINUS_12_WIDTH < art.COLUMN - 1  # clear of the divider


@pytest.fixture
def canvas():
    """A blank panel-sized 1-bit image and a pen on it."""
    image = pytest.importorskip("PIL.Image")
    draw = pytest.importorskip("PIL.ImageDraw")
    img = image.new("1", (W, H), 255)
    return img, draw.Draw(img)


def test_the_chips_stay_in_the_band(canvas):
    img, pen = canvas
    art.corner_chips(pen, W, H)
    b = art.BORDER
    inside = [img.getpixel((x, y)) for x in range(b, W - b) for y in range(b, H - b)]
    assert all(inside), "a chip reaches into the screen"


def test_only_the_corners_have_anything(canvas):
    img, pen = canvas
    art.corner_chips(pen, W, H)
    c = art.CHIP
    black = [(x, y) for x in range(W) for y in range(H) if img.getpixel((x, y)) == 0]
    assert all((x < c or x >= W - c) and (y < c or y >= H - c) for x, y in black), "no traces between the chips"


def test_a_chip_in_each_corner_turned_to_it(canvas):
    img, pen = canvas
    art.corner_chips(pen, W, H)
    c = art.CHIP

    def corner(x0, y0, flip_x, flip_y):
        return [
            img.getpixel((x0 + (c - 1 - i if flip_x else i), y0 + (c - 1 - j if flip_y else j)))
            for j in range(c)
            for i in range(c)
        ]

    top_left = corner(0, 0, False, False)
    assert top_left.count(0) > 30  # a chip, not just a trace
    assert corner(W - c, 0, True, False) == top_left
    assert corner(W - c, H - c, True, True) == top_left
    assert corner(0, H - c, False, True) == top_left


@pytest.fixture
def face_font():
    """DejaVu Sans Mono Bold at the face size, or a skip where it isn't installed."""
    image_font = pytest.importorskip("PIL.ImageFont")
    if not Path(art.FACE_FONT).exists():
        pytest.skip("no DejaVu Sans Mono (fonts-dejavu-core)")
    return image_font.truetype(art.FACE_FONT, art.FACE_SIZE)


@pytest.mark.parametrize("emotion", art.EMOTIONS)
def test_each_face_fits_its_column(face_font, emotion):
    assert face_font.getlength(art.FACES[emotion]) < art.COLUMN - 3  # a px clear of the divider each side


def test_a_face_is_drawn_centred(canvas, face_font):
    img, pen = canvas
    art.draw_face(pen, "cool", 200, 60, face_font)
    xs = [x for x in range(W) for y in range(H) if img.getpixel((x, y)) == 0]
    ys = [y for x in range(W) for y in range(H) if img.getpixel((x, y)) == 0]
    assert abs((min(xs) + max(xs)) / 2 - 200) <= 3
    assert abs((min(ys) + max(ys)) / 2 - 60) <= 4
