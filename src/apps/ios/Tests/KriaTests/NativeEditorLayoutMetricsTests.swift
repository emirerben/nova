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
        // The panel's own ceiling with the keyboard up is untouched.
        let kb = pro(keyboard: true, shrinksWhileTyping: true)
        XCTAssertEqual(kb.panelMaxHeight(areaHeight: 300, previewHeight: 80), kb.panelBudget(areaHeight: 300), accuracy: 0.01)
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

    func testKeyboardAndAccessibilityKeepLegacyPanelBudget() {
        let kb = pro(keyboard: true)
        XCTAssertEqual(kb.panelHeight(areaHeight: 300, previewHeight: 258, expansion: 1), 294, accuracy: 0.01)
        let ax = pro(accessibility: true)
        XCTAssertEqual(ax.panelHeight(areaHeight: 500, previewHeight: 150, expansion: 1), 440, accuracy: 0.01)
        XCTAssertEqual(kb.panelRange(areaHeight: 300, previewHeight: 258), 0, accuracy: 0.01)
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
}
