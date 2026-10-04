import XCTest
@testable import Kria

/// Unit tests for the shared text move/resize/rotate/snap layer
/// (`NativeTextTransformMath`, `NativeTextLiveTransform`, the slide adapter).
///
/// The `golden` numbers were computed by running the formulas of the native
/// preview AS THEY STOOD BEFORE the extraction (NativeEditorMediaViews.swift at
/// origin/feat/slide-parity-base) with a Python port, so these tests prove the
/// native preview's math is unchanged by the refactor. The `Legacy` helpers are
/// verbatim copies of the pre-refactor inline arithmetic and are cross-checked
/// against the shared layer over a sweep of inputs.
final class NativeTextTransformLayerTests: XCTestCase {
    private let canvas = CGSize(width: 360, height: 640)

    // MARK: Legacy reference (verbatim pre-refactor arithmetic)

    private enum Legacy {
        static func corner(_ bounds: CGRect, _ degrees: Double) -> CGPoint {
            let radians: CGFloat = CGFloat(degrees) * .pi / 180
            let dx: CGFloat = bounds.width / 2, dy: CGFloat = bounds.height / 2
            return CGPoint(x: bounds.midX + dx * cos(radians) - dy * sin(radians),
                           y: bounds.midY + dx * sin(radians) + dy * cos(radians))
        }
        static func scale(_ scale: Double, current: Double, reference: Double, width: Double) -> Double {
            max(scale * current / reference, max(8 / reference, 0.2 / width))
        }
        static func center(anchor: CGPoint, bounds: (Double, Double), canvas: CGSize, scale: Double, rotation: Double, translation: CGPoint) -> CGPoint {
            let angle = rotation * .pi / 180
            let dx = (bounds.0 - anchor.x) * canvas.width * scale
            let dy = (bounds.1 - anchor.y) * canvas.height * scale
            return CGPoint(x: (anchor.x + translation.x) * canvas.width + dx * cos(angle) - dy * sin(angle),
                           y: (anchor.y + translation.y) * canvas.height + dx * sin(angle) + dy * cos(angle))
        }
    }

    private func baseline(size: Double = 72, width: Double = 0.84, rotation: Double = 10, anchor: CGPoint = CGPoint(x: 0.5, y: 0.8)) -> TextTransformBaseline {
        TextTransformBaseline(id: "t", anchor: anchor, sizePx: size, widthFrac: width, rotationDeg: rotation)
    }

    // MARK: Golden values (captured from the pre-refactor native code)

    func testGoldenCornerPointAndCornerDelta() {
        let corner = NativeTextTransformMath.cornerPoint(of: CGRect(x: 100, y: 200, width: 80, height: 40), rotationDegrees: 30)
        XCTAssertEqual(corner.x, 164.64101615137756, accuracy: 1e-9)
        XCTAssertEqual(corner.y, 257.3205080756888, accuracy: 1e-9)
        let delta = NativeTextTransformMath.cornerDelta(start: CGVector(dx: 30, dy: 40), next: CGVector(dx: 60, dy: -10))
        XCTAssertEqual(delta?.scale ?? 0, 1.216552506059644, accuracy: 1e-9)
        XCTAssertEqual(delta?.rotationDegrees ?? 0, -62.59242456218159, accuracy: 1e-9)
        XCTAssertNil(NativeTextTransformMath.cornerDelta(start: CGVector(dx: 0.5, dy: 0.5), next: CGVector(dx: 9, dy: 9)), "a grab on the anchor defines no direction")
    }

