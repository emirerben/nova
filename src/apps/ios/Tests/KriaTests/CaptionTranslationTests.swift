import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1501: the translated caption line (docs/pipelines/dual-captions.md), model and preview compile.
///
/// Failure modes, written before the code:
///  (a) an untouched document that carries `caption_meta.translation` and cue `translation` re-encodes differently
///      (a Save without a translation edit changes bytes), or one without them grows the keys;
///  (b) an editor edit drops or rewrites the server-owned `outcome` / `dropped_cue_ids`, or an unknown newer key;
///  (c) `editor_capabilities.caption_translation = {"languages": [...]}` decodes as a closed capability (no languages),
///      so the track never shows;
///  (d) the preview draws a translated line while the server's gate is closed (no capability) or the switch is off;
///  (e) the translated line wraps, is cut at the frame edge, or overlaps the spoken block; a line too wide even at
///      24 px is drawn anyway instead of being dropped;
///  (f) the default size / stroke / colour differ from the contract (round(0.6 × spoken) clamped 28–120,
///      max(2, round(0.6 × spoken stroke)), #F5D33C);
///  (g) a moved (detached) line ignores its position or a line override, or moving it moves the spoken caption;
///  (h) an applied translation answer leaves stale lines of the previous language, or an `unmet` answer changes
///      the document;
///  (i) a route refusal (old build, feature off, same language, no captions, too long, rate limit) shows the generic
///      "couldn't translate" line instead of saying what to do.
@MainActor final class CaptionTranslationTests: XCTestCase {
    private func compiler() throws -> NativeEditorRenderCompiler {
        try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
    }

    private let capability: [String: JSONValue] = ["caption_translation": .object(["languages": .array([.string("de"), .string("en"), .string("pt"), .string("tr")])])]

    private func snapshot(cues: [[String: JSONValue]], meta: [String: JSONValue], capabilities: [String: JSONValue]? = nil) -> [String: JSONValue] {
        [
            "schema_version": .number(2), "kind": .string("editor"),
            "editor_capabilities": .object(capabilities ?? capability.merging(["caption_cues": .bool(true), "caption_meta": .bool(true)]) { a, _ in a }),
            "editor_payload": .object(["base_generation": .string("g"), "sections": .object([
                "timeline_slots": .array([.object(["clip_index": .number(0), "in_s": .number(0), "duration_s": .number(6)])]),
                "caption_cues": .array(cues.map(JSONValue.object)),
                "caption_meta": .object(meta),
            ])]),
        ]
    }

    private let cues: [[String: JSONValue]] = [
        ["id": .string("c0"), "text": .string("3 tane stratejide birleştiriyorlar."), "start_s": .number(1), "end_s": .number(3),
         "translation": .string("through three strategies.")],
        ["id": .string("c1"), "text": .string("İlki en kolayı."), "start_s": .number(3), "end_s": .number(5)],
    ]
    private var track: [String: JSONValue] {
        ["enabled": .bool(true), "language": .string("en"), "source_language": .string("tr"), "size_px": .null,
         "color": .string("#F5D33C"), "font": .null, "stroke_width": .null, "position_x_frac": .null, "position_y_frac": .null,
         "line_overrides": .object([:]), "outcome": .string("partial"), "dropped_cue_ids": .array([.string("c1")]),
         "future_key": .string("kept")]
    }

