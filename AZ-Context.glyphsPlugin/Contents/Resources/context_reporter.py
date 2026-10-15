"""AZ Context ReporterPlugin — sibling board on the EditView.

Draws a single row of curated sibling glyphs beside the live edit glyph, in the
current master. This file holds the Glyphs/AppKit bridge; the board model and
persistence live in ``context_board``.
"""

from __future__ import annotations

import os
import sys
import traceback

import objc
from GlyphsApp import Glyphs
from GlyphsApp.plugins import ReporterPlugin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AppKit import (  # noqa: E402
    NSAffineTransform,
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSInsetRect,
    NSIntersectsRect,
    NSMakePoint,
)

from Quartz import CGContextGetClipBoundingBox  # noqa: E402

import context_board  # noqa: E402

try:
    import context_smart_ruler_bridge as _smart_ruler_bridge  # noqa: E402
except Exception:
    _smart_ruler_bridge = None

GAP = 60.0
# Solid silhouette fill used while the Hand tool is active (preview-like).
PREVIEW_FILL_RGBA = (0.0, 0.0, 0.0, 1.0)
# On-screen points; every draw divides by the view scale so strokes stay a fixed
# on-screen weight (a 1u glyph-space stroke fattens as you zoom in).
STROKE_WIDTH = 1.0
# Hide the Variants metric-tick labels when the em displays smaller than this
# (upm * view scale), where fixed-size labels would crowd the shrunken row.
LABEL_MIN_EM_PT = 300.0


ENABLED_KEY = "com.hugojourdan.AZContext.enabled"


