"""AZ Context palette — the board controls in the right palette column.

They used to live in the Edit view's context menu; Hugo moved them out
(2026-10-11). One section, two rows: the mode (Custom | Axes), then

- Custom: two text fields around an "H" marker, ``[ nH ] H [ Oo ]`` — the
  glyphs drawn left and right of the edited one, typed as characters or
  ``/name`` (Glyphs edit-view syntax), saved in Glyphs.defaults as you type;
- Axes: an axis dropdown (a segmented control ran out of room past 4 axes).

Glyphs makes one palette instance per document window; each reads its OWN
window's font, never ``Glyphs.font`` (a WindowServer round-trip per call).
Loaded by ``plugin.py``; Glyphs finds ``AZContextPalette`` through the
``Principal Classes`` array in Info.plist.
"""

from __future__ import annotations

import traceback

import objc
from AppKit import (
    NSColor,
    NSControlSizeSmall,
    NSFont,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSLayoutConstraint,
    NSMakeRect,
    NSPopUpButton,
    NSSegmentDistributionFillEqually,
    NSSegmentedControl,
    NSTextAlignmentCenter,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
    NSViewWidthSizable,
)
from Foundation import NSNotificationCenter, NSUserDefaultsDidChangeNotification
from GlyphsApp import UPDATEINTERFACE, Glyphs
from GlyphsApp.plugins import PalettePlugin

import context_board

MODES: tuple[tuple[str, str], ...] = (
    ("glyphs", "Custom"),  # labels renamed by Hugo 2026-10-11; keys stay (stored boards)
    ("axis", "Axes"),
)
VARIANTS_SEGMENT = 1
VARIANTS_TIP = "Axes needs a font with 2+ masters on an axis"
FIELD_TIPS = {
    "left": "Glyphs left of the edited one: type characters, or /name for glyphs without Unicode (/h.sc)",
    "right": "Glyphs right of the edited one: type characters, or /name for glyphs without Unicode (/h.sc)",
}
PAD_X = 10.0
PAD_Y = 6.0  # below the second row
PAD_TOP = 0.0  # above the mode buttons (Hugo 2026-10-11: was 8, then 4)
ROW_GAP = 3.0  # mode buttons to the second row (Hugo 2026-10-11: was 6)
ROW_H = 22.0
SEG_H = 20.0  # a small NSSegmentedControl's height
HEIGHT = PAD_Y + ROW_H + ROW_GAP + SEG_H + PAD_TOP
WIDTH = 220.0
FIELD_H = 20.0
FIELD_GAP = 20.0  # between the two fields, the "H" centered in it (Hugo 2026-10-11)
MARK_ALPHA = 0.5
NOTE_TEXT = "Works only when one glyph is typed in the current tab."  # Hugo 2026-10-11
NOTE_ALPHA = 0.7  # over secondaryLabelColor: a bit quieter (Hugo 2026-10-11)
NOTE_LIFT = 5.0  # pt up from the body's center: the header leaves room under
# its title, so this centers the note between the title and the palette bottom
MARK_SIZE_UP = 4.0  # pt over the fields' small system size (Hugo 2026-10-11)
REPORTER_CLASS = "AZContextReporter"
SIDEBAR_CLASS = "GSSidebarStackView"  # Glyphs 4's palette column (an NSStackView)


def _reporter_on() -> bool:
    """True while View > Show AZ Context is on (the live set Glyphs draws)."""
    try:
        return any(type(reporter).__name__ == REPORTER_CLASS for reporter in Glyphs.activeReporters)
    except Exception:
        return True  # unreadable: keep the palette