    // (a) (b) (c)
    func testRoundTripIsByteIdenticalAndServerFieldsSurviveAnEdit() throws {
        let document = EditorDocument(snapshot: snapshot(cues: cues, meta: ["enabled": .bool(true), "translation": .object(track)]))
        XCTAssertEqual(document.encodeSnapshot(), EditorDocument(snapshot: document.encodeSnapshot()).encodeSnapshot())
        let sections = document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue
        XCTAssertEqual(sections?["caption_meta"]?.objectValue?["translation"], .object(track))
        XCTAssertEqual(sections?["caption_cues"]?.arrayValue?.first?.objectValue?["translation"], .string("through three strategies."))
        XCTAssertEqual(document.capabilities["caption_translation"]?.languages, ["de", "en", "pt", "tr"])
        XCTAssertEqual(document.capabilities["caption_translation"]?.editable, true)

        let plain = EditorDocument(snapshot: snapshot(cues: [cues[1]], meta: ["enabled": .bool(true)], capabilities: [:]))
        let plainSections = plain.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue
        XCTAssertNil(plainSections?["caption_meta"]?.objectValue?["translation"])
        XCTAssertNil(plainSections?["caption_cues"]?.arrayValue?.first?.objectValue?["translation"])

        var edited = document
        var value = try XCTUnwrap(edited.captionTranslation)
        value.color = "#FFFFFF"
        value.lineOverrides["c0"] = ["position_y_frac": .number(0.2)]
        edited.captionTranslation = value
        let written = try XCTUnwrap(edited.captionMeta["translation"]?.objectValue)
        XCTAssertEqual(written["outcome"], .string("partial"))
        XCTAssertEqual(written["dropped_cue_ids"], .array([.string("c1")]))
        XCTAssertEqual(written["future_key"], .string("kept"))
        XCTAssertEqual(written["color"], .string("#FFFFFF"))
    }

