import XCTest
import KriaMediaEngine
@testable import Kria

final class NativeSongReferenceTests: XCTestCase {
    func testParsesValidReferenceAndFormatsMillisecondCarry() {
        let variant: [String: JSONValue] = [
            "music_playback_mode": .string("reference_only"),
            "song_reference": .object([
                "track_id": .string("track-1"), "title": .string("  Night Drive  "),
                "artist": .string("  Lumen "), "start_s": .number(59.9995), "end_s": .number(61.234)
            ])
        ]
        XCTAssertEqual(NativeMusicPlaybackMode(variant: variant), .referenceOnly)
        let reference = NativeSongReference(variant: variant)
        XCTAssertEqual(reference?.title, "Night Drive")
        XCTAssertEqual(reference?.artist, "Lumen")
        XCTAssertEqual(reference?.timeRange, "01:00.000 – 01:01.234")
        XCTAssertEqual(reference?.copyText, "Night Drive by Lumen, 01:00.000 – 01:01.234")
    }

    func testRejectsIncompleteAndInvalidReferenceTimings() {
        let base: [String: JSONValue] = ["track_id": .string("track"), "title": .string("Song")]
        for timing in [
            ["start_s": JSONValue.number(-1), "end_s": .number(1)],
            ["start_s": .number(2), "end_s": .number(2)]
        ] {
            XCTAssertNil(NativeSongReference(variant: ["song_reference": .object(base.merging(timing) { _, next in next })]))
        }
        XCTAssertEqual(NativeMusicPlaybackMode(variant: [:]), .embedded)
    }

    func testEditorReferenceUsesTrimmedVideoDurationAndRequiresSaveForExtension() throws {
        let reference = try XCTUnwrap(NativeSongReference(variant: ["song_reference": .object([
            "track_id": .string("track"), "title": .string("Song"), "start_s": .number(10), "end_s": .number(20)
        ])]))

        XCTAssertEqual(NativeEditorSongReferencePresentation.make(reference: reference, baselineDuration: 10, currentDuration: 7, durationChanged: true),
            .reference(reference.withEndS(17)))
        XCTAssertEqual(NativeEditorSongReferencePresentation.make(reference: reference, baselineDuration: 10, currentDuration: 13, durationChanged: true), .saveToUpdateTiming)
        XCTAssertEqual(NativeEditorSongReferencePresentation.make(reference: reference, baselineDuration: 10, currentDuration: 7, durationChanged: false), .reference(reference))
    }

    @MainActor
    func testReferenceOnlyCompilerOmitsSongLanesButKeepsNarrationAndSFX() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        func source(_ id: String, duration: Double = 8) -> ResolvedEditorSource {
            ResolvedEditorSource(clipIndex: 0, mediaID: id,
                asset: MediaAsset(id: id, relativePath: id, fingerprint: fingerprint, duration: duration),
                url: URL(fileURLWithPath: "/fixture/\(id).mp4"))
        }
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4,
                              trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        var document = EditorDocument()
        document.music = .init(trackID: "song", startS: 1)
        document.backgroundMusic = .init(trackID: "bed", enabled: true, startS: 0, endS: 4)
        document.soundEffects = [.init(id: "hit", startS: 1, endS: 2, raw: ["trim_start_s": .number(0), "trim_end_s": .number(1)])]
        let item = NativeEditorTimelineItem(selection: .init(kind: .soundEffect, id: "hit"), start: 1, end: 2)
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))

        let program = try compiler.compile(document: document, clips: [clip], items: [item], sources: [0: source("original")],
            audioSources: ["song": source("song"), "bed": source("bed"), "narration": source("narration"), "sfx:hit": source("sfx")],
            referenceOnlyMusic: true)

        XCTAssertEqual(program.recipe.tracks.filter { $0.kind == .audio }.map(\.id).sorted(), ["narration", "sfx:hit"])
        XCTAssertEqual(program.recipe.tracks.first { $0.kind == .video }?.clips.first?.volume, 0)
        XCTAssertNil(program.assetURLs["audio-song"])
        XCTAssertNil(program.assetURLs["audio-bed"])
        XCTAssertNotNil(program.assetURLs["narration"])
        XCTAssertNotNil(program.assetURLs["audio-sfx:hit"])
    }

    @MainActor
    func testReferenceOnlyCompilerAppliesInitialSourceAudioPolicyBeforeCreatorEdits() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "original", relativePath: "original", fingerprint: fingerprint, duration: 4), url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 4, slotID: "slot")
        var document = EditorDocument(); document.music = .init(trackID: "song")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let policyMuted = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], referenceOnlyMusic: true, sourceAudioPreserved: false)
        XCTAssertEqual(policyMuted.recipe.tracks.first { $0.kind == .video }?.clips.first?.volume, 0)
        XCTAssertTrue(policyMuted.recipe.tracks.filter { $0.kind == .audio }.isEmpty)

        document.clips = [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 4, raw: ["muted": .bool(false)])]
        let explicitlyUnmuted = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], referenceOnlyMusic: true, sourceAudioPreserved: false)
        XCTAssertEqual(explicitlyUnmuted.recipe.tracks.first { $0.kind == .video }?.clips.first?.volume, 1)

        document.mix["original_level"] = .number(0.4)
        let explicitlyMixed = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], referenceOnlyMusic: true, sourceAudioPreserved: false)
        let clipGain = try XCTUnwrap(explicitlyMixed.recipe.tracks.first { $0.kind == .video }?.clips.first?.volume)
        XCTAssertEqual(clipGain, 1)
        XCTAssertEqual(explicitlyMixed.recipe.audio.originalVolume, 0.4)
        XCTAssertEqual(clipGain * explicitlyMixed.recipe.audio.originalVolume, 0.4)
    }
}
