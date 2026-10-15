"""AZ-Context.glyphsPlugin — entry-point GeneralPlugin.

Registers nothing user-facing in v1 beyond importing the reporter (so the
ReporterPlugin subclass is defined for Glyphs's loader) and registering the
settings pane. All load steps append to
``~/Library/Logs/AZ-Context.log`` because a GeneralPlugin's stdout does not
reach the Macro panel.
"""

from __future__ import annotations

import os
import sys
import traceback

LOG_PATH = os.path.expanduser("~/Library/Logs/AZ-Context.log")


def _log(message: str) -> None:
    """Append a message to the bundle's log file."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except Exception:
        pass


_log("=" * 60)
_log(f"[AZ Context] plugin.py module-load: pid={os.getpid()}")

try:
    import objc
    from GlyphsApp.plugins import GeneralPlugin

    _log("[AZ Context] imports OK")
except Exception:
    _log("[AZ Context] IMPORT FAILED:")
    _log(traceback.format_exc())
    raise

# Importing the reporter defines AZContextReporter(ReporterPlugin) so Glyphs's
# loader can resolve it from the Info.plist Principal Classes list.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import context_reporter as _reporter  # noqa: F401

    _log("[AZ Context] reporter import OK")
except Exception:
    _log("[AZ Context] REPORTER IMPORT FAILED:")
    _log(traceback.format_exc())
    raise

try:
    import context_palette as _palette  # noqa: F401  defines AZContextPalette (Principal Classes)

    _log("[AZ Context] palette import OK")
except Exception:
    _log("[AZ Context] PALETTE IMPORT FAILED:")
    _log(traceback.format_exc())

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
            _log("[AZ Context] settings pane registered")
        except Exception:
            _log("[AZ Context] settings pane FAILED:")
            _log(traceback.format_exc())

    @objc.python_method
    def __file__(self) -> str:
        return __file__
