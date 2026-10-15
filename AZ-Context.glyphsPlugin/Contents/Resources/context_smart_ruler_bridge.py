"""AZ-SmartRuler (Caliper) PyObjC bridge.

Reuses the compiled AZ-SmartRuler reporter's measurement pipeline
(``AZMeasurementPipeline`` + ``AZDrawing``) so AZ Context can paint the same
stem / terminal / intersection annotations on its sibling silhouettes that
Smart Ruler paints on the active layer. Both plugins live in the same Glyphs
process, so the ObjC classes are reachable via ``objc.lookUpClass`` once Smart
Ruler is loaded.

This is a *render* bridge, not a data API: ``pipelineForLayer:contentHash:``
returns an opaque cached-pipeline object that is only ever handed straight to
``AZDrawing.drawCachedPipeline:layer:scale:``. The measurement numbers are
never read out — Smart Ruler draws them itself.

Best-effort: when Smart Ruler is absent (classes missing) every call is a
no-op and the rest of the sibling overlay keeps working.
"""

from __future__ import annotations

import traceback
from collections import OrderedDict
from typing import Optional

try:
    import objc
except Exception:  # pragma: no cover - objc absent in headless tests
    objc = None

try:
    from AppKit import (
        NSAffineTransform,
        NSCompositingOperationSourceOver,
        NSGraphicsContext,
        NSImage,
        NSMakeRect,
        NSMakeSize,
        NSScreen,
        NSZeroRect,
    )
except Exception:  # pragma: no cover - AppKit absent in headless tests
    NSAffineTransform = None
    NSCompositingOperationSourceOver = None
    NSGraphicsContext = None
    NSImage = None
    NSMakeRect = None
    NSMakeSize = None
    NSScreen = None
    NSZeroRect = None

try:
    from Foundation import NSNotificationCenter, NSUserDefaults
except Exception:  # pragma: no cover - Foundation absent in headless tests
    NSNotificationCenter = None
    NSUserDefaults = None

# Smart Ruler's reporter class; its name appears in ``visibleReporters`` when
# the user has the reporter switched on in Glyphs' View menu.
SMART_RULER_REPORTER_CLASS = "AZSmartRulerReporter"
_PIPELINE_CLASS = "AZMeasurementPipeline"
_DRAWING_CLASS = "AZDrawing"
_HEATMAP_CLASS = "AZHeatmapRenderer"
# Smart Ruler's heatmap settings, read straight from NSUserDefaults so the
# sibling overlay mirrors whatever render mode Smart Ruler itself is in.
_HEATMAP_SHOW_KEY = "com.hugojourdan.AZSmartRuler.show.heatmap"
_HEATMAP_OPACITY_KEY = "com.hugojourdan.AZSmartRuler.heatmapOpacity"
_HEATMAP_MIN_KEY = "com.hugojourdan.AZSmartRuler.heatmap.minThickness"
_HEATMAP_MAX_KEY = "com.hugojourdan.AZSmartRuler.heatmap.maxThickness"
# Legend on (default YES in Smart Ruler) makes Smart Ruler halve the heatmap
# fill alpha on the active layer; mirror that here so siblings match the focus.
_HEATMAP_LEGEND_KEY = "com.hugojourdan.AZSmartRuler.heatmap.showLegend"
# Palette + quality are baked into the AZCachedHeatmap bitmap by the renderer
# (read from these defaults at compute time), so they must be part of the cache
# key — otherwise a palette/quality change leaves siblings on a stale bitmap.
_HEATMAP_PALETTE_KEY = "com.hugojourdan.AZSmartRuler.heatmapPalette"
_HEATMAP_QUALITY_KEY = "com.hugojourdan.AZSmartRuler.heatmapQuality"
_HEATMAP_OPACITY_DEFAULT = 0.8
# Cap the per-layer caches (keyed by content hash); mirrors Classifier.
_PIPELINE_CACHE_CAP = 200
_HEATMAP_CACHE_CAP = 200
# Label overlays are re-rendered per frame by AZDrawing (text layout per label ×
# every sibling — the sibling-measurement slowdown), so cache them as offscreen
# bitmaps and blit. Bitmaps are big; keep few, and past _MAX_IMAGE_PIXELS
# (extreme zoom) draw direct rather than hold a giant backing store.
_LABEL_IMAGE_CACHE_CAP = 12
_MAX_IMAGE_PIXELS = 8_000_000
# Every AZSettings setter in Smart Ruler posts this; the label bitmaps bake in
# what AZDrawing reads at draw time (show diffs, colours, font size, opacity,
# dashed lines — none of it in the content hash), so they must go on it.
_SETTINGS_CHANGED_NOTE = "AZSettingsDidChangeNotification"


