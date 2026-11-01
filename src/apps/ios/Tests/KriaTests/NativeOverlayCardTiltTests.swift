import XCTest
@testable import Kria
import KriaMediaEngine

#if DEBUG
/// KRI-1141: a Talking picture card can be tilted from the editor while the rest of `editor_style` stays closed.
/// The angle is one value, `media_overlays[i].editor_style.rotation_deg` (clockwise), that Save sends and the server's
/// Talking compile writes into the pinned recipe (`tests/routes/test_overlay_card_tilt.py`).
@MainActor
final class NativeOverlayCardTiltTests: XCTestCase {
    private func session(tilt: Bool, fullScreenCard: Bool = false, style: Bool = false) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.captionVisuals
        var document = EditorDocument(snapshot: draft.serverSnapshot)
        var raw: [String: JSONValue] = ["kind": .string("image"), "x_frac": .number(0.7), "y_frac": .number(0.3), "scale": .number(0.4),
                                        "entrance_token": .string("none"), "exit_token": .string("none")]
        if fullScreenCard { raw["display_mode"] = .string("fullscreen") }
        document.mediaOverlays = [.init(id: "card", startS: 0, endS: 2, kind: "image", raw: raw)]
        draft.serverSnapshot = document.encodeSnapshot()
        var capabilities: [String: JSONValue] = ["media_overlays": .bool(true), "overlays": .bool(true), "visual_blocks": .bool(true)]
        if tilt { capabilities["media_overlay_tilt"] = .bool(true) }
        if style { capabilities["visual_editor_style"] = .bool(true) }
        draft.serverSnapshot["editor_capabilities"] = .object(capabilities)
        return NativeEditorSession(draft: draft)
    }

    private let card = EditorSelection(kind: .mediaOverlay, id: "card")

    private func savedRotation(_ session: NativeEditorSession) -> JSONValue? {
        let sections = session.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue
        let overlays = sections?["media_overlays"]?.arrayValue ?? session.document.encodeSnapshot()["media_overlays"]?.arrayValue
        return overlays?.first?.objectValue?["editor_style"]?.objectValue?["rotation_deg"]
    }

    func testTheCardTurnsOnlyWhereTheServerOffersIt() {
        let closed = session(tilt: false)
        XCTAssertFalse(closed.canRotateVisual(card))
        let before = closed.document
        closed.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        XCTAssertEqual(closed.document, before, "no capability, no change")

        let open = session(tilt: true)
        XCTAssertTrue(open.canRotateVisual(card))
        XCTAssertFalse(open.canEdit("visual_editor_style"))
    }

    func testAngleIsWrittenAsTheCardsEditorStyleAndSurvivesTheSaveSnapshot() throws {
        let session = session(tilt: true)
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))

        XCTAssertEqual(session.visualRaw(card)?["editor_style"]?.objectValue?["rotation_deg"], .number(30))
        XCTAssertEqual(savedRotation(session), .number(30), "Save sends the angle")
        let reloaded = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(reloaded.mediaOverlays.first?.raw["editor_style"]?.objectValue?["rotation_deg"], .number(30))
    }

    func testOnlyTheAngleIsOpenZoomFitAndAnimationStayClosed() {
        let session = session(tilt: true)
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(12))
        let tilted = session.document
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        session.setVisualEditorStyle(card, key: "fit_mode", value: .string("contain"))
        session.setVisualEditorStyle(card, key: "animation", value: .object(["version": .number(1), "entrance": .string("pop")]))
        XCTAssertEqual(session.document, tilted)
    }

    func testStraighteningLeavesTheCardExactlyAsItWasLoaded() {
        let session = session(tilt: true)
        let loaded = session.document
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        XCTAssertNotEqual(session.document, loaded)
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(0))
        XCTAssertEqual(session.visualRaw(card)?["editor_style"], nil, "an upright card carries no style")
        XCTAssertEqual(session.document.mediaOverlays, loaded.mediaOverlays)
        XCTAssertNil(savedRotation(session))
    }

    func testOneUndoStepPerAngleChangeAndUndoRestoresTheCard() {
        let session = session(tilt: true)
        let loaded = session.document
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        session.undo()
        XCTAssertEqual(session.document, loaded)
    }

    func testAFullScreenCardCannotTurnAndGoingFullScreenStraightensIt() {
        let cutaway = session(tilt: true, fullScreenCard: true)
        XCTAssertFalse(cutaway.canRotateVisual(card))
        let before = cutaway.document
        cutaway.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        XCTAssertEqual(cutaway.document, before)

        let onTop = session(tilt: true)
        onTop.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        onTop.setMediaOverlayDisplayMode(id: "card", mode: "fullscreen")
        XCTAssertNil(onTop.visualRaw(card)?["editor_style"])
        XCTAssertEqual(onTop.visualRaw(card)?["display_mode"], .string("fullscreen"))
    }

    func testAnEditWithTheFullStyleCapabilityKeepsItsExistingBehaviour() {
        let session = session(tilt: false, style: true)
        XCTAssertTrue(session.canRotateVisual(card))
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(0))
        XCTAssertEqual(session.visualRaw(card)?["editor_style"]?.objectValue?["rotation_deg"], .number(0),
                       "the full editor still writes the whole style, even upright")
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        XCTAssertEqual(session.visualRaw(card)?["editor_style"]?.objectValue?["zoom"], .number(2))
    }

    /// The live preview compiles the card from the document while the server pins the same card with
    /// `transform.rotation_degrees`. Both turn clockwise about the card's centre: the preview's placement carries
    /// the angle the editor wrote, and an upright card is the plain card it was before.
    func testTheSavedAnglePlacesTheCardInThePreviewOnTheSameClockwiseAngle() throws {
        let videoSource = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4",
                fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 4),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let overlaySource = ResolvedEditorSource(preserveAlpha: true, clipIndex: -1, mediaID: "overlay-media",
            asset: MediaAsset(id: "overlay-asset", relativePath: "overlay.png",
                fingerprint: AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 50),
                naturalSize: MediaSize(width: 400, height: 400)),
            url: URL(fileURLWithPath: "/fixture/overlay.png"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let item = NativeEditorTimelineItem(selection: card, start: 0, end: 2)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        func placed(_ angle: Double?) throws -> TimelineClip {
            let session = session(tilt: true)
            if let angle { session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(angle)) }
            // Only the card matters here: the fixture's captions, text and visual blocks need sources of their own.
            let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
                mediaOverlays: session.document.mediaOverlays)
            let program = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: videoSource],
                mediaSources: ["overlay:card": overlaySource], deviceCaptions: true)
            return try XCTUnwrap(program.recipe.tracks.first(where: { $0.id == "overlays" })?.clips.first)
        }
        let upright = try placed(nil), right = try placed(30), left = try placed(-30)
        XCTAssertNil(upright.visualPlacement, "an upright card stays on the plain positioning path it always used")
        XCTAssertEqual(right.visualPlacement?.editorStyle?.rotationDegrees, 30)
        XCTAssertEqual(left.visualPlacement?.editorStyle?.rotationDegrees, -30)
        XCTAssertEqual(right.visualPlacement?.widthFraction, 0.4)
        XCTAssertEqual(try placed(0), upright, "Reset after a tilt is the untouched card")
    }
}
#endif
