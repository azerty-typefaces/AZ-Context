"""Override ``GSGlyphEditView`` event methods so AZ Context can act on clicks.

Why this exists: the reporter protocol gives no left-click hook at all.
The only place to intercept is the edit view itself. We override the selector
once via a PyObjC category, capturing each original IMP first so every event we
don't handle — and any error in our path — falls through to the original.
Editing is never broken.

- ``mouseDown:`` — a double-click on a drawn column opens that glyph / master.
  (A right-click sibling picker lived here too; removed by Hugo 2026-10-11.)
"""

from __future__ import annotations

import traceback
from typing import Callable, Optional

import objc

_INSTALLED = False
_MOUSE_HANDLER: Optional[Callable[[object, object], bool]] = None
_ORIG_MOUSE_DOWN = None  # captured original IMP, called for events we ignore


def set_mouse_handler(handler: Callable[[object, object], bool]) -> None:
    """Register the left-click handler. It receives ``(view, event)`` and returns
    True to swallow the click (it acted on a column)."""
    global _MOUSE_HANDLER
    _MOUSE_HANDLER = handler


def is_installed() -> bool:
    return _INSTALLED


def install(view: object = None) -> bool:
    """Override the edit-view selector. Idempotent; returns True once in place.
    ``view`` is unused (kept for call-site symmetry)."""
    global _INSTALLED, _ORIG_MOUSE_DOWN
    if _INSTALLED:
        return True
    try:
        edit_view_class = objc.lookUpClass("GSGlyphEditView")
    except Exception:
        traceback.print_exc()
        return False
    try:
        _ORIG_MOUSE_DOWN = edit_view_class.instanceMethodForSelector_("mouseDown:")

        # objc.Category requires the category class to be named like the base class.
        class GSGlyphEditView(objc.Category(edit_view_class)):
            def mouseDown_(self, event):  # noqa: N802  (ObjC selector)
                handler = _MOUSE_HANDLER
                if handler is not None:
                    try:
                        if handler(self, event):
                            return  # swallow — the column click was handled
                    except Exception:
                        traceback.print_exc()
                try:
                    _ORIG_MOUSE_DOWN(self, event)
                except Exception:
                    traceback.print_exc()

        _INSTALLED = True
        return True
    except Exception:
        traceback.print_exc()
        return False
