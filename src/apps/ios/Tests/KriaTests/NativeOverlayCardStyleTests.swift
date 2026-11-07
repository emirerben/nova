import XCTest
@testable import Kria
import KriaMediaEngine

#if DEBUG
/// KRI-1264: a Talking picture card zooms and animates from the editor while `editor_capabilities.media_overlay_style`
/// holds and `visual_editor_style` (the fit) stays closed. Save sends `media_overlays[i].editor_style`, which the
/// server's Talking compile pins (`tests/routes/test_overlay_card_style.py`).
@MainActor
final class NativeOverlayCardStyleTests: XCTestCase {
    private let card = EditorSelection(kind: .mediaOverlay, id: "card")
    private let pop: JSONValue = .object(["version": .number(1), "entrance": .string("pop"), "exit": .string("none"),
                                          "loop": .string("none"), "speed": .number(1)])
    private let still: JSONValue = .object(["version": .number(1), "entrance": .string("none"), "exit": .string("none"),
                                            "loop": .string("none"), "speed": .number(1)])

    private func session(style: Bool, tilt: Bool = false, fullScreenCard: Bool = false, block: Bool = false) -> NativeEditorSession {
        var draft = NativeEditorUITestFixtures.captionVisuals
        var document = EditorDocument(snapshot: draft.serverSnapshot)
        var raw: [String: JSONValue] = ["kind": .string("image"), "x_frac": .number(0.7), "y_frac": .number(0.3), "scale": .number(0.4),
                                        "entrance_token": .string("none"), "exit_token": .string("none")]
        if fullScreenCard { raw["display_mode"] = .string("fullscreen") }
        document.mediaOverlays = [.init(id: "card", startS: 0, endS: 2, kind: "image", raw: raw)]
        draft.serverSnapshot = document.encodeSnapshot()
        var capabilities: [String: JSONValue] = ["media_overlays": .bool(true), "overlays": .bool(true), "visual_blocks": .bool(true)]
        if style { capabilities["media_overlay_style"] = .bool(true) }
        if tilt { capabilities["media_overlay_tilt"] = .bool(true) }
        draft.serverSnapshot["editor_capabilities"] = .object(capabilities)
        return NativeEditorSession(draft: draft)
    }

    private func savedStyle(_ session: NativeEditorSession) -> [String: JSONValue]? {
        let snapshot = session.document.encodeSnapshot()
        let sections = snapshot["editor_payload"]?.objectValue?["sections"]?.objectValue
        let overlays = sections?["media_overlays"]?.arrayValue ?? snapshot["media_overlays"]?.arrayValue
        return overlays?.first?.objectValue?["editor_style"]?.objectValue
    }

    func testZoomAndAnimationOpenOnlyWhereTheServerOffersThem() {
        let closed = session(style: false, tilt: true)
        XCTAssertFalse(closed.canStyleVisual(card))
        let before = closed.document
        closed.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        closed.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")
        XCTAssertEqual(closed.document, before, "an old server or a closed gate: no change")

        let open = session(style: true)
        XCTAssertTrue(open.canStyleVisual(card))
        XCTAssertFalse(open.canEdit("visual_editor_style"))
        XCTAssertFalse(open.canRotateVisual(card), "the style capability does not open the angle")
    }

    func testTwoTimesZoomAndAPopAreSavedAsTheCardsEditorStyle() throws {
        let session = session(style: true)
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        session.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")

        let saved = try XCTUnwrap(savedStyle(session), "Save sends the style")
        XCTAssertEqual(saved["zoom"], .number(2))
        XCTAssertEqual(saved["animation"]?.objectValue?["entrance"], .string("pop"))
        XCTAssertEqual(saved["fit_mode"], .string("cover"))
        XCTAssertEqual(saved["rotation_deg"], .number(0))
        let reloaded = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(reloaded.mediaOverlays.first?.raw["editor_style"]?.objectValue?["zoom"], .number(2))
    }

