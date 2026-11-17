"""pi/eink/eink_art.py: the circuit border and the sunglasses robot."""

from __future__ import annotations

import typing

import eink_art as art
import pytest

W, H = 250, 122  # the panel
TERMINUS_12_WIDTH = 6  # px a character


def test_every_emotion_has_its_parts():
    assert set(art.PARTS) == set(typing.get_args(art.Emotion)) == set(art.EMOTIONS)
    assert len(art.EMOTIONS) == 25  # pwnagotchi's face names


def test_every_emotion_has_its_own_robot():
    assert len({art.robot(e) for e in art.EMOTIONS}) == len(art.EMOTIONS)


def test_the_upload_arrows_step():
    assert art.robot("upload") < art.robot("upload1") < art.robot("upload2")


@pytest.mark.parametrize("emotion", art.EMOTIONS)
def test_each_robot_stays_on_its_grid(emotion):
    assert all(0 <= x < art.GRID_W and 0 <= y < art.GRID_H for x, y in art.robot(emotion))


@pytest.mark.parametrize("emotion", art.EMOTIONS)
def test_each_label_fits_under_the_robot(emotion):
    assert len(art.label(emotion)) * TERMINUS_12_WIDTH < art.COLUMN - 1  # clear of the divider


@pytest.fixture
def canvas():
    """A blank panel-sized 1-bit image and a pen on it."""
    image = pytest.importorskip("PIL.Image")
    draw = pytest.importorskip("PIL.ImageDraw")
    img = image.new("1", (W, H), 255)
    return img, draw.Draw(img)


def test_the_border_stays_in_its_band(canvas):
    img, pen = canvas
    art.circuit_border(pen, W, H)
    b = art.BORDER
    inside = [img.getpixel((x, y)) for x in range(b, W - b) for y in range(b, H - b)]
    assert all(inside), "the border reaches into the screen"
    band = [img.getpixel((x, y)) for x in range(W) for y in range(H) if not (b <= x < W - b and b <= y < H - b)]
    assert band.count(0) > len(band) // 6, "too sparse to read as a circuit"


def test_a_chip_in_each_corner_turned_to_it(canvas):
    img, pen = canvas
    art.circuit_border(pen, W, H)
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


def test_the_robot_fits_its_column(canvas):
    img, pen = canvas
    art.draw_robot(pen, "cool", 200, 30)
    assert art.GRID_W * 3 < art.COLUMN - 1
    xs = [x for x in range(W) for y in range(H) if img.getpixel((x, y)) == 0]
    assert min(xs) >= 200 - art.GRID_W * 3 // 2 and max(xs) < 200 + art.GRID_W * 3 // 2
