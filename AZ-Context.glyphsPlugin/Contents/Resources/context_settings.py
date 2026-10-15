"""AZ Context — the "Glyphs > Settings…" preferences pane (bridge code).

A vanilla view hosted in an NSViewController and handed to Glyphs via the app
delegate's ``addViewToPreferences_`` (the same hook AZ-Colorflow uses). Controls
write straight to ``Glyphs.defaults`` (keys live in ``context_prefs``) and
invalidate the live edit view; the reporter reads those defaults every frame,
so no notification plumbing is needed.

All AppKit / vanilla / Glyphs imports are guarded so a partial environment can't
break plugin load — ``register_preferences_pane`` is then a silent no-op.
"""

from __future__ import annotations

import traceback

import context_prefs

try:
    import objc
    import vanilla
    from AppKit import (
        NSApplication,
        NSAttributedString,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSFontWeightRegular,
        NSForegroundColorAttributeName,
        NSImage,
        NSImageSymbolConfiguration,
        NSMakeRange,
        NSMakeRect,
        NSMakeSize,
        NSMutableAttributedString,
        NSNoTabsNoBorder,
        NSObject,
        NSPopUpButton,
        NSSwitch,
        NSTabView,
        NSTabViewItem,
        NSTextAttachment,
        NSThread,
        NSUserDefaults,
        NSView,
        NSViewController,
        NSViewHeightSizable,
        NSViewMaxXMargin,
        NSViewMinYMargin,
        NSViewWidthSizable,
    )
    from GlyphsApp import Glyphs

    _AVAILABLE = True
except Exception:  # pragma: no cover - only importable inside Glyphs
    _AVAILABLE = False

# Hold the controller + view for the app's lifetime so AppKit can't dealloc the
# pane out from under the Settings window.
_PANE_ADDED = False
_PANE_VC = None
_PANE_VIEW = None

_PANE_IDENTIFIER = "AZ Context"

# NSSwitch geometry — SAME values as AZ Dynamic Anchors' pane so the whole
# suite's switches match. NSSwitch has ONE size (42x25 at every controlSize);
# scaling the view's coordinate system is what actually shrinks the drawing.
_SWITCH_COLUMN = 60.0
_SWITCH_SCALE = 0.65
_ROW_LABEL_H = 18.0  # the label height each switch centres against


def _smart_ruler_installed() -> bool:
    try:
        import context_smart_ruler_bridge as bridge

        return bridge.SmartRulerBridge.shared().is_available()
    except Exception:
        return False


