import XCTest
import KriaMediaEngine
import AVFoundation
import CoreImage
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
///  (h) the stored default does not come back on reload (cue-native variants store it as `caption_animation_phases`);
///  (i) an entrance stored by the older Style > Text motion (`appearance.animation_phases`) stops drawing, is not the
///      shown default, or survives an All captions change beside the new default;
///  (j) Display Word animates only a line's first and last word instead of every word, or switching Display (or
///      turning the highlight on) resets the chosen animation to None.
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

    private func cueLayers(meta: [String: JSONValue], cuePhases: [JSONValue?], cueStyles: [[String: JSONValue]] = []) throws -> [PortableTextLayer] {
        let (source, clip) = source(duration: 4)
        let cues = cuePhases.enumerated().map { index, value in
            EditorCaptionCue(id: "cue-\(index)", startS: Double(index), endS: Double(index + 1), text: "Line \(index) words",
                             raw: (value.map { ["animation_phases": $0] } ?? [:]).merging(cueStyles.indices.contains(index) ? cueStyles[index] : [:]) { _, value in value })
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
        XCTAssertEqual(layers["b"]?.runs.map(\.text).joined(separator: " "), "We went out.", "each row still shows its whole sentence")
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

    private let silk: [String: JSONValue] = ["entrance": .string("silkReveal"), "exit": .string("none"), "loop": .string("none"), "speed": .number(1)]

    func testATextMotionEntranceDrawsEachLineAsOneRowUntilTheAllCaptionsDefaultReplacesIt() throws {
        let legacy: [String: JSONValue] = ["style": .string("sentence"), "appearance": .object(["animation_phases": .object(silk)])]
        let layers = try laneLayers(sentenceRows, meta: legacy)
        XCTAssertEqual(Set(layers.keys), ["a", "d"], "one row per sentence, as `guided_caption_presentation` draws it")
        XCTAssertEqual(layers["a"]?.animationPhases, phases(silk))
        XCTAssertEqual(layers["a"]?.runs.map(\.text).joined(separator: " "), "We went out.")

        var both = legacy
        both["animation_phases"] = .object(fade)
        let replaced = try laneLayers(sentenceRows, meta: both)
        XCTAssertEqual(replaced.count, 5, "the all-captions default wins and animates each line across its rows")
        XCTAssertEqual(replaced["a"]?.animationPhases?.entrance, .fade)

        let cue = try cueLayers(meta: legacy, cuePhases: [nil])
        XCTAssertEqual(cue.first?.animationPhases, phases(silk))
    }

    func testWordDisplayAnimatesEveryTextLaneWord() throws {
        let layers = try laneLayers(sentenceRows, meta: ["style": .string("word"), "animation_phases": .object(fade)])
        XCTAssertEqual(layers.count, 5)
        for (id, layer) in layers {
            XCTAssertEqual(layer.animationPhases, phases(fade), "word \(id) enters, exits and loops on its own")
        }
    }

    // MARK: Session: scope, capability, undo, Save, reload

    private func loaded(capability: Bool = true, storedDefault: [String: JSONValue]? = nil,
                        storedOverride: [String: JSONValue]? = nil,
                        storedTextMotion: [String: JSONValue]? = nil,
                        storedAppearance: [String: JSONValue]? = nil,
                        storedSize: Double? = nil,
                        storedCueOverrides: [String: [String: JSONValue]] = [:]) async -> (NativeEditorSession, EditorCommitSpy) {
        var capabilities: [String: JSONValue] = [
            "caption_cues": .object(["editable": .bool(true)]),
            "caption_meta": .object(["editable": .bool(true)]),
            "caption_editor_style": .bool(true),
        ]
        if capability {
            capabilities["caption_animation"] = .bool(true)
            capabilities["caption_style_scope"] = .bool(true)
        }
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
        if let storedTextMotion { variant["caption_editor_style"] = .object(["animation_phases": .object(storedTextMotion)]) }
        if let storedAppearance { variant["caption_editor_style"] = .object(storedAppearance) }
        if let storedSize { variant["caption_size_px"] = .number(storedSize) }
        if let cues = variant["caption_cues"]?.arrayValue {
            variant["caption_cues"] = .array(cues.map { value in
                guard var row = value.objectValue, let id = row["id"]?.stringValue else { return value }
                row.merge(storedCueOverrides[id] ?? [:]) { _, new in new }
                return .object(row)
            })
        }
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
        session.setCaptionStyleSize(.line("cue-1"), size: 150)
        session.placeCaptions(.all, at: .bottom)
        session.setCaptionPhase(.all, phase: "entrance", effect: "fade")
        session.setCaptionPhase(.line("cue-1"), phase: "loop", effect: "pulse")
        session.setCaptionAnimationSpeed(.all, speed: 2)
        XCTAssertEqual(session.document, before)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testCaptionSizeScopeReplacesSizeOverridesThroughUndoSaveAndReopen() async throws {
        let (session, fake) = await loaded()
        session.setCaptionStyleSize(.line("cue-1"), size: 150)
        let undoBefore = session.undoHistoryCount
        session.setCaptionStyleSize(.all, size: 90)
        XCTAssertEqual(session.undoHistoryCount, undoBefore + 1)
        XCTAssertEqual(session.captionStyleSize(.line("cue-1")), 90)
        XCTAssertEqual(session.captionStyleSize(.line("cue-2")), 90)
        session.undo()
        XCTAssertEqual(session.captionStyleSize(.line("cue-1")), 150)
        XCTAssertEqual(session.captionStyleSize(.line("cue-2")), 78)
        session.redo()
        session.placeCaptions(.line("cue-2"), at: .top)
        XCTAssertNil(session.captionPlacementMessage)
        let own = try XCTUnwrap(session.document.captionMeta["appearance"]?.objectValue?["line_overrides"]?.objectValue?["cue-2"]?.objectValue)
        XCTAssertGreaterThan(try XCTUnwrap(own["position_y_frac"]?.numberValue), 0.10)
        XCTAssertLessThan(try XCTUnwrap(own["position_y_frac"]?.numberValue), 0.3)
        await session.save()
        let meta = try XCTUnwrap(fake.lastRequest?.captionMeta)
        XCTAssertEqual(meta["size_px"], .number(90))
        XCTAssertNil(meta["appearance"]?.objectValue?["line_overrides"]?.objectValue?["cue-1"]?.objectValue?["size_px"])
        let (reopened, _) = await loaded(storedAppearance: meta["appearance"]?.objectValue, storedSize: meta["size_px"]?.numberValue)
        XCTAssertEqual(reopened.captionStyleSize(.line("cue-1")), 90)
        XCTAssertEqual(reopened.captionStyleSize(.line("cue-2")), 90)
        XCTAssertEqual(Double(reopened.captionPosition(id: "cue-2").y), try XCTUnwrap(own["position_y_frac"]?.numberValue))
    }

    func testCaptionSizeAndPositionReachTheNativeRecipeAtBoundarySizes() throws {
        for size in [36.0, 78, 150, 160] {
            let layers = try cueLayers(meta: [
                "size_px": .number(78),
                "appearance": .object([
                    "position_y_frac": .number(0.6),
                    "line_overrides": .object(["cue-0": .object([
                        "size_px": .number(size), "position_y_frac": .number(0.3)
                    ])])
                ])
            ], cuePhases: [nil, nil])
            XCTAssertEqual(layers[0].runs.first?.fontSize, size)
            XCTAssertEqual(layers[1].runs.first?.fontSize, 78)
            XCTAssertEqual(layers[0].anchorY, 576, accuracy: 0.01)
            XCTAssertEqual(layers[1].anchorY, 1152, accuracy: 0.01)
        }
    }

    func testCaptionPositionLeavesPerCueFontColorAndSizeInTheNativeRecipe() throws {
        let layers = try cueLayers(meta: ["appearance": .object(["position_y_frac": .number(0.4)])], cuePhases: [nil, nil], cueStyles: [
            ["font_family": .string("Inter"), "text_color": .string("#FF0000"), "size_px": .number(100)],
            ["font_family": .string("Pacifico"), "text_color": .string("#00FF00"), "size_px": .number(120)]
        ])
        XCTAssertEqual(layers.map { $0.runs.first?.fontSize }, [100, 120])
        XCTAssertTrue(try XCTUnwrap(layers[0].runs.first?.fontAssetID).contains("Inter"))
        XCTAssertTrue(try XCTUnwrap(layers[1].runs.first?.fontAssetID).contains("Pacifico"))
        XCTAssertEqual(layers[0].runs.first?.fill.green, 0)
        XCTAssertEqual(layers[1].runs.first?.fill.red, 0)
        XCTAssertEqual(layers.map(\.anchorY), [768, 768])
    }

    func testDefaultCaptionFontMatchesTheSharedRegistry() throws {
        let layers = try cueLayers(meta: [:], cuePhases: [nil])
        XCTAssertTrue(try XCTUnwrap(layers.first?.runs.first?.fontAssetID).contains("TikTokSans-Regular"))
    }

    func testSentencePlacementMeasuresTheRenderedSentenceRatherThanEachStoredWord() async throws {
        var rows = [row("a", "We", 0), row("b", "went", 0.5), row("c", "out.", 1)]
        for index in rows.indices {
            rows[index].raw["font_family"] = .string("Inter")
            rows[index].raw["size_px"] = .number(78)
            rows[index].raw["alignment"] = .string("left")
        }
        let values = rows.map { row in JSONValue.object(row.raw.merging([
            "id": .string(row.id), "text": .string(row.text), "start_s": .number(row.startS), "end_s": .number(row.endS)
        ]) { _, new in new }) }
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object(["caption_meta": .object(["editable": .bool(true)]), "caption_editor_style": .bool(true), "caption_style_scope": .bool(true)]),
            "editor_payload": .object(["sections": .object(["text_elements": .array(values), "caption_meta": .object(["style": .string("sentence"), "font_set": .bool(true), "font": .string("Pacifico")])])])
        ]
        let threadID = UUID()
        var caps = try XCTUnwrap(snapshot["editor_capabilities"]?.objectValue)
        caps["text_elements"] = .bool(true)
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["render_destination": .string("device"), "resolved_archetype": .string("guided_story"), "text_elements": .array(values), "caption_meta": snapshot["editor_payload"]?.objectValue?["sections"]?.objectValue?["caption_meta"] ?? .object([:]), "editor_capabilities": .object(caps)])
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.offersCaptionStyleScope)
        let font = try XCTUnwrap(NativeFontCatalog.shared.fontURL(for: "Pacifico"))
        for placement in [NativeTextBlockPlacement.left, .right] {
            session.placeCaptions(.all, at: placement)
            XCTAssertNil(session.captionPlacementMessage)
            let layers = try laneLayers(session.document.textElements, meta: session.document.captionMeta)
            let layer = try XCTUnwrap(layers["a"])
            XCTAssertEqual(layer.runs.first?.text, "We went out.")
            let bounds = try XCTUnwrap(AuthoredTextLayout.blockBounds(of: layer, fontURL: font))
            XCTAssertGreaterThanOrEqual(bounds.minX / 1080, NativeTextBlockGeometry.safeRect.minX - 0.002)
            XCTAssertLessThanOrEqual(bounds.maxX / 1080, NativeTextBlockGeometry.safeRect.maxX + 0.002)
        }
    }

    func testScopedCaptionsMatchSyntheticPreviewAndExportPixels() async throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { root.deleteLastPathComponent() }
        let folder = root.appendingPathComponent(".dev/caption-scope-proof", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let directory = folder.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let width = 1080, height = 1920
        let context = CIContext()
        let space = try XCTUnwrap(CGColorSpace(name: CGColorSpace.sRGB))
        let base = directory.appendingPathComponent("synthetic-base.png")
        try context.writePNGRepresentation(of: CIImage(color: CIColor(red: 0.2, green: 0.2, blue: 0.2)).cropped(to: CGRect(x: 0, y: 0, width: width, height: height)), to: base, format: .RGBA8, colorSpace: space)
        let asset = MediaAsset(id: "synthetic", relativePath: "synthetic-base.png", fingerprint: try SHA256Fingerprinter().fingerprint(file: base), duration: 2, naturalSize: MediaSize(width: 1080, height: 1920))
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "synthetic", asset: asset, url: base)
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2, trimIn: 0, trimOut: 2, sourceDuration: 2, slotID: "slot")
        let cues = [
            EditorCaptionCue(id: "one", startS: 0, endS: 1, text: "Hi", raw: ["font_family": .string("Inter"), "text_color": .string("#FF0000")]),
            EditorCaptionCue(id: "two", startS: 1, endS: 2, text: "Hi", raw: ["font_family": .string("Pacifico"), "text_color": .string("#00FF00")])
        ]
        let meta: [String: JSONValue] = ["style": .string("sentence"), "stroke_width": .number(0), "shadow_enabled": .bool(false), "appearance": .object([
            "line_overrides": .object(["one": .object(["size_px": .number(150), "position_x_frac": .number(0.3), "position_y_frac": .number(0.25)]),
                                        "two": .object(["size_px": .number(78), "position_x_frac": .number(0.7), "position_y_frac": .number(0.65)])])
        ])]
        let document = EditorDocument(editFormat: "subtitled", clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 2)], captionMeta: meta, captionCues: cues)
        let items = cues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        let program = try compiler().compile(document: document, clips: [clip], items: items, sources: [0: source], deviceCaptions: true)
        let output = directory.appendingPathComponent("caption-scopes.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none).export(recipe: program.recipe, assetURLs: program.assetURLs, outputURL: output)
        let preview = try await AVPlayerPreviewComposer(branding: .none).makePreview(recipe: program.recipe, assetURLs: program.assetURLs)
        var report: [[String: Any]] = []
        for (index, time) in [0.5, 1.5].enumerated() {
            var pixels: [[UInt8]] = []
            for (name, sourceAsset, composition) in [("preview", preview.playerItem.asset, preview.playerItem.videoComposition), ("export", AVURLAsset(url: output) as AVAsset, nil)] {
                let generator = AVAssetImageGenerator(asset: sourceAsset)
                generator.videoComposition = composition
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                let ci = CIImage(cgImage: image, options: [.colorSpace: NSNull()])
                var bytes = [UInt8](repeating: 0, count: width * height * 4)
                context.render(ci, toBitmap: &bytes, rowBytes: width * 4, bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
                pixels.append(bytes)
                try context.writePNGRepresentation(of: ci, to: directory.appendingPathComponent("\(name)-\(index).png"), format: .RGBA8, colorSpace: space)
            }
            let difference = zip(pixels[0], pixels[1]).reduce(0.0) { $0 + Double(abs(Int($1.0) - Int($1.1))) } / Double(pixels[0].count)
            XCTAssertLessThan(difference, 6)
            let ink = stride(from: 0, to: pixels[1].count, by: 4).filter { offset in
                index == 0 ? pixels[1][offset] > 150 && pixels[1][offset + 1] < 100 : pixels[1][offset + 1] > 150 && pixels[1][offset] < 100
            }
            XCTAssertGreaterThan(ink.count, 200)
            let x = ink.reduce(0.0) { $0 + Double(($1 / 4) % width) } / Double(max(1, ink.count)) / Double(width)
            XCTAssertEqual(x, index == 0 ? 0.3 : 0.7, accuracy: 0.04)
            report.append(["time": time, "mean_pixel_difference": difference, "colored_ink_pixels": ink.count, "ink_center_x": x])
        }
        try JSONSerialization.data(withJSONObject: report, options: .prettyPrinted).write(to: directory.appendingPathComponent("comparison.json"))
        print("CAPTION_SCOPE_EXPORT_PROOF \(directory.path)")
    }

    func testCaptionGestureAsksOnceOnReleaseAndAllIsOneUndoStep() async {
        let (session, _) = await loaded()
        session.setCaptionStyleSize(.line("cue-2"), size: 120)
        let before = session.document
        let undoBefore = session.undoHistoryCount
        session.beginDirectManipulation()
        for value in [0.4, 0.45, 0.5] {
            session.setCaptionGesturePosition(id: "cue-1", position: CGPoint(x: 0.5, y: value))
            XCTAssertFalse(session.captionGestureChoicePresented)
        }
        session.setCaptionGestureSize(id: "cue-1", size: 150)
        session.endDirectManipulation()
        XCTAssertTrue(session.captionGestureChoicePresented)
        session.finishCaptionGesture(applyToAll: true)
        XCTAssertFalse(session.captionGestureChoicePresented)
        XCTAssertEqual(session.captionStyleSize(.line("cue-1")), 150)
        XCTAssertEqual(session.captionStyleSize(.line("cue-2")), 150)
        XCTAssertEqual(session.undoHistoryCount, undoBefore + 1)
        session.undo()
        XCTAssertEqual(session.document, before)
    }

    func testPositionOnlyApplyAllRetainsSizesFontsAndColorsAndIsOneUndo() async throws {
        let appearance: [String: JSONValue] = ["line_overrides": .object([
            "cue-1": .object(["size_px": .number(100), "position_x_frac": .number(0.2), "position_y_frac": .number(0.3)]),
            "cue-2": .object(["size_px": .number(120), "position_x_frac": .number(0.7), "position_y_frac": .number(0.6)])
        ])]
        let own: [String: [String: JSONValue]] = [
            "cue-1": ["font_family": .string("Inter"), "text_color": .string("#FF0000")],
            "cue-2": ["font_family": .string("Pacifico"), "text_color": .string("#00FF00")]
        ]
        let (session, _) = await loaded(storedAppearance: appearance, storedCueOverrides: own)
        let before = session.document
        let undoBefore = session.undoHistoryCount
        session.beginDirectManipulation()
        session.setCaptionGesturePosition(id: "cue-1", position: CGPoint(x: 0.5, y: 0.4))
        session.endDirectManipulation()
        session.finishCaptionGesture(applyToAll: true)
        for id in ["cue-1", "cue-2"] {
            XCTAssertEqual(session.captionPosition(id: id), CGPoint(x: 0.5, y: 0.4))
            let cue = try XCTUnwrap(session.document.captionCues.first { $0.id == id })
            XCTAssertEqual(cue.raw["font_family"], own[id]?["font_family"])
            XCTAssertEqual(cue.raw["text_color"], own[id]?["text_color"])
        }
        XCTAssertEqual(session.captionStyleSize(.line("cue-1")), 100)
        XCTAssertEqual(session.captionStyleSize(.line("cue-2")), 120)
        XCTAssertEqual(session.undoHistoryCount, undoBefore + 1)
        session.undo()
        XCTAssertEqual(session.document, before)
    }

    func testSizeOnlyApplyAllClearsBothSizeRepresentationsAndKeepsPositionFontAndColor() async throws {
        let appearance: [String: JSONValue] = ["line_overrides": .object([
            "cue-1": .object(["size_px": .number(100), "position_x_frac": .number(0.2), "position_y_frac": .number(0.3)]),
            "cue-2": .object(["size_px": .number(120), "position_x_frac": .number(0.7), "position_y_frac": .number(0.6)])
        ])]
        let own: [String: [String: JSONValue]] = [
            "cue-1": ["size_px": .number(110), "font_family": .string("Inter"), "text_color": .string("#FF0000")],
            "cue-2": ["size_px": .number(130), "font_family": .string("Pacifico"), "text_color": .string("#00FF00")]
        ]
        let (session, fake) = await loaded(storedAppearance: appearance, storedCueOverrides: own)
        session.setCaptionStyleSize(.all, size: 150)
        XCTAssertEqual(session.captionPosition(id: "cue-1"), CGPoint(x: 0.2, y: 0.3))
        XCTAssertEqual(session.captionPosition(id: "cue-2"), CGPoint(x: 0.7, y: 0.6))
        for id in ["cue-1", "cue-2"] {
            XCTAssertEqual(session.captionStyleSize(.line(id)), 150)
            let cue = try XCTUnwrap(session.document.captionCues.first { $0.id == id })
            XCTAssertNil(cue.raw["size_px"])
            XCTAssertEqual(cue.raw["font_family"], own[id]?["font_family"])
            XCTAssertEqual(cue.raw["text_color"], own[id]?["text_color"])
        }
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionMeta?["size_px"], .number(150))
        XCTAssertTrue(try XCTUnwrap(fake.lastRequest?.captionCues).allSatisfy { $0.objectValue?["size_px"] == nil })
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

    func testAllCaptionsShowsATextMotionEntranceAndReplacesItInOneUndoStep() async throws {
        let (session, fake) = await loaded(storedOverride: popBounce, storedTextMotion: silk)
        XCTAssertEqual(session.captionAnimationPhases(.all), silk, "the Text motion entrance is the shown default")
        XCTAssertEqual(session.captionAnimationPhases(.line("cue-1")), silk)
        XCTAssertFalse(session.hasUnsavedChanges)
        let undoBefore = session.undoHistoryCount
        session.setCaptionPhase(.all, phase: "entrance", effect: "echoStack")
        XCTAssertEqual(session.undoHistoryCount, undoBefore + 1)
        XCTAssertNil(session.document.captionMeta["appearance"]?.objectValue?["animation_phases"])
        XCTAssertEqual(session.document.captionMeta["animation_phases"]?.objectValue?["entrance"], .string("echoStack"))
        XCTAssertTrue(session.document.captionCues.allSatisfy { $0.raw["animation_phases"] == nil })
        session.undo()
        XCTAssertEqual(session.captionAnimationPhases(.all), silk)
        XCTAssertEqual(session.captionAnimationPhases(.line("cue-2")), popBounce)
        session.redo()
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertNil(request.captionMeta?["appearance"]?.objectValue?["animation_phases"])
        XCTAssertEqual(request.captionMeta?["animation_phases"]?.objectValue?["entrance"], .string("echoStack"))
    }

    func testSwitchingDisplayOrHighlightKeepsTheChosenAnimation() async throws {
        let (session, fake) = await loaded(storedTextMotion: silk)
        session.setCaptionDisplay("word")
        XCTAssertEqual(session.captionAnimationPhases(.all), silk, "Word display keeps the Text motion entrance")
        XCTAssertNil(session.document.captionMeta["appearance"]?.objectValue?["animation_phases"])
        session.setCaptionDisplay("sentence")
        session.setCaptionAppearance(key: "highlight_spoken_word", value: .bool(true))
        session.setCaptionDisplay("word")
        XCTAssertEqual(session.captionAnimationPhases(.all), silk)
        await session.save()
        XCTAssertEqual(try XCTUnwrap(fake.lastRequest).captionMeta?["animation_phases"]?.objectValue, silk)

        let (chosen, _) = await loaded(storedDefault: popBounce)
        for display in ["word", "sentence", "word"] {
            chosen.setCaptionDisplay(display)
            XCTAssertEqual(chosen.captionAnimationPhases(.all), popBounce, display)
        }

        let (older, _) = await loaded(capability: false, storedTextMotion: silk)
        older.setCaptionDisplay("word")
        XCTAssertNil(older.document.captionMeta["animation_phases"], "a build that cannot save caption phases writes none")
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
