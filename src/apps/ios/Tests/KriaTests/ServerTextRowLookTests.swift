import XCTest
import KriaMediaEngine
@testable import Kria

/// Server text rows often name a preset `position` and carry no `y_frac` or
/// `font_family` (guided narration labels, older guided titles/thoughts,
/// voiceover intros). Both burns, cloud and the server-compiled phone recipe,
/// place such a row at `text_overlay._POSITION_Y` (top 0.15, middle 0.45,
/// bottom 0.85), ignore any fracs on it, and draw it in the registry's display
/// face, Playfair Display.
///
/// The editor load used to rewrite every row with y 0.5 (or its fracs) and
/// "Fraunces", so the preview drew the text mid-frame in the wrong face and any
/// text Save changed the burned face. The preview compiler also mapped the
/// presets to 0.2/0.8 and defaulted to Inter.
@MainActor final class ServerTextRowLookTests: XCTestCase {
    /// A guided narration label intro (`narration_labels.py`, `exclude_none`).
    private static let introJSON = """
    {"id": "4c1f0e1a9b2d4c3e8f7a6b5c4d3e2f10", "text": "Match Day", "start_s": 0.0, "end_s": 2.0,
     "role": "generative_intro", "position": "top", "size_class": "large", "alignment": "center",
     "effect": "fade-in", "source_params": {"narration_label_kind": "intro"}}
    """
    /// A participant label: custom placement, still no face.
    private static let participantJSON = """
    {"id": "9a8b7c6d5e4f40312a1b2c3d4e5f6a7b", "text": "LEO", "start_s": 0.5, "end_s": 2.5,
     "role": "generative_sequence", "position": "custom", "x_frac": 0.08, "y_frac": 0.76,
     "size_class": "small", "alignment": "left", "effect": "static",
     "source_params": {"narration_label_kind": "participant"}}
    """
    /// A named row an older app build already saved with its invented look.
    private static let staleJSON = """
    {"id": "guided-thought-chapter-2", "text": "post match pub", "start_s": 1.0, "end_s": 3.0,
     "role": "generative_intro", "position": "bottom", "x_frac": 0.5, "y_frac": 0.5,
     "font_family": "Fraunces", "size_px": 60.0, "alignment": "center", "effect": "fade-in"}
    """
    private static let introID = "4c1f0e1a9b2d4c3e8f7a6b5c4d3e2f10"
    private static let participantID = "9a8b7c6d5e4f40312a1b2c3d4e5f6a7b"
    private static let staleID = "guided-thought-chapter-2"