if _AVAILABLE:

    def _ns_color(rgba: tuple) -> object:
        """(r, g, b, a) -> a device-RGB NSColor."""
        red, green, blue, alpha = rgba
        return NSColor.colorWithDeviceRed_green_blue_alpha_(red, green, blue, alpha)

    def _rgba(color: object) -> tuple:
        """NSColor -> a device-RGB (r, g, b, a) tuple."""
        rgb = color.colorUsingColorSpaceName_("NSDeviceRGBColorSpace") or color
        return (rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent(), rgb.alphaComponent())

    class AZContextSwitchTarget(NSObject):
        """ObjC target for the NSSwitch rows (a plain Python object can't be an
        NSControl's target, and vanilla has no Switch wrapper to bridge it)."""

        def initWithCallback_(self, callback: object) -> object:
            self = objc.super(AZContextSwitchTarget, self).init()
            if self is None:
                return None
            self._callback = callback
            return self

        def switchToggled_(self, sender: object) -> None:
            try:
                self._callback(sender)
            except Exception:
                traceback.print_exc()

    class AZContextSettingsView:
        """Builds the vanilla pane and writes edits to ``Glyphs.defaults``."""

        WIDTH = 300
        HEIGHT = 222
        MARGIN = 14

        def __init__(self) -> None:
            self._build()

        @property
        def view(self) -> object:
            return self.group.getNSView()

        @objc.python_method
        def _build(self) -> None:
            margin = self.MARGIN
            inner = self.WIDTH - margin * 2
            well_w = 64
            well_x = self.WIDTH - margin - well_w
            group = vanilla.Group((0, 0, self.WIDTH, self.HEIGHT))

            group.siblingsHeader = vanilla.TextBox((margin, 12, inner, 18), "Siblings")

            fill = context_prefs.decode_color(
                Glyphs.defaults[context_prefs.FILL_COLOR_KEY], context_prefs.DEFAULT_FILL_RGBA
            )
            stroke = context_prefs.decode_color(
                Glyphs.defaults[context_prefs.STROKE_COLOR_KEY], context_prefs.DEFAULT_STROKE_RGBA
            )

            group.outlineLabel = vanilla.TextBox((margin, 42, 140, 18), "Outline")
            group.outlineWell = vanilla.ColorWell(
                (well_x, 38, well_w, 24), callback=self._outline_changed, color=_ns_color(stroke)
            )
            group.fillLabel = vanilla.TextBox((margin, 74, 140, 18), "Background")
            group.fillWell = vanilla.ColorWell(
                (well_x, 70, well_w, 24), callback=self._fill_changed, color=_ns_color(fill)
            )

            self._targets = []
            label_w = inner - _SWITCH_COLUMN
            show_guides = context_prefs.decode_bool(
                Glyphs.defaults[context_prefs.SHOW_GUIDES_KEY], context_prefs.DEFAULT_SHOW_GUIDES
            )
            group.guidesLabel = vanilla.TextBox((margin, 102, label_w, 18), "Show metric guides")
            self._guides_switch = self._add_switch(group, 102, show_guides, self._guides_changed)

            group.rulerHeader = vanilla.TextBox((margin, 134, inner, 18), "Smart Ruler")
            installed = _smart_ruler_installed()
            show_meas = context_prefs.decode_bool(
                Glyphs.defaults[context_prefs.SHOW_MEASUREMENTS_KEY],
                context_prefs.DEFAULT_SHOW_MEASUREMENTS,
            )
            title = (
                "Show measurements on siblings"
                if installed
                else "Show measurements (Smart Ruler not installed)"
            )
            group.measLabel = vanilla.TextBox((margin, 160, label_w, 18), title)
            self._meas_switch = self._add_switch(
                group, 160, show_meas if installed else False, self._measurements_changed
            )
            if not installed:
                self._meas_switch.setEnabled_(False)

            self.group = group

        @objc.python_method
        def _add_switch(self, group: object, top: float, value: bool, callback: object) -> object:
            """One NSSwitch, right-aligned on its row — same construction (and
            _SWITCH_SCALE) as AZ Dynamic Anchors so the suite's switches match.

            Vanilla ships no Switch wrapper, so this parks a raw control in the
            Group's view — positioned in the group's OWN, unflipped coordinates
            (vanilla only flips its own children's posSize). NSSwitch has ONE
            size regardless of controlSize; scaling the view's coordinate
            system (scaleUnitSquareToSize) is what actually shrinks the ink,
            and the frame then matches the artwork so hit-testing lines up."""
            control = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
            control.sizeToFit()
            size = control.frame().size
            control.scaleUnitSquareToSize_(NSMakeSize(_SWITCH_SCALE, _SWITCH_SCALE))
            width = size.width * _SWITCH_SCALE
            height = size.height * _SWITCH_SCALE
            control.setFrame_(
                NSMakeRect(
                    self.WIDTH - self.MARGIN - width,
                    self.HEIGHT - top - _ROW_LABEL_H + (_ROW_LABEL_H - height) / 2.0,
                    width,
                    height,
                )
            )
            control.setState_(1 if value else 0)
            # NSControl does NOT retain its target; hold it or the switch goes dead.
            target = AZContextSwitchTarget.alloc().initWithCallback_(callback)
            self._targets.append(target)
            control.setTarget_(target)
            control.setAction_("switchToggled:")
            group.getNSView().addSubview_(control)
            return control

        @objc.python_method
        def _redraw(self) -> None:
            """Invalidate the visible edit view — NEVER ``Glyphs.redraw()``:
            in G4 its forced full pass paints the active layer with a
            mismatched transform (mini node constellation, no outline)."""
            view = None
            try:
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

        @objc.python_method
        def _outline_changed(self, sender: object) -> None:
            Glyphs.defaults[context_prefs.STROKE_COLOR_KEY] = context_prefs.encode_color(
                _rgba(sender.get())
            )
            self._redraw()

        @objc.python_method
        def _fill_changed(self, sender: object) -> None:
            Glyphs.defaults[context_prefs.FILL_COLOR_KEY] = context_prefs.encode_color(
                _rgba(sender.get())
            )
            self._redraw()

        @objc.python_method
        def _guides_changed(self, sender: object) -> None:
            Glyphs.defaults[context_prefs.SHOW_GUIDES_KEY] = bool(sender.state())
            self._redraw()

        @objc.python_method
        def _measurements_changed(self, sender: object) -> None:
            Glyphs.defaults[context_prefs.SHOW_MEASUREMENTS_KEY] = bool(sender.state())
            self._redraw()

    class AZContextPreferencesController(NSViewController):
        """NSViewController wrapper that gives the pane a fixed size."""

        def initWithSettingsView_(self, settings_view: object) -> object:
            self = objc.super(AZContextPreferencesController, self).init()
            if self is None:
                return None
            self._settings_view = settings_view
            self._apply_size()
            return self

        def loadView(self) -> None:
            try:
                self.setView_(self._settings_view.view)
                self._apply_size()
            except Exception:
                traceback.print_exc()

        @objc.python_method
        def _apply_size(self) -> None:
            try:
                view = self._settings_view
                self.setPreferredContentSize_(NSMakeSize(float(view.WIDTH), float(view.HEIGHT)))
            except Exception:
                traceback.print_exc()

    # ---- AZ Toolkit shared Settings hub (protocol v1) ----------------------
    # All AZ plugins share ONE "AZ Toolkit" entry in Glyphs > Settings; each
    # plugin's pane is a tab inside it. Rendezvous: the hub NSViewController is
    # parked in NSThread.mainThread().threadDictionary() under _HUB_KEY —
    # whichever AZ plugin loads first creates the hub (its own uniquely-named
    # copy of this class) and registers it via addViewToPreferences:; everyone
    # else finds it there. UI: a full-width NSPopUpButton switches between the
    # panes hosted in a tabless NSTabView below it; a pane whose plugin is
    # licensed (bought, not trial) gets an accent-colored 􀇻 after its name.
    # Keep the selectors stable across ALL AZ plugins (protocol v1.1):
    #   addSettingsPane:(NSViewController*)  pane.title = the popup item ("AZ X"),
    #                                        pane.representedObject = jump id;
    #                                        pane MAY expose azLicensed (BOOL)
    #   showSettingsPaneWithIdentifier:(NSString*)
    _HUB_KEY = "AZToolkit.SettingsHub.v1"
    _HUB_TITLE = "AZ Toolkit"
    _HUB_BADGE = "\U001001fb"  # SF Symbol checkmark seal, one astral codepoint
    _POPUP_BAR_H = 41.0  # 10 top + 25 popup + 6 gap

    class AZContextToolkitHub(NSViewController):
        def init(self) -> object:
            self = objc.super(AZContextToolkitHub, self).init()
            if self is None:
                return None
            self._panes = []
            self.setTitle_(_HUB_TITLE)
            self.setRepresentedObject_(_HUB_TITLE)
            self.setPreferredContentSize_(NSMakeSize(360.0, 240.0))
            return self

        def loadView(self) -> None:
            root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 240))
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(12, 240 - 35, 360 - 24, 25), False
            )
            popup.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
            popup.setTarget_(self)
            popup.setAction_("azToolkitPopupChanged:")
            tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 240 - _POPUP_BAR_H))
            tabs.setTabViewType_(NSNoTabsNoBorder)
            tabs.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            root.addSubview_(popup)
            root.addSubview_(tabs)
            self._popup = popup
            self._tabs = tabs
            self.setView_(root)

        def azToolkitPopupChanged_(self, sender: object) -> None:
            try:
                self._tabs.selectTabViewItemAtIndex_(sender.indexOfSelectedItem())
            except Exception:
                pass

        # Glyphs 4's Settings sidebar reads GSPreferenceViewControllerProtocol
        # off each pane: required -imageName, optional -image (both guarded).
        def imageName(self) -> str:
            return "wrench.and.screwdriver"

        def image(self) -> object:
            symbol_ctor = getattr(
                NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None
            )
            if symbol_ctor is None:  # pre-macOS 11: no SF Symbols
                return None
            for name in ("wrench.and.screwdriver", "gearshape.2", "hammer"):
                image = symbol_ctor(name, _HUB_TITLE)
                if image is not None:
                    image.setTemplate_(True)
                    return image
            return None

        def addSettingsPane_(self, pane: object) -> None:
            try:
                self.view()  # force loadView so _popup/_tabs exist
                tabs = self._tabs
                ident = pane.representedObject() or pane.title() or ""
                for index in range(tabs.numberOfTabViewItems()):
                    if tabs.tabViewItemAtIndex_(index).identifier() == ident:
                        return  # already registered (e.g. two document windows)
                title = pane.title() or str(ident)
                label = title[3:] if title.startswith("AZ ") else title
                size = pane.preferredContentSize()
                view = pane.view()
                if size.width < 1 or size.height < 1:
                    size = view.frame().size
                # Pin the pane at its designed size, top-left; the flexible
                # right/bottom margins absorb the stretch NSTabView applies.
                container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, size.width, size.height))
                view.setFrame_(NSMakeRect(0, 0, size.width, size.height))
                view.setAutoresizingMask_(NSViewMaxXMargin | NSViewMinYMargin)
                container.addSubview_(view)
                item = NSTabViewItem.alloc().initWithIdentifier_(ident)
                item.setLabel_(label)
                item.setView_(container)
                insert_at = tabs.numberOfTabViewItems()
                for index in range(tabs.numberOfTabViewItems()):
                    if label < str(tabs.tabViewItemAtIndex_(index).label()):
                        insert_at = index
                        break
                tabs.insertTabViewItem_atIndex_(item, insert_at)
                # Popup shows the FULL plugin name ("AZ Colorflow"), not the
                # stripped tab label — the sort above still uses the label.
                self._popup.insertItemWithTitle_atIndex_(title, insert_at)
                if self._pane_licensed(pane):
                    text = NSMutableAttributedString.alloc().initWithString_attributes_(
                        title + "  ",
                        {NSFontAttributeName: NSFont.menuFontOfSize_(0)},
                    )
                    text.appendAttributedString_(self._badge_string())
                    self._popup.itemAtIndex_(insert_at).setAttributedTitle_(text)
                self._panes.append(pane)  # retain: AppKit must not dealloc it
                self._popup.selectItemAtIndex_(tabs.indexOfTabViewItem_(tabs.selectedTabViewItem()))
                self._recalc_preferred_size()
            except Exception:
                traceback.print_exc()

        @objc.python_method
        def _badge_string(self) -> object:
            """The licensed badge, appended after the plugin name.

            Preferred: checkmark.seal.fill as a PALETTE symbol image (white
            checkmark inside an accent-colored seal, macOS 12+) attached
            inline via NSTextAttachment — plain text runs render SF symbols
            monochrome, so the checkmark can't be white there. Fallback:
            the accent-colored 􀇻 glyph as text (checkmark = knocked out).
            """
            accent = getattr(NSColor, "controlAccentColor", None)
            color = accent() if accent is not None else NSColor.systemBlueColor()
            try:
                image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "checkmark.seal.fill", "licensed"
                )
                size_conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                    12.0, NSFontWeightRegular
                )
                palette = NSImageSymbolConfiguration.configurationWithPaletteColors_(
                    [NSColor.whiteColor(), color]
                )
                image = image.imageWithSymbolConfiguration_(
                    size_conf.configurationByApplyingConfiguration_(palette)
                )
                attachment = NSTextAttachment.alloc().init()
                attachment.setImage_(image)
                size = image.size()
                font = NSFont.menuFontOfSize_(0)
                # center the seal on the cap height of the menu font
                offset_y = round((font.capHeight() - size.height) / 2.0)
                attachment.setBounds_(NSMakeRect(0, offset_y, size.width, size.height))
                return NSAttributedString.attributedStringWithAttachment_(attachment)
            except Exception:
                pass
            text = NSMutableAttributedString.alloc().initWithString_attributes_(
                _HUB_BADGE, {NSFontAttributeName: NSFont.menuFontOfSize_(0)}
            )
            text.addAttribute_value_range_(
                NSForegroundColorAttributeName, color, NSMakeRange(0, text.length())
            )
            return text

        @objc.python_method
        def _pane_licensed(self, pane: object) -> bool:
            """Protocol v1.1: a pane MAY expose azLicensed (bought, not trial).
            Debug: the AZToolkitFakeLicensed defaults key badges EVERY pane."""
            try:
                if NSUserDefaults.standardUserDefaults().boolForKey_("AZToolkitFakeLicensed"):
                    return True
                if pane.respondsToSelector_(b"azLicensed"):
                    return bool(pane.valueForKey_("azLicensed"))
            except Exception:
                pass
            return False

        def showSettingsPaneWithIdentifier_(self, identifier: object) -> None:
            try:
                self.view()
                tabs = self._tabs
                for index in range(tabs.numberOfTabViewItems()):
                    if tabs.tabViewItemAtIndex_(index).identifier() == identifier:
                        tabs.selectTabViewItemAtIndex_(index)
                        self._popup.selectItemAtIndex_(index)
                        break
            except Exception:
                pass
            try:
                delegate = NSApplication.sharedApplication().delegate()
                if delegate is not None and delegate.respondsToSelector_(
                    b"showMainPreferencesWithIdentifier:"
                ):
                    delegate.showMainPreferencesWithIdentifier_(_HUB_TITLE)
            except Exception:
                pass

        @objc.python_method
        def _recalc_preferred_size(self) -> None:
            try:
                max_width, max_height = 300.0, 200.0
                for pane in self._panes:
                    size = pane.preferredContentSize()
                    if size.width < 1 or size.height < 1:
                        size = pane.view().frame().size
                    max_width = max(max_width, size.width)
                    max_height = max(max_height, size.height)
                self.setPreferredContentSize_(NSMakeSize(max_width, max_height + _POPUP_BAR_H))
            except Exception:
                traceback.print_exc()

    def _toolkit_hub() -> object:
        """Find-or-create the shared AZ Toolkit hub (None if it can't be hosted)."""
        try:
            pool = NSThread.mainThread().threadDictionary()
            hub = pool.objectForKey_(_HUB_KEY)
            if hub is not None:
                return hub
            delegate = NSApplication.sharedApplication().delegate()
            # respondsToSelector_, not hasattr: hasattr on the bridged delegate
            # scans the entire ObjC selector table when the method is missing.
            if delegate is None or not delegate.respondsToSelector_(b"addViewToPreferences:"):
                return None
            hub = AZContextToolkitHub.alloc().init()
            if hub is None:
                return None
            pool.setObject_forKey_(hub, _HUB_KEY)  # retained here for the app's lifetime
            delegate.addViewToPreferences_(hub)
            return hub
        except Exception:
            traceback.print_exc()
            return None


