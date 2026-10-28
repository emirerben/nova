import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1007: caption In / Out / Loop + speed, for one line or for all captions.
///
/// Failure modes, written before the code:
///  (a) a line's own phases lose to the all-captions default, or the default misses lines without their own;
///  (b) "All captions" leaves a line override behind, so lines that look alike in the picker draw differently;
///  (c) unset phases change today's preview (cue pop-in, karaoke sweep, static text-lane lines) or the Save payload;
///  (d) a word-highlight line drops its sweep when phases are set;
///  (e) a text-lane (guided / song) line animates per word row instead of as one line, or a row's own phases
///      do not govern its displayed line, or "none" phases draw differently from unset;
///  (f) a build without `editor_capabilities.caption_animation` writes phases;
///  (g) an "All captions" change takes more than one Undo, or does not reach Save as the default plus override-free rows;
///  (h) the stored default does not come back on reload (cue-native variants store it as `caption_animation_phases`).
@MainActor final class CaptionAnimationTests: XCTestCase {
    private let fade: [String: JSONValue] = ["entrance": .string("fade"), "exit": .string("fade"), "loop": .string("pulse"), "speed": .number(1.5)]
    private let still: [String: JSONValue] = ["entrance": .string("none"), "exit": .string("none"), "loop": .string("none"), "speed": .number(1)]
    private let popBounce: [String: JSONValue] = ["entrance": .string("pop"), "exit": .string("none"), "loop": .string("bounce"), "speed": .number(1)]

    private func compiler() throws -> NativeEditorRenderCompiler {
        try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
    }