def smart_ruler_enabled(visible_reporters: object) -> bool:
    """True when Smart Ruler's reporter is in the user's visible reporters.

    ``visible_reporters`` is the raw ``Glyphs.defaults["visibleReporters"]``
    value (an NSArray / list of class-name strings, or None).
    """
    try:
        names = list(visible_reporters or [])
    except Exception:
        return False
    return SMART_RULER_REPORTER_CLASS in names


class SmartRulerBridge:
    """Lazily-resolved bridge to Smart Ruler's measurement pipeline."""

    _shared: "Optional[SmartRulerBridge]" = None

    @classmethod
    def shared(cls) -> "SmartRulerBridge":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def __init__(self) -> None:
        self._pipeline_cls: object = None
        self._drawing_cls: object = None
        self._checked = False
        self._cache: "OrderedDict[object, object]" = OrderedDict()
        self._heatmap_cls: object = None
        self._heatmap_checked = False
        self._heatmap_cache: "OrderedDict[object, object]" = OrderedDict()
        # (content hash, scale, canvas color) -> (NSImage, origin, size in units)
        self._label_image_cache: "OrderedDict[object, object]" = OrderedDict()
        # Keys observed on an earlier frame: an image is only worth rendering
        # for a key that repeats (during a zoom every frame has a fresh scale,
        # so building bitmaps there would just churn memory for zero reuse).
        self._label_image_seen: "OrderedDict[object, bool]" = OrderedDict()
        self._settings_observer: object = None
        self._install_settings_observer()

    def _install_settings_observer(self) -> None:
        """Drop the cached label bitmaps whenever Smart Ruler's settings change.

        Without this a toggle (show diffs, a colour, the font size…) left the
        ghosts on a stale image until their geometry changed (Hugo 2026-10-11)."""
        if NSNotificationCenter is None or self._settings_observer is not None:
            return
        try:
            self._settings_observer = (
                NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                    _SETTINGS_CHANGED_NOTE, None, None, self._smart_ruler_settings_changed
                )
            )
        except Exception:
            self._settings_observer = None

    def _smart_ruler_settings_changed(self, _note: object) -> None:
        self._label_image_cache.clear()
        self._label_image_seen.clear()

    def _ensure_lookup(self) -> bool:
        """Resolve the ObjC classes once. True when both are present."""
        if self._checked:
            return self._pipeline_cls is not None and self._drawing_cls is not None
        self._checked = True
        if objc is not None:
            try:
                self._pipeline_cls = objc.lookUpClass(_PIPELINE_CLASS)
            except Exception:
                self._pipeline_cls = None
            try:
                self._drawing_cls = objc.lookUpClass(_DRAWING_CLASS)
            except Exception:
                self._drawing_cls = None
        return self._pipeline_cls is not None and self._drawing_cls is not None

    def is_available(self) -> bool:
        """True when Smart Ruler's measurement classes are loaded."""
        return self._ensure_lookup()

    def _content_hash(self, layer: object) -> object:
        try:
            return self._pipeline_cls.contentHashForLayer_(layer)
        except Exception:
            traceback.print_exc()
            return None

    def _pipeline_for_layer(self, layer: object, content_hash: object) -> object:
        """Return the (LRU-cached) opaque pipeline for ``layer``, or None."""
        if content_hash in self._cache:
            pipeline = self._cache.pop(content_hash)
            self._cache[content_hash] = pipeline  # refresh LRU position
            return pipeline
        try:
            pipeline = self._pipeline_cls.pipelineForLayer_contentHash_(layer, content_hash)
        except Exception:
            traceback.print_exc()
            return None
        if pipeline is None:
            return None
        self._cache[content_hash] = pipeline
        if len(self._cache) > _PIPELINE_CACHE_CAP:
            self._cache.popitem(last=False)  # evict oldest
        return pipeline

    def draw_measurements(
        self,
        layer: object,
        x_offset: float,
        scale: float,
        canvas_color: object = None,
    ) -> None:
        """Paint Smart Ruler's overlay on ``layer``, translated by ``x_offset``
        em units along x — mirroring Smart Ruler's current render mode: a
        stem-thickness HEATMAP when Smart Ruler's heatmap mode is on, otherwise
        the numeric measurement labels. ``scale`` is the EditView zoom factor
        (labels are sized in pixels, so it is mandatory). No-op when Smart Ruler
        is absent or the layer yields nothing."""
        if layer is None or scale is None or scale <= 0:
            return
        if NSAffineTransform is None or NSGraphicsContext is None:
            return
        # Heatmap mode mirrors Smart Ruler: the label renderer is skipped and the
        # gradient owns the silhouette (the content hash comes from the pipeline
        # class, so that lookup is still required).
        if self._heatmap_enabled() and self._ensure_heatmap() and self._ensure_lookup():
            self._draw_heatmap(layer, x_offset)
            return
        if not self._ensure_lookup():
            return
        content_hash = self._content_hash(layer)
        if content_hash is None:
            return
        pipeline = self._pipeline_for_layer(layer, content_hash)
        if pipeline is None:
            return
        if self._blit_label_image(pipeline, layer, x_offset, scale, canvas_color, content_hash):
            return
        try:
            drawing = self._drawing_cls.alloc().init()
            if canvas_color is not None:
                try:
                    drawing.setCanvasColor_(canvas_color)
                except Exception:
                    pass
            context = NSGraphicsContext.currentContext()
            context.saveGraphicsState()
            try:
                if x_offset != 0.0:
                    transform = NSAffineTransform.transform()
                    transform.translateXBy_yBy_(float(x_offset), 0.0)
                    transform.concat()
                drawing.drawCachedPipeline_layer_scale_(pipeline, layer, float(scale))
            finally:
                context.restoreGraphicsState()
        except Exception:
            traceback.print_exc()

    @staticmethod
    def _color_key(canvas_color: object) -> object:
        """Stable cache-key form of the canvas color (labels adapt to it)."""
        if canvas_color is None:
            return None
        try:
            return str(canvas_color.description())
        except Exception:
            return None

    def _blit_label_image(
        self,
        pipeline: object,
        layer: object,
        x_offset: float,
        scale: float,
        canvas_color: object,
        content_hash: object,
    ) -> bool:
        """Draw the label overlay from an offscreen bitmap; True when drawn.

        AZDrawing lays out and draws every label's text per call — cheap for
        Smart Ruler's single focus layer, a crawl across all siblings at 60 fps.
        The overlay is static while the view rests, so it is rendered once per
        (geometry, zoom, canvas color) and blitted. False = caller draws direct
        (first sight of a key, mid-zoom frames, or an over-budget bitmap)."""
        if NSImage is None:
            return False
        key = (content_hash, round(float(scale), 4), self._color_key(canvas_color))
        entry = self._label_image_cache.get(key)
        if entry is None:
            if key not in self._label_image_seen:
                self._label_image_seen[key] = True
                if len(self._label_image_seen) > _LABEL_IMAGE_CACHE_CAP * 4:
                    self._label_image_seen.popitem(last=False)
                return False
            entry = self._render_label_image(pipeline, layer, scale, canvas_color)
            if entry is None:
                return False
            self._label_image_cache[key] = entry
            if len(self._label_image_cache) > _LABEL_IMAGE_CACHE_CAP:
                self._label_image_cache.popitem(last=False)  # evict oldest
        else:
            self._label_image_cache.pop(key)
            self._label_image_cache[key] = entry  # refresh LRU position
        image, origin_x, origin_y, width_units, height_units = entry
        try:
            image.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(origin_x + float(x_offset), origin_y, width_units, height_units),
                NSZeroRect,
                NSCompositingOperationSourceOver,
                1.0,
            )
            return True
        except Exception:
            traceback.print_exc()
            return False

    @staticmethod
    def _max_backing_scale() -> float:
        """backingScaleFactor of the DEEPEST attached screen (1.0 when unknown).

        lockFocus sizes an offscreen image from the deepest screen, not the key
        or main one — with a Retina laptop next to a 1x external display set as
        main, mainScreen() would under-report the backing store by 2x."""
        if NSScreen is None:
            return 1.0
        try:
            scales = [float(screen.backingScaleFactor()) for screen in NSScreen.screens()]
            return max(scales) or 1.0
        except Exception:  # no screens attached, or AppKit unhappy
            return 1.0

    def _render_label_image(
        self,
        pipeline: object,
        layer: object,
        scale: float,
        canvas_color: object,
    ) -> object:
        """Render the label overlay into an NSImage sized 1 image point per
        screen point (crisp at the current zoom). Returns (image, origin_x,
        origin_y, width_units, height_units) in glyph units, or None."""
        try:
            bounds = layer.bounds
            width = float(bounds.size.width)
            height = float(bounds.size.height)
            if width <= 0.0 or height <= 0.0:
                return None
            # Labels sit on measurement chords and overhang the outline by up
            # to ~half a label box (screen-sized) — pad in glyph units.
            pad = 80.0 / float(scale) + 40.0
            origin_x = float(bounds.origin.x) - pad
            origin_y = float(bounds.origin.y) - pad
            width_units = width + 2.0 * pad
            height_units = height + 2.0 * pad
            pixel_width = width_units * float(scale)
            pixel_height = height_units * float(scale)
            # initWithSize_ is in POINTS and lockFocus builds the backing store at
            # the deepest attached screen's backingScaleFactor (2x as soon as any
            # display is Retina), so the real bitmap is backing² bigger than the
            # point area — budget in backing pixels or the cap lets through images
            # 4x the intended size.
            backing = self._max_backing_scale()
            if pixel_width * pixel_height * backing * backing > _MAX_IMAGE_PIXELS:
                return None
            image = NSImage.alloc().initWithSize_(NSMakeSize(pixel_width, pixel_height))
            image.lockFocus()
            try:
                transform = NSAffineTransform.transform()
                transform.scaleBy_(float(scale))
                transform.translateXBy_yBy_(-origin_x, -origin_y)
                transform.concat()
                drawing = self._drawing_cls.alloc().init()
                if canvas_color is not None:
                    try:
                        drawing.setCanvasColor_(canvas_color)
                    except Exception:
                        pass
                drawing.drawCachedPipeline_layer_scale_(pipeline, layer, float(scale))
            finally:
                image.unlockFocus()
            return (image, origin_x, origin_y, width_units, height_units)
        except Exception:
            traceback.print_exc()
            return None

    def _ensure_heatmap(self) -> bool:
        """Resolve AZHeatmapRenderer once. True when present (older Smart Ruler
        builds without the heatmap feature simply keep the label overlay)."""
        if self._heatmap_checked:
            return self._heatmap_cls is not None
        self._heatmap_checked = True
        if objc is not None:
            try:
                self._heatmap_cls = objc.lookUpClass(_HEATMAP_CLASS)
            except Exception:
                self._heatmap_cls = None
        return self._heatmap_cls is not None

    def _heatmap_enabled(self) -> bool:
        """True when Smart Ruler's heatmap render mode is switched on."""
        if NSUserDefaults is None:
            return False
        try:
            return bool(NSUserDefaults.standardUserDefaults().boolForKey_(_HEATMAP_SHOW_KEY))
        except Exception:
            return False

    def _heatmap_opacity(self) -> float:
        if NSUserDefaults is None:
            return _HEATMAP_OPACITY_DEFAULT
        try:
            defaults = NSUserDefaults.standardUserDefaults()
            if defaults.objectForKey_(_HEATMAP_OPACITY_KEY) is None:
                return _HEATMAP_OPACITY_DEFAULT
            return max(0.1, min(1.0, float(defaults.doubleForKey_(_HEATMAP_OPACITY_KEY))))
        except Exception:
            return _HEATMAP_OPACITY_DEFAULT

    def _heatmap_legend_on(self) -> bool:
        """Mirror Smart Ruler's "Legend" toggle (default YES). When on, Smart
        Ruler halves the heatmap fill alpha on the focus layer."""
        if NSUserDefaults is None:
            return True
        try:
            defaults = NSUserDefaults.standardUserDefaults()
            if defaults.objectForKey_(_HEATMAP_LEGEND_KEY) is None:
                return True  # registered default is YES
            return bool(defaults.boolForKey_(_HEATMAP_LEGEND_KEY))
        except Exception:
            return True

    def _palette_quality(self) -> tuple:
        """Current (palette, quality) enum tags — part of the heatmap cache key
        because the renderer bakes both into the bitmap at compute time."""
        if NSUserDefaults is None:
            return (0, 0)
        try:
            defaults = NSUserDefaults.standardUserDefaults()
            return (
                int(defaults.integerForKey_(_HEATMAP_PALETTE_KEY)),
                int(defaults.integerForKey_(_HEATMAP_QUALITY_KEY)),
            )
        except Exception:
            return (0, 0)

    def _heatmap_thresholds(self) -> tuple:
        """The manual thin/thick thresholds (glyph units); 0 disables a side.

        Auto-threshold (derived from the master's Dimensions) is not mirrored —
        the gradient still renders, only the thin/thick flag colors are off."""
        if NSUserDefaults is None:
            return (0.0, 0.0)
        try:
            defaults = NSUserDefaults.standardUserDefaults()
            low = max(0.0, float(defaults.doubleForKey_(_HEATMAP_MIN_KEY)))
            high = max(0.0, float(defaults.doubleForKey_(_HEATMAP_MAX_KEY)))
            return (low, high)
        except Exception:
            return (0.0, 0.0)

    def _heatmap_for_layer(self, layer: object, low: float, high: float) -> object:
        """Return the (LRU-cached) ``AZCachedHeatmap`` for ``layer`` at the given
        thresholds, or None. The renderer's compute is slow (~25 ms) and has no
        internal cache, so cache by (content hash, thresholds): sibling geometry
        is static, so this computes once then reuses it across redraws."""
        content_hash = self._content_hash(layer)
        if content_hash is None:
            return None
        # Palette + quality are baked into the bitmap, so they belong in the key:
        # a change must miss the cache and recompute (matching the focus layer).
        palette, quality = self._palette_quality()
        key = (content_hash, low, high, palette, quality)
        if key in self._heatmap_cache:
            heatmap = self._heatmap_cache.pop(key)
            self._heatmap_cache[key] = heatmap  # refresh LRU position
            return heatmap
        try:
            heatmap = self._heatmap_cls.heatmapForLayer_contentHash_minThickness_maxThickness_(
                layer,
                content_hash,
                float(low),
                float(high),
            )
        except Exception:
            traceback.print_exc()
            return None
        if heatmap is None:
            return None
        self._heatmap_cache[key] = heatmap
        if len(self._heatmap_cache) > _HEATMAP_CACHE_CAP:
            self._heatmap_cache.popitem(last=False)  # evict oldest
        return heatmap

    @staticmethod
    def _layer_bezier(layer: object) -> object:
        """The layer's outline as an NSBezierPath (heatmap clip). Tolerates both
        the GlyphsApp-wrapped layer (``bezierPath`` property) and a raw ObjC
        GSLayer copy (``bezierPath`` selector)."""
        try:
            bezier = layer.bezierPath
            if callable(bezier):
                bezier = bezier()
            return bezier
        except Exception:
            return None

    def _draw_heatmap(self, layer: object, x_offset: float) -> None:
        """Paint Smart Ruler's stem-thickness heatmap on ``layer``, clipped to
        its outline and translated by ``x_offset``."""
        low, high = self._heatmap_thresholds()
        heatmap = self._heatmap_for_layer(layer, low, high)
        if heatmap is None:
            return
        clip = self._layer_bezier(layer)
        if clip is None:
            return
        opacity = self._heatmap_opacity()
        # Smart Ruler halves the fill alpha on the focus layer when the legend is
        # shown; match it so the siblings render at the same effective opacity.
        if self._heatmap_legend_on():
            opacity *= 0.5
        try:
            context = NSGraphicsContext.currentContext()
            context.saveGraphicsState()
            try:
                if x_offset != 0.0:
                    transform = NSAffineTransform.transform()
                    transform.translateXBy_yBy_(float(x_offset), 0.0)
                    transform.concat()
                self._heatmap_cls.drawHeatmap_clipToPath_alpha_(heatmap, clip, float(opacity))
            finally:
                context.restoreGraphicsState()
        except Exception:
            traceback.print_exc()
