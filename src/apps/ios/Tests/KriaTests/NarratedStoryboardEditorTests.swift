import XCTest
import KriaMediaEngine
@testable import Kria

/// A cloud Voiceover (narrated) edit's storyboard text — opening title,
/// PLAYER n labels, spoken scores — previews where the cloud burns it.
///
/// The server authors those bars as presets ("top"/"bottom", a size class,
/// no face) and burns them at y 0.15 / 0.85 in Playfair Display. The editor
/// load fills a missing `y_frac`/`font_family` with 0.5 / Fraunces, so the
/// status route now serves the resolved look
/// (`text_element.resolve_narrated_storyboard_look`). These rows are copied
/// from what it serves.
@MainActor final class NarratedStoryboardEditorTests: XCTestCase {
    private static let titleJSON = """
    {"alignment": "center", "effect": "fade-in", "end_s": 1.4, "font_family": "Playfair Display",
     "id": "da49651a407b5a79aee6859a966a2b88", "position": "custom", "role": "generative_intro",
     "size_class": "large", "size_px": 120.0, "source_params": {"narrated_storyboard": "intro"},
     "start_s": 0.0, "text": "Match Day", "x_frac": 0.5, "y_frac": 0.15}
    """
    private static let playerJSON = """
    {"alignment": "center", "effect": "fade-in", "end_s": 2.0, "font_family": "Playfair Display",
     "id": "e2cb4b98b2f555582c82f34d51f9e2da", "position": "custom", "role": "generative_sequence",
     "size_class": "small", "size_px": 36.0,
     "source_params": {"editable_placeholder": true, "narrated_storyboard": "narrated_storyboard:placeholder:1",
                       "participant_key": "clip:clip_0"},
     "start_s": 0.0, "text": "PLAYER 1", "x_frac": 0.5, "y_frac": 0.85}
    """
    private static let titleID = "da49651a407b5a79aee6859a966a2b88"
    private static let playerID = "e2cb4b98b2f555582c82f34d51f9e2da"

    private static func row(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue]? {
        if case let .object(value) = value { value } else { nil }
    }

    /// A cloud narrated variant opened in the editor, through the real load path.
    private func loadedSession() async throws -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID(); let jobID = UUID()
        let rows: JSONValue = .array([try Self.row(Self.titleJSON), try Self.row(Self.playerJSON)])
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"), "sections": .object(["text_elements": rows]),
        ])]
        let capabilities: JSONValue = .object(["text_elements": .bool(true), "caption_cues": .object(["editable": .bool(true)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "narrated", draftRevision: 1, snapshotHash: "h",
                etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "variant_id": .string("narrated"), "resolved_archetype": .string("narrated"),
                "render_generation_id": .string("g1"), "editor_capabilities": capabilities, "text_elements": rows,
                "caption_cues": .array([.object(["text": .string("Intro to the final"), "start_s": .number(0), "end_s": .number(1)])]),
            ],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true,
                captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [],
            captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    func testStoryboardBarsKeepTheCloudLookThroughTheEditorLoad() async throws {
        let (session, _) = try await loadedSession()
        let byID = Dictionary(uniqueKeysWithValues: session.document.textElements.map { ($0.id, $0) })

        let title = try XCTUnwrap(byID[Self.titleID])
        XCTAssertEqual(title.raw["y_frac"], .number(0.15))
        XCTAssertEqual(title.raw["font_family"], .string("Playfair Display"))
        XCTAssertEqual(title.raw["position"], .string("custom"))
        XCTAssertFalse(title.isCaption)
        let player = try XCTUnwrap(byID[Self.playerID])
        XCTAssertEqual(player.raw["y_frac"], .number(0.85))
        XCTAssertEqual(player.raw["font_family"], .string("Playfair Display"))

        // The preview draws them exactly where (and how) the cloud burns them.
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let items = [title, player].map { NativeEditorTimelineItem(selection: .init(kind: .text, id: $0.id), start: $0.startS, end: $0.endS) }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: EditorDocument(textElements: [title, player]),
            clips: [clip], items: items, sources: [0: source])
        let layers = Dictionary(uniqueKeysWithValues: program.recipe.textLayers.map { ($0.id, $0) })

        let titleLayer = try XCTUnwrap(layers[Self.titleID])
        XCTAssertEqual(titleLayer.anchorX, 0.5 * 1080, accuracy: 0.001)
        XCTAssertEqual(titleLayer.anchorY, 0.15 * 1920, accuracy: 0.001)
        XCTAssertEqual(titleLayer.effect.rawValue, "fade-in")
        let titleRun = try XCTUnwrap(titleLayer.runs.first)
        XCTAssertEqual(titleRun.fontSize, 120)
        XCTAssertEqual(program.assetURLs[titleRun.fontAssetID]?.lastPathComponent, "PlayfairDisplay-Bold.ttf")

        let playerLayer = try XCTUnwrap(layers[Self.playerID])
        XCTAssertEqual(playerLayer.anchorY, 0.85 * 1920, accuracy: 0.001)
        let playerRun = try XCTUnwrap(playerLayer.runs.first)
        XCTAssertEqual(playerRun.fontSize, 36)
        XCTAssertEqual(program.assetURLs[playerRun.fontAssetID]?.lastPathComponent, "PlayfairDisplay-Bold.ttf")
    }

    func testRenamingAPlayerSavesEveryStoryboardBarWithItsLook() async throws {
        let (session, fake) = try await loadedSession()

        session.updateTextContent(id: Self.playerID, content: "LEO")
        await session.save()

        let saved = try XCTUnwrap(fake.lastRequest?.textElements).compactMap(Self.object)
        let byID = Dictionary(uniqueKeysWithValues: saved.compactMap { row in row["id"]?.stringValue.map { ($0, row) } })
        let title = try XCTUnwrap(byID[Self.titleID])
        XCTAssertEqual(title["text"], .string("Match Day"))
        XCTAssertEqual(title["position"], .string("custom"))
        XCTAssertEqual(title["y_frac"], .number(0.15))
        XCTAssertEqual(title["size_px"], .number(120))
        XCTAssertEqual(title["font_family"], .string("Playfair Display"))
        XCTAssertEqual(title["source_params"], .object(["narrated_storyboard": .string("intro")]))
        let player = try XCTUnwrap(byID[Self.playerID])
        XCTAssertEqual(player["text"], .string("LEO"))
        XCTAssertEqual(player["y_frac"], .number(0.85))
        XCTAssertEqual(player["font_family"], .string("Playfair Display"))
        XCTAssertEqual(Self.object(player["source_params"])?["narrated_storyboard"],
                       .string("narrated_storyboard:placeholder:1"))
    }
}