    private func layers(meta: [String: JSONValue], cues: [[String: JSONValue]]? = nil, capabilities: [String: JSONValue]? = nil) throws -> [PortableTextLayer] {
        let document = EditorDocument(snapshot: snapshot(cues: cues ?? self.cues, meta: meta, capabilities: capabilities))
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 6, trimIn: 0, trimOut: 6, sourceDuration: 6, slotID: "slot")
        let items = document.captionCues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        return try compiler().compile(document: document, clips: [clip], items: items, sources: [0: source], deviceCaptions: true).recipe.textLayers
    }

    /// The block of a layer measured with the font file it was compiled with (`font-<file name>`).
    private func bounds(_ layer: PortableTextLayer) throws -> CGRect {
        let directory = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        let name = String(try XCTUnwrap(layer.runs.first).fontAssetID.dropFirst("font-".count))
        let files = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: nil)?.compactMap { $0 as? URL } ?? []
        let font = try XCTUnwrap(files.first { $0.lastPathComponent == name }, name)
        return try XCTUnwrap(AuthoredTextLayout.blockBounds(of: layer, fontURL: font))
    }

    // (d) (e) (f)
    func testAttachedLineIsOneRowUnderTheSpokenBlockWithTheContractLook() throws {
        let drawn = try layers(meta: ["enabled": .bool(true), "translation": .object(track)])
        let spoken = try XCTUnwrap(drawn.first { $0.id == "caption-c0" })
        let line = try XCTUnwrap(drawn.first { $0.id == "caption-tr-0" })
        XCTAssertNil(drawn.first { $0.id == "caption-tr-1" }, "a cue without a translation draws none")
        XCTAssertEqual(line.runs.count, 1, "one row")
        XCTAssertEqual(line.runs[0].fontSize, CaptionTranslationLook.defaultSize(spoken: 78))
        XCTAssertEqual(line.runs[0].fontSize, 47)
        XCTAssertEqual(line.runs[0].strokeWidth, 2 * 2, "max(2, round(0.6 × 4)) drawn as a double-width stroke run")
        XCTAssertEqual(line.runs[0].fill, TextInk(red: 0xF5 / 255.0, green: 0xD3 / 255.0, blue: 0x3C / 255.0, alpha: 1))
        XCTAssertNil(line.karaoke)
        XCTAssertEqual(line.start, spoken.start); XCTAssertEqual(line.end, spoken.end)
        let spokenBox = try bounds(spoken), lineBox = try bounds(line)
        XCTAssertGreaterThanOrEqual(lineBox.minY, spokenBox.maxY + 0.25 * 47 - 0.5, "attached under the spoken ink with a 0.25 × size gap")
        XCTAssertLessThanOrEqual(lineBox.maxY, 0.90 * 1920)

        XCTAssertNil(try layers(meta: ["enabled": .bool(true), "translation": .object(track)], capabilities: ["caption_cues": .bool(true)])
            .first { $0.id.hasPrefix("caption-tr-") }, "gate closed: no translated line")
        var off = track; off["enabled"] = .bool(false)
        XCTAssertNil(try layers(meta: ["enabled": .bool(true), "translation": .object(off)]).first { $0.id.hasPrefix("caption-tr-") })
    }

    // (e)
    func testAWideLineShrinksToFitAndOneTooWideAtTheFloorIsDropped() throws {
        var wide = cues
        wide[0]["translation"] = .string("Sie verbinden das in drei Strategien, die kaum jemand nutzt.")
        let line = try XCTUnwrap(try layers(meta: ["enabled": .bool(true), "translation": .object(track)], cues: wide).first { $0.id == "caption-tr-0" })
        let box = try bounds(line)
        XCTAssertEqual(line.runs.count, 1)
        XCTAssertLessThan(line.runs[0].fontSize, 47)
        XCTAssertGreaterThanOrEqual(line.runs[0].fontSize, 24)
        XCTAssertLessThanOrEqual(box.width, 0.9 * 1080 + 0.5)
        XCTAssertGreaterThanOrEqual(box.minX, 0.05 * 1080 - 0.5); XCTAssertLessThanOrEqual(box.maxX, 0.95 * 1080 + 0.5)

        wide[0]["translation"] = .string(String(repeating: "Strategien ", count: 12))
        XCTAssertNil(try layers(meta: ["enabled": .bool(true), "translation": .object(track)], cues: wide).first { $0.id.hasPrefix("caption-tr-") },
                     "too wide even at 24 px: dropped, never wrapped or cut")
    }

    // (g)
    func testDetachedPositionAndLineOverrideMoveOnlyTheTranslatedLine() throws {
        let before = try layers(meta: ["enabled": .bool(true), "translation": .object(track)])
        var moved = track
        moved["position_y_frac"] = .number(0.2)
        moved["line_overrides"] = .object(["c0": .object(["position_x_frac": .number(0.4), "size_px": .number(60)])])
        let after = try layers(meta: ["enabled": .bool(true), "translation": .object(moved)])
        XCTAssertEqual(before.first { $0.id == "caption-c0" }, after.first { $0.id == "caption-c0" }, "the spoken caption never moves")
        let line = try XCTUnwrap(after.first { $0.id == "caption-tr-0" })
        XCTAssertEqual(line.runs[0].fontSize, 60)
        XCTAssertEqual(line.anchorY, 0.2 * 1920, accuracy: 0.5)
        XCTAssertEqual(line.anchorX, 0.4 * 1080, accuracy: 0.5)
    }

    // (d) (e): every fixture pair the UI test shoots compiles, with the session's own timeline items, to a spoken and
    // a translated layer for each cue.
    func testEveryFixturePairCompilesBothLines() throws {
        let languages = ["tr", "de", "en", "pt"]
        for spoken in languages {
            for translated in languages where translated != spoken {
                let session = NativeEditorSession(draft: NativeEditorUITestFixtures.dualCaptions(arguments: ["-ui-testing-editor-dual-pair-\(spoken)-\(translated)"]),
                                                  operations: LocalEditorOperations())
                let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
                    asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 6),
                    url: URL(fileURLWithPath: "/fixture/original.mp4"))
                let clips = session.timelineClips.map { clip -> EditorClip in var clip = clip; clip.sourceClipIndex = 0; return clip }
                let layers = try compiler().compile(document: session.document, clips: clips, items: session.timelineItems,
                                                    sources: [0: source], deviceCaptions: true).recipe.textLayers
                let ids = layers.map(\.id)
                for layer in layers where layer.id.hasPrefix("caption-tr-") {
                    let box = try bounds(layer)
                    XCTAssertEqual(layer.runs.count, 1)
                    XCTAssertLessThanOrEqual(box.width + layer.runs[0].strokeWidth / 2, 0.9 * 1080 + 0.5, "\(spoken)-\(translated) \(layer.id) size \(layer.runs[0].fontSize)")
                    XCTAssertGreaterThanOrEqual(box.minX, 0.05 * 1080 - 1, "\(spoken)-\(translated) \(layer.id)")
                    XCTAssertLessThanOrEqual(box.maxX, 0.95 * 1080 + 1, "\(spoken)-\(translated) \(layer.id)")
                    print("PAIR \(spoken)-\(translated) \(layer.id) size=\(layer.runs[0].fontSize) box=\(box)")
                }
                XCTAssertTrue(ids.contains("caption-tr-0") && ids.contains("caption-tr-1"), "\(spoken)-\(translated): \(ids)")
                XCTAssertTrue(ids.contains("caption-native-caption-0"), "\(spoken)-\(translated): \(ids)")
            }
        }
    }

    // (i) every refusal the route can answer reads as its own honest message, never the generic "unmet" line.
    func testRouteRefusalsMapToTheirOwnReasons() {
        func refusal(_ status: Int, _ code: String?) -> CaptionTranslationFailure {
            CaptionTranslationFailure.from(APIError.requestFailed(status: status, detail: RequestFailureDetail(nil,
                object: code.map { ["code": .string($0)] })))
        }
        XCTAssertEqual(refusal(426, "client_update_required"), .updateRequired)
        XCTAssertEqual(refusal(422, "caption_translation_disabled"), .disabled)
        XCTAssertEqual(refusal(422, "caption_translation_same_language"), .sameLanguage)
        XCTAssertEqual(refusal(422, "caption_translation_no_captions"), .noCaptions)
        XCTAssertEqual(refusal(422, "caption_translation_too_long"), .tooLong)
        XCTAssertEqual(refusal(429, nil), .rateLimited)
        XCTAssertEqual(refusal(500, nil), .unmet)
        XCTAssertEqual(CaptionTranslationFailure.from(APIError.offline), .offline)
    }

    // (h)
    func testApplyingAnAnswerReplacesLinesAndAnUnmetAnswerChangesNothing() throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.dualCaptions(arguments: ["-ui-testing-editor-dual-pair-tr-en"]),
                                          operations: LocalEditorOperations())
        XCTAssertTrue(session.offersCaptionTranslation)
        XCTAssertEqual(session.captionTranslationLanguages, ["de", "en", "pt"])
        let before = session.document
        let unmet = CaptionTranslationResponse(language: "de", sourceLanguage: "tr", outcome: "unmet",
            translations: before.captionCues.map { .init(id: $0.id, text: nil) }, droppedCueIDs: before.captionCues.map(\.id))
        XCTAssertEqual(session.applyCaptionTranslation(unmet, requested: "de"), .failed(language: "de"))
        XCTAssertEqual(session.document, before)

        let ids = before.captionCues.map(\.id)
        let partial = CaptionTranslationResponse(language: "de", sourceLanguage: "tr", outcome: "partial",
            translations: [.init(id: ids[0], text: "Sie verbinden das."), .init(id: ids[1], text: nil)], droppedCueIDs: [ids[1]])
        XCTAssertEqual(session.applyCaptionTranslation(partial, requested: "de"), .finished(language: "de", dropped: 1))
        XCTAssertEqual(session.document.captionCues.map(\.translation), ["Sie verbinden das.", nil], "no stale English line is left")
        XCTAssertEqual(session.document.captionTranslation?.language, "de")
        XCTAssertEqual(session.document.captionTranslation?.droppedCueIDs, [ids[1]])
        XCTAssertEqual(session.document.captionTranslation?.outcome, "partial")
    }
}
