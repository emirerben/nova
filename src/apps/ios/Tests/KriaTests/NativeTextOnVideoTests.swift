import XCTest
@testable import Kria

/// KRI-508 (plan 027): editing text and titles right on the video.
final class NativeTextOnVideoTests: XCTestCase {
    private let pill = CGSize(width: 232, height: 52)
    private let focused = CGSize(width: 250, height: 444)

    // MARK: action pill placement (D3)

    func testPillSitsAboveTheTextCentredOnIt() {
        let center = NativeTextPillPlacement.center(
            selection: CGRect(x: 40, y: 120, width: 170, height: 40), pill: pill, canvas: focused)
        XCTAssertEqual(center.x, 125, accuracy: 0.01)
        XCTAssertEqual(center.y, 120 - 14 - 26, accuracy: 0.01)
    }

    func testPillFlipsBelowATextNearTheTop() {
        let center = NativeTextPillPlacement.center(
            selection: CGRect(x: 40, y: 10, width: 170, height: 40), pill: pill, canvas: focused)
        XCTAssertEqual(center.y, 50 + 18 + 26, accuracy: 0.01)
    }

    func testPillStaysInsideTheCanvasNearItsEdges() {
        let left = NativeTextPillPlacement.center(
            selection: CGRect(x: 0, y: 200, width: 40, height: 30), pill: pill, canvas: focused)
        XCTAssertEqual(left.x - pill.width / 2, 8, accuracy: 0.01)
        let right = NativeTextPillPlacement.center(
            selection: CGRect(x: 220, y: 200, width: 30, height: 30), pill: pill, canvas: focused)
        XCTAssertEqual(right.x + pill.width / 2, focused.width - 8, accuracy: 0.01)
    }

    func testPillSitsOnTheTopEdgeOfAFullFrameText() {
        let center = NativeTextPillPlacement.center(
            selection: CGRect(x: 0, y: 0, width: 250, height: 444), pill: pill, canvas: focused)
        XCTAssertEqual(center.y, 8 + 26, accuracy: 0.01)
    }

    func testPillNeedsAPreviewWideEnoughToHoldIt() {
        XCTAssertFalse(NativeTextPillPlacement.fits(canvasWidth: 145), "the default 258pt-tall preview keeps the island actions")
        XCTAssertTrue(NativeTextPillPlacement.fits(canvasWidth: 248))
        XCTAssertTrue(NativeTextPillPlacement.fits(canvasWidth: 250))
    }

    // MARK: size range (D11)

    func testGesturesKeepTheSizeBetween24And320() {
        XCTAssertEqual(NativeTextSizeRange.clampedScale(10, baseSize: 104), 320.0 / 104, accuracy: 0.0001)
        XCTAssertEqual(NativeTextSizeRange.clampedScale(0.1, baseSize: 104), 24.0 / 104, accuracy: 0.0001)
        XCTAssertEqual(NativeTextSizeRange.clampedScale(1.5, baseSize: 104), 1.5, accuracy: 0.0001)
    }

    func testATextAlreadyOutsideTheRangeDoesNotJump() {
        // Bigger than the cap: may shrink, can't grow.
        XCTAssertEqual(NativeTextSizeRange.clampedScale(1.2, baseSize: 400), 1, accuracy: 0.0001)
        XCTAssertEqual(NativeTextSizeRange.clampedScale(0.5, baseSize: 400), 0.5, accuracy: 0.0001)
        // Smaller than the floor: may grow, can't shrink.
        XCTAssertEqual(NativeTextSizeRange.clampedScale(0.5, baseSize: 12), 1, accuracy: 0.0001)
        XCTAssertEqual(NativeTextSizeRange.clampedScale(3, baseSize: 12), 3, accuracy: 0.0001)
        XCTAssertEqual(NativeTextSizeRange.clampedScale(.nan, baseSize: 104), 1)
    }

    // MARK: typing on the video (D5)

    func testTypingSizeFollowsThePreviewButStaysReadable() {
        let preview = CGSize(width: 185, height: 329)
        XCTAssertEqual(NativeTextInlineLayout.displaySize(sizePx: 104, previewSize: preview, aspectRatio: 9.0 / 16),
                       104 * 185 / 1080, accuracy: 0.01)
        XCTAssertEqual(NativeTextInlineLayout.displaySize(sizePx: 36, previewSize: preview, aspectRatio: 9.0 / 16),
                       NativeTextInlineLayout.minimumDisplaySize, accuracy: 0.01)
        XCTAssertEqual(NativeTextInlineLayout.displaySize(sizePx: 4000, previewSize: preview, aspectRatio: 9.0 / 16),
                       329.0 / 3, accuracy: 0.01)
        XCTAssertEqual(NativeTextInlineLayout.canvasWidth(aspectRatio: 16.0 / 9), 1920)
        XCTAssertEqual(NativeTextInlineLayout.canvasWidth(aspectRatio: 1), 1080)
    }