class AZContextPalette(PalettePlugin):
    @objc.python_method
    def settings(self) -> None:
        self.name = "AZ Context"
        self._last_state = None
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        view.setAutoresizingMask_(NSViewWidthSizable)
        self._view = view
        self._mode_seg = self._segmented(
            [label for _mode, label in MODES], "azContextPaletteMode:", top_row=True
        )
        view.addSubview_(self._mode_seg)
        self._build_second_row()
        self._build_note()
        self.dialog = view
        self.min = HEIGHT
        self.max = HEIGHT
        # Registered here, not in start(): a secondary class in a multi-class
        # bundle does not reliably get start() (AZ-DynamicAnchors' palette).
        Glyphs.addCallback(self.update, UPDATEINTERFACE)
        # Toggling the reporter in the View menu rewrites GSVisibleReporters,
        # so a defaults change is the cue to re-check the palette's visibility.
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "azContextDefaultsChanged:", NSUserDefaultsDidChangeNotification, None
        )

    @objc.python_method
    def _build_second_row(self) -> None:
        """The sibling fields and the axis dropdown, sharing the second row
        (``_sync`` shows the one the mode needs)."""
        width = self._view.frame().size.width - PAD_X * 2
        y = PAD_Y + (ROW_H - FIELD_H) / 2
        mono = NSFont.monospacedSystemFontOfSize_weight_(
            NSFont.smallSystemFontSize(), NSFontWeightRegular
        )
        self._fields = {}
        for side in context_board.SIDES:
            field = NSTextField.alloc().init()
            field.setControlSize_(NSControlSizeSmall)
            field.setFont_(mono)
            field.setUsesSingleLineMode_(True)
            field.cell().setScrollable_(True)
            field.setToolTip_(FIELD_TIPS[side])
            if side == "left":
                field.setAlignment_(NSTextAlignmentRight)  # text runs up to the H
            field.setDelegate_(self)
            field.setTranslatesAutoresizingMaskIntoConstraints_(False)
            self._view.addSubview_(field)
            self._fields[side] = field
        mark = NSTextField.labelWithString_("H")
        mark.setFont_(
            NSFont.monospacedSystemFontOfSize_weight_(
                NSFont.smallSystemFontSize() + MARK_SIZE_UP, NSFontWeightSemibold
            )
        )
        mark.setAlphaValue_(MARK_ALPHA)
        mark.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self._view.addSubview_(mark)
        self._mark = mark
        # Constraints, not autoresizing masks: masks split a column resize
        # between a field and its flexible margin, so the gap drifted (Hugo
        # 2026-10-11). Fixed gap, equal fields, H centered in the gap.
        left, right, view = self._fields["left"], self._fields["right"], self._view
        NSLayoutConstraint.activateConstraints_(
            [
                left.leadingAnchor().constraintEqualToAnchor_constant_(view.leadingAnchor(), PAD_X),
                right.trailingAnchor().constraintEqualToAnchor_constant_(
                    view.trailingAnchor(), -PAD_X
                ),
                right.leadingAnchor().constraintEqualToAnchor_constant_(
                    left.trailingAnchor(), FIELD_GAP
                ),
                right.widthAnchor().constraintEqualToAnchor_(left.widthAnchor()),
                mark.centerXAnchor().constraintEqualToAnchor_constant_(
                    left.trailingAnchor(), FIELD_GAP / 2
                ),
                mark.centerYAnchor().constraintEqualToAnchor_(left.centerYAnchor()),
                *[
                    field.bottomAnchor().constraintEqualToAnchor_constant_(view.bottomAnchor(), -y)
                    for field in (left, right)
                ],
                *[
                    field.heightAnchor().constraintEqualToConstant_(FIELD_H)
                    for field in (left, right)
                ],
            ]
        )
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(PAD_X, PAD_Y, width, ROW_H), False
        )
        popup.setControlSize_(NSControlSizeSmall)
        popup.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        popup.setTarget_(self)
        popup.setAction_("azContextPaletteAxis:")
        popup.setAutoresizingMask_(NSViewWidthSizable)
        self._view.addSubview_(popup)
        self._axis_popup = popup
        self._axis_items: tuple[tuple[str, str], ...] | None = (
            None  # (tag, name) shown; None = not filled
        )

    @objc.python_method
    def _build_note(self) -> None:
        """The note shown instead of the controls when the tab is not one glyph."""
        note = NSTextField.wrappingLabelWithString_(NOTE_TEXT)
        note.setSelectable_(False)  # wrappingLabel defaults to selectable
        note.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        note.setTextColor_(NSColor.secondaryLabelColor())
        note.setAlignment_(NSTextAlignmentCenter)
        note.setAlphaValue_(NOTE_ALPHA)
        note.setHidden_(True)
        note.setTranslatesAutoresizingMaskIntoConstraints_(False)
        view = self._view
        view.addSubview_(note)
        NSLayoutConstraint.activateConstraints_(
            [
                note.leadingAnchor().constraintEqualToAnchor_constant_(view.leadingAnchor(), PAD_X),
                note.trailingAnchor().constraintEqualToAnchor_constant_(
                    view.trailingAnchor(), -PAD_X
                ),
                note.centerYAnchor().constraintEqualToAnchor_constant_(
                    view.centerYAnchor(), -NOTE_LIFT
                ),
            ]
        )
        self._note = note

    @objc.python_method
    def start(self) -> None:
        return

    @objc.python_method
    def __del__(self) -> None:
        try:
            Glyphs.removeCallback(self.update)
            NSNotificationCenter.defaultCenter().removeObserver_(self)
        except Exception:
            pass

    @objc.python_method
    def _segmented(self, labels: list[str], selector: str, top_row: bool = False) -> object:
        segmented = NSSegmentedControl.alloc().init()
        segmented.setSegmentCount_(len(labels))
        segmented.setControlSize_(NSControlSizeSmall)
        segmented.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        for index, text in enumerate(labels):
            segmented.setLabel_forSegment_(text, index)
        segmented.setTarget_(self)
        segmented.setAction_(selector)
        segmented.sizeToFit()
        # Full column width, equal segments, following column resizes: the
        # natural width (184 pt for the modes) overflowed Hugo's 170 pt column.
        segmented.setSegmentDistribution_(NSSegmentDistributionFillEqually)
        segmented.setAutoresizingMask_(NSViewWidthSizable)
        y = PAD_Y + ROW_H + ROW_GAP if top_row else PAD_Y
        width = self._view.frame().size.width - PAD_X * 2
        segmented.setFrame_(NSMakeRect(PAD_X, y, width, segmented.frame().size.height))
        return segmented

    @objc.python_method
    def _font(self) -> object:
        try:
            controller = self.windowController()
            document = controller.document() if controller is not None else None
            return document.font if document is not None else None
        except Exception:
            return None

    @objc.python_method
    def update(self, sender: object = None) -> None:
        try:
            self._apply_visibility()
            self._sync()
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _apply_visibility(self) -> None:
        """Show the palette only while the reporter draws (Hugo 2026-10-11):
        hide its whole sidebar section, header included. The column is a
        stack view that detaches hidden sections, so the rest moves up. Glyphs
        3's sidebar is not, so there the palette always shows."""
        section = self._view
        while section is not None and section.superview() is not None:
            if type(section.superview()).__name__ == SIDEBAR_CLASS:
                hidden = not _reporter_on()
                if bool(section.isHidden()) != hidden:
                    section.setHidden_(hidden)
                return
            section = section.superview()

    @objc.python_method
    def _sync(self, force: bool = False) -> None:
        """Mirror the window font's board onto the controls. Runs on every
        UPDATEINTERFACE, so it bails when nothing it shows has changed."""
        font = self._font()
        if font is None:
            return
        board = context_board.load()
        mode = board.get("mode", "glyphs")
        axes = tuple((axis.axisTag, str(axis.name or axis.axisTag)) for axis in font.axes)
        tags = tuple(tag for tag, _name in axes)
        variants_ok = bool(tags) and len(font.masters) >= 2
        axis = None
        if mode == "axis" and variants_ok:
            _ordered, _index, axis = context_board.axis_master_order(font, board)
        texts = (board.get("left", ""), board.get("right", ""))
        # The board only draws around a lone glyph (the reporter's own gate),
        # so a multi-glyph tab or the font view gets the note instead.
        tab = font.currentTab
        single = tab is not None and context_board.single_glyph(tab) is not None
        state = (objc.pyobjc_id(font), mode, texts, axes, axis, variants_ok, single)
        if state == self._last_state and not force:
            return
        self._last_state = state
        modes = [key for key, _label in MODES]
        self._mode_seg.setHidden_(not single)
        self._mode_seg.setSelectedSegment_(modes.index(mode) if mode in modes else 0)
        self._mode_seg.setEnabled_forSegment_(variants_ok, VARIANTS_SEGMENT)
        self._mode_seg.setToolTip_forSegment_(
            None if variants_ok else VARIANTS_TIP, VARIANTS_SEGMENT
        )
        for side, text in zip(context_board.SIDES, texts):
            field = self._fields[side]
            if field.currentEditor() is None:  # never rewrite a field mid-typing
                field.setStringValue_(text)
            field.setHidden_(mode != "glyphs" or not single)
        self._mark.setHidden_(mode != "glyphs" or not single)
        if axes != self._axis_items:
            self._axis_items = axes
            self._axis_popup.removeAllItems()
            menu = self._axis_popup.menu()
            # Axis NAMES (Hugo 2026-10-11); the board still stores the tag. Via
            # the menu: addItemWithTitle_ would drop a duplicate name.
            for _tag, name in axes:
                menu.addItemWithTitle_action_keyEquivalent_(name, None, "")
        if axis in tags:
            self._axis_popup.selectItemAtIndex_(tags.index(axis))
        self._axis_popup.setHidden_(mode != "axis" or not single)
        self._note.setHidden_(single)

    # ObjC action / delegate selectors (NO @objc.python_method)

    def azContextPaletteMode_(self, sender: object) -> None:
        try:
            index = int(sender.selectedSegment())
            mode = MODES[index][0] if 0 <= index < len(MODES) else "glyphs"
            context_board.mutate(self._font(), lambda board: {**board, "mode": mode})
            self._sync(force=True)
        except Exception:
            traceback.print_exc()

    def azContextDefaultsChanged_(self, notification: object) -> None:
        """Any defaults write; re-check on the next main-loop turn, once the
        View-menu toggle has updated the active reporters (and off any
        background thread that wrote a default)."""
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "azContextCheckReporter:", None, False
        )

    def azContextCheckReporter_(self, _sender: object) -> None:
        try:
            self._apply_visibility()
        except Exception:
            traceback.print_exc()

    def controlTextDidChange_(self, notification: object) -> None:
        """A sibling field changed: save it to the font and redraw, per keystroke."""
        try:
            field = notification.object()
            side = "left" if field == self._fields["left"] else "right"
            context_board.set_side_text(self._font(), side, str(field.stringValue()))
        except Exception:
            traceback.print_exc()

    def azContextPaletteAxis_(self, sender: object) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            tags = [tag for tag, _name in self._axis_items or ()]
            if 0 <= index < len(tags):
                tag = tags[index]
                context_board.mutate(self._font(), lambda board: {**board, "axis": tag})
            self._sync(force=True)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def __file__(self) -> str:
        """Please leave this method unchanged (SDK convention)."""
        return __file__