    func testGoldenLiveCenterAndFrame() {
        var live = NativeTextLiveTransform()
        live.begin(baseline: baseline(), bounds: TextTransformBounds(centerX: 0.5, centerY: 0.82, width: 0.6, height: 0.1))
        live.resize(current: baseline(), scale: 1.5, rotation: 20, snapRotation: false)
        live.move(to: CGPoint(x: 0.6, y: 0.75))
        XCTAssertEqual(live.scale, 1.5, accuracy: 1e-12)
        XCTAssertEqual(live.rotation, 20, accuracy: 1e-12)
        XCTAssertEqual(live.translation.x, 0.1, accuracy: 1e-12)
        XCTAssertEqual(live.translation.y, -0.05, accuracy: 1e-12)
        // rotation 20 here (not 30): recompute the golden centre for these inputs.
        let center = live.center(in: canvas)!
        let expected = Legacy.center(anchor: CGPoint(x: 0.5, y: 0.8), bounds: (0.5, 0.82), canvas: canvas, scale: 1.5, rotation: 20, translation: CGPoint(x: 0.1, y: -0.05))
        XCTAssertEqual(center.x, expected.x, accuracy: 1e-9)
        XCTAssertEqual(center.y, expected.y, accuracy: 1e-9)
        let frame = live.frame(in: canvas)!
        XCTAssertEqual(frame.width, 324, accuracy: 1e-9)
        XCTAssertEqual(frame.height, 96, accuracy: 1e-9)
        XCTAssertEqual(frame.midX, center.x, accuracy: 1e-9)

        // The 30-degree golden (Python port of the old updateTextAlignment/frame).
        var thirty = NativeTextLiveTransform()
        thirty.begin(baseline: baseline(rotation: 0), bounds: TextTransformBounds(centerX: 0.5, centerY: 0.82, width: 0.6, height: 0.1))
        thirty.resize(current: baseline(rotation: 0), scale: 1.5, rotation: 30, snapRotation: false)
        thirty.move(to: CGPoint(x: 0.6, y: 0.75))
        let golden = thirty.center(in: canvas)!
        XCTAssertEqual(golden.x, 206.40000000000003, accuracy: 1e-9)
        XCTAssertEqual(golden.y, 496.62768775266113, accuracy: 1e-9)
    }

    func testGoldenResizeScaleRotationAndClamps() {
        var live = NativeTextLiveTransform()
        live.begin(baseline: baseline(), bounds: nil)
        live.resize(current: baseline(), scale: 1.5, rotation: 30, snapRotation: true)
        XCTAssertEqual(live.scale, 1.5, accuracy: 1e-12)
        XCTAssertEqual(live.rotation, 30, accuracy: 1e-12, "raw 40 sits outside the detent")
        live.resize(current: baseline(), scale: 1, rotation: 78, snapRotation: true)
        XCTAssertEqual(live.rotation, 80, accuracy: 1e-12, "raw 88 snaps to 90, minus the baseline 10")
        live.resize(current: baseline(), scale: 1, rotation: 78, snapRotation: false)
        XCTAssertEqual(live.rotation, 78, accuracy: 1e-12, "pinch/move never snap")
        live.resize(current: baseline(size: 20), scale: 0.1, rotation: 0, snapRotation: false)
        XCTAssertEqual(live.scale, max(0.1 * 20 / 72, max(8.0 / 72, 0.2 / 0.84)), accuracy: 1e-12)
        var floor = NativeTextLiveTransform()
        floor.begin(baseline: baseline(size: 20), bounds: nil)
        floor.resize(current: baseline(size: 20), scale: 0.1, rotation: 0, snapRotation: false)
        XCTAssertEqual(floor.scale, 0.4, accuracy: 1e-12, "size floor 8pt / 20 = 0.4 beats the width floor")
    }

    func testSharedLayerMatchesLegacyArithmeticAcrossASweep() {
        for rotation in stride(from: -180.0, through: 180, by: 37.5) {
            for size in [12.0, 72, 199] {
                let rect = CGRect(x: 40, y: 90, width: 120, height: 36)
                let a = NativeTextTransformMath.cornerPoint(of: rect, rotationDegrees: rotation)
                let b = Legacy.corner(rect, rotation)
                XCTAssertEqual(a.x, b.x, accuracy: 1e-9); XCTAssertEqual(a.y, b.y, accuracy: 1e-9)
                for scale in [0.05, 0.7, 1, 2.3] {
                    var live = NativeTextLiveTransform()
                    let base = baseline(size: size, rotation: rotation)
                    live.begin(baseline: base, bounds: TextTransformBounds(centerX: 0.4, centerY: 0.6, width: 0.3, height: 0.05))
                    live.resize(current: base, scale: scale, rotation: 13, snapRotation: false)
                    XCTAssertEqual(live.scale, Legacy.scale(scale, current: size, reference: size, width: 0.84), accuracy: 1e-12)
                    let center = live.center(in: canvas)!
                    let legacy = Legacy.center(anchor: base.anchor, bounds: (0.4, 0.6), canvas: canvas, scale: live.scale, rotation: live.rotation, translation: live.translation)
                    XCTAssertEqual(center.x, legacy.x, accuracy: 1e-9); XCTAssertEqual(center.y, legacy.y, accuracy: 1e-9)
                }
            }
        }
    }

