import XCTest
@testable import Kria

/// KRI-170: preview/panel sizing is pure, so the legacy default and the new
/// grow/decouple behavior are pinned with hardcoded numbers (never by
/// re-deriving the old formula here).
final class NativeEditorLayoutMetricsTests: XCTestCase {
    // iPhone 16 Pro-like: 393×852 screen, safe 59 top / 34 bottom.
    private func pro(
        aspect: CGFloat = 9.0 / 16, chrome: CGFloat = 0, keyboard: Bool = false, accessibility: Bool = false,
        shrinksWhileTyping: Bool = false
    ) -> NativeEditorLayoutMetrics {
        NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: 759), safeAreaTop: 59, safeAreaBottom: 34,
            topChromeHeight: chrome, previewAspectRatio: aspect,
            keyboardVisible: keyboard, isAccessibilitySize: accessibility,
            shrinksPreviewWhileTyping: shrinksWhileTyping
        )
    }

    // iPhone SE-like: 375×667 screen, safe 20 / 0.
    private func se(aspect: CGFloat = 9.0 / 16, chrome: CGFloat = 0) -> NativeEditorLayoutMetrics {
        NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 375, height: 647), safeAreaTop: 20, safeAreaBottom: 0,
            topChromeHeight: chrome, previewAspectRatio: aspect,
            keyboardVisible: false, isAccessibilitySize: false
        )
    }

    // MARK: typing gives a text panel the preview's room (KRI-185)

    func testATextPanelBeingTypedIntoShrinksThePreviewButKeepsItReadable() {
        let typing = pro(keyboard: true, shrinksWhileTyping: true)
        XCTAssertEqual(typing.defaultPreviewHeight, NativeEditorLayoutMetrics.typingPreviewHeight, accuracy: 0.001)
        XCTAssertEqual(typing.previewHeight(resize: 0), NativeEditorLayoutMetrics.typingPreviewHeight, accuracy: 0.001)
        // Compact but never hidden, and smaller than the untouched keyboard-up split.
        XCTAssertGreaterThan(typing.defaultPreviewHeight, NativeEditorLayoutMetrics.minPreviewHeight)
        XCTAssertLessThan(typing.defaultPreviewHeight, pro(keyboard: true).defaultPreviewHeight)
    }

    func testOtherPanelsAndTheKeyboardDownLayoutAreUnchanged() {
        // Opting out keeps today's keyboard-up split exactly.
        XCTAssertEqual(pro(keyboard: true).defaultPreviewHeight, 258.06, accuracy: 0.01)
        // Opting in changes nothing while the keyboard is down.
        XCTAssertEqual(
            pro(shrinksWhileTyping: true).defaultPreviewHeight, pro().defaultPreviewHeight, accuracy: 0.001
        )
        // The panel can still rise over the (now-shrunk) preview with the
        // keyboard up: a text block positioned away from the frame's center
        // can land illegibly small in that shrunk preview, so the panel must
        // be able to grow past its default to cover it, not get stuck there.
        let kb = pro(keyboard: true, shrinksWhileTyping: true)
        XCTAssertEqual(kb.panelMaxHeight(areaHeight: 300, previewHeight: 80), 428, accuracy: 0.01)
    }

    // MARK: default preview (unchanged from before KRI-170)

    func testDefaultPreviewHeightMatchesLegacyValues() {
        XCTAssertEqual(pro().defaultPreviewHeight, 284, accuracy: 0.01)
        XCTAssertEqual(se().defaultPreviewHeight, 226.78, accuracy: 0.01)
        XCTAssertEqual(pro(aspect: 16.0 / 9).defaultPreviewHeight, 124, accuracy: 0.01)
        XCTAssertEqual(pro(aspect: 1).defaultPreviewHeight, 284, accuracy: 0.01)
        XCTAssertEqual(pro(chrome: 40).defaultPreviewHeight, 244, accuracy: 0.01)
        XCTAssertEqual(pro(accessibility: true).defaultPreviewHeight, 150, accuracy: 0.01)
        // Keyboard: reference is the viewport itself (0.34 × 759).
        XCTAssertEqual(pro(keyboard: true).defaultPreviewHeight, 258.06, accuracy: 0.01)
    }

    func testSongReferenceRetainsQuarterScreenPreviewAfterCollapsedBar() {
        // iPhone 17 Pro Max: 860pt viewport + 62/34pt safe areas = 956pt window.
        // Its 44pt song bar plus 4pt spacing previously reduced the 284pt cap to 236pt.
        let base = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 440, height: 860), safeAreaTop: 62, safeAreaBottom: 34,
            topChromeHeight: 48, previewAspectRatio: 9.0 / 16,
            keyboardVisible: false, isAccessibilitySize: false
        )
        let withSongReference = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 440, height: 860), safeAreaTop: 62, safeAreaBottom: 34,
            topChromeHeight: 48, previewAspectRatio: 9.0 / 16,
            keyboardVisible: false, isAccessibilitySize: false,
            reservesSongReferencePreviewFloor: true
        )

        XCTAssertEqual(base.defaultPreviewHeight, 236, accuracy: 0.01)
        XCTAssertEqual(withSongReference.defaultPreviewHeight, 239, accuracy: 0.01)
        XCTAssertGreaterThanOrEqual(
            withSongReference.defaultPreviewHeight,
            956 * NativeEditorLayoutMetrics.songReferenceMinimumPreviewScreenFraction
        )
    }

    func testSongReferenceFloorIsBypassedWhileKeyboardIsVisible() {
        let keyboardWithSongReference = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 440, height: 860), safeAreaTop: 62, safeAreaBottom: 34,
            topChromeHeight: 100, previewAspectRatio: 9.0 / 16,
            keyboardVisible: true, isAccessibilitySize: false,
            reservesSongReferencePreviewFloor: true
        )
        let keyboardWithoutSongReference = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 440, height: 860), safeAreaTop: 62, safeAreaBottom: 34,
            topChromeHeight: 100, previewAspectRatio: 9.0 / 16,
            keyboardVisible: true, isAccessibilitySize: false
        )

        // With the keyboard visible, the floor must not raise the 184pt
        // budget to 215pt. This catches removal of the `!keyboardVisible`
        // guard directly.
        XCTAssertEqual(keyboardWithSongReference.defaultPreviewHeight, 184, accuracy: 0.01)
        XCTAssertEqual(
            keyboardWithSongReference.defaultPreviewHeight,
            keyboardWithoutSongReference.defaultPreviewHeight,
            accuracy: 0.01
        )

        let typing = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 440, height: 860), safeAreaTop: 62, safeAreaBottom: 34,
            topChromeHeight: 48, previewAspectRatio: 9.0 / 16,
            keyboardVisible: true, isAccessibilitySize: false,
            reservesSongReferencePreviewFloor: true, shrinksPreviewWhileTyping: true
        )
        XCTAssertEqual(typing.defaultPreviewHeight, NativeEditorLayoutMetrics.typingPreviewHeight, accuracy: 0.01)
        XCTAssertLessThan(
            typing.defaultPreviewHeight,
            956 * NativeEditorLayoutMetrics.songReferenceMinimumPreviewScreenFraction
        )
    }

    // MARK: preview grow / shrink

    func testPreviewGrowsBeyondDefaultButStaysBounded() {
        let m = pro()
        XCTAssertEqual(m.maxPreviewHeight, 439, accuracy: 0.01)
        XCTAssertGreaterThan(m.maxPreviewHeight, m.defaultPreviewHeight)
        XCTAssertEqual(m.previewHeight(resize: -10_000), 439, accuracy: 0.01)
        XCTAssertEqual(m.previewHeight(resize: -50), 334, accuracy: 0.01)
        XCTAssertEqual(m.previewHeight(resize: 0), 284, accuracy: 0.01)
        XCTAssertLessThanOrEqual(m.maxPreviewHeight, 852 * 0.6)
    }

    func testPreviewShrinkClampsToMinimum() {
        XCTAssertEqual(pro().previewHeight(resize: 10_000), 80, accuracy: 0.01)
        XCTAssertEqual(pro().shrinkRange, 204, accuracy: 0.01)
        XCTAssertEqual(pro().growRange, 155, accuracy: 0.01)
    }

    func testSmallScreenStillLeavesTimelineStrip() {
        let m = se()
        XCTAssertEqual(m.maxPreviewHeight, 327, accuracy: 0.01)
        XCTAssertGreaterThanOrEqual(m.maxPreviewHeight, m.defaultPreviewHeight)
        // Even with banners eating the column, max never drops below default.
        let crowded = se(chrome: 120)
        XCTAssertGreaterThanOrEqual(crowded.maxPreviewHeight, crowded.defaultPreviewHeight)
    }

    func testLandscapePreviewCannotOverflowWidth() {
        let m = pro(aspect: 16.0 / 9)
        XCTAssertLessThanOrEqual(m.maxPreviewHeight * 16.0 / 9, 393 - 32 + 0.01)
        XCTAssertGreaterThanOrEqual(m.maxPreviewHeight, m.defaultPreviewHeight)
    }

    func testKeyboardDisablesGrowth() {
        let m = pro(keyboard: true)
        XCTAssertEqual(m.maxPreviewHeight, m.defaultPreviewHeight, accuracy: 0.01)
        XCTAssertEqual(m.previewHeight(resize: -500), m.defaultPreviewHeight, accuracy: 0.01)
    }

    // MARK: panel

    func testPanelDefaultsMatchLegacyAtZeroExpansion() {
        let m = pro()
        let preview = m.previewHeight(resize: 0)               // 284
        let area: CGFloat = 759 - 94 - (preview + 10) - 44     // 327
        XCTAssertEqual(m.panelHeight(areaHeight: area, previewHeight: preview, expansion: 0), 267, accuracy: 0.01)
        // Plenty of room: legacy value was min(budget, 284).
        XCTAssertEqual(m.panelDefaultHeight(areaHeight: 500), 284, accuracy: 0.01)
    }

    func testPanelExpansionRisesOverThePreviewWithoutResizingIt() {
        let m = pro()
        let preview = m.previewHeight(resize: 0)
        let area: CGFloat = 327
        let full = m.panelHeight(areaHeight: area, previewHeight: preview, expansion: 1)
        XCTAssertEqual(full, 659, accuracy: 0.01)
        // Panel top (from area top) is above the preview's bottom edge…
        let panelTop = area - NativeEditorIslandMetrics.bottomPadding - full
        XCTAssertLessThan(panelTop, -(NativeEditorLayoutMetrics.resizeHandleHeight))
        // …up to (and no further than) the top chrome, covering the transport too.
        let previewRegionTop = -(preview + 10 + 44)
        XCTAssertEqual(panelTop, previewRegionTop, accuracy: 0.01)
        // The preview height is a function of previewResize only.
        XCTAssertEqual(m.previewHeight(resize: 0), preview, accuracy: 0.01)
    }

    func testPanelMaxIsIndependentOfPreviewGrowth() {
        let m = pro()
        let small = m.previewHeight(resize: 0)
        let big = m.previewHeight(resize: -10_000)
        let smallArea = 759 - 94 - (small + 10) - 44
        let bigArea = 759 - 94 - (big + 10) - 44
        XCTAssertEqual(
            m.panelMaxHeight(areaHeight: smallArea, previewHeight: small),
            m.panelMaxHeight(areaHeight: bigArea, previewHeight: big), accuracy: 0.01
        )
    }

    func testPanelExpansionIsClamped() {
        let m = pro()
        let a = m.panelHeight(areaHeight: 327, previewHeight: 284, expansion: 5)
        let b = m.panelHeight(areaHeight: 327, previewHeight: 284, expansion: 1)
        XCTAssertEqual(a, b, accuracy: 0.01)
        XCTAssertEqual(m.panelHeight(areaHeight: 327, previewHeight: 284, expansion: -3),
                       m.panelHeight(areaHeight: 327, previewHeight: 284, expansion: 0), accuracy: 0.01)
    }

    func testAccessibilitySizeKeepsLegacyPanelBudgetButKeyboardCanNowGrow() {
        // A text block editable while the keyboard is up must be able to rise
        // over the shrunk preview, same as any other panel (see the dedicated
        // panelMaxHeight assertion above) -- it is no longer stuck at budget.
        let kb = pro(keyboard: true)
        XCTAssertEqual(kb.panelHeight(areaHeight: 300, previewHeight: 258, expansion: 1), 606, accuracy: 0.01)
        XCTAssertEqual(kb.panelRange(areaHeight: 300, previewHeight: 258), 312, accuracy: 0.01)
        // Accessibility sizing is unaffected: still capped at budget.
        let ax = pro(accessibility: true)
        XCTAssertEqual(ax.panelHeight(areaHeight: 500, previewHeight: 150, expansion: 1), 440, accuracy: 0.01)
        XCTAssertEqual(ax.panelRange(areaHeight: 500, previewHeight: 150), 0, accuracy: 0.01)
    }

    // MARK: fullscreen

    func testFullscreenFitsWithinNinetyPercentOfScreen() {
        let screen = CGSize(width: 393, height: 852)
        let portrait = NativeEditorLayoutMetrics.fullscreenSize(screen: screen, aspect: 9.0 / 16)
        XCTAssertEqual(portrait.width, 353.7, accuracy: 0.1)
        XCTAssertEqual(portrait.height, 628.8, accuracy: 0.1)
        let square = NativeEditorLayoutMetrics.fullscreenSize(screen: screen, aspect: 1)
        XCTAssertEqual(square.width, 353.7, accuracy: 0.1)
        XCTAssertEqual(square.height, 353.7, accuracy: 0.1)
        let wide = NativeEditorLayoutMetrics.fullscreenSize(screen: screen, aspect: 16.0 / 9)
        XCTAssertEqual(wide.width, 353.7, accuracy: 0.1)
        XCTAssertEqual(wide.height, 198.96, accuracy: 0.1)
        for size in [portrait, square, wide] {
            XCTAssertLessThanOrEqual(size.width, screen.width * 0.9 + 0.01)
            XCTAssertLessThanOrEqual(size.height, screen.height * 0.9 + 0.01)
        }
    }

    func testFullscreenTallAspectIsHeightBound() {
        let size = NativeEditorLayoutMetrics.fullscreenSize(screen: CGSize(width: 800, height: 400), aspect: 9.0 / 16)
        XCTAssertEqual(size.height, 360, accuracy: 0.01)
        XCTAssertEqual(size.width, 202.5, accuracy: 0.01)
    }

    // MARK: KRI-508 text edit bar

    private func textEditing(lines: Int, header: CGFloat? = 0) -> NativeEditorLayoutMetrics {
        NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: 520), safeAreaTop: 59, safeAreaBottom: 336,
            topChromeHeight: 0, previewAspectRatio: 9.0 / 16,
            keyboardVisible: true, isAccessibilitySize: false,
            textEditBarHeight: TextEditBar.height(lines: lines), measuredHeaderHeight: header
        )
    }

    func testTextEditBarPinsThePanelAboveTheKeyboardAndGrowsPerLine() {
        let one = textEditing(lines: 1), two = textEditing(lines: 2)
        let area: CGFloat = 520 - 0 - one.defaultPreviewHeight - 10
        XCTAssertEqual(one.panelDefaultHeight(areaHeight: area), TextEditBar.height(lines: 1), accuracy: 0.001,
                       "the panel is exactly the bar")
        XCTAssertEqual(one.panelRange(areaHeight: area, previewHeight: one.defaultPreviewHeight), 0)
        XCTAssertEqual(one.previewHeight(resize: 40), one.defaultPreviewHeight, accuracy: 0.001,
                       "a stored timeline-handle resize does not shrink the preview under the bar")
        XCTAssertEqual(one.defaultPreviewHeight - two.defaultPreviewHeight, TextEditBar.lineHeight, accuracy: 0.001,
                       "each extra line takes one line height from the preview")
        XCTAssertGreaterThan(one.defaultPreviewHeight, NativeEditorLayoutMetrics.typingPreviewHeight,
                             "with the header away the video is larger than the old 120pt typing preview")
    }

    func testTextEditBarKeepsRoomForAKeyboardGap() {
        // 12 top + 44 title row + 6 + 50 tabs + 6 + 8 + box + 10 bottom; the island adds 6 more.
        XCTAssertEqual(TextEditBar.chromeHeight + TextEditBar.topPadding + TextEditBar.bottomPadding, 136)
        XCTAssertGreaterThanOrEqual(TextEditBar.bottomPadding + NativeEditorIslandMetrics.bottomPadding, 16)
    }

    func testAnchoredPanelStartsAtTheAnchorForEveryPanelAndFillsDownToTheBottom() {
        let top = NativeEditorLayoutMetrics.panelAnchorTop(screenHeight: 874, safeAreaTop: 59, isAccessibilitySize: false)
        XCTAssertEqual(top, 874 * 0.41 - 59, accuracy: 0.001)
        let metrics = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: 874 - 59 - 34), safeAreaTop: 59, safeAreaBottom: 34,
            topChromeHeight: 0, previewAspectRatio: 9.0 / 16,
            keyboardVisible: false, isAccessibilitySize: false,
            anchoredPanelTop: top, measuredHeaderHeight: 0
        )
        let preview = metrics.previewHeight(resize: 80)
        XCTAssertEqual(preview, metrics.defaultPreviewHeight, accuracy: 0.001, "a stored handle resize cannot move the anchor")
        // The panel is what is left under the preview + its padding + the transport row.
        let area = 874 - 59 - 34 - (preview + NativeEditorLayoutMetrics.previewVerticalPadding)
        let panel = metrics.panelDefaultHeight(areaHeight: area)
        XCTAssertEqual(panel + NativeEditorIslandMetrics.bottomPadding + NativeEditorLayoutMetrics.transportHeight, area, accuracy: 0.001,
                       "the panel fills the room under the anchor instead of stopping at the old cap")
        XCTAssertEqual(preview + NativeEditorLayoutMetrics.previewVerticalPadding + NativeEditorLayoutMetrics.transportHeight, top, accuracy: 0.001,
                       "preview + padding + transport row end exactly at the anchor")
        XCTAssertGreaterThan(panel, NativeEditorLayoutMetrics.defaultPanelCap, "taller than the old 284pt cap")
    }

    func testAccessibilitySizesAnchorHigherToGivePanelsRoom() {
        let normal = NativeEditorLayoutMetrics.panelAnchorTop(screenHeight: 852, safeAreaTop: 59, isAccessibilitySize: false)
        let large = NativeEditorLayoutMetrics.panelAnchorTop(screenHeight: 852, safeAreaTop: 59, isAccessibilitySize: true)
        XCTAssertLessThan(large, normal)
    }
}
