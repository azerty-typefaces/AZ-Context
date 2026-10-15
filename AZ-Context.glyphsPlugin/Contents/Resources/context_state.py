"""Pure board model for AZ Context. No Glyphs imports — unit-testable.

A board is ``{"left": str, "right": str, "axis": str | None, "mode": str}``.
``left``/``right`` are the sibling strings typed in the palette (Hugo
2026-10-11), drawn left and right of the edited glyph: plain characters, plus
``/name`` for glyphs without a Unicode value — a space or the next ``/`` ends
the name, as in Glyphs' edit view. ``mode`` is ``"glyphs"`` (those siblings in
the current master) or ``"axis"`` (the active glyph across the masters on
``axis``). Boards stored with the removed ``"overlay"`` and ``"spacing"`` modes
normalize to ``"glyphs"``; boards from before the text fields hold a
``"columns"`` list and convert once. All mutators return a new board dict; none mutate in place.
"""

from __future__ import annotations

SIDES: tuple[str, ...] = ("left", "right")
DEFAULT_LEFT = "nH"  # == the old default row n H [glyph] O o
DEFAULT_RIGHT = "Oo"
VALID_MODES: tuple[str, ...] = ("glyphs", "axis")
DEFAULT_MODE = "glyphs"


def default_board() -> dict:
    """Return a fresh default board (nH | Oo siblings, auto axis, glyphs mode)."""
    return {"left": DEFAULT_LEFT, "right": DEFAULT_RIGHT, "axis": None, "mode": DEFAULT_MODE}


def _mode_of(board: dict) -> str:
    """Return the board's mode, defaulting to ``glyphs``."""
    mode = board.get("mode")
    return mode if mode in VALID_MODES else DEFAULT_MODE


def normalize_board(raw: object) -> dict:
    """Coerce arbitrary stored data into a valid board dict."""
    if not isinstance(raw, dict):
        return default_board()
    if isinstance(raw.get("left"), str) or isinstance(raw.get("right"), str):
        left = raw.get("left") if isinstance(raw.get("left"), str) else ""
        right = raw.get("right") if isinstance(raw.get("right"), str) else ""
    else:
        # Pre-text-field board: a "columns" list laid out alternating outward
        # (1st nearest left, 2nd nearest right, ...). Keep that row.
        columns = [c for c in raw.get("columns") or [] if isinstance(c, str) and c]
        if columns:
            left = join_tokens([name_token(c) for c in reversed(columns[0::2])])
            right = join_tokens([name_token(c) for c in columns[1::2]])
        else:
            left, right = DEFAULT_LEFT, DEFAULT_RIGHT
    axis_raw = raw.get("axis")
    axis = axis_raw if isinstance(axis_raw, str) and axis_raw else None
    return {"left": left, "right": right, "axis": axis, "mode": _mode_of(raw)}


def tokens(text: str) -> list[str]:
    """Split a sibling string into tokens: single characters or ``/name``.
    Whitespace is not a sibling (type ``/space`` for the space glyph)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "/":
            end = i + 1
            while end < len(text) and text[end] not in "/ ":
                end += 1
            if end > i + 1:
                out.append(text[i:end])
            i = end + 1 if end < len(text) and text[end] == " " else end
            continue
        if not text[i].isspace():
            out.append(text[i])
        i += 1
    return out


def join_tokens(items: list[str]) -> str:
    """Inverse of ``tokens``: a space ends a ``/name`` before a plain character."""
    text = ""
    for i, item in enumerate(items):
        if i and items[i - 1].startswith("/") and not item.startswith("/"):
            text += " "
        text += item
    return text


def name_token(name: str) -> str:
    """Token for a glyph NAME: itself when one character long, else ``/name``."""
    return name if len(name) == 1 else f"/{name}"


def set_side(board: dict, side: str, text: str) -> dict:
    """Replace the ``side`` ("left"/"right") sibling string. Return a new board."""
    return {**normalize_board(board), side: text} if side in SIDES else normalize_board(board)


def set_axis(board: dict, axis: str | None) -> dict:
    """Set the active axis tag (``None`` = auto). Return a new board."""
    return {**normalize_board(board), "axis": axis}


def set_mode(board: dict, mode: str) -> dict:
    """Set the display mode (see VALID_MODES). Invalid -> ``glyphs``."""
    return {**normalize_board(board), "mode": mode if mode in VALID_MODES else DEFAULT_MODE}