    // MARK: Move, clamp, grab

    func testMovedPositionClampsToTheCanvas() {
        let moved = NativeTextTransformMath.movedPosition(from: CGPoint(x: 0.9, y: 0.1), translation: CGSize(width: 90, height: -200), canvas: canvas)
        XCTAssertEqual(moved.x, 1); XCTAssertEqual(moved.y, 0)
        let inside = NativeTextTransformMath.movedPosition(from: CGPoint(x: 0.5, y: 0.5), translation: CGSize(width: 36, height: 64), canvas: canvas)
        XCTAssertEqual(inside.x, 0.6, accuracy: 1e-12); XCTAssertEqual(inside.y, 0.6, accuracy: 1e-12)
    }

    func testCornerGrabPrefersTheCornerOnlyWhenCloser() {
        let rect = CGRect(x: 100, y: 100, width: 100, height: 40)
        let corner = NativeTextTransformMath.cornerPoint(of: rect, rotationDegrees: 0)
        XCTAssertTrue(NativeTextTransformMath.grabsCorner(at: CGPoint(x: corner.x + 10, y: corner.y + 10), corner: corner, bounds: rect))
        XCTAssertFalse(NativeTextTransformMath.grabsCorner(at: CGPoint(x: corner.x + 40, y: corner.y), corner: corner, bounds: rect))
        let tiny = CGRect(x: 100, y: 100, width: 10, height: 10)
        let tinyCorner = NativeTextTransformMath.cornerPoint(of: tiny, rotationDegrees: 0)
        XCTAssertFalse(NativeTextTransformMath.grabsCorner(at: CGPoint(x: 105, y: 105), corner: tinyCorner, bounds: tiny), "centre wins on a tiny text")
    }

    func testHandleStaysFullyInsideTheStage() {
        // Text hugging the bottom-right: the raw corner is off-stage, the handle is not.
        let rect = CGRect(x: 300, y: 620, width: 90, height: 40)
        let handle = NativeTextTransformMath.handleCenter(of: rect, rotationDegrees: 0, canvas: canvas, inset: 14)
        XCTAssertEqual(handle.x, 346); XCTAssertEqual(handle.y, 626)
        XCTAssertEqual(NativeTextTransformMath.handleCenter(of: rect, rotationDegrees: 0, canvas: canvas, inset: nil), CGPoint(x: 390, y: 660), "native keeps the raw corner")
        for rotation in stride(from: -360.0, through: 360, by: 45) {
            let h = NativeTextTransformMath.handleCenter(of: CGRect(x: -20, y: -10, width: 60, height: 30), rotationDegrees: rotation, canvas: canvas, inset: 14)
            XCTAssertTrue(CGRect(origin: .zero, size: canvas).insetBy(dx: 14, dy: 14).contains(h))
        }
    }

    // MARK: Slide anchor semantics (must match SlidePostTextLayout / the server render)

    private func element(alignment: String, position: String = "custom", x: Double? = nil, y: Double? = 0.5) -> SlidePostTextElement {
        var e = SlidePostTextElement(id: "e", text: "Hello")
        e.alignment = alignment; e.position = position; e.xFrac = x; e.yFrac = y
        return e
    }

