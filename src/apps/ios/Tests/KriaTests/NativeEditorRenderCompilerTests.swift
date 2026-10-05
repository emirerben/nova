import XCTest
import AVFoundation
import UIKit
import KriaMediaEngine
@testable import Kria

@MainActor final class NativeEditorRenderCompilerTests: XCTestCase {
    func testFootageRateAndCropCompileAsAdditiveRecipeFields() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 4),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0.5, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3,
            raw: ["playback_rate": .number(2), "source_crop": .object([
                "x": .number(0.1), "y": .number(0.2), "width": .number(0.7), "height": .number(0.6)
            ])])])
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let rendered = try XCTUnwrap(compiler.compile(document: document, clips: [clip], items: [], sources: [0: source]).recipe.tracks.first?.clips.first)
        XCTAssertEqual(rendered.rate, 2)
        XCTAssertEqual(rendered.sourceStart, 0.5)
        XCTAssertEqual(rendered.sourceDuration, 3.5)
        XCTAssertEqual(try XCTUnwrap(rendered.holdDuration), 1.25, accuracy: 0.0001)
        XCTAssertEqual(rendered.sourceCrop, .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
    }

    /// KRI-282: a draft whose speech excerpts play from one clip over another
    /// clip's visuals keeps them as a `clip-audio` track (never dropped), and the
    /// visuals' own sound is muted under each excerpt so the two never double up.
    func testClipAudioLaneCompilesExcerptOverOtherClipsVisuals() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        func source(_ index: Int) -> ResolvedEditorSource {
            ResolvedEditorSource(clipIndex: index, mediaID: "original-\(index)",
                asset: MediaAsset(id: "local-\(index)", relativePath: "original-\(index).mp4", fingerprint: fingerprint, duration: 6),
                url: URL(fileURLWithPath: "/fixture/original-\(index).mp4"))
        }
        let clips = (0..<2).map { index in
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: index, start: Double(index) * 3, end: Double(index) * 3 + 3,
                       trimIn: 0, trimOut: 3, sourceDuration: 6, slotID: "slot-\(index)")
        }
        var document = EditorDocument(clips: [.init(id: "slot-0", clipIndex: 0, inS: 0, durationS: 3),
                                              .init(id: "slot-1", clipIndex: 1, inS: 0, durationS: 3)])
        let snapshot: [String: JSONValue] = ["clip_audio": .array([.object([
            "id": .string("e1"), "source_clip_index": .number(0), "source_start_s": .number(1), "source_end_s": .number(2.5),
            "start_s": .number(3.5), "gain": .number(0.9)])])]
        document.clipAudio = EditorDocument.decode(snapshot: snapshot).clipAudio
        XCTAssertEqual(document.clipAudio.count, 1)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: source(0), 1: source(1)]).recipe
        let track = try XCTUnwrap(recipe.tracks.first { $0.kind == .audio })
        let excerpt = try XCTUnwrap(track.clips.first)
        XCTAssertEqual(excerpt.sourceAssetID, "source-0")
        XCTAssertEqual(excerpt.sourceStart, 1)
        XCTAssertEqual(excerpt.sourceDuration, 1.5, accuracy: 0.0001)
        XCTAssertEqual(excerpt.timelineStart, 3.5)
        XCTAssertEqual(excerpt.volume, 0.9, accuracy: 0.0001)
        let window = try XCTUnwrap(recipe.audio.muteWindows.first)
        XCTAssertEqual(window.clipIDs, ["slot-1"])
        XCTAssertEqual(window.start, 3.5, accuracy: 0.0001)
        XCTAssertEqual(window.end, 5, accuracy: 0.0001)
    }

    func testUnreadableClipAudioRowRefusesInsteadOfDroppingSpeech() throws {
        let document = EditorDocument.decode(snapshot: ["clip_audio": .array([.object(["id": .string("broken")])])])
        XCTAssertEqual(document.unreadableClipAudio, 1)
        var renderable = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3)])
        renderable.unreadableClipAudio = 1
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3, sourceDuration: 6, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        XCTAssertThrowsError(try compiler.compile(document: renderable, clips: [clip], items: [], sources: [0: source])) { error in
            XCTAssertEqual(error as? NativeEditorRenderError, .unsupportedLane("clip audio"))
        }
    }

    func testRetimingPreservesAdjacentClipWindows() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        var clips: [EditorClip] = []
        for index in 0..<2 {
            let start = Double(index) * 3
            clips.append(EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0,
                start: start, end: start + 3, trimIn: 0, trimOut: 3,
                sourceDuration: 6, slotID: "slot-\(index)"))
        }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        for rate in [0.5, 2.0] {
            let first = EditorTimelineSlot(id: "slot-0", clipIndex: 0, inS: 0, durationS: 3,
                raw: ["playback_rate": .number(rate)])
            let second = EditorTimelineSlot(id: "slot-1", clipIndex: 0, inS: 0, durationS: 3)
            let document = EditorDocument(clips: [first, second])
            let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: source]).recipe
            let rendered = try XCTUnwrap(recipe.tracks.first(where: { $0.kind == .video })?.clips)
            XCTAssertEqual(rendered[0].duration, 3, accuracy: 0.0001)
            XCTAssertEqual(rendered[0].sourceDuration, min(3, 3 * rate), accuracy: 0.0001)
            XCTAssertEqual(rendered[1].timelineStart, 3)
            XCTAssertEqual(rendered[1].duration, 3)
            XCTAssertNoThrow(try recipe.validate())
        }
    }

    /// KRI-164: `NativeEditorInteraction.transitionOverlap`'s `duration*0.3`
    /// floor (0.333333*0.3 = 0.0999999) lands just under its 0.1s cutoff, so
    /// the projection abuts these clips with zero overlap even though both
    /// declare a crossfade -- exactly the shape of job `d33dca56…`'s clips
    /// 7/8/10. The compiler used to keep the declared 0.103s transition
    /// anyway, producing a clip whose `timelineStart` exactly equalled the
    /// previous clip's `end`; Composition then rejected the whole recipe
    /// with `invalidTimeline`. Drive the real `timelineProjection` function
    /// to reproduce that exact floating-point geometry, not hand-picked
    /// numbers, then build `EditorClip`s the same way
    /// `NativeEditorSession.timelineClips` does (a plain pass-through of the
    /// window's `start`/`end`) so this exercises the actual overlap the
    /// compiler receives in production.
    func testTransitionClampsToProjectedOverlapAtSubFrameSlotBoundary() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint), url: url)
        let durations = [0.866667, 0.333333, 0.866667]
        let slots = durations.enumerated().map { index, duration in
            EditorTimelineSlot(id: "slot-\(index)", clipIndex: 0, inS: 0, durationS: duration,
                transitionAfter: index < durations.count - 1 ? "crossfade" : "cut",
                transitionDurationS: index < durations.count - 1 ? 0.103 : nil)
        }
        let projection = NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil)
        XCTAssertEqual(projection.clipWindows.count, 3)
        // Confirm the projection still reproduces the zero-overlap boundary
        // this test exists to guard. If this ever stops being true, the
        // assertions below are no longer exercising KRI-164's geometry.
        XCTAssertEqual(projection.clipWindows[1].overlapBefore, 0)
        XCTAssertEqual(projection.clipWindows[1].start, projection.clipWindows[0].end, accuracy: 0.0001)

        let clips = zip(slots, projection.clipWindows).map { slot, window in
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: slot.clipIndex,
                start: window.start, end: window.end, trimIn: 0, trimOut: window.end - window.start,
                sourceDuration: 10, slotID: slot.id)
        }
        let document = EditorDocument(clips: slots)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: source]).recipe
        let rendered = try XCTUnwrap(recipe.tracks.first(where: { $0.kind == .video })?.clips)
        XCTAssertEqual(rendered.count, 3)
        XCTAssertNil(rendered[1].transition,
            "Clip 2 has zero projected overlap with clip 1; a transition here claims overlap the projection never left room for")
        for (index, clip) in rendered.enumerated() where index > 0 {
            guard let transition = clip.transition else { continue }
            let previousEnd = rendered[index - 1].timelineStart + rendered[index - 1].duration
            XCTAssertLessThanOrEqual(transition.duration, previousEnd - clip.timelineStart + 1e-9)
        }
        XCTAssertNoThrow(try recipe.validate())
        _ = try await LivePreviewComposition(recipe: recipe, assetURLs: ["source-0": url])
    }

    func testTransitionShortensToOverlapWhenRequestedDurationExceedsIt() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        // 0.5s slots with a requested 0.3s crossfade: transitionOverlap caps
        // this at leftDuration*0.3 = 0.15, well under the requested 0.3.
        let slots = [
            EditorTimelineSlot(id: "slot-0", clipIndex: 0, inS: 0, durationS: 0.5,
                transitionAfter: "crossfade", transitionDurationS: 0.3),
            EditorTimelineSlot(id: "slot-1", clipIndex: 0, inS: 0, durationS: 0.5),
        ]
        let projection = NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil)
        XCTAssertEqual(projection.clipWindows[1].overlapBefore, 0.15, accuracy: 0.0001)
        let clips = zip(slots, projection.clipWindows).map { slot, window in
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: slot.clipIndex,
                start: window.start, end: window.end, trimIn: 0, trimOut: window.end - window.start,
                sourceDuration: 6, slotID: slot.id)
        }
        let document = EditorDocument(clips: slots)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: source]).recipe
        let rendered = try XCTUnwrap(recipe.tracks.first(where: { $0.kind == .video })?.clips)
        XCTAssertEqual(try XCTUnwrap(rendered[1].transition).duration, 0.15, accuracy: 0.0001)
    }

    func testStillVisualKeepsItsWindowWhenFootageRateIsStored() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 4),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let image = ResolvedEditorSource(clipIndex: -1, mediaID: "still",
            asset: MediaAsset(id: "still", relativePath: "still.png", fingerprint: fingerprint),
            url: URL(fileURLWithPath: "/fixture/still.png"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        var document = EditorDocument(snapshot: NativeEditorUITestFixtures.captionVisuals.serverSnapshot)
        document.captionCues = []
        document.visualBlocks[0].startS = 0
        document.visualBlocks[0].endS = 3
        document.visualBlocks[0].raw["media_kind"] = .string("image")
        document.visualBlocks[0].raw["playback_rate"] = .number(2)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
            mediaSources: ["visual:paper-media:paper-media": image]).recipe
        let rendered = try XCTUnwrap(recipe.tracks.first(where: { $0.kind == .overlay })?.clips.first)
        XCTAssertEqual(rendered.duration, 3)
        XCTAssertEqual(rendered.rate, 1)
    }

    func testExplicitWordDisplaySeparatesWordsAndClampsEditedTiming() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        for highlighted in [false, true] {
            let cue = EditorCaptionCue(id: "cue", startS: 1, endS: 2, text: "Hello world", raw: ["words": .array([
                .object(["text": .string("Old"), "start_s": .number(0), "end_s": .number(5)])])])
            let document = EditorDocument(captionMeta: ["style": .string("word"), "highlight_color": .string("#FF0000"),
                "appearance": .object(["highlight_spoken_word": .bool(highlighted)])], captionCues: [cue])
            let item = NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 1, end: 2)
            let layers = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe.textLayers
            XCTAssertEqual(layers.count, 2)
            XCTAssertEqual(layers.map { $0.runs.map(\.text).joined() }, ["Hello", "world"])
            XCTAssertEqual(layers[0].start, 1, accuracy: 0.001)
            XCTAssertEqual(layers[0].end, layers[1].start, accuracy: 0.001)
            XCTAssertEqual(layers[1].end, 2, accuracy: 0.001)
            XCTAssertEqual(layers[0].runs.first?.fill, highlighted ? TextInk(red: 1, green: 0, blue: 0, alpha: 1) : TextInk(red: 1, green: 1, blue: 1, alpha: 1))
        }
    }

    func testStyledLegacyOverlayCompilesZeroAndPositiveFrozenTail() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "base", relativePath: "base.mov", fingerprint: fingerprint),
            url: URL(fileURLWithPath: "/fixture/base.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        for sourceDuration in [1.0, 3.0] {
            let media = ResolvedEditorSource(clipIndex: -1, mediaID: "card", asset: MediaAsset(id: "card", relativePath: "card.mov",
                fingerprint: fingerprint, duration: sourceDuration, naturalSize: MediaSize(width: 96, height: 160)), url: URL(fileURLWithPath: "/fixture/card.mov"))
            let document = EditorDocument(mediaOverlays: [.init(id: "overlay", startS: 0, endS: 3,
                raw: ["editor_style": .object(NativeVisualAuthoring.defaultStyle)])])
            let item = NativeEditorTimelineItem(selection: .init(kind: .mediaOverlay, id: "overlay"), start: 0, end: 3)
            var recipe = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source], mediaSources: ["overlay:overlay": media]).recipe
            let trackIndex = try XCTUnwrap(recipe.tracks.firstIndex { $0.kind == .overlay })
            XCTAssertEqual(try XCTUnwrap(recipe.tracks[trackIndex].clips[0].holdDuration), 3 - sourceDuration)
            XCTAssertNoThrow(try recipe.validate())
            recipe.tracks[trackIndex].clips[0].holdDuration = 4
            XCTAssertThrowsError(try recipe.validate(), "Held time cannot exceed the placement window")
        }
    }

    func testCaptionAlignmentMatchesFinalASSMargins() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        for (alignment, anchor) in [("left", 80.0), ("center", 540.0), ("right", 1000.0)] {
            let document = EditorDocument(captionMeta: ["appearance": .object(["alignment": .string(alignment), "highlight_spoken_word": .bool(false)])],
                captionCues: [.init(id: "cue", startS: 0, endS: 2, text: "One more game")])
            let item = NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 0, end: 2)
            let recipe = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe
            XCTAssertEqual(try XCTUnwrap(recipe.textLayers.first).anchorX, anchor, accuracy: 0.001)
        }
    }

    // KRI-280: the phone compiles every caption cue in the Talking frame
    // (`phone_captions.py`: lower-third safe zone, pop-in) whatever the format,
    // so a phone Narrated preview must place and animate captions like phone
    // Talking. A cloud Narrated burn keeps its low position and no pop.
    func testDeviceNarratedCaptionsPreviewInTheTalkingFrame() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let item = NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 0, end: 2)
        func caption(format: String, device: Bool) throws -> PortableTextLayer {
            let document = EditorDocument(editFormat: format, captionCues: [.init(id: "cue", startS: 0, endS: 2, text: "First we pack")])
            return try XCTUnwrap(compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source],
                                                  deviceCaptions: device).recipe.textLayers.first)
        }
        let phoneTalking = try caption(format: "subtitled", device: true)
        let phoneNarrated = try caption(format: "narrated_planned", device: true)
        let cloudNarrated = try caption(format: "narrated_planned", device: false)
        XCTAssertEqual(phoneNarrated.anchorY, phoneTalking.anchorY, accuracy: 0.001)
        XCTAssertEqual(phoneNarrated.effect, phoneTalking.effect)
        XCTAssertEqual(phoneNarrated.effect, .captionPop)
        XCTAssertEqual(cloudNarrated.effect, PortableTextLayer.Effect.none)
        XCTAssertGreaterThan(cloudNarrated.anchorY, phoneNarrated.anchorY, "cloud Narrated burns lower than the phone")
    }

    /// The read-only opening title of a phone Voiceover edit, exactly as the
    /// status route sends it (`phone_narrated_plan.narrated_title_element`).
    static func readOnlyTitle(_ text: String) -> EditorTextElement {
        EditorTextElement(id: "narrated-title", text: text, startS: 0, endS: 1.6, role: "generative_intro", raw: [
            "id": .string("narrated-title"), "text": .string(text), "start_s": .number(0), "end_s": .number(1.6),
            "role": .string("generative_intro"), "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.15),
            "font_family": .string("Playfair Display"), "size_px": .number(120), "size_class": .string("large"),
            "alignment": .string("center"), "effect": .string("fade-in"), "removed": .bool(false), "behind_subject": .bool(false),
            "source_params": .object(["narrated_storyboard": .string("intro"), "read_only": .bool(true)]),
        ])
    }

    // KRI-455: the preview draws a phone Voiceover edit's read-only title where
    // the phone exports it. Expected runs are the server's compiled `title-0`
    // layer for the same element (`phone_narrated_plan._compile_title_layers`,
    // portrait): same font file and size, same anchor, same line breaks and
    // line x, same shadow, same fade-in. Baselines differ by under a pixel per
    // line away from the block's centre: the server truncates the line step to
    // whole pixels (`text_overlay_skia._measure_block`), the native layout
    // doesn't. The title has no timeline item (nothing may select it), draws
    // beneath the captions like the export, and its caption mirror is still
    // skipped.
    func testReadOnlyTitleCompilesLikeTheExportedPhoneTitle() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 6,
            trimIn: 0, trimOut: 6, sourceDuration: 6, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let mirror = EditorTextElement(id: "mirror", text: "First we pack", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])])
        let cueItem = NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 0, end: 2)
        let shadow = TextBlurLayer(color: TextInk(red: 0, green: 0, blue: 0, alpha: 160.0 / 255), sigma: 12, dx: 0, dy: 6)
        let exported: [(String, [(String, Double, Double)])] = [
            ("Cacio e pepe in 10 minutes", [("Cacio e pepe in", 123.66, 246.84), ("10 minutes", 238.86, 429.84)]),
            ("Çılbır: the Turkish eggs everyone gets wrong", [("Çılbır: the", 258.84, 63.84), ("Turkish eggs", 179.04, 246.84),
                                                              ("everyone gets", 162.66, 429.84), ("wrong", 360.84, 612.84)]),
        ]
        for (text, lines) in exported {
            let document = EditorDocument(editFormat: "narrated_planned", textElements: [Self.readOnlyTitle(text), mirror],
                captionCues: [.init(id: "cue", startS: 0, endS: 2, text: "First we pack")])
            let recipe = try compiler.compile(document: document, clips: [clip], items: [cueItem], sources: [0: source],
                                              deviceCaptions: true).recipe
            XCTAssertEqual(recipe.textLayers.map(\.id).first, "narrated-title", "the title draws beneath the captions")
            XCTAssertEqual(recipe.textLayers.count, 2, "title + the cue; the caption mirror is skipped")
            let title = try XCTUnwrap(recipe.textLayers.first)
            XCTAssertEqual(title.start, 0)
            XCTAssertEqual(title.end, 1.6, accuracy: 0.0001)
            XCTAssertEqual(title.effect, .fadeIn)
            XCTAssertNil(title.motion)
            XCTAssertEqual(title.anchorX, 540, accuracy: 0.001)
            XCTAssertEqual(title.anchorY, 288, accuracy: 0.001)
            XCTAssertEqual(title.runs.map(\.text), lines.map(\.0), text)
            for (run, line) in zip(title.runs, lines) {
                XCTAssertEqual(run.fontAssetID, "font-PlayfairDisplay-Bold.ttf")
                XCTAssertEqual(run.fontSize, 120)
                XCTAssertEqual(run.x, line.1, accuracy: 0.01, line.0)
                XCTAssertEqual(run.baselineY, line.2, accuracy: 2, line.0)
                XCTAssertEqual(run.fill, TextInk(red: 1, green: 1, blue: 1, alpha: 1))
                XCTAssertEqual(run.strokeWidth, 0)
                XCTAssertEqual(run.blurLayers, [shadow])
            }
        }
    }

    // KRI-110: guided-story captions are caption_cue-tagged TextElements, not
    // caption_cues rows, so the block above never sees them. The generic
    // per-element pass must apply the same caption_meta styling via
    // applyingCaptionMeta's raw-field overlay.
    func testGuidedStoryCaptionTaggedTextElementHonorsCaptionMetaStyling() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let captionElement = EditorTextElement(id: "caption-1", text: "Spoken words", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])])
        let document = EditorDocument(
            textElements: [captionElement],
            captionMeta: ["color": .string("#FF0000"), "stroke_width": .number(9), "size_px": .number(101)]
        )
        let item = NativeEditorTimelineItem(selection: .init(kind: .text, id: "caption-1"), start: 0, end: 2)
        let recipe = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe
        let run = try XCTUnwrap(XCTUnwrap(recipe.textLayers.first).runs.first)
        XCTAssertEqual(run.fill, TextInk(red: 1, green: 0, blue: 0, alpha: 1))
        // AuthoredTextLayout doubles the authored stroke width (a centered
        // outline needs 2x the visible border thickness) — matches the
        // pre-existing cue-native caption path's own convention.
        XCTAssertEqual(run.strokeWidth, 18)
        XCTAssertEqual(run.fontSize, 101)
    }

    // Talking/subtitled documents carry each caption as a caption_cues row AND
    // as a caption_cue-tagged text element (the API mirrors cues into the
    // editor's text lane). The generic per-element pass and the cue-native
    // caption block must never both fire for it: that burned every sentence
    // twice, once mid-frame over the speaker's face (KRI-172 render 1aff3f03).
    func testCaptionTaggedTextElementIsSkippedWhenCaptionCuesAlsoCoverIt() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let staleTextElement = EditorTextElement(id: "cue-dup", text: "Number three, Mason Greenwood?", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])])
        let cue = EditorCaptionCue(id: "cue-dup", startS: 0, endS: 2, text: "Number three, Mason Greenwood?")
        let document = EditorDocument(textElements: [staleTextElement], captionCues: [cue])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "cue-dup"), start: 0, end: 2),
            NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue-dup"), start: 0, end: 2),
        ]
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.count, 1, "the same sentence must be burned exactly once")
        XCTAssertEqual(recipe.textLayers.first?.id, "caption-cue-dup", "the surviving layer must come from the cue-native path")
    }

    // The session's timeline has no item for a mirrored caption, and the
    // compiler throws on any text element it reaches without one. Compiling
    // the session's own document and items proves the two skip the same
    // elements; each sentence still burns once, from its cue.
    func testTalkingDocumentCompilesAgainstTheSessionTimeline() throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.talkingCaptions)
        let clipIndex = try XCTUnwrap(session.timelineClips.first?.sourceClipIndex)
        let source = ResolvedEditorSource(clipIndex: clipIndex, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: session.document, clips: session.timelineClips,
            items: session.timelineItems, sources: [clipIndex: source]).recipe
        XCTAssertEqual(recipe.textLayers.map(\.id), session.document.captionCues.map { "caption-\($0.id)" })
    }

    // KRI-202: talk-to-camera (subtitled) documents can reach the client with
    // two caption_cues rows whose windows overlap (the cloud's own de-overlap
    // guard, phone_captions._prepare_cues, runs on a compiled copy the native
    // editor's live document never passes through). Burning both unclamped
    // showed two caption rows stacked on the same frame.
    func testOverlappingCaptionCuesAreClampedToNonOverlappingWindows() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 5,
            trimIn: 0, trimOut: 5, sourceDuration: 5, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let earlyCue = EditorCaptionCue(id: "cue-a", startS: 0, endS: 3, text: "First sentence here")
        let overlappingCue = EditorCaptionCue(id: "cue-b", startS: 2, endS: 5, text: "Second sentence here")
        let document = EditorDocument(captionCues: [earlyCue, overlappingCue])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue-a"), start: 0, end: 3),
            NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue-b"), start: 2, end: 5),
        ]
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.count, 2)
        let first = try XCTUnwrap(recipe.textLayers.first { $0.id == "caption-cue-a" })
        let second = try XCTUnwrap(recipe.textLayers.first { $0.id == "caption-cue-b" })
        XCTAssertLessThanOrEqual(first.end, second.start, "adjacent cues must never be visible at the same time")
        XCTAssertEqual(first.end, 2, "the earlier cue must clamp to the next cue's start")
    }

    func testGuidedStorySentenceCaptionProjectionKeepsSourceItemsAndLeavesTitlesAlone() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let title = EditorTextElement(id: "title", text: "A title", startS: 0, endS: 3)
        let captions = [
            EditorTextElement(id: "caption-0", text: "It", startS: 0, endS: 0.1,
                role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])]),
            EditorTextElement(id: "caption-1", text: "costs", startS: 0.3, endS: 0.4,
                role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")]), "removed": .bool(true)]),
            EditorTextElement(id: "caption-2", text: "172.5", startS: 0.6, endS: 0.7,
                role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])]),
            EditorTextElement(id: "caption-3", text: "dollars.”", startS: 0.9, endS: 1.0,
                role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])]),
        ]
        let document = EditorDocument(textElements: [title] + captions,
            captionMeta: ["style": .string("sentence"), "y_frac": .number(0.7), "color": .string("#FF0000")])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "title"), start: 0, end: 3),
        ] + captions.map { element in
                NativeEditorTimelineItem(selection: .init(kind: .text, id: element.id), start: element.startS, end: element.endS)
            }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe

        XCTAssertEqual(recipe.textLayers.first?.runs.map(\.text).joined(), "A title")
        let rendered = recipe.textLayers.filter { $0.id.hasPrefix("caption-") }
        XCTAssertEqual(rendered.map(\.id), ["caption-0", "caption-2", "caption-3"])
        // Each run is a wrapped line; the layout trims its boundary whitespace.
        XCTAssertEqual(rendered.map { $0.runs.map(\.text).joined(separator: " ") }, Array(repeating: "It 172.5 dollars.”", count: 3))
        XCTAssertEqual(rendered.map(\.start), [0, 0.6, 0.9])
        XCTAssertEqual(rendered.map(\.end), [0.6, 0.9, 1.0], "sentence display must span word gaps")
        XCTAssertTrue(rendered.allSatisfy { $0.anchorY == 0.7 * 1920 })
        XCTAssertTrue(rendered.allSatisfy { $0.runs.first?.fill == TextInk(red: 1, green: 0, blue: 0, alpha: 1) })
        XCTAssertEqual(document.textElements.dropFirst().map(\.id), ["caption-0", "caption-1", "caption-2", "caption-3"])
        XCTAssertEqual(document.textElements.map(\.text), ["A title", "It", "costs", "172.5", "dollars.”"])
        XCTAssertEqual(document.textElements.dropFirst().map(\.endS), [0.1, 0.4, 0.7, 1.0])
    }

    // KRI-110: every font the Captions Style tab offers must resolve to its
    // registry file for a caption-tagged text element, the same file the
    // backend's registry names — "Inter" is deliberately the guided caption's
    // own default face (Inter-Bold), so picking it is a visual no-op.
    func testGuidedStoryCaptionFontChoiceResolvesToTheRegistryFile() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let captionElement = EditorTextElement(id: "caption-1", text: "Spoken words", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")]), "font_family": .string("Inter-Bold")])
        let item = NativeEditorTimelineItem(selection: .init(kind: .text, id: "caption-1"), start: 0, end: 2)
        XCTAssertEqual(NativeEditorWireContract.captionFonts, NativeFontCatalog.shared.pickerFonts)
        for (font, file) in [("Inter", "Inter-Bold.ttf"), ("Fraunces", "Fraunces-Bold.ttf"), ("Space Grotesk", "SpaceGrotesk-Bold.ttf"),
                             ("Bebas Neue", "BebasNeue-Regular.ttf"), ("Great Vibes", "GreatVibes-Regular.ttf"),
                             // "Outfit" is repeated in the registry; the live (last) entry must win.
                             ("Outfit", "Outfit-VF.ttf")] {
            let document = EditorDocument(textElements: [captionElement],
                captionMeta: ["font": .string(font), "font_set": .bool(true)])
            let program = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source])
            let run = try XCTUnwrap(XCTUnwrap(program.recipe.textLayers.first).runs.first)
            XCTAssertEqual(program.assetURLs[run.fontAssetID]?.lastPathComponent, file, font)
        }
        // Without font_set the element keeps its own face even if a font is named.
        let untouched = try compiler.compile(document: EditorDocument(textElements: [captionElement], captionMeta: ["font": .string("Fraunces")]),
            clips: [clip], items: [item], sources: [0: source])
        let run = try XCTUnwrap(XCTUnwrap(untouched.recipe.textLayers.first).runs.first)
        XCTAssertEqual(untouched.assetURLs[run.fontAssetID]?.lastPathComponent, "Inter-Bold.ttf")
    }

    // KRI-110: the Display choice projects onto the caption's entrance the way
    // the backend's _apply_guided_caption_meta does — "word" pops in,
    // "sentence" is static — so the toggle is visible in the local preview.
    func testGuidedStoryCaptionDisplayStyleProjectsOntoTheEntranceEffect() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let captionElement = EditorTextElement(id: "caption-1", text: "Spoken words", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")]), "effect": .string("static")])
        let plainElement = EditorTextElement(id: "title-1", text: "A title", startS: 0, endS: 2, role: "generative_intro", raw: ["effect": .string("static")])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "caption-1"), start: 0, end: 2),
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "title-1"), start: 0, end: 2),
        ]
        for (style, effect) in [("word", "pop-in"), ("sentence", "static")] {
            let recipe = try compiler.compile(document: EditorDocument(textElements: [captionElement, plainElement], captionMeta: ["style": .string(style)]),
                clips: [clip], items: items, sources: [0: source]).recipe
            XCTAssertEqual(recipe.textLayers.first(where: { $0.id == "caption-1" })?.effect.rawValue, effect, style)
            XCTAssertEqual(recipe.textLayers.first(where: { $0.id == "title-1" })?.effect.rawValue, "static", "ordinary text is untouched")
        }
    }

    // KRI-110: a text element whose text is (transiently) empty draws nothing
    // rather than failing the whole composition — retyping a caption passes
    // through the empty string.
    func testWhitespaceOnlyTextElementIsSkippedNotFatal() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let document = EditorDocument(textElements: [
            EditorTextElement(id: "caption-1", text: "  \n", startS: 0, endS: 2, role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")])]),
            EditorTextElement(id: "title-1", text: "A title", startS: 0, endS: 2, role: "generative_intro"),
        ])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "caption-1"), start: 0, end: 2),
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "title-1"), start: 0, end: 2),
        ]
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.map(\.id), ["title-1"])
    }

    // A non-caption text element must not be touched by caption_meta, and
    // the global "Show captions" off toggle must drop caption-tagged
    // elements while leaving ordinary text alone.
    func testCaptionMetaDisabledSkipsOnlyCaptionTaggedTextElements() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3,
            trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let captionElement = EditorTextElement(id: "caption-1", text: "Spoken words", startS: 0, endS: 2,
            role: "generative_sequence", raw: ["source_params": .object(["source": .string("caption_cue")]), "color": .string("#0000FF")])
        let plainElement = EditorTextElement(id: "title-1", text: "A title", startS: 0, endS: 2, role: "generative_intro")
        let document = EditorDocument(textElements: [captionElement, plainElement], captionMeta: ["enabled": .bool(false), "color": .string("#FF0000")])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "caption-1"), start: 0, end: 2),
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "title-1"), start: 0, end: 2),
        ]
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.map(\.id), ["title-1"])
        // The surviving element's own color is untouched by captionMeta.
        let run = try XCTUnwrap(XCTUnwrap(recipe.textLayers.first).runs.first)
        XCTAssertEqual(run.fill, TextInk(red: 1, green: 1, blue: 1, alpha: 1))
    }

    func testNarrationKeepsVoiceTrackAndSlowsShortOriginalFootage() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 2), url: URL(fileURLWithPath: "/original.mp4"))
        let narration = ResolvedEditorSource(clipIndex: -1, mediaID: "voice", asset: MediaAsset(id: "voice", relativePath: "voice.mp4", fingerprint: fingerprint, duration: 5), url: URL(fileURLWithPath: "/voice.mp4"))
        var document = try NativeNarratedSourceTiming.hydrate(EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: 5)]), sources: [0: source])
        XCTAssertEqual(document.clips[0].raw["native_source_span_s"], .number(1.95))
        document.music = .init(trackID: "already-in-narration-mix")
        let program = try compiler.compile(document: document, clips: [EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 5, trimIn: 0, trimOut: 1.95, sourceDuration: 2, slotID: "shot")], items: [], sources: [0: source], audioSources: ["narration": narration])
        let visual = try XCTUnwrap(program.recipe.tracks.first(where: { $0.kind == .video })?.clips.first)
        XCTAssertEqual(visual.rate, 0.39, accuracy: 0.0001)
        XCTAssertEqual(visual.volume, 0)
        let voice = try XCTUnwrap(program.recipe.tracks.first(where: { $0.id == "narration" })?.clips.first)
        XCTAssertEqual(voice.sourceDuration, 5)
        XCTAssertEqual(voice.volume, 1)
        XCTAssertEqual(program.recipe.tracks.filter { $0.kind == .audio }.count, 1, "Do not layer the music bed over the rendered voiceover mix twice")
        XCTAssertEqual(program.assetURLs["narration"], narration.url)
    }

    /// KRI-374: the creator's song plays from the recipe's window, camera audio is forced to 0 even when the
    /// slot explicitly un-mutes it or the mix keeps the original level, and no catalog music is layered on top.
    func testCreatorSongEmitsSongTrackAndMutesCameraAudio() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6), url: URL(fileURLWithPath: "/original.mp4"))
        let song = ResolvedEditorSource(clipIndex: -1, mediaID: "song-item", asset: MediaAsset(id: "song-item", relativePath: "song.wav", fingerprint: fingerprint, duration: 200), url: URL(fileURLWithPath: "/song.wav"))
        var document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: 4, raw: ["muted": .bool(false)])])
        document.mix = ["original_level": .number(1)]
        document.music = .init(trackID: "catalog-bed")
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 6, slotID: "shot")
        let bed = NativeEditorSongBed(assetID: "song-item", sourceStart: 108, sourceDuration: 15, volume: 0.8, fadeIn: 0.5, fadeOut: 3)
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                           audioSources: [NativeEditorRenderCompiler.songSourceKey: song], sourceAudioPreserved: true, songBed: bed)

        let visual = try XCTUnwrap(program.recipe.tracks.first { $0.kind == .video }?.clips.first)
        XCTAssertEqual(visual.volume, 0, "an explicit un-mute or original_level must not leak camera audio over the song")
        let audio = program.recipe.tracks.filter { $0.kind == .audio }
        XCTAssertEqual(audio.map(\.id), ["song"], "no catalog music beside the creator's song")
        let rendered = try XCTUnwrap(audio.first?.clips.first)
        XCTAssertEqual(rendered.sourceStart, 108)
        XCTAssertEqual(rendered.sourceDuration, 4, accuracy: 0.0001, "the song never outlasts the video")
        XCTAssertEqual(rendered.timelineStart, 0)
        XCTAssertEqual(rendered.volume, 0.8, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(rendered.audioFadeIn), 0.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(rendered.audioFadeOut), 2, accuracy: 0.0001, "a fade is capped at half the played length")
        XCTAssertEqual(program.assetURLs["song"], song.url)

        // A window past the end of the file is refused rather than silently clamped to nothing.
        let past = NativeEditorSongBed(assetID: "song-item", sourceStart: 500)
        let clamped = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                           audioSources: [NativeEditorRenderCompiler.songSourceKey: song], songBed: past)
        XCTAssertTrue(clamped.recipe.tracks.filter { $0.kind == .audio }.isEmpty)
        // ...and with no song actually playing, the camera is not muted for nothing (KRI-428).
        XCTAssertEqual(try XCTUnwrap(clamped.recipe.tracks.first { $0.kind == .video }?.clips.first).volume, 1)
    }

    /// KRI-428: an edited bed (volume, start) is what the preview plays, and a removed song (no song source) leaves
    /// the camera's own audio, even when the slot never carried an explicit un-mute.
    func testEditedSongBedIsPlayedAndRemovedSongRestoresCameraAudio() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6), url: URL(fileURLWithPath: "/original.mp4"))
        let song = ResolvedEditorSource(clipIndex: -1, mediaID: "song-item", asset: MediaAsset(id: "song-item", relativePath: "song.wav", fingerprint: fingerprint, duration: 200), url: URL(fileURLWithPath: "/song.wav"))
        let document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: 4)])
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 6, slotID: "shot")
        let edited = NativeEditorSongBed(assetID: "song-item", sourceStart: 42.5, sourceDuration: 4, volume: 0.35)
        let playing = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                           audioSources: [NativeEditorRenderCompiler.songSourceKey: song], sourceAudioPreserved: false, songBed: edited)
        let bed = try XCTUnwrap(playing.recipe.tracks.first { $0.id == "song" }?.clips.first)
        XCTAssertEqual(bed.sourceStart, 42.5, accuracy: 0.0001)
        XCTAssertEqual(bed.volume, 0.35, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(playing.recipe.tracks.first { $0.kind == .video }?.clips.first).volume, 0)

        let removed = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], sourceAudioPreserved: true)
        XCTAssertNil(removed.recipe.tracks.first { $0.id == "song" })
        XCTAssertEqual(try XCTUnwrap(removed.recipe.tracks.first { $0.kind == .video }?.clips.first).volume, 1)
    }

    /// KRI-457: the song follows the video. The bed's own duration is only the window the last saved recipe had, so
    /// an extended video plays the song longer (while it has time left) and a shortened one plays it shorter.
    func testSongLengthFollowsTheVideoNotTheStaleBedDuration() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 30), url: URL(fileURLWithPath: "/original.mp4"))
        func song(duration: Double) -> ResolvedEditorSource {
            ResolvedEditorSource(clipIndex: -1, mediaID: "song-item", asset: MediaAsset(id: "song-item", relativePath: "song.wav", fingerprint: fingerprint, duration: duration), url: URL(fileURLWithPath: "/song.wav"))
        }
        func compile(video seconds: Double, songLength: Double, start: Double, preserved: Bool = true) throws -> NativeEditorRenderProgram {
            let document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: seconds)])
            let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: seconds, trimIn: 0, trimOut: seconds, sourceDuration: 30, slotID: "shot")
            // The bed still says 4s: the length of the window the last saved recipe had.
            let bed = NativeEditorSongBed(assetID: "song-item", sourceStart: start, sourceDuration: 4, volume: 0.7, fadeIn: 0.5, fadeOut: 3)
            return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                        audioSources: [NativeEditorRenderCompiler.songSourceKey: song(duration: songLength)],
                                        sourceAudioPreserved: preserved, songBed: bed)
        }
        func songClip(_ program: NativeEditorRenderProgram) -> TimelineClip? { program.recipe.tracks.first { $0.id == "song" }?.clips.first }
        func cameraLevels(_ program: NativeEditorRenderProgram) -> [Double] { program.recipe.tracks.first { $0.kind == .video }?.clips.map(\.volume) ?? [] }

        // Extended past the bed's 4s: the song plays the whole video.
        let extended = try compile(video: 9, songLength: 200, start: 100)
        XCTAssertEqual(try XCTUnwrap(songClip(extended)).sourceDuration, 9, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(songClip(extended)).sourceStart, 100)
        XCTAssertEqual(try XCTUnwrap(songClip(extended)).audioFadeOut ?? 0, 3, accuracy: 0.0001, "the fade-out lands at the song's real end")
        // Shortened below it: shorter.
        let shortened = try compile(video: 2.5, songLength: 200, start: 100)
        XCTAssertEqual(try XCTUnwrap(songClip(shortened)).sourceDuration, 2.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(songClip(shortened)).audioFadeOut ?? 0, 1.25, accuracy: 0.0001, "fades stay capped at half the length")
        // The song runs out before the video: it plays to its end and stops, with no throw, and camera stays muted.
        let runsOut = try compile(video: 9, songLength: 105, start: 100)
        XCTAssertEqual(try XCTUnwrap(songClip(runsOut)).sourceDuration, 5, accuracy: 0.0001)
        XCTAssertEqual(cameraLevels(runsOut), [0])
        // A start past the end of the file plays nothing, so the camera is not muted for nothing.
        let past = try compile(video: 9, songLength: 105, start: 500)
        XCTAssertNil(songClip(past))
        XCTAssertEqual(cameraLevels(past), [1])
    }

    func testWithoutSongSourceCameraAudioFollowsTheUsualRules() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 6), url: URL(fileURLWithPath: "/original.mp4"))
        let document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: 4)])
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 6, slotID: "shot")
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source])
        XCTAssertEqual(try XCTUnwrap(program.recipe.tracks.first { $0.kind == .video }?.clips.first).volume, 1)
        XCTAssertTrue(program.recipe.tracks.filter { $0.kind == .audio }.isEmpty)
    }

    func testNarrationTailHoldsUntouchedFinalClipInsteadOfSlowingIt() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let first = ResolvedEditorSource(clipIndex: 0, mediaID: "first", asset: MediaAsset(id: "first", relativePath: "first.mp4", fingerprint: fingerprint, duration: 2), url: URL(fileURLWithPath: "/first.mp4"))
        let last = ResolvedEditorSource(clipIndex: 1, mediaID: "last", asset: MediaAsset(id: "last", relativePath: "last.mp4", fingerprint: fingerprint, duration: 3), url: URL(fileURLWithPath: "/last.mp4"))
        let document = EditorDocument(clips: [
            .init(id: "first", clipIndex: 0, inS: 0, durationS: 2),
            .init(id: "last", clipIndex: 1, inS: 0, durationS: 3)
        ])
        // A retimed first shot leaves the final authored shot at three seconds,
        // while narration extends its output window to five seconds.
        let clips = [
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2, trimIn: 0, trimOut: 2, sourceDuration: 2, slotID: "first"),
            EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 1, start: 2, end: 7, trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "last")
        ]
        let recipe = try compiler.compile(document: document, clips: clips, items: [], sources: [0: first, 1: last]).recipe
        let rendered = try XCTUnwrap(recipe.tracks.first(where: { $0.kind == .video })?.clips.last)
        XCTAssertEqual(rendered.rate, 1, accuracy: 0.0001)
        XCTAssertEqual(rendered.sourceDuration, 3, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(rendered.holdDuration), 2, accuracy: 0.0001)
    }

    func testNarrationCanEndBeforeAnExtendedVisualTimeline() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 8), url: URL(fileURLWithPath: "/original.mp4"))
        let narration = ResolvedEditorSource(clipIndex: -1, mediaID: "voice", asset: MediaAsset(id: "voice", relativePath: "voice.mp4", fingerprint: fingerprint, duration: 5), url: URL(fileURLWithPath: "/voice.mp4"))
        let document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: 8)])
        let program = try compiler.compile(document: document, clips: [EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 8, trimIn: 0, trimOut: 8, sourceDuration: 8, slotID: "shot")], items: [], sources: [0: source], audioSources: ["narration": narration])
        let voice = try XCTUnwrap(program.recipe.tracks.first { $0.id == "narration" }?.clips.first)
        XCTAssertEqual(voice.sourceDuration, 5)
        XCTAssertEqual(voice.volume, 1)
        XCTAssertEqual(program.recipe.tracks.first { $0.kind == .video }?.clips.first?.volume, 0)
    }

    func testTalkingHeadWithoutSlotsCompilesFromTextFreeBaseAndKeepsTextLive() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let duration = try await AVURLAsset(url: url).load(.duration).seconds
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        var document = EditorDocument(editFormat: "talking_head", textElements: [
            .init(id: "title", text: "Editable title", startS: 0, endS: min(2, duration))
        ], capabilities: ["timeline": .init(editable: false)], revision: .init(baseGeneration: "generation"))
        let items = [NativeEditorTimelineItem(selection: .init(kind: .text, id: "title"), start: 0, end: min(2, duration), zIndex: 0, sourceIndex: 0)]
        XCTAssertThrowsError(try compiler.compile(document: document, clips: [], items: items, sources: [:])) {
            XCTAssertEqual($0 as? NativeEditorRenderError, .missingVideoTrack)
        }
        let base = try XCTUnwrap(NativeEditorBaseSource(variant: [
            "resolved_archetype": .string("talking_head"),
            "base_video_path": .string("job/base.mp4"),
            "base_video_url": .string("https://storage.example/base.mp4"),
            "output_url": .string("https://storage.example/finished.mp4")
        ], document: document))
        document = try base.hydrate(document, duration: duration)
        XCTAssertEqual(document.clips.count, 1)
        XCTAssertEqual(document.capabilities["timeline"]?.editable, false)
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
            trimIn: 0, trimOut: duration, sourceDuration: duration, slotID: "native-composite-base")
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: base.mediaID,
            asset: MediaAsset(id: "base", relativePath: "base.mp4", fingerprint: try SHA256Fingerprinter().fingerprint(file: url)), url: url)
        let program = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source])
        let preview = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        XCTAssertEqual(program.recipe.textLayers.count, 1)
        document.textElements[0].text = "Changed locally"
        let changed = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source])
        _ = try preview.updateText(recipe: changed.recipe, assetURLs: changed.assetURLs)
        XCTAssertEqual(changed.recipe.textLayers.first?.runs.map(\.text).joined(separator: " "), "Changed locally")
    }

    /// Job 385e3b13 shape: a card and no video clip. The card alone made the
    /// recipe valid, so the editor played it over a black canvas instead of
    /// failing over to the finished MP4.
    func testMediaOverlayWithoutVideoClipsDoesNotCompile() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let media = ResolvedEditorSource(clipIndex: -1, mediaID: "card", asset: MediaAsset(id: "card", relativePath: "card.png",
            fingerprint: fingerprint, naturalSize: MediaSize(width: 96, height: 160)), url: URL(fileURLWithPath: "/fixture/card.png"))
        let document = EditorDocument(editFormat: "subtitled", mediaOverlays: [.init(id: "card", startS: 0.2, endS: 1.2)])
        let item = NativeEditorTimelineItem(selection: .init(kind: .mediaOverlay, id: "card"), start: 0.2, end: 1.2)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        XCTAssertThrowsError(try compiler.compile(document: document, clips: [], items: [item], sources: [:],
                                                  mediaSources: ["overlay:card": media])) {
            XCTAssertEqual($0 as? NativeEditorRenderError, .missingVideoTrack)
        }
        // The same card over a clip is an ordinary preview.
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: fingerprint), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2,
            trimIn: 0, trimOut: 2, sourceDuration: 2, slotID: "slot")
        let recipe = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source],
                                          mediaSources: ["overlay:card": media]).recipe
        XCTAssertEqual(recipe.tracks.first { $0.kind == .video }?.clips.count, 1)
        XCTAssertEqual(recipe.tracks.first { $0.kind == .overlay }?.clips.count, 1)
    }

    func testAuthoredMotionNormalizesServerDefaultsAndBounds() throws {
        XCTAssertNil(try NativeEditorRenderCompiler.normalizedMotion(effect: "slide-up", raw: nil))
        XCTAssertNil(try NativeEditorRenderCompiler.normalizedMotion(effect: "slide-up", raw: .object(["version": .number(1)])))
        let motion = try XCTUnwrap(NativeEditorRenderCompiler.normalizedMotion(effect: "slide-down", raw: .object([
            "version": .number(2), "speed": .number(99), "intensity": .number(-1),
            "easing": .string("unknown"), "exit_s": .number(0.6)
        ])))
        XCTAssertEqual(motion.speed, 4)
        XCTAssertEqual(motion.intensity, 0)
        XCTAssertEqual(motion.direction, .down)
        XCTAssertEqual(motion.travelPx, 220)
        XCTAssertEqual(motion.easing, .easeOutCubic)
        XCTAssertEqual(motion.exitS, 0.6)
        try motion.validate()
    }

    func testSourceFixtureUsesLiveCompositorAndUpdatesText() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: url)
        let sources = Dictionary(uniqueKeysWithValues: [0, 1].map { index in
            (index, ResolvedEditorSource(clipIndex: index, mediaID: "source-\(index)",
                asset: MediaAsset(id: "source-\(index)", relativePath: "source-\(index)", fingerprint: fingerprint), url: url))
        })
        let program = try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems, sources: sources)
        XCTAssertFalse(program.recipe.textLayers.isEmpty)
        XCTAssertEqual(program.recipe.textLayers.first?.runs.first?.fontVariations["opsz"], 9)
        _ = try await LivePreviewComposition(recipe: program.recipe, assetURLs: program.assetURLs)
        let textID = try XCTUnwrap(session.document.textElements.first?.id)
        XCTAssertEqual(session.document.textElements.first?.raw["position"], .string("custom"))
        for content in ["This entire sentence stays on one line even past the old width", "First\nSecond", "First\n\nThird", "First\n"] {
            session.updateTextContent(id: textID, content: content)
            XCTAssertEqual(session.document.textElements.first { $0.id == textID }?.raw["wrap_lines"], .bool(false))
            let edited = try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems, sources: sources)
            let layer = try XCTUnwrap(edited.recipe.textLayers.first { $0.id == textID })
            XCTAssertEqual(layer.runs.map(\.text), content.components(separatedBy: "\n").map { $0.isEmpty ? " " : $0 })
            _ = try await LivePreviewComposition(recipe: edited.recipe, assetURLs: edited.assetURLs)
        }
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertNotNil(session.player?.currentItem?.videoComposition)
        await session.prepareFixtureSourcePreview(url: URL(fileURLWithPath: "/unavailable-native-source.mp4"))
        if case .failed = session.sourcePreviewState {} else { XCTFail("Missing source should fail preparation") }
        session.togglePlayback()
        XCTAssertFalse(session.isPlaying, "A stale player must not play behind an unavailable source preview")
    }

    func testVisualCardCompilesFillAndBaseAudioWindow() throws {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        var document = EditorDocument(editFormat: "montage", clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)])
        document.visualBlocks = [.init(id: "card", kind: "text_card", startS: 1, endS: 3, raw: [
            "background": .object(["type": .string("solid"), "color": .string("#123456")]),
            "audio_policy": .object(["base": .string("mute")]), "transition_in": .string("fade")
        ])]
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source]).recipe
        XCTAssertEqual(recipe.visualFills.count, 1)
        XCTAssertEqual(recipe.visualFills[0].start, 1)
        XCTAssertTrue(recipe.visualFills[0].fadeIn)
        XCTAssertEqual(recipe.audio.muteWindows, [.init(start: 1, end: 3, clipIDs: ["slot"])])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.visualBlocks))
    }

    /// KRI-182 step 1: a phone-lane image media overlay (x/y/scale/start/end,
    /// "fade" entrance/exit tokens) plus a point sfx with gain compile onto
    /// the overlays track and a dedicated sfx audio track respectively. The
    /// "fade" tokens must not throw (see the entrance/exit guards in
    /// NativeEditorRenderCompiler.swift) -- `visualPlacement` legitimately
    /// stays nil here (no `editor_style` on this overlay), so positioning is
    /// asserted through the same cover-fit `transform` every other pip/
    /// overlay card already uses.
    func testImageOverlayAndPointSfxCompileOntoTheOverlayAndAudioTracks() throws {
        let videoFingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let videoSource = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: videoFingerprint, duration: 4),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let overlayFingerprint = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 50)
        let overlaySource = ResolvedEditorSource(preserveAlpha: true, clipIndex: -1, mediaID: "overlay-media",
            asset: MediaAsset(id: "overlay-asset", relativePath: "overlay.png", fingerprint: overlayFingerprint, naturalSize: MediaSize(width: 400, height: 400)),
            url: URL(fileURLWithPath: "/fixture/overlay.png"))
        let sfxFingerprint = AssetFingerprint(hex: String(repeating: "c", count: 64), byteCount: 30)
        let sfxSource = ResolvedEditorSource(clipIndex: -1, mediaID: "sfx-media",
            asset: MediaAsset(id: "sfx-asset", relativePath: "sfx.wav", fingerprint: sfxFingerprint, duration: 1.5),
            url: URL(fileURLWithPath: "/fixture/sfx.wav"))

        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let overlay = EditorTimedEffect(id: "overlay-1", startS: 1, endS: 3, kind: "image",
            raw: ["kind": .string("image"), "x_frac": .number(0.6), "y_frac": .number(0.7), "scale": .number(0.4),
                  "z": .number(1), "entrance_token": .string("fade"), "exit_token": .string("fade")])
        let sfx = EditorTimedEffect(id: "sfx-1", startS: 1.2, endS: 1.2, pointS: 1.2, kind: "sfx",
            raw: ["at_s": .number(1.2), "gain": .number(1.5)])
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
            soundEffects: [sfx], mediaOverlays: [overlay])
        let items: [NativeEditorTimelineItem] = [
            .init(selection: .init(kind: .mediaOverlay, id: "overlay-1"), start: 1, end: 3),
            .init(selection: .init(kind: .soundEffect, id: "sfx-1"), start: 1.2, end: 1.2),
        ]
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let program = try compiler.compile(document: document, clips: [clip], items: items,
            sources: [0: videoSource], audioSources: ["sfx:sfx-1": sfxSource], mediaSources: ["overlay:overlay-1": overlaySource])

        let overlayTrack = try XCTUnwrap(program.recipe.tracks.first(where: { $0.id == "overlays" }))
        let overlayClip = try XCTUnwrap(overlayTrack.clips.first)
        XCTAssertNil(overlayClip.visualPlacement, "no editor_style on this phone-lane overlay -- positioning stays on the transform path")
        XCTAssertEqual(overlayClip.transform.scale, 0.225, accuracy: 0.0001)
        XCTAssertEqual(overlayClip.transform.positionX, 108, accuracy: 0.0001)
        XCTAssertEqual(overlayClip.transform.positionY, 384, accuracy: 0.0001)
        XCTAssertEqual(overlayClip.timelineStart, 1)
        XCTAssertEqual(overlayClip.overlayPreserveAlpha, true)
        XCTAssertNotEqual(overlayClip.overlayPopIn, true, "\"fade\" is not \"pop_in\"")
        XCTAssertNil(overlayClip.overlayDissolveSeed, "\"fade\" is not \"dissolve-out\"")

        let sfxTrack = try XCTUnwrap(program.recipe.tracks.first(where: { $0.id == "sfx:sfx-1" }))
        let sfxClip = try XCTUnwrap(sfxTrack.clips.first)
        XCTAssertEqual(sfxClip.volume, 1.5)
        XCTAssertEqual(sfxClip.timelineStart, 1.2, accuracy: 0.0001)
        XCTAssertEqual(sfxClip.sourceDuration, 1.5, accuracy: 0.0001)
    }

    /// KRI-182: a phone Talking card's "fade" tokens must actually fade it,
    /// on the pinned device recipe's curve, without moving the card off the
    /// transform (cover-fit) positioning every existing pip card uses.
    func testFadeTokensFadeTheCardWithoutChangingItsPlacement() throws {
        let videoFingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let videoSource = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: videoFingerprint, duration: 4),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        // Non-square on purpose: where transform and placement sizing diverge.
        let overlaySource = ResolvedEditorSource(preserveAlpha: true, clipIndex: -1, mediaID: "overlay-media",
            asset: MediaAsset(id: "overlay-asset", relativePath: "overlay.png",
                fingerprint: AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 50),
                naturalSize: MediaSize(width: 400, height: 200)),
            url: URL(fileURLWithPath: "/fixture/overlay.png"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let item = NativeEditorTimelineItem(selection: .init(kind: .mediaOverlay, id: "card"), start: 1, end: 3)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        func compiled(entrance: String, exit: String, styled: Bool = false) throws -> TimelineClip {
            var raw: [String: JSONValue] = ["kind": .string("image"), "x_frac": .number(0.6), "y_frac": .number(0.7),
                "scale": .number(0.4), "entrance_token": .string(entrance), "exit_token": .string(exit)]
            if styled { raw["editor_style"] = .object(NativeVisualAuthoring.defaultStyle) }
            let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
                mediaOverlays: [EditorTimedEffect(id: "card", startS: 1, endS: 3, kind: "image", raw: raw)])
            let recipe = try compiler.compile(document: document, clips: [clip], items: [item],
                sources: [0: videoSource], mediaSources: ["overlay:card": overlaySource]).recipe
            XCTAssertNoThrow(try recipe.validate())
            return try XCTUnwrap(recipe.tracks.first(where: { $0.id == "overlays" })?.clips.first)
        }

        let plain = try compiled(entrance: "none", exit: "none")
        let faded = try compiled(entrance: "fade", exit: "fade")
        XCTAssertNil(plain.overlayFadeIn)
        XCTAssertNil(plain.overlayFadeOut)
        XCTAssertEqual(faded.overlayFadeIn, true)
        XCTAssertEqual(faded.overlayFadeOut, true)
        // Positioning is untouched: no placement, and apart from the fade
        // fields the faded clip is the plain clip, transform included.
        XCTAssertNil(faded.visualPlacement)
        var unfaded = faded
        unfaded.overlayFadeIn = nil
        unfaded.overlayFadeOut = nil
        XCTAssertEqual(unfaded, plain)

        // A real ramp, not a static card: 0 at the edges, half after 75 ms,
        // opaque between -- the pinned device recipe's 0.15 s curve.
        XCTAssertEqual(faded.overlayFadeAlpha(at: 1), 0, accuracy: 1e-9)
        XCTAssertEqual(faded.overlayFadeAlpha(at: 1.075), 0.5, accuracy: 1e-9)
        XCTAssertEqual(faded.overlayFadeAlpha(at: 1.15), 1, accuracy: 1e-9)
        XCTAssertEqual(faded.overlayFadeAlpha(at: 2), 1)
        XCTAssertEqual(faded.overlayFadeAlpha(at: 2.925), 0.5, accuracy: 1e-9)
        XCTAssertEqual(faded.overlayFadeAlpha(at: 3), 0, accuracy: 1e-9)
        for time in stride(from: 1.0, through: 3.0, by: 0.05) {
            XCTAssertEqual(faded.overlayFadeAlpha(at: time), VisualMediaPlacement.fadeEnvelope(at: time,
                windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true), accuracy: 1e-12)
            XCTAssertEqual(plain.overlayFadeAlpha(at: time), 1)
        }

        // Each edge is independent.
        let entranceOnly = try compiled(entrance: "fade", exit: "none")
        XCTAssertEqual(entranceOnly.overlayFadeIn, true)
        XCTAssertNil(entranceOnly.overlayFadeOut)
        XCTAssertEqual(entranceOnly.overlayFadeAlpha(at: 2.99), 1)

        // The mirror image of entranceOnly: only the exit token is "fade".
        let exitOnly = try compiled(entrance: "none", exit: "fade")
        XCTAssertNil(exitOnly.overlayFadeIn)
        XCTAssertEqual(exitOnly.overlayFadeOut, true)
        XCTAssertEqual(exitOnly.overlayFadeAlpha(at: 1.01), 1)
        XCTAssertEqual(exitOnly.overlayFadeAlpha(at: 2.925), 0.5, accuracy: 1e-9)
        XCTAssertNil(exitOnly.visualPlacement)

        // The entrance and exit guards are independent, so a card can author
        // "fade" in and "dissolve-out" out on the same clip -- the compiler
        // must set both fields rather than one silently winning.
        let fadeInDissolveOut = try compiled(entrance: "fade", exit: "dissolve-out")
        XCTAssertEqual(fadeInDissolveOut.overlayFadeIn, true)
        XCTAssertNil(fadeInDissolveOut.overlayFadeOut)
        XCTAssertNotNil(fadeInDissolveOut.overlayDissolveSeed)

        // A styled card already sits on the placement path; it fades there,
        // with no second fade on the clip.
        let styled = try compiled(entrance: "fade", exit: "fade", styled: true)
        let placement = try XCTUnwrap(styled.visualPlacement)
        XCTAssertTrue(placement.fadeIn)
        XCTAssertTrue(placement.fadeOut)
        XCTAssertNil(styled.overlayFadeIn)
        XCTAssertNil(styled.overlayFadeOut)
        // Each edge is independent on the placement path too.
        let styledEntranceOnly = try XCTUnwrap(compiled(entrance: "fade", exit: "none", styled: true).visualPlacement)
        XCTAssertTrue(styledEntranceOnly.fadeIn)
        XCTAssertFalse(styledEntranceOnly.fadeOut)
        let styledExitOnly = try XCTUnwrap(compiled(entrance: "none", exit: "fade", styled: true).visualPlacement)
        XCTAssertFalse(styledExitOnly.fadeIn)
        XCTAssertTrue(styledExitOnly.fadeOut)
    }

    func testLongCutTimelineReusesTracksAndCrossfadesKeepTwoSources() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        for overlap in [false, true] {
            let count = overlap ? 4 : 40
            let length = overlap ? 1.0 : 0.1
            let step = overlap ? 0.75 : 0.1
            let clips = (0..<count).map { index in
                TimelineClip(id: "cut-\(index)", sourceAssetID: "source", sourceDuration: length,
                    timelineStart: Double(index) * step,
                    transition: overlap && index > 0 ? Transition(duration: 0.25) : nil)
            }
            let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source.mp4")],
                tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": url])
            let tracks = try await preview.playerItem.asset.loadTracks(withMediaType: .video)
            XCTAssertEqual(tracks.count, overlap ? 2 : 1)
            XCTAssertEqual(preview.description.duration, Double(count - 1) * step + length, accuracy: 0.000001)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            for second in [0.05, preview.description.duration / 2, preview.description.duration - 0.05] {
                let frame = try await generator.image(at: CMTime(seconds: second, preferredTimescale: 600))
                XCTAssertEqual(frame.image.width, 1080)
                XCTAssertEqual(frame.image.height, 1920)
            }
        }
    }

    func testLeavingAndReleasingEditorStopsRetainedPlayer() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        var session: NativeEditorSession? = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: url)
        let player = try XCTUnwrap(session?.player)
        defer { player.pause() }
        session?.togglePlayback()
        session?.pausePlayback()
        XCTAssertEqual(player.rate, 0)
        XCTAssertEqual(session?.isPlaying, false)
        session?.togglePlayback()
        XCTAssertEqual(player.rate, 1)
        weak var released = session
        session = nil
        for _ in 0..<100 {
            if released == nil { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertNil(released)
        XCTAssertEqual(player.rate, 0, "A player retained by its view must be silent after the editor closes")
    }

    func testReplacingPreviewStopsOldPlayerBeforeNewPlayerCanPlay() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: url)
        let oldPlayer = try XCTUnwrap(session.player)
        defer { oldPlayer.pause(); session.player?.pause() }
        session.togglePlayback()
        for _ in 0..<100 {
            if oldPlayer.timeControlStatus == .playing { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(oldPlayer.rate, 1)
        // Source preparation/overlay changes replace a playing preview.
        await session.prepareFixtureSourcePreview(url: url)
        let replacement = try XCTUnwrap(session.player)
        XCTAssertFalse(replacement === oldPlayer)
        XCTAssertEqual(oldPlayer.rate, 0, "A retained outgoing preview must never continue its audio")
        XCTAssertNil(oldPlayer.currentItem, "The outgoing view must not be able to restart detached media")
        for _ in 0..<100 {
            if replacement.timeControlStatus == .playing { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(replacement.rate, 1)
        session.togglePlayback()
        XCTAssertFalse(session.isPlaying)
        XCTAssertEqual(replacement.rate, 0)
        XCTAssertEqual(oldPlayer.rate, 0, "Pausing the editor must leave no second audio stream")
    }

    func testPhotoAndVideoOverlayPlaybackAdvancesAfterScrubbing() async throws {
        let footage = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let imageURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        defer { try? FileManager.default.removeItem(at: imageURL) }
        let image = UIGraphicsImageRenderer(size: CGSize(width: 48, height: 48)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 48, height: 48))
        }
        try XCTUnwrap(image.pngData()).write(to: imageURL)
        for (kind, url) in [("image", imageURL), ("video", footage)] {
            var draft = NativeEditorUITestFixtures.captionVisuals
            var document = EditorDocument(snapshot: draft.serverSnapshot)
            document.visualBlocks[0].raw["media_kind"] = .string(kind)
            document.visualBlocks[0].raw["playback_rate"] = .number(2)
            draft.serverSnapshot = document.encodeSnapshot()
            let session = NativeEditorSession(draft: draft)
            let media = ResolvedEditorSource(clipIndex: -1, mediaID: "fixture",
                asset: MediaAsset(id: "fixture", relativePath: url.lastPathComponent,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: url), duration: kind == "video" ? 4 : nil), url: url)
            await session.prepareFixtureSourcePreview(url: footage, mediaSources: ["visual:paper-media:paper-media": media])
            XCTAssertEqual(session.sourcePreviewState, .ready)
            // Slow dragging lets several composed stills finish before Play;
            // the final request may still be rendering during the handoff.
            for step in 0..<24 {
                session.seek(to: 0.1 + Double(step) * 0.025)
                try await Task.sleep(for: .milliseconds(60))
            }
            session.seek(to: 0.5)
            for _ in 0..<100 {
                if session.scrubPreviewFrame != nil { break }
                try await Task.sleep(for: .milliseconds(25))
            }
            XCTAssertNotNil(session.scrubPreviewFrame)
            session.togglePlayback()
            for _ in 0..<160 {
                if session.currentTime >= 1.5 { break }
                try await Task.sleep(for: .milliseconds(25))
            }
            XCTAssertGreaterThanOrEqual(session.currentTime, 1.5, "Playback must advance into the \(kind) overlay")
            XCTAssertNil(session.scrubPreviewFrame, "The scrub still must not cover continuous playback")
            session.seek(to: 0.75)
            XCTAssertFalse(session.isPlaying, "A new scrub still pauses playback")
        }
    }

    func testTrimKeepsPlayerStableUntilReleaseThenRefreshes() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        let originalItem = try XCTUnwrap(session.player?.currentItem)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let originalDuration = session.duration
        session.beginTrim(clipID: clip.id, edge: .trailing)
        for step in 1...8 {
            session.updateTrim(by: -Double(step) * 0.05)
            try await Task.sleep(for: .milliseconds(35))
            XCTAssertTrue(session.player?.currentItem === originalItem)
            XCTAssertEqual(session.duration, originalDuration - Double(step) * 0.05, accuracy: 0.01)
        }
        session.endTrim()
        for _ in 0..<100 {
            if session.player?.currentItem !== originalItem { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertFalse(session.player?.currentItem === originalItem)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(session.duration, originalDuration - 0.4, accuracy: 0.01)
    }

    func testLivePreviewStillDecodesAfterTrimAndUndo() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let originalDuration = session.duration
        session.addText(content: "Final caption")
        let tail = try XCTUnwrap(session.document.textElements.last)
        session.updateTextTiming(id: tail.id, startS: originalDuration - 0.3, endS: originalDuration)
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let clip = try XCTUnwrap(session.timelineClips.first)
        session.beginTrim(clipID: clip.id, edge: .trailing)
        session.updateTrim(by: -min(1, (clip.end - clip.start) / 2))
        session.endTrim()
        XCTAssertLessThan(session.duration, originalDuration - 0.3)
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let item = try XCTUnwrap(session.player?.currentItem)
        let generator = AVAssetImageGenerator(asset: item.asset)
        generator.videoComposition = item.videoComposition
        let frame = try await generator.image(at: CMTime(seconds: session.duration - 0.1, preferredTimescale: 600))
        XCTAssertGreaterThan(frame.image.width, 0)
        session.undo()
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(session.duration, originalDuration, accuracy: 0.01)
        XCTAssertEqual(session.document.textElements.last?.id, tail.id)
    }

    func testTrimPastTextAndCaptionBoundariesPreservesUndo() throws {
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let document = EditorDocument(textElements: [
            .init(id: "crossing", text: "Crossing", startS: 1, endS: 4),
            .init(id: "tail", text: "Tail", startS: 3, endS: 4)
        ], captionCues: [.init(id: "cue", startS: 3, endS: 4, text: "Last caption")])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "crossing"), start: 1, end: 4),
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "tail"), start: 3, end: 4),
            NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 3, end: 4)
        ]
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        func compile(_ duration: Double) throws -> KriaMediaEngine.EditRecipe {
            let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
                trimIn: 0, trimOut: duration, sourceDuration: 4, slotID: "slot")
            return try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        }
        XCTAssertEqual(try compile(4).textLayers.count, 3)
        let trimmed = try compile(2)
        XCTAssertEqual(trimmed.textLayers.map(\.id), ["crossing"])
        XCTAssertEqual(trimmed.textLayers.first?.end, 2)
        XCTAssertEqual(try compile(1).textLayers.count, 0)
        XCTAssertEqual(try compile(4).textLayers.count, 3)
        XCTAssertEqual(document.textElements.last?.startS, 3)
    }

    func testRoundedContainerEndDoesNotRejectTextAtFinalCut() throws {
        let duration = 44.866
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
            trimIn: 0, trimOut: duration, sourceDuration: duration, slotID: "slot")
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let document = EditorDocument(textElements: [.init(id: "title", text: "Final title", startS: 0, endS: 44.867)],
            captionCues: [.init(id: "cue", startS: 43, endS: 44.867, text: "Last caption")])
        let items = [
            NativeEditorTimelineItem(selection: .init(kind: .text, id: "title"), start: 0, end: 44.867),
            NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: "cue"), start: 43, end: 44.867)
        ]
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source]).recipe
        XCTAssertEqual(recipe.textLayers.count, 2)
        for layer in recipe.textLayers { XCTAssertEqual(layer.end, duration, accuracy: 0.000001) }
        XCTAssertEqual(document.textElements[0].endS, 44.867)
        XCTAssertEqual(document.captionCues[0].endS, 44.867)
    }

    func testWordCaptionsUseStoredTimingsUntilTheirTextIsEdited() throws {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
            trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)), url: URL(fileURLWithPath: "/fixture/original.mov"))
        var document = EditorDocument(editFormat: "subtitled", clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4)],
            captionMeta: ["style": .string("word"), "font": .string("Inter")],
            captionCues: [.init(id: "cue", startS: 1, endS: 3, text: "hello world", raw: ["words": .array([
                .object(["text": .string("hello"), "start_s": .number(1.2), "end_s": .number(1.5)]),
                .object(["text": .string("world"), "start_s": .number(2.1), "end_s": .number(2.8)])
            ])])])
        let item = NativeEditorTimelineItem(selection: EditorSelection(kind: .captionCue, id: "cue"), start: 1, end: 3, zIndex: 400, sourceIndex: 0)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let original = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe.textLayers[0]
        XCTAssertEqual(original.start, 1.2, accuracy: 0.0001)
        XCTAssertEqual(original.end, 2.8, accuracy: 0.0001)
        XCTAssertEqual(original.karaoke?.starts.last ?? -1, 0.9, accuracy: 0.0001)
        XCTAssertEqual(original.karaoke?.activeOnly, true)
        document.captionCues[0].text = "new words"
        let edited = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe.textLayers[0]
        XCTAssertEqual(edited.start, 1)
        XCTAssertEqual(edited.end, 3)
        XCTAssertEqual(edited.karaoke?.starts, [0, 1])
        document.captionMeta["enabled"] = .bool(false)
        XCTAssertTrue(try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source]).recipe.textLayers.isEmpty)
    }

    /// KRI-232: a cleaned-up phone Talking edit keeps its cues/words on the cut
    /// timeline. Every burned word must land on the SOURCE instant it is
    /// spoken -- cut-time + everything removed before it -- in spoken order.
    /// Before the fix the source played whole, so "Abi" burned 0.64s and
    /// "Yılda" 1.0s ahead of the speech.
    func testWordCaptionsTrackSpeechAcrossSpeechCleanupCuts() throws {
        let removed: [(start: Double, end: Double)] = [(2.55, 2.97), (4.365, 4.581), (10.97, 11.33)]
        func spoken(_ cutTime: Double) -> Double {
            removed.reduce(cutTime) { time, span in time >= span.start ? time + span.end - span.start : time }
        }
        func words(_ rows: [(String, Double, Double)]) -> JSONValue {
            .array(rows.map { .object(["text": .string($0.0), "start_s": .number($0.1), "end_s": .number($0.2)]) })
        }
        let cues: [EditorCaptionCue] = [
            .init(id: "c4", startS: 6.62, endS: 7.44, text: "Abi çok güzeldi.", raw: ["words": words([
                ("Abi", 6.62, 6.76), ("çok", 6.76, 6.94), ("güzeldi.", 6.94, 7.44)])]),
            .init(id: "c5", startS: 8.90, endS: 10.18, text: "Her yıl bunu en az bir kere yapalım.", raw: ["words": words([
                ("Her", 8.90, 9.10), ("yıl", 9.10, 9.24), ("bunu", 9.24, 9.44), ("en", 9.44, 9.54),
                ("az", 9.54, 9.62), ("bir", 9.62, 9.78), ("kere", 9.78, 9.86), ("yapalım.", 9.86, 10.18)])]),
            .init(id: "c6", startS: 10.48, endS: 11.02, text: "Yılda bir kere.", raw: ["words": words([
                ("Yılda", 10.48, 10.64), ("bir", 10.64, 10.84), ("kere.", 10.84, 11.02)])]),
        ]
        var document = EditorDocument(editFormat: "subtitled",
            captionMeta: ["style": .string("word"), "appearance": .object(["highlight_spoken_word": .bool(false)])],
            captionCues: cues, capabilities: ["timeline": .init(editable: false)], revision: .init(baseGeneration: "g"))
        document = try NativePhoneTalkingSource.hydrate(document, clipIndex: 0, duration: 14.8, removed: removed)
        let projection = NativeEditorInteraction.timelineProjection(slots: document.clips, carousel: nil)
        let clips = projection.clipWindows.map { window -> EditorClip in
            let slot = document.clips[window.sourceIndex]
            return EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: window.start, end: window.end,
                trimIn: slot.inS, trimOut: slot.inS + (slot.durationS ?? 0), sourceDuration: 14.8, slotID: slot.id)
        }
        XCTAssertEqual(projection.totalDuration, 13.804, accuracy: 0.001)
        let items = cues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "local", relativePath: "original.mov",
            fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 14.8), url: URL(fileURLWithPath: "/fixture/original.mov"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: document, clips: clips, items: items, sources: [0: source]).recipe
        XCTAssertNoThrow(try recipe.validate())
        let video = try XCTUnwrap(recipe.tracks.first { $0.kind == .video }?.clips)
        XCTAssertEqual(video.map(\.sourceStart), [0, 2.97, 4.581, 11.33])
        // Output instant -> source instant through the compiled video track.
        func sourceTime(_ t: Double) throws -> Double {
            let clip = try XCTUnwrap(video.last { $0.timelineStart <= t + 0.000_001 })
            return clip.sourceStart + (t - clip.timelineStart) * clip.rate
        }
        let expected = cues.flatMap { cue -> [(String, Double)] in
            guard case .array(let rows)? = cue.raw["words"] else { return [] }
            return rows.compactMap { row in row.objectValue.flatMap { o in
                o["text"]?.stringValue.flatMap { text in o["start_s"]?.numberValue.map { (text, spoken($0)) } } } }
        }
        let layers = recipe.textLayers.sorted { $0.start < $1.start }
        XCTAssertEqual(layers.map { $0.runs.map(\.text).joined() }, expected.map(\.0))
        for (layer, word) in zip(layers, expected) {
            XCTAssertEqual(try sourceTime(layer.start), word.1, accuracy: 0.01, "\(word.0) must burn when it is spoken")
        }
        XCTAssertEqual(try sourceTime(layers[0].start), 7.256, accuracy: 0.01)
        XCTAssertEqual(try sourceTime(try XCTUnwrap(layers.last { $0.runs.map(\.text).joined() == "Yılda" }).start), 11.476, accuracy: 0.01)
    }

    func testClipTrimAndAuthoredTextCompileFromPublicDocument() throws {
        let id = UUID()
        let clip = EditorClip(id: id, assetID: UUID(), sourceClipIndex: 2, start: 0, end: 4,
                              trimIn: 1, trimOut: 5, sourceDuration: 6, slotID: "slot-a")
        let draft = EditorDraft(projectID: UUID(), clips: [clip], text: [],
            captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: draft)
        session.addText(content: "Original footage")
        let textID = try XCTUnwrap(session.document.textElements.first?.id)
        session.setTextSize(id: textID, sizePX: 48)
        session.setTextPhase(id: textID, phase: "exit", effect: "typewriter")
        session.applyTextPreset(id: textID, preset: "Highlight")
        let fontDirectory = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: fontDirectory)
        let source = ResolvedEditorSource(clipIndex: 2, mediaID: "original", asset: MediaAsset(id: "local",
            relativePath: "original.mov", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)),
            url: URL(fileURLWithPath: "/fixture/original.mov"))
        let program = try compiler.compile(document: session.document, clips: session.timelineClips,
            items: session.timelineItems, sources: [2: source])
        XCTAssertEqual(program.recipe.tracks[0].clips[0].sourceStart, 1)
        XCTAssertEqual(program.recipe.tracks[0].clips[0].sourceDuration, 4)
        XCTAssertEqual(program.recipe.tracks[0].clips[0].sourceAssetID, "source-2")
        let text = try XCTUnwrap(program.recipe.textLayers.first)
        XCTAssertEqual(text.animationPhases?.exit, .typewriter)
        XCTAssertNotNil(text.background)
        XCTAssertEqual(text.runs[0].fontSize, 48)
        XCTAssertTrue(program.recipe.effectiveCapabilities.contains(.authoredText))
        let baseline = try XCTUnwrap(session.document.textElements.first)
        session.transformText(from: baseline, scale: 25, rotationDelta: 0)
        let enlarged = try compiler.compile(document: session.document, clips: session.timelineClips,
            items: session.timelineItems, sources: [2: source])
        XCTAssertEqual(enlarged.recipe.textLayers.first?.runs.first?.fontSize, 1200)
    }
}
