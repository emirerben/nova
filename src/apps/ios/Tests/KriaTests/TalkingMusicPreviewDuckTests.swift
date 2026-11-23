import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1562: the editor preview ducks a Talking edit's background music under the speaker's words, as the export does.
/// Ways this could fail: a bed with no words still carries windows or a role (a no-cue Talking edit must stay
/// byte-identical to before); a non-Talking edit with cues ducks (its voice is not on the video track); a muted or
/// disabled bed still marks a track; words closer than the server's bridge stay split; a cue's words are read as the
/// cue's whole span; a bed shorter than the video plays once and then goes silent.
@MainActor final class TalkingMusicPreviewDuckTests: XCTestCase {
    private let fingerprint = AssetFingerprint(hex: String(repeating: "c", count: 64), byteCount: 100)

    private func source(_ id: String, duration: Double) -> ResolvedEditorSource {
        ResolvedEditorSource(clipIndex: 0, mediaID: id,
            asset: MediaAsset(id: id, relativePath: id, fingerprint: fingerprint, duration: duration),
            url: URL(fileURLWithPath: "/fixture/\(id).mp4"))
    }

    private func word(_ text: String, _ start: Double, _ end: Double) -> JSONValue {
        .object(["text": .string(text), "start_s": .number(start), "end_s": .number(end)])
    }