    func testSlideBlockCenterHonoursLeftCentreRightAnchors() {
        let stage = CGSize(width: 400, height: 500), block = CGSize(width: 100, height: 40)
        let left = SlidePostTextGeometry.blockCenter(for: element(alignment: "left", x: 0.1), blockSize: block, canvas: stage)
        XCTAssertEqual(left.x, 0.1 + 0.125, accuracy: 1e-12, "x_frac is the LEFT edge")
        let right = SlidePostTextGeometry.blockCenter(for: element(alignment: "right", x: 0.9), blockSize: block, canvas: stage)
        XCTAssertEqual(right.x, 0.9 - 0.125, accuracy: 1e-12, "x_frac is the RIGHT edge")
        let centre = SlidePostTextGeometry.blockCenter(for: element(alignment: "center", x: 0.5), blockSize: block, canvas: stage)
        XCTAssertEqual(centre.x, 0.5, accuracy: 1e-12)
        XCTAssertEqual(left.y, 0.5)
    }

    func testSlidePresetsMatchServerForBothAspectRatios() {
        for stage in [CGSize(width: 360, height: 640), CGSize(width: 400, height: 500)] {
            for (preset, y) in [("top", 0.12), ("center", 0.5), ("bottom", 0.82)] {
                let e = element(alignment: "center", position: preset, x: nil, y: nil)
                let center = SlidePostTextGeometry.visualCenter(for: e, blockSize: CGSize(width: 80, height: 30), canvas: stage)
                XCTAssertEqual(center.y, CGFloat(y) * stage.height, accuracy: 1e-9)
                XCTAssertEqual(center.x, stage.width / 2, accuracy: 1e-9)
            }
        }
    }

    func testSlideRotationPivotsOnTheAnchorNotTheCentre() {
        let stage = CGSize(width: 400, height: 500), block = CGSize(width: 100, height: 40)
        var left = element(alignment: "left", x: 0.1, y: 0.5)
        left.setRotation(90)
        // Block centre is (0.1*400 + 50, 250) = (90, 250); anchor (40, 250). A clockwise 90deg turn puts it below the anchor.
        let center = SlidePostTextGeometry.visualCenter(for: left, blockSize: block, canvas: stage)
        XCTAssertEqual(center.x, 40, accuracy: 1e-9)
        XCTAssertEqual(center.y, 300, accuracy: 1e-9)
        var centred = element(alignment: "center", x: 0.5, y: 0.5)
        centred.setRotation(37)
        let c = SlidePostTextGeometry.visualCenter(for: centred, blockSize: block, canvas: stage)
        XCTAssertEqual(c.x, 200, accuracy: 1e-9); XCTAssertEqual(c.y, 250, accuracy: 1e-9)
    }

    func testSlideRotationLivesInExtraAndIsClampedAndRemovedWhenUpright() throws {
        var e = element(alignment: "center")
        e.setRotation(450)
        XCTAssertEqual(e.rotationDeg, 90)
        XCTAssertEqual(e.extra["rotation_deg"], .number(90))
        e.setRotation(-720)
        XCTAssertNil(e.extra["rotation_deg"], "upright text round-trips without the key")
        e.setRotation(-359)
        XCTAssertTrue((-360...360).contains(e.rotationDeg))
        e.setRotation(33)
        let round = try JSONDecoder().decode(SlidePostTextElement.self, from: JSONEncoder().encode(e))
        XCTAssertEqual(round.rotationDeg, 33)
    }

    func testApplyTransformScalesSizeAndWidthTogetherWithinServerBounds() {
        var e = element(alignment: "center")
        e.sizePx = 86; e.maxWidthFrac = nil
        let base = e.transformBaseline
        var live = NativeTextLiveTransform()
        live.begin(baseline: base, bounds: nil)
        live.resize(current: base, scale: 1.5, rotation: 15, snapRotation: false)
        e.applyTransform(from: base, live: live)
        XCTAssertEqual(e.sizePx, 129)
        XCTAssertEqual(e.maxWidthFrac ?? 0, 1, accuracy: 1e-12, "width is capped at the full canvas")
        XCTAssertEqual(e.rotationDeg, 15, accuracy: 1e-12)
        live.resize(current: base, scale: 50, rotation: 0, snapRotation: false)
        e.applyTransform(from: base, live: live)
        XCTAssertEqual(e.sizePx, 200)
        live.resize(current: base, scale: 0.0001, rotation: 0, snapRotation: false)
        e.applyTransform(from: base, live: live)
        XCTAssertGreaterThanOrEqual(e.sizePx, 8)
        XCTAssertGreaterThanOrEqual(e.maxWidthFrac ?? 0, 0.2)
        XCTAssertFalse(e.sizePx > 200)
    }

