import XCTest
@testable import Kria

@MainActor
final class TeleprompterWindowTests: XCTestCase {
    func testLegacyDraftAndMalformedWindowDecodeToSafeDefaults() throws {
        let legacy = """
        {"text":"saved words","revision":"00000000-0000-0000-0000-000000000001","anchorUTF16":3}
        """.data(using: .utf8)!
        let restored = try JSONDecoder().decode(TeleprompterScriptDraft.self, from: legacy)
        XCTAssertEqual(restored.text, "saved words")
        XCTAssertEqual(restored.window, .init())

        let malformed = """
        {"text":"saved words","revision":"00000000-0000-0000-0000-000000000001","anchorUTF16":3,"window":{"appearance":"unknown","normalizedX":"bad"}}
        """.data(using: .utf8)!
        XCTAssertEqual(try JSONDecoder().decode(TeleprompterScriptDraft.self, from: malformed).window, .init())
    }

    func testDraftAndManifestRoundTripPreservesWindowPreference() throws {
        let window = TeleprompterWindowPreference(appearance: .glass, normalizedX: 0.25, normalizedY: 0.75,
                                                  widthFraction: 0.62, preferredHeight: 218)
        let draft = TeleprompterScriptDraft(text: "one two", anchorUTF16: 4, window: window)
        XCTAssertEqual(try JSONDecoder().decode(TeleprompterScriptDraft.self, from: JSONEncoder().encode(draft)), draft)

        let manifest = TeleprompterTakeManifest(script: draft)
        XCTAssertEqual(try JSONDecoder().decode(TeleprompterTakeManifest.self, from: JSONEncoder().encode(manifest)).script.window, window)
    }

    func testMovementAndExtremeResizeRemainInsideLargeAndSmallCanvases() {
        for canvas in [CGRect(x: 0, y: 0, width: 390, height: 500), CGRect(x: 12, y: 60, width: 120, height: 80)] {
            let initial = TeleprompterWindowPreference(normalizedX: 0, normalizedY: 0, widthFraction: 0.4, preferredHeight: 120)
            let moved = TeleprompterWindowGeometry.moving(initial, in: canvas, by: CGSize(width: 10_000, height: 10_000))
            let enlarged = TeleprompterWindowGeometry.resizing(moved, in: canvas, by: CGSize(width: 10_000, height: 10_000))
            let frame = TeleprompterWindowGeometry.frame(preference: enlarged, in: canvas)
            XCTAssertTrue(canvas.contains(frame), "\(canvas) must contain \(frame)")
        }
    }

    func testNonFiniteOrOutOfRangePreferenceIsClampedBeforeGeometry() {
        var unsafe = TeleprompterWindowPreference()
        unsafe.normalizedX = .infinity
        unsafe.normalizedY = -.infinity
        unsafe.widthFraction = .nan
        unsafe.preferredHeight = .infinity
        let frame = TeleprompterWindowGeometry.frame(preference: unsafe, in: CGRect(x: 0, y: 0, width: 390, height: 500))
        XCTAssertTrue(frame.origin.x.isFinite && frame.origin.y.isFinite && frame.width.isFinite && frame.height.isFinite)
        XCTAssertTrue(CGRect(x: 0, y: 0, width: 390, height: 500).contains(frame))
    }

    func testFractionalCanvasContainsUnsnapppedFrame() {
        let canvas = CGRect(x: 12.25, y: 60.5, width: 301.5, height: 245.25)
        let preference = TeleprompterWindowPreference(normalizedX: 1, normalizedY: 1, widthFraction: 0.57, preferredHeight: 143.5)
        XCTAssertTrue(canvas.contains(TeleprompterWindowGeometry.frame(preference: preference, in: canvas)))
    }

    func testRequestedMinimumHeightProtectsOneLineWhenCanvasAllowsIt() {
        let canvas = CGRect(x: 0.25, y: 0.5, width: 320.5, height: 280.25)
        let frame = TeleprompterWindowGeometry.frame(preference: .init(preferredHeight: 96), in: canvas, minimumHeight: 138.5)
        XCTAssertEqual(frame.height, 138.5, accuracy: 0.001)
        XCTAssertTrue(canvas.contains(frame))

        let compact = CGRect(x: 0.25, y: 0.5, width: 120.5, height: 80.25)
        XCTAssertEqual(TeleprompterWindowGeometry.frame(preference: .init(preferredHeight: 96), in: compact, minimumHeight: 138.5).height,
                       compact.height, accuracy: 0.001)
    }

    func testResizeKeepsUpperLeftAnchorWhenSpaceAllowsAndFitsSmallerCanvas() {
        let canvas = CGRect(x: 0, y: 0, width: 390, height: 500)
        let original = TeleprompterWindowPreference(normalizedX: 0, normalizedY: 0, widthFraction: 0.5, preferredHeight: 144)
        let start = TeleprompterWindowGeometry.frame(preference: original, in: canvas)
        let resized = TeleprompterWindowGeometry.resizing(original, in: canvas, by: CGSize(width: 40, height: 40))
        let result = TeleprompterWindowGeometry.frame(preference: resized, in: canvas)
        XCTAssertEqual(result.minX, start.minX, accuracy: 1)
        XCTAssertEqual(result.minY, start.minY, accuracy: 1)

        let compact = TeleprompterWindowGeometry.frame(preference: resized, in: CGRect(x: 0, y: 0, width: 120, height: 80))
        XCTAssertTrue(CGRect(x: 0, y: 0, width: 120, height: 80).contains(compact))
    }

    func testChangingWindowDoesNotEraseTextOrReadingAnchor() {
        let model = TeleprompterScript(draft: .init(text: "one two three", anchorUTF16: 4))
        model.setWindowPreference(.init(appearance: .solid, normalizedX: 1, normalizedY: 1, widthFraction: 0.5, preferredHeight: 200))
        XCTAssertEqual(model.text, "one two three")
        XCTAssertEqual(model.anchorUTF16, 4)
        XCTAssertEqual(model.draft.window.appearance, .solid)
    }

    func testResetGeometryDefaultsKeepSelectedAppearance() {
        let selectedAppearance = TeleprompterWindowAppearance.glass
        let reset = TeleprompterWindowPreference(appearance: selectedAppearance)
        XCTAssertEqual(reset.appearance, .glass)
        XCTAssertEqual(reset.normalizedX, 0.5)
        XCTAssertEqual(reset.normalizedY, 0)
        XCTAssertEqual(reset.widthFraction, 1)
        XCTAssertEqual(reset.preferredHeight, 144)
    }
}