    func testTypingFieldIsPlacedByAlignmentAndKeptInside() {
        let canvas = CGSize(width: 300, height: 400)
        func field(_ alignment: String, x: CGFloat = 0.5, widths: [CGFloat] = [100], centerY: CGFloat? = nil) -> CGRect {
            NativeTextInlineLayout.fieldFrame(lineWidths: widths, lineHeight: 20, anchor: CGPoint(x: x, y: 0.25),
                                              alignment: alignment, centerY: centerY, canvas: canvas)
        }
        XCTAssertEqual(field("center"), CGRect(x: 88, y: 84, width: 124, height: 32))
        XCTAssertEqual(field("left").minX, 150 - 12, accuracy: 0.01, "a left-aligned text grows rightwards from its anchor")
        XCTAssertEqual(field("right").maxX, 150 + 12, accuracy: 0.01, "a right-aligned text grows leftwards from its anchor")
        XCTAssertEqual(field("left", x: 0.9).maxX, canvas.width - 4, accuracy: 0.01, "a field that would leave the frame is pulled in")
        XCTAssertEqual(field("right", x: 0.05).minX, 4, accuracy: 0.01)
        let twoLines = field("center", widths: [80, 140], centerY: 150)
        XCTAssertEqual(twoLines.height, 52, accuracy: 0.01)
        XCTAssertEqual(twoLines.midY, 150, accuracy: 0.01, "the field sits on the measured block centre")
    }

    func testALongTitleShrinksToFitTheVideoWhileTyping() {
        XCTAssertEqual(NativeTextInlineLayout.fittedSize(38, longestLineAt: 330, available: 173), 19)
        XCTAssertEqual(NativeTextInlineLayout.fittedSize(20, longestLineAt: 100, available: 173), 20, "a line that fits keeps its size")
        XCTAssertEqual(NativeTextInlineLayout.fittedSize(20, longestLineAt: 1000, available: 173), 12, "never below 12pt")
    }

    func testTypingOnTheVideoIsATextPanelWithABar() {
        XCTAssertEqual(NativeEditorPanel.textInline("title").tool, .text)
        XCTAssertNotEqual(NativeEditorPanel.textInline("title"), .text("title"))
        XCTAssertEqual(NativeTextInlineBar.height(rowHeight: 44), 118, accuracy: 0.01)
    }

    func testTypingLayoutGivesTheVideoTheRoomAboveTheBar() {
        let bar = NativeTextInlineBar.height(rowHeight: 44)
        // iPhone 15-class with the keyboard up and the header hidden while typing.
        let typing = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: 457), safeAreaTop: 59, safeAreaBottom: 0,
            topChromeHeight: 0, previewAspectRatio: 9.0 / 16, keyboardVisible: true, isAccessibilitySize: false,
            shrinksPreviewWhileTyping: true, captionEditBarHeight: bar, measuredHeaderHeight: 0)
        XCTAssertEqual(typing.previewHeight(resize: 0), 457 - 10 - 6 - bar, accuracy: 0.01)
        XCTAssertGreaterThan(typing.previewHeight(resize: 0), NativeEditorLayoutMetrics.typingPreviewHeight * 2)
    }

    // MARK: guides and notices

    func testAlignmentFeedbackExposesTheCentreLineItSignals() {
        var feedback = NativeTextAlignmentFeedback()
        _ = feedback.update(center: CGPoint(x: 100, y: 37), size: CGSize(width: 50, height: 20), rotation: 0,
                            canvas: CGSize(width: 200, height: 400))
        XCTAssertTrue(feedback.activeGuides.contains("center-x"))
        XCTAssertFalse(feedback.activeGuides.contains("center-y"))
        feedback.reset()
        XCTAssertTrue(feedback.activeGuides.isEmpty)
    }

    func testRotatedTextGetsItsOuterBoxForThePill() {
        let box = NativeVideoPreview.boundingBox(CGRect(x: 0, y: 0, width: 100, height: 20), degrees: 90)
        XCTAssertEqual(box.width, 20, accuracy: 0.01)
        XCTAssertEqual(box.height, 100, accuracy: 0.01)
        XCTAssertEqual(box.midX, 50, accuracy: 0.01)
        XCTAssertEqual(box.midY, 10, accuracy: 0.01)
        XCTAssertEqual(NativeVideoPreview.boundingBox(CGRect(x: 1, y: 2, width: 3, height: 4), degrees: 0),
                       CGRect(x: 1, y: 2, width: 3, height: 4))
    }

    func testTwoNoticesOfTheSameKindAreDifferentNotices() {
        XCTAssertNotEqual(NativeEditorPreviewNoticeState(kind: .lockedTitle), NativeEditorPreviewNoticeState(kind: .lockedTitle))
    }
}
