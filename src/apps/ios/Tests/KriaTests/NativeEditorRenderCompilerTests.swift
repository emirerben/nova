import XCTest
import AVFoundation
import KriaMediaEngine
@testable import Kria

@MainActor final class NativeEditorRenderCompilerTests: XCTestCase {
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
            XCTAssertEqual($0 as? RecipeError, .invalidTimeline)
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