    private func source(duration: Double) -> (ResolvedEditorSource, EditorClip) {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: duration),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
            trimIn: 0, trimOut: duration, sourceDuration: duration, slotID: "slot")
        return (source, clip)
    }

    private func phases(_ value: [String: JSONValue]) -> TextAnimationPhases? { NativeEditorRenderCompiler.captionPhases(.object(value)) }

    // MARK: Cue-native captions (narrated, subtitled)

    private func cueLayers(meta: [String: JSONValue], cuePhases: [JSONValue?]) throws -> [PortableTextLayer] {
        let (source, clip) = source(duration: 4)
        let cues = cuePhases.enumerated().map { index, value in
            EditorCaptionCue(id: "cue-\(index)", startS: Double(index), endS: Double(index + 1), text: "Line \(index) words",
                             raw: value.map { ["animation_phases": $0] } ?? [:])
        }
        let items = cues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        let document = EditorDocument(editFormat: "subtitled", clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
                                      captionMeta: meta, captionCues: cues)
        return try compiler().compile(document: document, clips: [clip], items: items, sources: [0: source], deviceCaptions: true).recipe.textLayers
    }

    func testALineOverrideBeatsTheDefaultAndOtherLinesTakeTheDefault() throws {
        let layers = try cueLayers(meta: ["style": .string("sentence"), "animation_phases": .object(fade)],
                                   cuePhases: [.object(still), nil])
        XCTAssertEqual(layers.map(\.animationPhases), [phases(still), phases(fade)])
        XCTAssertEqual(layers[0].effect, .none, "None on a line is a choice: no pop and no default fade")
    }

    func testUnsetPhasesKeepTodaysPopInAndAnInvalidRowFallsBackToTheDefault() throws {
        let unset = try cueLayers(meta: ["style": .string("sentence")], cuePhases: [nil, nil])
        XCTAssertEqual(unset.map(\.effect), [.captionPop, .captionPop])
        XCTAssertTrue(unset.allSatisfy { $0.animationPhases == nil })
        var bad = fade
        bad["speed"] = .number(9)
        let fallback = try cueLayers(meta: ["style": .string("sentence"), "animation_phases": .object(popBounce)], cuePhases: [.object(bad)])
        XCTAssertEqual(fallback.first?.animationPhases, phases(popBounce))
    }

    func testWordHighlightKeepsItsSweepAndThePhasesMoveTheWholeLine() throws {
        for style in ["word", "sentence"] {
            let layers = try cueLayers(meta: ["style": .string(style), "animation_phases": .object(fade),
                                              "appearance": .object(["highlight_spoken_word": .bool(true)])], cuePhases: [nil])
            XCTAssertFalse(layers.isEmpty)
            XCTAssertTrue(layers.allSatisfy { $0.effect == .karaokeLine && $0.karaoke != nil }, style)
            XCTAssertTrue(layers.allSatisfy { $0.animationPhases == phases(fade) }, style)
        }
    }

    // MARK: Text-lane captions (guided, song, montage)

    private func row(_ id: String, _ text: String, _ start: Double, phases: [String: JSONValue]? = nil) -> EditorTextElement {
        var raw: [String: JSONValue] = ["source_params": .object(["source": .string("caption_cue")])]
        if let phases { raw["animation_phases"] = .object(phases) }
        return EditorTextElement(id: id, text: text, startS: start, endS: start + 0.5, role: "generative_narration_caption", raw: raw)
    }

    private func laneLayers(_ rows: [EditorTextElement], meta: [String: JSONValue]) throws -> [String: PortableTextLayer] {
        let (source, clip) = source(duration: 4)
        let items = rows.map { NativeEditorTimelineItem(selection: .init(kind: .text, id: $0.id), start: $0.startS, end: $0.endS) }
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)], textElements: rows, captionMeta: meta)
        let layers = try compiler().compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe.textLayers
        return Dictionary(uniqueKeysWithValues: layers.map { ($0.id, $0) })
    }

    private var sentenceRows: [EditorTextElement] {
        [row("a", "We", 0), row("b", "went", 0.5), row("c", "out.", 1), row("d", "So", 1.5), row("e", "good.", 2)]
    }

    func testATextLaneLineEntersOnItsFirstRowExitsOnItsLastAndLoopsThroughout() throws {
        let layers = try laneLayers(sentenceRows, meta: ["style": .string("sentence"), "animation_phases": .object(fade)])
        func edges(_ id: String) -> [String] {
            guard let value = layers[id]?.animationPhases else { return [] }
            return [value.entrance.rawValue, value.exit.rawValue, value.loop.rawValue]
        }
        XCTAssertEqual(edges("a"), ["fade", "none", "pulse"])
        XCTAssertEqual(edges("b"), ["none", "none", "pulse"])
        XCTAssertEqual(edges("c"), ["none", "fade", "pulse"])
        XCTAssertEqual(edges("d"), ["fade", "none", "pulse"])
        XCTAssertEqual(edges("e"), ["none", "fade", "pulse"])
    }

    func testARowsOwnPhasesGovernItsWholeLineAndNoneMatchesUnset() throws {
        var rows = sentenceRows
        rows[4] = row("e", "good.", 2, phases: popBounce)
        let layers = try laneLayers(rows, meta: ["style": .string("sentence"), "animation_phases": .object(fade)])
        XCTAssertEqual(layers["d"]?.animationPhases?.entrance, .pop, "the line's governing row is on its last word")
        XCTAssertEqual(layers["d"]?.animationPhases?.loop, .bounce)
        XCTAssertEqual(layers["a"]?.animationPhases?.loop, .pulse, "the other line keeps the default")

        let unset = try laneLayers(sentenceRows, meta: ["style": .string("sentence")])
        let none = try laneLayers(sentenceRows, meta: ["style": .string("sentence"), "animation_phases": .object(still)])
        XCTAssertEqual(none, unset)
        XCTAssertTrue(unset.values.allSatisfy { $0.animationPhases == nil })
    }

    // MARK: Session: scope, capability, undo, Save, reload

    private func loaded(capability: Bool = true, storedDefault: [String: JSONValue]? = nil,
                        storedOverride: [String: JSONValue]? = nil) async -> (NativeEditorSession, EditorCommitSpy) {
        var capabilities: [String: JSONValue] = [
            "caption_cues": .object(["editable": .bool(true)]),
            "caption_meta": .object(["editable": .bool(true)]),
        ]
        if capability { capabilities["caption_animation"] = .bool(true) }
        var variant: [String: JSONValue] = [
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "caption_cues": .array([
                .object(["id": .string("cue-1"), "text": .string("Simit ve cay"), "start_s": .number(0), "end_s": .number(1)]),
                .object(["id": .string("cue-2"), "text": .string("Fiyatlar uygun"), "start_s": .number(1), "end_s": .number(2)]
                    .merging(storedOverride.map { ["animation_phases": .object($0)] } ?? [:]) { _, new in new }),
            ]),
            "editor_capabilities": .object(capabilities),
        ]
        if let storedDefault { variant["caption_animation_phases"] = .object(storedDefault) }
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h",
                                         etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:],
                                         canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: false, captionMeta: true, timeline: false, mix: false, captionCues: true),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [],
                                                              captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    func testWithoutTheCapabilityNothingIsWritten() async {
        let (session, _) = await loaded(capability: false)
        let before = session.document
        session.setCaptionPhase(.all, phase: "entrance", effect: "fade")
        session.setCaptionPhase(.line("cue-1"), phase: "loop", effect: "pulse")
        session.setCaptionAnimationSpeed(.all, speed: 2)
        XCTAssertEqual(session.document, before)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testAllCaptionsClearsLineOverridesInOneUndoStepAndSavesBoth() async throws {
        let (session, fake) = await loaded(storedOverride: popBounce)
        XCTAssertEqual(session.captionAnimationPhases(.all)["entrance"], .string("pop"), "unset cue captions pop in today")
        XCTAssertEqual(session.captionAnimationPhases(.line("cue-2")), popBounce)
        session.setCaptionPhase(.line("cue-1"), phase: "loop", effect: "float")
        XCTAssertEqual(session.captionAnimationPhases(.line("cue-1"))["loop"], .string("float"))
        let undoBefore = session.undoHistoryCount
        session.setCaptionPhase(.all, phase: "entrance", effect: "fade")
        XCTAssertEqual(session.undoHistoryCount, undoBefore + 1)
        XCTAssertTrue(session.document.captionCues.allSatisfy { $0.raw["animation_phases"] == nil })
        for line in ["cue-1", "cue-2"] {
            XCTAssertEqual(session.captionAnimationPhases(.line(line))["entrance"], .string("fade"))
            XCTAssertEqual(session.captionAnimationPhases(.line(line))["loop"], .string("none"), "the default replaces the line's own")
        }

        session.undo()
        XCTAssertEqual(session.captionAnimationPhases(.line("cue-2")), popBounce)
        XCTAssertNil(session.document.captionMeta["animation_phases"])
        session.redo()

        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertEqual(request.captionMeta?["animation_phases"]?.objectValue?["entrance"], .string("fade"))
        let cues = try XCTUnwrap(request.captionCues, "the stored override is cleared on the server too")
        XCTAssertEqual(cues.count, 2)
        XCTAssertTrue(cues.allSatisfy { $0.objectValue?["animation_phases"] == nil })
    }

    func testUnsetSavesCarryNoPhasesAndTheStoredDefaultReloads() async throws {
        let (plain, fake) = await loaded()
        plain.setCaptionSize(90)
        await plain.save()
        let wire = String(decoding: try JSONEncoder().encode(try XCTUnwrap(fake.lastRequest)), as: UTF8.self)
        XCTAssertFalse(wire.contains("animation_phases"))

        let (reloaded, _) = await loaded(storedDefault: popBounce)
        XCTAssertEqual(reloaded.captionAnimationPhases(.all), popBounce)
        XCTAssertEqual(reloaded.captionAnimationPhases(.line("cue-1")), popBounce)
        XCTAssertFalse(reloaded.hasUnsavedChanges)
    }

    func testATextLaneLineChangeLeavesOnlyThatRowsPhasesOnItsLine() async throws {
        let threadID = UUID()
        func wire(_ element: EditorTextElement) -> JSONValue {
            .object(element.raw.merging(["id": .string(element.id), "text": .string(element.text),
                                          "start_s": .number(element.startS), "end_s": .number(element.endS)]) { _, new in new })
        }
        var rows = sentenceRows
        rows[0] = row("a", "We", 0, phases: popBounce)
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h",
                                         etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:],
                                         canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "text_elements": .array(rows.map(wire)),
                "caption_meta": .object(["enabled": .bool(true), "style": .string("sentence")]),
                "editor_capabilities": .object(["text_elements": .bool(true), "caption_meta": .object(["editable": .bool(true)]),
                                                "caption_animation": .bool(true)]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [],
                                                              captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertEqual(session.captionAnimationPhases(.line("c")), popBounce, "row a governs the line c sits on")
        XCTAssertEqual(session.captionAnimationPhases(.all)["entrance"], .string("none"), "text-lane captions hold still today")

        session.setCaptionPhase(.line("c"), phase: "loop", effect: "float")
        let stored = Dictionary(uniqueKeysWithValues: session.document.textElements.map { ($0.id, $0.raw["animation_phases"]) })
        XCTAssertNil(stored["a"] ?? nil)
        XCTAssertEqual(stored["c"]??.objectValue?["loop"], .string("float"))
        XCTAssertEqual(stored["c"]??.objectValue?["entrance"], .string("pop"), "the line's look carries over to its new owner")
        XCTAssertNil(stored["d"] ?? nil, "the next line is untouched")
        XCTAssertEqual(session.captionAnimationPhases(.line("a"))["loop"], .string("float"))
    }
}