def register_preferences_pane() -> None:
    """Add the AZ Context pane to Glyphs > Settings, once per app session.

    The pane lands as a tab in the shared "AZ Toolkit" entry (see the hub above);
    if the hub can't be built, fall back to registering a standalone pane.
    """
    global _PANE_ADDED, _PANE_VC, _PANE_VIEW
    if not _AVAILABLE or _PANE_ADDED:
        return
    try:
        delegate = NSApplication.sharedApplication().delegate()
    except Exception:
        return
    # respondsToSelector_, not hasattr: hasattr on the bridged delegate scans
    # the entire ObjC selector table when the method is missing.
    if delegate is None or not delegate.respondsToSelector_(b"addViewToPreferences:"):
        return
    try:
        settings_view = AZContextSettingsView()
        controller = AZContextPreferencesController.alloc().initWithSettingsView_(settings_view)
        if controller is None:
            return
        controller.setTitle_(_PANE_IDENTIFIER)
        controller.setRepresentedObject_(_PANE_IDENTIFIER)
        _PANE_ADDED = True
        _PANE_VIEW = settings_view
        _PANE_VC = controller
        hub = _toolkit_hub()
        if hub is not None and hub.respondsToSelector_(b"addSettingsPane:"):
            hub.addSettingsPane_(controller)
        else:
            delegate.addViewToPreferences_(controller)  # solo fallback
    except Exception:
        traceback.print_exc()
