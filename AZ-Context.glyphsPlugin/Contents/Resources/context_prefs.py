"""Display preferences for AZ Context (pure, unit-testable).

Holds the ``Glyphs.defaults`` key names, the built-in default values, and the
encode/decode helpers. Colors are persisted as a JSON ``"[r, g, b, a]"`` string
(NSUserDefaults is a property-list store; a JSON string is always plist-safe and
round-trips through ``json.loads`` as a real list — same approach as the board
store). The reporter reads these every frame; the Settings pane writes them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

FILL_COLOR_KEY = "com.hugojourdan.AZContext.fillColor"
STROKE_COLOR_KEY = "com.hugojourdan.AZContext.strokeColor"
SHOW_MEASUREMENTS_KEY = "com.hugojourdan.AZContext.showMeasurements"
SHOW_GUIDES_KEY = "com.hugojourdan.AZContext.showGuides"

# Defaults match the reporter's original hard-coded silhouette colors.
DEFAULT_FILL_RGBA: tuple[float, float, float, float] = (0.55, 0.35, 0.85, 0.14)
DEFAULT_STROKE_RGBA: tuple[float, float, float, float] = (0.42, 0.22, 0.70, 0.55)
DEFAULT_SHOW_MEASUREMENTS = True
DEFAULT_SHOW_GUIDES = False  # Hugo 2026-10-10: no metric lines by default; switch stays in Settings

RGBA = tuple[float, float, float, float]


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def encode_color(rgba: Iterable[float]) -> str:
    """Serialize an (r, g, b, a) sequence to a clamped JSON string."""
    seq = list(rgba) + [1.0]
    values = [_clamp(float(component)) for component in seq[:4]]
    return json.dumps(values)


def decode_color(raw: object, fallback: RGBA) -> RGBA:
    """Parse a stored color (JSON string or list) into a clamped RGBA tuple.

    Returns ``fallback`` for None / malformed / wrong-length values."""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return fallback
    try:
        values = [float(component) for component in cast("list[Any]", data)]
    except (TypeError, ValueError):
        return fallback
    if len(values) != 4:
        return fallback
    return (_clamp(values[0]), _clamp(values[1]), _clamp(values[2]), _clamp(values[3]))


def decode_bool(raw: object, default: bool) -> bool:
    """Parse a stored flag; None (key never set) yields ``default``."""
    if raw is None:
        return default
    return bool(raw)