    func testTheFitStaysClosedAndAnAddedVisualBlockStaysPlain() {
        let session = session(style: true)
        let before = session.document
        session.setVisualEditorStyle(card, key: "fit_mode", value: .string("contain"))
        XCTAssertEqual(session.document, before)
        let block = EditorSelection(kind: .visualBlock, id: "block")
        XCTAssertFalse(session.canStyleVisual(block), "a Talking edit's added visual block compiles without a style")
    }

    func testZoomOneAndNoAnimationLeaveTheCardExactlyAsLoaded() {
        let session = session(style: true)
        let loaded = session.document
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        session.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")
        XCTAssertNotEqual(session.document, loaded)
        session.setVisualEditorStyle(card, key: "zoom", value: .number(1))
        session.setVisualEditorStyle(card, key: "animation", value: still, animationPhase: "entrance")
        XCTAssertNil(session.visualRaw(card)?["editor_style"], "a plain card carries no style")
        XCTAssertEqual(session.document.mediaOverlays, loaded.mediaOverlays)
        XCTAssertNil(savedStyle(session))
    }

    func testEachChangeIsOneUndoStepAndRedoPutsItBack() {
        let session = session(style: true)
        let loaded = session.document
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        let zoomed = session.document
        session.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")
        let popped = session.document
        session.undo()
        XCTAssertEqual(session.document, zoomed)
        session.undo()
        XCTAssertEqual(session.document, loaded)
        session.redo()
        XCTAssertEqual(session.document, zoomed)
        session.redo()
        XCTAssertEqual(session.document, popped)
    }

    func testOutOfRangeZoomIsIgnored() {
        let session = session(style: true)
        let before = session.document
        for zoom in [0.5, 4.5, .nan] { session.setVisualEditorStyle(card, key: "zoom", value: .number(zoom)) }
        XCTAssertEqual(session.document, before)
        session.setVisualEditorStyle(card, key: "zoom", value: .number(4))
        XCTAssertEqual(session.visualRaw(card)?["editor_style"]?.objectValue?["zoom"], .number(4))
    }

    func testAFullScreenCardCannotZoomAndGoingFullScreenDropsTheStyle() {
        let cutaway = session(style: true, fullScreenCard: true)
        XCTAssertFalse(cutaway.canStyleVisual(card))
        let before = cutaway.document
        cutaway.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        XCTAssertEqual(cutaway.document, before)

        let onTop = session(style: true)
        onTop.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        onTop.setMediaOverlayDisplayMode(id: "card", mode: "fullscreen")
        XCTAssertNil(onTop.visualRaw(card)?["editor_style"])
    }

    func testATiltKeepsTheZoomAndAnimationAndTheyKeepTheTilt() throws {
        let session = session(style: true, tilt: true)
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        session.setVisualEditorStyle(card, key: "rotation_deg", value: .number(30))
        session.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")
        let saved = try XCTUnwrap(savedStyle(session))
        XCTAssertEqual(saved["zoom"], .number(2))
        XCTAssertEqual(saved["rotation_deg"], .number(30))
        XCTAssertEqual(saved["animation"]?.objectValue?["entrance"], .string("pop"))
    }

    /// The live preview compiles the card from the document: a zoomed, popped card moves onto the placement path with
    /// the same style the server pins, so preview and export draw it the same way (`PlacedCardStyleTests`).
    func testThePreviewPlacesTheZoomedPoppedCardWithItsStyle() throws {
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
        let session = session(style: true)
        session.setVisualEditorStyle(card, key: "zoom", value: .number(2))
        session.setVisualEditorStyle(card, key: "animation", value: pop, animationPhase: "entrance")
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
            mediaOverlays: session.document.mediaOverlays)
        let program = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: videoSource],
            mediaSources: ["overlay:card": overlaySource], deviceCaptions: true)
        let placed = try XCTUnwrap(program.recipe.tracks.first(where: { $0.id == "overlays" })?.clips.first)
        XCTAssertEqual(placed.visualPlacement?.editorStyle?.zoom, 2)
        XCTAssertEqual(placed.visualPlacement?.editorStyle?.animation.entrance, .pop)
        XCTAssertEqual(placed.visualPlacement?.widthFraction, 0.4, "the zoom is in the style; the card keeps its size")
    }
}
#endif