class AZContextReporter(ReporterPlugin):
    """Draws a glyphs x masters comparison board around a single edit glyph."""

    @objc.python_method
    def settings(self) -> None:
        self.menuName = "AZ Context"
        self._measure_pairs = []
        self._row_x_spans = []
        self._measure_layer_cache = {}  # (glyph name, master id) -> (change token, layer)

    @objc.python_method
    def _enabled(self) -> bool:
        value = Glyphs.defaults[ENABLED_KEY]
        return True if value is None else bool(value)

    @objc.python_method
    def _layer_for(self, master: object, glyph_name: str) -> object:
        if master is None:
            return None
        glyph = master.font.glyphs[glyph_name]
        if glyph is None:
            return None
        return glyph.layers[master.id]

    @objc.python_method
    def _sibling_change_token(self, master: object, glyph_name: str) -> object:
        """Cheap "has this sibling changed" token: the glyph's lastChange date
        plus its layer components' base-glyph dates (editing the active glyph
        must refresh composites built on it). No geometry walk."""
        try:
            font = master.font
            glyph = font.glyphs[glyph_name]
            if glyph is None:
                return None
            token = [str(glyph.lastChange)]
            # ponytail: one component level; nested composites re-cache only
            # when their direct base changes — deepen if that ever bites.
            layer = glyph.layers[master.id]
            for component in layer.components:
                base = font.glyphs[component.componentName]
                if base is not None:
                    token.append(str(base.lastChange))
            return tuple(token)
        except Exception:
            return None

    @objc.python_method
    def _measure_layer(self, master: object, glyph_name: str) -> object:
        """The sibling's layer, decomposed when it has components — CACHED.

        Smart Ruler's pipeline and the node markers read ``layer.paths``, which
        is empty for a pure-component (composite) glyph — so without the
        decompose the overlay silently shows nothing on composites. Doing that
        copy per redraw is what made measurements slow: siblings only change
        when actually edited, so the result is cached keyed on the glyphs'
        lastChange dates and rebuilt only then."""
        key = (glyph_name, str(master.id))
        token = self._sibling_change_token(master, glyph_name)
        cached = self._measure_layer_cache.get(key)
        if cached is not None and token is not None and cached[0] == token:
            return cached[1]
        layer = self._layer_for(master, glyph_name)
        if layer is None:
            return None
        result = layer
        try:
            if len(layer.components) > 0:
                decomposed = layer.copyDecomposedLayer()
                if decomposed is not None:
                    # copyDecomposedLayer detaches the copy; the Smart Ruler
                    # pipeline needs the parent (glyph case) and associated
                    # master (stem dimensions) or it filters every measurement
                    # away — restore both so composites measure like outlines.
                    try:
                        decomposed.setParent_(layer.parent)
                        decomposed.setAssociatedMasterId_(layer.associatedMasterId)
                    except Exception:
                        traceback.print_exc()
                    result = decomposed
        except Exception:
            traceback.print_exc()
        if len(self._measure_layer_cache) > 256:  # ponytail: crude cap, fonts switch rarely
            self._measure_layer_cache.clear()
        self._measure_layer_cache[key] = (token, result)
        return result

    @objc.python_method
    def _advance(self, master: object, glyph_name: str, fallback: float) -> float:
        layer = self._layer_for(master, glyph_name)
        if layer is None:
            return fallback
        try:
            return float(layer.width)
        except Exception:
            return fallback

    @objc.python_method
    def _bezier(self, master: object, glyph_name: str) -> object:
        layer = self._layer_for(master, glyph_name)
        if layer is None:
            return None
        try:
            return layer.completeBezierPath
        except Exception:
            return None

    @objc.python_method
    def _translated_path(self, bezier: object, x: float, y: float) -> object:
        """Return a copy of ``bezier`` translated to (x, y), or None."""
        try:
            path = bezier.copy()
        except Exception:
            return None
        transform = NSAffineTransform.transform()
        transform.translateXBy_yBy_(float(x), float(y))
        try:
            path.transformUsingAffineTransform_(transform)
        except Exception:
            return None
        return path

    @objc.python_method
    def _view_scale(self) -> float:
        """Edit-view scale (screen px per glyph unit), never zero/negative.

        NOT named ``_scale``: GSReporter exposes a ``scale`` property whose ObjC
        ivar ``_scale`` shadows a Python method of that name, so ``self._scale``
        resolves to a float and ``self._scale()`` raises 'not callable'."""
        try:
            scale = float(self.getScale())
        except Exception:
            return 1.0
        return scale if scale > 0.0 else 1.0

    @objc.python_method
    def _fill_stroke(self, path: object, fill: object, stroke: object, scale: float) -> None:
        line_width = STROKE_WIDTH / scale  # fixed on-screen weight at any zoom
        if not self._may_paint(path, line_width):
            return
        fill.set()
        path.fill()
        stroke.set()
        path.setLineWidth_(line_width)
        path.stroke()

    @objc.python_method
    def _may_paint(self, path: object, pad: float) -> bool:
        """False when ``path`` lies wholly outside the current clip, so painting
        it changes no pixel. Not just a saving: CoreGraphics took 10-68 ms per
        frame to fill ONE Variants outline sitting in an off-screen band left of
        the view (Glyphs 4 4106, measured 2026-10-11), while the same outline on
        screen or further off filled in microseconds."""
        try:
            clip = CGContextGetClipBoundingBox(NSGraphicsContext.currentContext().CGContext())
            return NSIntersectsRect(NSInsetRect(path.bounds(), -pad, -pad), clip)
        except Exception:
            return True

    @objc.python_method
    def background(self, layer: object) -> None:
        try:
            self._draw(layer)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def inactiveLayerBackground(self, layer: object) -> None:
        """Caret AFTER the single glyph: Glyphs 4 then has NO active layer at
        all (view.activeLayer() is None), draws the glyph as an INACTIVE layer
        and never calls background() — the board vanished on every trailing
        caret. Route that one state through the normal draw; any other
        inactive layer (multi-glyph tab) bails in context_board.single_glyph, and the
        preview panel never reaches this hook (self.controller is None there)."""
        if self._no_active_layer():
            self.background(layer)

    @objc.python_method
    def _no_active_layer(self) -> bool:
        try:
            view = self.controller.graphicView()
            return view is not None and view.activeLayer() is None
        except Exception:
            return False

    @objc.python_method
    def _draw(self, layer: object) -> None:
        # Clear BEFORE the early-outs: a frame that draws no board must leave
        # no stale guide spans or measurements behind.
        self._measure_pairs = []
        self._row_x_spans = []
        if not self._enabled():
            return
        glyph = layer.parent
        if glyph is None:
            return
        font = glyph.parent
        if font is None:
            return
        try:
            tab = font.currentTab
            if tab is None or context_board.single_glyph(tab) is None:
                return
        except Exception:
            return

        board = context_board.load()
        features = context_board.tab_features(tab)
        solid = self._hand_tool_active()
        mode = board.get("mode")
        if mode == "axis":
            self._draw_axis_mode(font, glyph, board, solid)
        else:
            self._draw_glyph_mode(font, glyph, board, solid, features)
        # With the Hand tool the board is a clean preview, so drop the editing
        # overlays (metric guides, measurements).
        if not solid:
            # ponytail: guides draw over the silhouettes (spans filled during the
            # sibling draw above); faint 0.3-alpha lines, so the overlap is hidden.
            self._render_guides(font)
        if not solid:
            self._render_measurements(font)

    @objc.python_method
    def _hand_tool_active(self) -> bool:
        """True when Glyphs' Hand (pan) tool is the active tool."""
        try:
            view = self.controller.graphicView()
            window = view.window() if view is not None else None
            controller = window.windowController() if window is not None else None
            tool = controller.toolDrawDelegate() if controller is not None else None
            if tool is None:
                return False
            return "Hand" in str(tool.className())
        except Exception:
            return False

    @objc.python_method
    def _pref_color(self, key: str) -> object:
        """Build an NSColor from the (device-RGB) color stored under ``key``."""
        return NSColor.colorWithDeviceRed_green_blue_alpha_(*context_board.color(key))

    @objc.python_method
    def _sibling_colors(self, solid: bool) -> tuple[object, object]:
        """Return (fill, stroke) for sibling silhouettes. Solid (Hand tool) =
        opaque black (preview-like); otherwise the user's configured faint
        fill + stroke (Glyphs > Settings > AZ Context)."""
        if solid:
            fill = NSColor.colorWithCalibratedRed_green_blue_alpha_(*PREVIEW_FILL_RGBA)
            return fill, fill
        fill = self._pref_color(context_board.FILL_COLOR_KEY)
        stroke = self._pref_color(context_board.STROKE_COLOR_KEY)
        return fill, stroke

    @objc.python_method
    def _show_guides(self) -> bool:
        return bool(Glyphs.defaults[context_board.SHOW_GUIDES_KEY])

    @objc.python_method
    def _labels_fit_zoom(self, upm: float) -> bool:
        """False when zoomed out below LABEL_MIN_EM_PT (the em's on-screen point
        size == upm * view scale), where fixed-size labels would crowd siblings."""
        try:
            return upm * float(self.getScale()) >= LABEL_MIN_EM_PT
        except Exception:
            return True

    @objc.python_method
    def _label_font(self, size: float) -> object:
        """Monospaced label font, with fallbacks for older macOS / API gaps."""
        try:
            return NSFont.monospacedSystemFontOfSize_weight_(size, 0.0)
        except Exception:
            return NSFont.userFixedPitchFontOfSize_(size) or NSFont.labelFontOfSize_(size)

    @objc.python_method
    def _render_guides(self, font: object) -> None:
        """Current master's metric lines (baseline, x-height, cap, asc, desc)
        spanning the sibling row, so cross-sibling vertical alignment is visible.
        Glyphs already draws these full-width for the active master; the value
        here is the explicit row span (and a reference grid in Variants mode)."""
        if not self._show_guides() or not self._row_x_spans:
            return
        master = font.selectedFontMaster
        if master is None:
            return
        x0 = min(x for x, _w in self._row_x_spans)
        x1 = max(x + w for x, w in self._row_x_spans)
        metrics = [0.0]
        for attr in ("xHeight", "capHeight", "ascender", "descender"):
            try:
                metrics.append(float(getattr(master, attr)))
            except Exception:
                continue
        color = self._pref_color(context_board.STROKE_COLOR_KEY).colorWithAlphaComponent_(0.3)
        color.set()
        line_width = STROKE_WIDTH / self._view_scale()  # fixed on-screen weight at any zoom
        for y in metrics:
            line = NSBezierPath.bezierPath()
            line.moveToPoint_(NSMakePoint(x0, y))
            line.lineToPoint_(NSMakePoint(x1, y))
            line.setLineWidth_(line_width)
            line.stroke()

    @objc.python_method
    def _show_measurements(self) -> bool:
        return bool(Glyphs.defaults[context_board.SHOW_MEASUREMENTS_KEY])

    @objc.python_method
    def _draw_glyph_mode(
        self,
        font: object,
        glyph: object,
        board: dict,
        solid: bool,
        features: tuple[str, ...] = (),
    ) -> None:
        """The sibling strings typed in the palette, left and right of the live
        glyph, in the current master, with the tab's active OpenType features
        applied (ss01 on: a -> a.ss01). A typed copy of the edited glyph is
        drawn too (HH|H|HH is a spacing check)."""
        active_name = glyph.name
        columns, active_index = context_board.sibling_row(font, active_name, board, features)
        current_master = font.selectedFontMaster
        upm = float(font.upm or 1000)

        # x positions in glyph units, anchored so the active glyph sits at x=0.
        x_positions: list[float] = [0.0] * len(columns)
        active_width = self._advance(current_master, active_name, upm * 0.5)
        self._row_x_spans.append((0.0, active_width))
        cursor = active_width + GAP
        for i in range(active_index + 1, len(columns)):
            x_positions[i] = cursor
            cursor += self._advance(current_master, columns[i], upm * 0.5) + GAP
        cursor = -GAP
        for i in range(active_index - 1, -1, -1):
            cursor -= self._advance(current_master, columns[i], upm * 0.5)
            x_positions[i] = cursor
            cursor -= GAP

        fill, stroke = self._sibling_colors(solid)
        scale = self._view_scale()

        for i, glyph_name in enumerate(columns):
            if i == active_index:
                continue  # the live glyph; Glyphs draws it
            bezier = self._bezier(current_master, glyph_name)
            if bezier is None:
                continue
            path = self._translated_path(bezier, x_positions[i], 0.0)
            if path is None:
                continue
            self._fill_stroke(path, fill, stroke, scale)
            advance = self._advance(current_master, glyph_name, upm * 0.5)
            self._row_x_spans.append((x_positions[i], advance))
            measure_layer = self._measure_layer(current_master, glyph_name)
            self._measure_pairs.append((measure_layer, x_positions[i]))

    @objc.python_method
    def _draw_axis_mode(self, font: object, glyph: object, board: dict, solid: bool) -> None:
        """The active glyph across the other masters on the chosen axis: lower
        axis value to the left, higher to the right (display-only)."""
        ordered, current_index, axis = context_board.axis_master_order(font, board)
        if axis is None or len(ordered) < 2:
            return
        active_name = glyph.name
        master_by_name = {master.name: master for master in font.masters}
        current_master = font.selectedFontMaster
        upm = float(font.upm or 1000)
        fill, stroke = self._sibling_colors(solid)
        active_width = self._advance(current_master, active_name, upm * 0.5)
        self._row_x_spans.append((0.0, active_width))
        scale = self._view_scale()
        # Per-variant metric ticks: (master, x, width) of every drawn column.
        tick_entries: list[tuple] = []

        # Every master on the axis line, uncapped. The Siblings per-side count
        # used to cap this too, and at "2" a three-master width line showed one
        # neighbour and silently dropped Extended (Hugo, 2026-10-10).
        left_indices: list[int] = []
        if axis in context_board.SLANT_AXIS_TAGS:
            # Slant/italic: always show the upright<->italic counterpart(s) on
            # the right, never the left, whichever master is current.
            right_indices = [i for i in range(len(ordered)) if i != current_index]
        else:
            right_indices = list(range(current_index + 1, len(ordered)))
            left_indices = list(range(current_index - 1, -1, -1))

        # Higher axis value (heavier / wider / italic) to the right.
        cursor = active_width + GAP
        for index in right_indices:
            master = master_by_name.get(ordered[index])
            width = self._advance(master, active_name, upm * 0.5)
            bezier = self._bezier(master, active_name)
            if bezier is not None:
                path = self._translated_path(bezier, cursor, 0.0)
                if path is not None:
                    self._fill_stroke(path, fill, stroke, scale)
                    self._row_x_spans.append((cursor, width))
                    measure_layer = self._measure_layer(master, active_name)
                    self._measure_pairs.append((measure_layer, cursor))
                    tick_entries.append((master, cursor, width))
            cursor += width + GAP

        # Lower axis value (lighter / narrower) to the left.
        cursor = -GAP
        for index in left_indices:
            master = master_by_name.get(ordered[index])
            width = self._advance(master, active_name, upm * 0.5)
            cursor -= width
            bezier = self._bezier(master, active_name)
            if bezier is not None:
                path = self._translated_path(bezier, cursor, 0.0)
                if path is not None:
                    self._fill_stroke(path, fill, stroke, scale)
                    self._row_x_spans.append((cursor, width))
                    measure_layer = self._measure_layer(master, active_name)
                    self._measure_pairs.append((measure_layer, cursor))
                    tick_entries.append((master, cursor, width))
            cursor -= GAP

        if not solid and self._show_guides():
            self._render_variant_ticks(current_master, tick_entries, scale, upm)

    @objc.python_method
    def _render_variant_ticks(
        self,
        current_master: object,
        entries: list,
        scale: float,
        upm: float,
    ) -> None:
        """Short per-column tick + value at each variant master's OWN x-height /
        cap height — only where it differs from the current master's, since the
        row-wide guides already carry the current metrics. A tick therefore
        means vertical-metric drift on that master."""
        if not entries:
            return
        base_color = self._pref_color(context_board.STROKE_COLOR_KEY)
        line_color = base_color.colorWithAlphaComponent_(0.5)
        show_labels = self._labels_fit_zoom(upm)
        attributes = {
            NSFontAttributeName: self._label_font(9.0 / scale),
            NSForegroundColorAttributeName: base_color.colorWithAlphaComponent_(0.9),
        }
        for master, x, width in entries:
            for prefix, attr in (("x", "xHeight"), ("cap", "capHeight")):
                try:
                    value = float(getattr(master, attr))
                    if value == float(getattr(current_master, attr)):
                        continue
                except Exception:
                    continue
                line_color.set()
                tick = NSBezierPath.bezierPath()
                tick.moveToPoint_(NSMakePoint(x, value))
                tick.lineToPoint_(NSMakePoint(x + width, value))
                tick.setLineWidth_(STROKE_WIDTH / scale)  # fixed on-screen weight at any zoom
                tick.stroke()
                if show_labels:
                    try:
                        label = NSAttributedString.alloc().initWithString_attributes_(
                            f"{prefix} {value:g}", attributes
                        )
                        label.drawAtPoint_(NSMakePoint(x, value + 3.0 / scale))
                    except Exception:
                        traceback.print_exc()

    @objc.python_method
    def _smart_ruler_active(self) -> bool:
        """True when AZ-SmartRuler is installed AND switched on (its reporter is
        in the user's visible reporters)."""
        if _smart_ruler_bridge is None:
            return False
        try:
            # Live list, not the ``visibleReporters`` default (stale on G4 4106).
            visible = [type(reporter).__name__ for reporter in Glyphs.activeReporters]
        except Exception:
            visible = None
        if not _smart_ruler_bridge.smart_ruler_enabled(visible):
            return False
        return _smart_ruler_bridge.SmartRulerBridge.shared().is_available()

    @objc.python_method
    def _visible_glyph_span(self, font: object) -> tuple | None:
        """(min_x, max_x) of the viewport in glyph units (active-layer space),
        or None when it can't be derived. Lets the expensive sibling overlays
        (measurement blits, node markers) skip columns fully offscreen —
        zoomed in on the active glyph, that's most of them."""
        try:
            view = font.currentTab.graphicView()
            rect = view.visibleRect()
            active = view.activePosition()
            scale = float(view.scale()) or 1.0
            pad = 200.0 / scale  # labels overhang the advance a little
            min_x = (rect.origin.x - active.x) / scale - pad
            max_x = (rect.origin.x + rect.size.width - active.x) / scale + pad
            return (min_x, max_x)
        except Exception:
            return None

    @objc.python_method
    def _span_offscreen(self, span: tuple | None, x_offset: float, layer: object) -> bool:
        """True when ``layer`` drawn at ``x_offset`` lies fully outside ``span``."""
        if span is None:
            return False
        try:
            width = float(layer.width)
        except Exception:
            width = 0.0
        return x_offset + width < span[0] or x_offset > span[1]

    @objc.python_method
    def _render_measurements(self, font: object) -> None:
        """Paint Smart Ruler's measurement overlay on each sibling layer, at its
        on-canvas x offset — only while Smart Ruler is installed and enabled."""
        if (
            not self._measure_pairs
            or not self._show_measurements()
            or not self._smart_ruler_active()
        ):
            return
        try:
            scale = float(font.currentTab.scale)
        except Exception:
            scale = 1.0
        if scale <= 0.0:
            scale = 1.0
        canvas_color = None
        try:
            graphic_view = font.currentTab.graphicView()
            if graphic_view is not None:
                canvas_color = graphic_view.canvasColor()
        except Exception:
            canvas_color = None
        bridge = _smart_ruler_bridge.SmartRulerBridge.shared()
        span = self._visible_glyph_span(font)
        for layer, x_offset in self._measure_pairs:
            if layer is None or self._span_offscreen(span, x_offset, layer):
                continue
            bridge.draw_measurements(layer, float(x_offset), scale, canvas_color=canvas_color)

    # ------------------------------------------------------------------ #
    # ObjC action selectors (NO @objc.python_method)                     #
    # ------------------------------------------------------------------ #

    def toggleEnabled_(self, sender: object) -> None:
        try:
            Glyphs.defaults[ENABLED_KEY] = not self._enabled()
            context_board.repaint()
        except Exception:
            traceback.print_exc()