    private func recipe(format: String = "subtitled", cues: [EditorCaptionCue], bed: EditorBackgroundMusic?, song: Bool = false,
                        bedDuration: Double = 30, videoDuration: Double = 10) throws -> KriaMediaEngine.EditRecipe {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: videoDuration,
                              trimIn: 0, trimOut: videoDuration, sourceDuration: videoDuration, slotID: "slot")
        var document = EditorDocument(editFormat: format, clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: videoDuration)],
                                      captionMeta: ["enabled": .bool(false)], captionCues: cues, backgroundMusic: bed)
        document.captionMeta["enabled"] = .bool(false)
        let items = cues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source("original", duration: videoDuration)],
                                    audioSources: (bed?.trackID.map { [$0: source($0, duration: bedDuration)] } ?? [:])
                                        .merging(song ? [NativeEditorRenderCompiler.songSourceKey: source("creator-song", duration: bedDuration)] : [:]) { $1 }).recipe
    }

    private var spokenCues: [EditorCaptionCue] {
        [EditorCaptionCue(id: "a", startS: 1, endS: 3, text: "hello there my friend",
                          raw: ["words": .array([word("hello", 1, 1.4), word("there", 1.5, 1.9), word("my", 2.0, 2.3), word("friend", 2.4, 3)])]),
         EditorCaptionCue(id: "b", startS: 5, endS: 6, text: "bye now")]
    }

    private let bed = EditorBackgroundMusic(trackID: "bed", enabled: true, startS: 0, endS: 30, gainDB: -18)

    func testTalkingBedIsMusicRoleAndDucksUnderMergedWordWindows() throws {
        let result = try recipe(cues: spokenCues, bed: bed)
        let track = try XCTUnwrap(result.tracks.first { $0.id == "background-music" })
        XCTAssertEqual(track.role, .music)
        XCTAssertEqual(result.audio.speechDuckGain, 0.25)
        XCTAssertEqual(result.audio.speechWindows, [SpeechWindow(start: 1, end: 3), SpeechWindow(start: 5, end: 6)])
        XCTAssertEqual(track.clips.first?.volume ?? 0, pow(10, -18 / 20), accuracy: 1e-9)
    }

    func testWordsWithinTheServerBridgeMergeAndWiderGapsStaySplit() throws {
        let cues = [EditorCaptionCue(id: "a", startS: 0, endS: 3, text: "one two three four",
            raw: ["words": .array([word("one", 0, 0.5), word("two", 0.74, 1.0), word("three", 1.25, 1.6), word("four", 2.0, 2.4)])])]
        let windows = try recipe(cues: cues, bed: bed).audio.speechWindows
        XCTAssertEqual(windows, [SpeechWindow(start: 0, end: 1.0), SpeechWindow(start: 1.25, end: 1.6), SpeechWindow(start: 2.0, end: 2.4)])
    }

    func testCuesWithoutWordTimingsOrWithBadSpansFallBackToTheCueSpanOrAreDropped() throws {
        let cues = [EditorCaptionCue(id: "a", startS: 2, endS: 4, text: "no words"),
                    EditorCaptionCue(id: "b", startS: 6, endS: 6, text: "empty span"),
                    EditorCaptionCue(id: "c", startS: 8, endS: 9, text: "x", raw: ["words": .array([word("x", 9, 8)])])]
        XCTAssertEqual(try recipe(cues: cues, bed: bed).audio.speechWindows, [SpeechWindow(start: 2, end: 4)])
    }

    func testBedShorterThanTheVideoRepeatsItsSectionWithConsecutiveClips() throws {
        let short = EditorBackgroundMusic(trackID: "bed", enabled: true, startS: 2, endS: 6, gainDB: -18)
        let track = try XCTUnwrap(try recipe(cues: spokenCues, bed: short).tracks.first { $0.id == "background-music" })
        XCTAssertEqual(track.clips.map(\.timelineStart), [0, 4, 8])
        XCTAssertEqual(track.clips.map(\.sourceDuration), [4, 4, 2])
        XCTAssertEqual(track.clips.map(\.sourceStart), [2, 2, 2])
        XCTAssertEqual(Set(track.clips.map(\.id)).count, 3)
        XCTAssertTrue(track.clips.allSatisfy { abs($0.volume - pow(10, -18 / 20)) < 1e-9 })
    }

    func testNoCuesMeansNoWindowsNoRoleAndTodaysSingleClip() throws {
        let short = EditorBackgroundMusic(trackID: "bed", enabled: true, startS: 2, endS: 6, gainDB: -18)
        let result = try recipe(cues: [], bed: short)
        let track = try XCTUnwrap(result.tracks.first { $0.id == "background-music" })
        XCTAssertNil(track.role)
        XCTAssertEqual(track.clips.count, 1)
        XCTAssertEqual(track.clips.first?.sourceDuration, 4)
        XCTAssertTrue(result.audio.speechWindows.isEmpty)
        XCTAssertNil(result.audio.speechDuckGain)
    }

    func testNonTalkingMutedAndDisabledBedsStayUntouched() throws {
        let montage = try recipe(format: "montage", cues: spokenCues, bed: bed)
        XCTAssertNil(montage.tracks.first { $0.id == "background-music" }?.role)
        XCTAssertTrue(montage.audio.speechWindows.isEmpty)
        var muted = bed; muted.muted = true
        var disabled = bed; disabled.enabled = false
        for off in [muted, disabled] {
            let result = try recipe(cues: spokenCues, bed: off)
            XCTAssertNil(result.tracks.first { $0.id == "background-music" })
            XCTAssertTrue(result.audio.speechWindows.isEmpty)
            XCTAssertNil(result.audio.speechDuckGain)
        }
        let none = try recipe(cues: spokenCues, bed: nil)
        XCTAssertTrue(none.audio.speechWindows.isEmpty)
    }

    func testEncodedNoCueTalkingRecipeCarriesNoDuckKeys() throws {
        let data = try JSONEncoder().encode(try recipe(cues: [], bed: bed))
        let text = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertFalse(text.contains("speechWindows"))
        XCTAssertFalse(text.contains("speechDuckGain"))
        XCTAssertFalse(text.contains("\"role\""))
    }

    // MARK: the creator's own song on a Talking edit (KRI-1578)

    private func camera(_ recipe: KriaMediaEngine.EditRecipe) throws -> Double {
        try XCTUnwrap(recipe.tracks.first { $0.kind == .video }?.clips.first).volume
    }

    func testTalkingSongDucksLikeTheBedAndNeverMutesTheVoice() throws {
        let result = try recipe(cues: spokenCues, bed: nil, song: true)
        let track = try XCTUnwrap(result.tracks.first { $0.id == "song" })
        XCTAssertEqual(track.role, .music)
        XCTAssertEqual(result.audio.speechDuckGain, 0.25)
        XCTAssertEqual(result.audio.speechWindows, [SpeechWindow(start: 1, end: 3), SpeechWindow(start: 5, end: 6)])
        XCTAssertEqual(try camera(result), 1, "the speaker's voice is the camera audio and keeps playing")
    }

    func testMontageSongStillMutesTheCameraAndNeverDucks() throws {
        let result = try recipe(format: "montage", cues: spokenCues, bed: nil, song: true)
        XCTAssertNil(try XCTUnwrap(result.tracks.first { $0.id == "song" }).role)
        XCTAssertTrue(result.audio.speechWindows.isEmpty)
        XCTAssertNil(result.audio.speechDuckGain)
        XCTAssertEqual(try camera(result), 0)
    }

    func testTalkingSongWithNoCuesHasNoRoleNoWindowsAndTheVoiceStays() throws {
        let result = try recipe(cues: [], bed: nil, song: true)
        XCTAssertNil(try XCTUnwrap(result.tracks.first { $0.id == "song" }).role)
        XCTAssertTrue(result.audio.speechWindows.isEmpty)
        XCTAssertNil(result.audio.speechDuckGain)
        XCTAssertEqual(try camera(result), 1)
    }

    func testTalkingEditWithoutASongHasNoSongTrackRoleOrWindowsFromTheSong() throws {
        let result = try recipe(cues: spokenCues, bed: nil, song: false)
        XCTAssertNil(result.tracks.first { $0.id == "song" })
        XCTAssertTrue(result.audio.speechWindows.isEmpty)
        XCTAssertEqual(try camera(result), 1)
    }

    func testTalkingSongShorterThanTheVideoLoopsItsWindowLikeTheServer() throws {
        let track = try XCTUnwrap(try recipe(cues: spokenCues, bed: nil, song: true, bedDuration: 4).tracks.first { $0.id == "song" })
        XCTAssertEqual(track.clips.map(\.timelineStart), [0, 4, 8])
        XCTAssertEqual(track.clips.map(\.sourceDuration), [4, 4, 2])
        XCTAssertEqual(track.clips.map(\.sourceStart), [0, 0, 0])
        XCTAssertEqual(Set(track.clips.map(\.id)).count, 3)
    }

    func testMontageSongShorterThanTheVideoPlaysOnceAndStops() throws {
        let track = try XCTUnwrap(try recipe(format: "montage", cues: spokenCues, bed: nil, song: true, bedDuration: 4).tracks.first { $0.id == "song" })
        XCTAssertEqual(track.clips.map(\.sourceDuration), [4])
    }
}