    private static func row(_ json: String) throws -> [String: JSONValue] {
        try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: Data(json.utf8)).objectValue)
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue]? {
        if case let .object(value) = value { value } else { nil }
    }

    private func served() throws -> [[String: JSONValue]] {
        [try Self.row(Self.introJSON), try Self.row(Self.participantJSON), try Self.row(Self.staleJSON)]
    }

    /// The variant opened in the editor through the real load path.
    private func loadedSession() async throws -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID(); let jobID = UUID()
        let rows: JSONValue = .array(try served().map(JSONValue.object))
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"), "sections": .object(["text_elements": rows]),
        ])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "guided", draftRevision: 1, snapshotHash: "h",
                etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "variant_id": .string("guided"), "render_generation_id": .string("g1"),
                "editor_capabilities": .object(["text_elements": .bool(true)]), "text_elements": rows,
            ],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true,
                captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [],
            captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    private func byID(_ rows: [[String: JSONValue]]) -> [String: [String: JSONValue]] {
        Dictionary(uniqueKeysWithValues: rows.compactMap { row in row["id"]?.stringValue.map { ($0, row) } })
    }

    func testLoadKeepsEveryServerRowExactlyAsSent() async throws {
        let (session, _) = try await loadedSession()
        let loaded = byID(session.document.textElements.map(\.raw))
        for row in try served() {
            XCTAssertEqual(loaded[try XCTUnwrap(row["id"]?.stringValue)], row)
        }
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// The phone's export recipe is compiled on the server with the cloud's
    /// placement and face, so one preview check covers both destinations.
    func testPreviewDrawsServerRowsWhereAndHowTheVideoBurnsThem() async throws {
        let (session, _) = try await loadedSession()
        let elements = session.document.textElements
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let items = elements.map { NativeEditorTimelineItem(selection: .init(kind: .text, id: $0.id), start: $0.startS, end: $0.endS) }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: EditorDocument(textElements: elements), clips: [clip], items: items, sources: [0: source])
        let layers = Dictionary(uniqueKeysWithValues: program.recipe.textLayers.map { ($0.id, $0) })
        func face(_ layer: PortableTextLayer) throws -> String? {
            program.assetURLs[try XCTUnwrap(layer.runs.first).fontAssetID]?.lastPathComponent
        }

        let intro = try XCTUnwrap(layers[Self.introID])
        XCTAssertEqual(intro.anchorX, 0.5 * 1080, accuracy: 0.001)
        XCTAssertEqual(intro.anchorY, 0.15 * 1920, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(intro.runs.first).fontSize, 120)
        XCTAssertEqual(try face(intro), "PlayfairDisplay-Bold.ttf")

        let participant = try XCTUnwrap(layers[Self.participantID])
        XCTAssertEqual(participant.anchorX, 0.08 * 1080, accuracy: 0.001)
        XCTAssertEqual(participant.anchorY, 0.76 * 1920, accuracy: 0.001)
        XCTAssertEqual(try face(participant), "PlayfairDisplay-Bold.ttf")

        // The burn ignores the stale fracs on a named row and keeps its saved face.
        let stale = try XCTUnwrap(layers[Self.staleID])
        XCTAssertEqual(stale.anchorX, 0.5 * 1080, accuracy: 0.001)
        XCTAssertEqual(stale.anchorY, 0.85 * 1920, accuracy: 0.001)
        XCTAssertEqual(try face(stale), "Fraunces-Bold.ttf")
    }

    func testTextSaveSendsUntouchedRowsAsServedAndOnlyTheEdit() async throws {
        let (session, fake) = try await loadedSession()

        session.updateTextContent(id: Self.participantID, content: "MESSI")
        await session.save()

        let saved = byID(try XCTUnwrap(fake.lastRequest?.textElements).compactMap(Self.object))
        let served = byID(try served())
        XCTAssertEqual(saved[Self.introID], served[Self.introID])
        XCTAssertEqual(saved[Self.staleID], served[Self.staleID])
        var participant = try XCTUnwrap(served[Self.participantID])
        participant["text"] = .string("MESSI"); participant["wrap_lines"] = .bool(false)
        XCTAssertEqual(saved[Self.participantID], participant)
    }

    /// `addText` still edits through the legacy `EditorDraft` projection, which
    /// round-trips every row; the rows it did not touch must come back as sent.
    func testLegacyDraftEditKeepsOtherRowsAsServed() async throws {
        let (session, _) = try await loadedSession()

        session.addText(content: "New line")

        let rows = byID(session.document.textElements.map(\.raw))
        for row in try served() {
            XCTAssertEqual(rows[try XCTUnwrap(row["id"]?.stringValue)], row)
        }
        let added = try XCTUnwrap(session.document.textElements.first { $0.text == "New line" }?.raw)
        XCTAssertEqual(added["position"], .string("custom"))
        XCTAssertEqual(added["x_frac"], .number(0.5))
        XCTAssertEqual(added["y_frac"], .number(0.5))
        XCTAssertEqual(added["font_family"], .string("Fraunces"))
    }

    /// A drag starts from where the row burns and makes it "custom", so the
    /// burn honours the new spot instead of the preset.
    func testMovingANamedRowStartsAtItsPresetAndBecomesCustom() async throws {
        let (session, _) = try await loadedSession()
        let stale = try XCTUnwrap(session.document.textElements.first { $0.id == Self.staleID })
        XCTAssertEqual(stale.anchor, CGPoint(x: 0.5, y: 0.85))
        let intro = try XCTUnwrap(session.document.textElements.first { $0.id == Self.introID })
        XCTAssertEqual(intro.anchor, CGPoint(x: 0.5, y: 0.15))
        // The Text panel's position steppers start from the same spot.
        XCTAssertEqual(session.textAnchor(id: Self.staleID), CGPoint(x: 0.5, y: 0.85))
        XCTAssertNil(session.textAnchor(id: "missing"))

        session.setTextPosition(id: Self.introID, x: intro.anchor.x, y: intro.anchor.y + 0.05)

        let moved = try XCTUnwrap(session.document.textElements.first { $0.id == Self.introID }?.raw)
        XCTAssertEqual(moved["position"], .string("custom"))
        XCTAssertEqual(moved["x_frac"], .number(0.5))
        XCTAssertEqual(try XCTUnwrap(moved["y_frac"]?.numberValue), 0.2, accuracy: 0.000_001)
        XCTAssertNil(moved["font_family"])
    }

    /// The Text panel's position steppers move one axis; the other stays where
    /// the row burns (its preset), not at the stale fracs an old build saved.
    func testMovingOneAxisKeepsTheOtherAtTheBurnedSpot() async throws {
        let (session, _) = try await loadedSession()

        session.moveText(id: Self.staleID, x: 0.6)

        let row = try XCTUnwrap(session.textElement(id: Self.staleID)?.raw)
        XCTAssertEqual(row["position"], .string("custom"))
        XCTAssertEqual(row["x_frac"], .number(0.6))
        XCTAssertEqual(row["y_frac"], .number(0.85))
    }

    /// A legacy `EditorDraft` change to a served row writes back only the field
    /// it changed: a move makes the row custom without inventing a face, and a
    /// face change keeps the named preset.
    func testDraftEditWritesOnlyTheChangedFieldOfAServedRow() throws {
        let rowID = UUID()
        let served: [String: JSONValue] = [
            "id": .string(rowID.uuidString), "text": .string("Title"), "start_s": .number(0), "end_s": .number(2),
            "role": .string("generative_intro"), "position": .string("top"), "size_class": .string("large"),
        ]
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object(["text_elements": .array([.object(served)])])])]
        func saved(position: CGPoint, style: String) throws -> [String: JSONValue] {
            let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: rowID, content: "Title", position: position, style: style)],
                captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot)
            let sections = Self.object(Self.object(draft.persistedSnapshot()["editor_payload"])?["sections"])
            guard case let .array(rows)? = sections?["text_elements"] else { XCTFail("no text_elements"); return [:] }
            return try XCTUnwrap(rows.first.flatMap(Self.object))
        }

        XCTAssertEqual(try saved(position: CGPoint(x: 0.5, y: 0.15), style: "Playfair Display"), served)

        let moved = try saved(position: CGPoint(x: 0.3, y: 0.6), style: "Playfair Display")
        XCTAssertEqual(moved["position"], .string("custom"))
        XCTAssertEqual(moved["x_frac"], .number(0.3))
        XCTAssertEqual(moved["y_frac"], .number(0.6))
        XCTAssertNil(moved["font_family"])

        let restyled = try saved(position: CGPoint(x: 0.5, y: 0.15), style: "Inter")
        XCTAssertEqual(restyled["position"], .string("top"))
        XCTAssertNil(restyled["x_frac"]); XCTAssertNil(restyled["y_frac"])
        XCTAssertEqual(restyled["font_family"], .string("Inter"))
    }

    /// An empty or blank name burns in the default face, as on the server; an
    /// unknown name would otherwise fail the whole preview compile.
    func testEmptyFontNameMeansTheDefaultFace() {
        for raw: [String: JSONValue] in [["font_family": .string("")], ["font_family": .string("  ")], ["font_family": .null], [:]] {
            XCTAssertEqual(EditorTextElement.fontFamily(of: raw), EditorTextElement.defaultFontFamily, "\(raw)")
        }
        XCTAssertEqual(EditorTextElement.fontFamily(of: ["font_family": .string("Inter")]), "Inter")
    }

    func testAnchorMatchesTheServerPresetTable() {
        let cases: [([String: JSONValue], CGPoint)] = [
            (["position": .string("top")], CGPoint(x: 0.5, y: 0.15)),
            (["position": .string("middle"), "y_frac": .number(0.9)], CGPoint(x: 0.5, y: 0.45)),
            (["position": .string("bottom"), "x_frac": .number(0.1)], CGPoint(x: 0.5, y: 0.85)),
            (["position": .string("custom"), "x_frac": .number(0.2), "y_frac": .number(0.3)], CGPoint(x: 0.2, y: 0.3)),
            (["position": .string("custom")], CGPoint(x: 0.5, y: 0.45)),
        ]
        for (raw, expected) in cases { XCTAssertEqual(EditorTextElement.anchor(of: raw), expected, "\(raw)") }
    }
}
