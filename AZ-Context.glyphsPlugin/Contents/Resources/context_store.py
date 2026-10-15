"""Persistence for the AZ Context board.

Backends are injected as callables so the store is unit-testable with plain
objects. The reporter wires the real ``Glyphs.defaults`` and ``font.userData``.
Read order: per-font userData, else the global default. Writes always target
per-font storage (copy-on-write) unless ``set_global_default`` is used.

The board is persisted as a JSON **string**. NSUserDefaults / userData are
property-list stores: they reject Python ``None`` and hand structured values
back as NSDictionary/NSArray (not native ``dict``/``list``) on read. A JSON
string sidesteps both — it is always plist-safe and ``json.loads`` returns a
real ``dict`` with ``None`` restored from ``null``.
"""

from __future__ import annotations

import json
from typing import Callable

from context_state import default_board, normalize_board

BOARD_KEY = "com.hugojourdan.AZContext.board"


def _encode(board: dict) -> str:
    """Normalize ``board`` and serialize it to a JSON string for storage."""
    return json.dumps(normalize_board(board))


def _decode(raw: object) -> dict:
    """Parse a stored value (JSON string, or a legacy mapping) into a board."""
    if isinstance(raw, str):
        try:
            data: object = json.loads(raw)
        except (ValueError, TypeError):
            data = None
    else:
        data = raw
    return normalize_board(data)


class BoardStore:
    """Read/write the board with copy-on-write to per-font storage."""

    def __init__(
        self,
        defaults_get: Callable[[], object],
        defaults_set: Callable[[object], None],
        userdata_get: Callable[[], object],
        userdata_set: Callable[[object], None],
    ) -> None:
        self._defaults_get = defaults_get
        self._defaults_set = defaults_set
        self._userdata_get = userdata_get
        self._userdata_set = userdata_set

    def ensure_global_seed(self) -> None:
        """Seed the global default board if none is stored yet."""
        if self._defaults_get() is None:
            self._defaults_set(_encode(default_board()))

    def load(self) -> dict:
        """Return the effective board: per-font override, else global default."""
        raw = self._userdata_get()
        if raw is None:
            raw = self._defaults_get()
        return _decode(raw)

    def save_font(self, board: dict) -> None:
        """Write the board to per-font storage (copy-on-write)."""
        self._userdata_set(_encode(board))

    def set_global_default(self, board: dict) -> None:
        """Overwrite the global default board."""
        self._defaults_set(_encode(board))
