"""Board access shared by the reporter and the palette — the Glyphs side.

The pure board model lives in ``context_state`` / ``context_store``; this module
wires it to Glyphs: per-font userData over the global default, the typed
sibling strings resolved to glyphs, the designspace walk
(TTL-cached) and repainting the visible edit view.
The reporter and the palette both import it, so neither reaches into the other.
"""

from __future__ import annotations

import time
import traceback
from typing import Callable

import objc
from Foundation import NSAttributedString
from GlyphsApp import Glyphs

import context_designspace
import context_state
from context_store import BOARD_KEY, BoardStore

_DESIGNSPACE_TTL = 2.0  # seconds; Font Info master edits show on the board <=2 s late
_designspace_cache: tuple | None = None  # (font key, expiry, result)
_SHAPE_TTL = 2.0  # seconds; feature-code edits reach the siblings <=2 s late
_side_cache: dict = {}  # (font key, text, features) -> (expiry, [glyph names])
# GSFeatureComposer's error: is a bare ^@; declared as an out-pointer so PyObjC
# hands it valid storage (a NULL write there would crash Glyphs).
_COMPOSE_SELECTOR = b"composeString:forFont:featureTags:script:language:writingDirection:characterIndexesByLayerIndex:error:"
try:
    objc.registerMetaDataForSelector(
        b"GSFeatureComposer", _COMPOSE_SELECTOR, {"arguments": {9: {"type_modifier": b"o"}}}
    )
except Exception:
    pass


def store_for(font: object) -> BoardStore:
    """The board store for ``font``: its userData over the global default."""
    store = BoardStore(
        defaults_get=lambda: Glyphs.defaults[BOARD_KEY],
        defaults_set=lambda value: Glyphs.defaults.__setitem__(BOARD_KEY, value),
        userdata_get=lambda: _userdata_get(font),
        userdata_set=lambda value: font.userData.__setitem__(BOARD_KEY, value),
    )
    store.ensure_global_seed()
    return store


def _userdata_get(font: object) -> object:
    try:
        return font.userData[BOARD_KEY]
    except Exception:
        return None


def load(font: object) -> dict:
    """The current board for ``font`` (normalized)."""
    return store_for(font).load()


def mutate(font: object, mutator: Callable[[dict], dict | None]) -> None:
    """Load ``font``'s board, transform it via ``mutator``, save it, repaint."""
    if font is None:
        return
    store = store_for(font)
    new_board = mutator(store.load())
    if new_board is not None:
        store.save_font(new_board)
    # repaint(), not Glyphs.redraw(): a forced full pass (e.g. right after a
    # modal ends) paints the active layer with a mismatched transform in G4.
    repaint(font)


def sibling_row(
    font: object, active_name: str, board: dict, features: tuple[str, ...] = ()
) -> tuple[list[str], int]:
    """The row the reporter draws, left to right, and the edited glyph's index
    in it. Typed characters go through the edit tab's active ``features`` (ss01
    on: a -> a.ss01); ``/name`` stays as typed. Tokens the font lacks are skipped."""
    left = _side_names(font, board.get("left", ""), features)
    right = _side_names(font, board.get("right", ""), features)
    return [*left, active_name, *right], len(left)


def _side_names(font: object, text: str, features: tuple[str, ...]) -> list[str]:
    """One side's glyph names, cached: shaping costs ~1 ms per call."""
    try:
        key = (objc.pyobjc_id(font), text, features)
    except Exception:
        key = (id(font), text, features)
    now = time.monotonic()
    cached = _side_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    names: list[str] = []
    run: list[str] = []  # the current run of typed characters, shaped together

    def flush() -> None:
        if not run:
            return
        shaped = _shape(font, "".join(run), features) if features else None
        if shaped is None:
            shaped = [glyph_name(font, char) for char in run]
        names.extend(name for name in shaped if name)
        run.clear()

    for token in context_state.tokens(text):
        if token.startswith("/"):
            flush()
            name = glyph_name(font, token)
            if name:
                names.append(name)
        else:
            run.append(token)
    flush()
    if len(_side_cache) > 64:  # ponytail: crude cap, one entry per text/feature set
        _side_cache.clear()
    _side_cache[key] = (now + _SHAPE_TTL, names)
    return names


def _shape(font: object, chars: str, features: tuple[str, ...]) -> list[str | None] | None:
    """Glyph names for ``chars`` with ``features`` applied by Glyphs' own shaper
    (private ``GSFeatureComposer``; script/language left to it). None when the
    call fails, so the caller falls back to unshaped glyphs."""
    try:
        composer = objc.lookUpClass("GSFeatureComposer")
        shaped, _error = (
            composer.composeString_forFont_featureTags_script_language_writingDirection_characterIndexesByLayerIndex_error_(
                NSAttributedString.alloc().initWithString_(chars),
                font,
                list(features),
                None,
                None,
                0,
                None,
                None,
            )
        )
        if shaped is None:
            return None
        # Substituted glyphs come back as private-use code points (a.ss01 =
        # U+10FFEF); font.glyphs[char] maps those back like real Unicode.
        return [glyph_name(font, char) for char in str(shaped.string())]
    except Exception:
        return None


