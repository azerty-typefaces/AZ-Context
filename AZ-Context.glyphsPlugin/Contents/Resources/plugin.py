"""AZ-Context.glyphsPlugin — entry-point GeneralPlugin.

Imports the reporter and the palette so Glyphs' loader finds the classes listed
in Info.plist's Principal Classes, and registers the settings pane.
"""

from __future__ import annotations

import os
import sys
import traceback

import objc
from GlyphsApp.plugins import GeneralPlugin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context_reporter  # noqa: E402, F401  defines AZContextReporter

try:
    import context_palette  # noqa: F401  defines AZContextPalette
except Exception:
    traceback.print_exc()

    # A listed principal class that fails to load drops the WHOLE bundle,
    # reporter included, so stand in a do-nothing class under the same name.
    class AZContextPalette(GeneralPlugin):  # noqa: N801
        @objc.python_method
        def settings(self) -> None:
            return  # no name -> no menu item; no view -> never shown


class AZContext(GeneralPlugin):
    """Lifecycle plugin: registers the settings pane."""

    @objc.python_method
    def settings(self) -> None:
        self.name = "AZ Context"

    @objc.python_method
    def start(self) -> None:
        try:
            import context_settings

            context_settings.register_preferences_pane()
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def __file__(self) -> str:
        return __file__
