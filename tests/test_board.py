"""Tests for the board model in context_board, run without Glyphs.

Run from the repo root: ``uv run --with pytest pytest tests``
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


class _Defaults(dict):
    registered: dict = {}

    def __missing__(self, key: str) -> object:
        return self.registered.get(key)  # like Glyphs.defaults: registered value, else None


# Stand-ins for the Glyphs-only modules context_board imports.
_defaults = _Defaults()
_glyphs_app = types.ModuleType("GlyphsApp")
_glyphs_app.Glyphs = types.SimpleNamespace(
    defaults=_defaults, registerDefault=_defaults.registered.__setitem__
)
_foundation = types.ModuleType("Foundation")
_foundation.NSAttributedString = object
sys.modules.update(
    {"GlyphsApp": _glyphs_app, "Foundation": _foundation, "objc": types.ModuleType("objc")}
)
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "AZ-Context.glyphsPlugin/Contents/Resources")
)

import context_board as board  # noqa: E402

DEFAULT = {"left": "nH", "right": "Oo", "axis": None, "mode": "glyphs"}


def test_normalize_keeps_valid_fields() -> None:
    assert board.normalize_board(None) == DEFAULT
    assert board.normalize_board(42) == DEFAULT
    raw = {"left": "/h.sc n", "right": 7, "axis": "wght", "mode": "axis"}
    assert board.normalize_board(raw) == {
        "left": "/h.sc n",
        "right": "Oo",
        "axis": "wght",
        "mode": "axis",
    }
    assert board.normalize_board({"left": ""})["left"] == ""  # an empty side is allowed
    assert board.normalize_board({"mode": "spacing"})["mode"] == "glyphs"


def test_tokens() -> None:
    assert board.tokens("nH") == ["n", "H"]
    assert board.tokens("/h.sc n/a.ss01/b H") == ["/h.sc", "n", "/a.ss01", "/b", "H"]
    assert board.tokens(" a  b / /") == ["a", "b"]  # whitespace and bare slashes skipped


def test_load_and_mutate_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(board, "repaint", lambda font: None)
    defaults = board.Glyphs.defaults
    defaults.clear()
    assert board.load() == DEFAULT
    defaults[board.BOARD_KEY] = "not json{"
    assert board.load() == DEFAULT
    board.set_side_text(object(), "left", "HH")
    board.mutate(object(), lambda current: {**current, "mode": "axis"})
    assert board.load() == {**DEFAULT, "left": "HH", "mode": "axis"}
    board.mutate(None, lambda current: {**current, "left": "zz"})  # no font: no write
    assert board.load()["left"] == "HH"


def test_display_settings() -> None:
    defaults = board.Glyphs.defaults
    defaults.clear()
    assert board.color(board.FILL_COLOR_KEY) == (0.55, 0.35, 0.85, 0.14)  # registered default
    assert defaults[board.SHOW_GUIDES_KEY] is False
    assert defaults[board.SHOW_MEASUREMENTS_KEY] is True
    board.set_color(board.STROKE_COLOR_KEY, (1, 0, 0, 0.5))
    assert board.color(board.STROKE_COLOR_KEY) == (1.0, 0.0, 0.0, 0.5)
    for broken in ("nope", "[1, 2]", None):
        defaults[board.STROKE_COLOR_KEY] = broken
        assert board.color(board.STROKE_COLOR_KEY) == (0.42, 0.22, 0.70, 0.55)


# 2-axis font: wght (Thin=0, Bold=100) x wdth (Normal=100, Cond=75), all roman.
AXES = ["wght", "wdth"]
NAMES = ["Thin", "Bold", "Cond Thin", "Cond Bold"]
VALUES = {
    "Thin": {"wght": 0.0, "wdth": 100.0},
    "Bold": {"wght": 100.0, "wdth": 100.0},
    "Cond Thin": {"wght": 0.0, "wdth": 75.0},
    "Cond Bold": {"wght": 100.0, "wdth": 75.0},
}
ITALIC = {name: False for name in NAMES}


def test_group_by_wght_splits_by_width() -> None:
    grouped = board.group_masters_by_axis(AXES, NAMES, VALUES, ITALIC)
    groups = grouped["wght"]
    assert sorted(sorted(g) for g in groups) == [
        ["Bold", "Thin"],
        ["Cond Bold", "Cond Thin"],
    ]


def test_group_for_master_and_sort() -> None:
    grouped = board.group_masters_by_axis(AXES, NAMES, VALUES, ITALIC)
    group = board.group_for_master(grouped["wght"], "Bold")
    assert set(group) == {"Thin", "Bold"}
    assert board.sort_group_by_axis(group, "wght", VALUES) == ["Thin", "Bold"]


def test_auto_axis_ties_break_by_order() -> None:
    grouped = board.group_masters_by_axis(AXES, NAMES, VALUES, ITALIC)
    # both axes give a group of size 2 for "Bold"; first axis wins
    assert board.auto_axis(grouped, AXES, "Bold") == "wght"


def test_italic_discrimination_on_weight_axis() -> None:
    names = ["Reg", "Bold", "Reg It", "Bold It"]
    values = {
        "Reg": {"wght": 0.0},
        "Bold": {"wght": 100.0},
        "Reg It": {"wght": 0.0},
        "Bold It": {"wght": 100.0},
    }
    italic = {"Reg": False, "Bold": False, "Reg It": True, "Bold It": True}
    grouped = board.group_masters_by_axis(["wght"], names, values, italic)
    groups = [sorted(g) for g in grouped["wght"]]
    assert ["Bold", "Reg"] in groups
    assert ["Bold It", "Reg It"] in groups


def test_single_master_has_no_axis() -> None:
    grouped = board.group_masters_by_axis(["wght"], ["Reg"], {"Reg": {"wght": 0.0}}, {"Reg": False})
    assert board.group_for_master(grouped["wght"], "Reg") == ["Reg"]
    assert board.auto_axis(grouped, ["wght"], "Reg") is None


def test_slant_axis_does_not_split_roman_italic() -> None:
    names = ["Upright", "Slanted"]
    values = {"Upright": {"slnt": 0.0}, "Slanted": {"slnt": 0.0}}
    italic = {"Upright": False, "Slanted": True}
    grouped = board.group_masters_by_axis(["slnt"], names, values, italic)
    assert len(grouped["slnt"]) == 1
    assert sorted(grouped["slnt"][0]) == ["Slanted", "Upright"]