def tab_features(tab: object) -> tuple[str, ...]:
    """Feature tags switched on in the edit tab's Features button, sorted."""
    try:
        return tuple(sorted(str(tag) for tag in tab.selectedFeatures()))
    except Exception:
        return ()


def glyph_name(font: object, token: str) -> str | None:
    """Glyph name for a token: ``/name`` by name, a character by Unicode."""
    try:
        glyph = font.glyphs[token[1:] if token.startswith("/") else token]
        return glyph.name if glyph is not None else None
    except Exception:
        return None


def set_side_text(font: object, side: str, text: str) -> None:
    """Store what the user typed in the ``side`` palette field."""
    mutate(font, lambda board: context_state.set_side(board, side, text))


def single_glyph(tab: object) -> object:
    """The glyph when the edit tab holds exactly one real glyph, else None.

    With the caret AFTER the glyph, Glyphs adds a trailing empty placeholder
    layer (parent is None), so only layers with a real parent glyph count."""
    # Index the layout manager's array directly: Glyphs 4's tab.layers proxy
    # rebuilds the WHOLE layer list on every index step, so this per-frame
    # check used to cost O(tab length) in long proof tabs.
    try:
        entries = tab.graphicView().layoutManager().cachedLayers()
        total = entries.count()
        at = entries.objectAtIndex_
    except Exception:
        entries = list(tab.layers)
        total = len(entries)
        at = entries.__getitem__
    try:
        found = None
        for i in range(total):
            entry = at(i)
            glyph = entry.parent if entry is not None else None
            if glyph is not None:
                if found is not None:
                    return None
                found = glyph
        return found
    except Exception:
        return None


def repaint(font: object = None) -> None:
    """Invalidate the VISIBLE edit view of ``font`` (default: the front font).

    Targets the tab's graphic view actually on screen: for a reporter in G4
    ``self.controller`` can point at a stale/background editor, and
    invalidating that makes G4 repaint the active layer with a mismatched
    transform (scattered nodes, no outline). setNeedsDisplay, never
    Glyphs.redraw() (its forced full pass shows the same corruption)."""
    view = None
    try:
        if font is None:
            font = Glyphs.font
        tab = font.currentTab if font is not None else None
        view = tab.graphicView() if tab is not None else None
    except Exception:
        view = None
    try:
        if view is not None:
            view.setNeedsDisplay_(True)
        else:
            Glyphs.redraw()
    except Exception:
        traceback.print_exc()


def designspace(font: object) -> tuple[list[str], list[str], dict, dict]:
    """(axis tags, master names, master axis values, master italic flags).

    Cached ~2 s per font: this masters x axes walk crosses the PyObjC bridge on
    every access and measured 0.25 s / 12 s of draw time when run per frame."""
    global _designspace_cache
    try:
        font_key = objc.pyobjc_id(font)
    except Exception:
        font_key = id(font)
    now = time.monotonic()
    cached = _designspace_cache
    if cached is not None and cached[0] == font_key and now < cached[1]:
        return cached[2]
    result = _designspace_uncached(font)
    _designspace_cache = (font_key, now + _DESIGNSPACE_TTL, result)
    return result


def _designspace_uncached(font: object) -> tuple[list[str], list[str], dict, dict]:
    axes_tags = [axis.axisTag for axis in font.axes]
    master_names = [master.name for master in font.masters]
    master_values: dict[str, dict[str, float]] = {}
    master_italic: dict[str, bool] = {}
    for master in font.masters:
        values: dict[str, float] = {}
        for index, axis in enumerate(font.axes):
            try:
                value = float(master.internalAxesValues[axis.axisId])
            except Exception:
                try:
                    value = float(master.axes[index])
                except Exception:
                    value = 0.0
            values[axis.axisTag] = value
        master_values[master.name] = values
        master_italic[master.name] = bool(getattr(master, "italicAngle", 0) or 0)
    return axes_tags, master_names, master_values, master_italic


def axis_master_order(font: object, board: dict) -> tuple[list[str], int, str | None]:
    """(master names on the board's axis sorted by axis value, index of the
    current master, axis tag) — ``([], 0, None)`` when nothing applies. An
    unset or unknown board axis resolves to the auto axis."""
    axes_tags, master_names, master_values, master_italic = designspace(font)
    if not axes_tags or len(master_names) < 2:
        return [], 0, None
    grouped = context_designspace.group_masters_by_axis(
        axes_tags, master_names, master_values, master_italic
    )
    current = font.selectedFontMaster.name
    axis = board.get("axis")
    if axis not in axes_tags:
        axis = context_designspace.auto_axis(grouped, axes_tags, current)
    if axis is None:
        return [], 0, None
    group = context_designspace.group_for_master(grouped.get(axis, []), current)
    ordered = context_designspace.sort_group_by_axis(group, axis, master_values)
    try:
        current_index = ordered.index(current)
    except ValueError:
        current_index = 0
    return ordered, current_index, axis