    func testMoveOnlyWritesPositionNotSizeOrWidth() {
        var e = element(alignment: "left", position: "bottom", x: nil, y: nil)
        let before = e
        e.position = "custom"; e.xFrac = 0.2; e.yFrac = 0.3
        XCTAssertEqual(e.sizePx, before.sizePx)
        XCTAssertNil(e.maxWidthFrac)
        XCTAssertEqual(SlidePostTextLayout.anchor(for: e).x, 0.2)
    }

    // MARK: Alignment feedback through the live layer

    func testLiveLayerReportsAlignmentGuideCrossings() {
        var live = NativeTextLiveTransform()
        let base = baseline(rotation: 0, anchor: CGPoint(x: 0.3, y: 0.5))
        live.begin(baseline: base, bounds: TextTransformBounds(centerX: 0.3, centerY: 0.5, width: 0.2, height: 0.05))
        var feedback = NativeTextAlignmentFeedback()
        live.resize(current: base, scale: 1, rotation: 0, snapRotation: false)
        _ = live.updateAlignment(&feedback, in: canvas)
        live.move(to: CGPoint(x: 0.2, y: 0.4))
        _ = live.updateAlignment(&feedback, in: canvas)
        live.move(to: CGPoint(x: 0.5, y: 0.4))
        XCTAssertTrue(live.updateAlignment(&feedback, in: canvas), "reaching the centre line buzzes once")
        XCTAssertFalse(live.updateAlignment(&feedback, in: canvas))
    }

    // MARK: Undo grouping through the slide session

    @MainActor func testOneGestureIsOneUndoStepAndTwoGesturesAreTwo() async throws {
        let itemID = "11111111-1111-1111-1111-111111111111"
        let refs = [SlidePostSlide(id: "s1", assetID: "a1", kind: "image")]
        let state = SlidePostState(itemID: itemID, title: "T", jobID: nil,
            draft: SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: refs, caption: "c", renderedVersion: 1),
            assets: [.init(id: "a1", kind: "image", status: "ready", sourceURL: URL(string: "https://storage.test/source"), durationS: nil, mediaStatus: "available")],
            renderStatus: "ready", renderedVersion: 1, slides: [.init(id: "s1", assetID: "a1", kind: "image", url: URL(string: "https://storage.test/s1"))], bundleURL: nil)
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(state)) }
        defer { NativeEditorURLProtocol.handler = nil }
        let session = SlidePostSession(defaults: UserDefaults(suiteName: "NativeTextTransformLayerTests.\(UUID())")!)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        let id = try XCTUnwrap(session.addText(slideID: "s1", text: "Hi"))
        func texts() -> [SlidePostTextElement] { session.draft?.slides[0].edits?.effectiveTexts ?? [] }
        let start = texts()[0]

        for step in 1...20 {
            session.updateText(slideID: "s1", textID: id, coalescing: "gesture-A") { $0.position = "custom"; $0.xFrac = 0.5 + Double(step) / 100; $0.yFrac = 0.5 }
        }
        for step in 1...20 {
            session.updateText(slideID: "s1", textID: id, coalescing: "gesture-B") { $0.setRotation(Double(step)); $0.sizePx = 86 + step }
        }
        XCTAssertEqual(texts()[0].rotationDeg, 20)
        session.undoEdit()
        XCTAssertEqual(texts()[0].rotationDeg, 0, "gesture B undone as one step")
        XCTAssertEqual(texts()[0].xFrac ?? 0, 0.7, accuracy: 1e-12, "gesture A intact")
        session.undoEdit()
        XCTAssertEqual(texts()[0].position, start.position, "gesture A undone as one step")
        XCTAssertEqual(texts()[0].xFrac, start.xFrac)
        session.redoEdit(); session.redoEdit()
        XCTAssertEqual(texts()[0].sizePx, 106)
    }
}
