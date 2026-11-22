#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Level check for music under speech on a Talking edit (KRI-1562).
///
/// The speaker's voice is the video clip's own audio (a 400 Hz tone muxed onto the
/// video track), never an audio-kind track, so the existing `duckMusicDuringSpeech`
/// cannot see it. The recipe carries the spoken windows instead (`audio.speechWindows`),
/// and the music-role audio track (a 2 kHz bed split into two consecutive clips, the
/// second starting mid-timeline) ducks under them. The recipe is exported through the
/// real native exporter and each band is measured inside and outside the windows.
final class TalkingMusicDuckLevelTests: XCTestCase {
    private static let speechHz = 400.0, musicHz = 2_000.0
    private static let seconds = 6
    private static let windows = [SpeechWindow(start: 1.0, end: 2.0), SpeechWindow(start: 4.0, end: 5.0)]
    // Clear of the 0.15 s attack before a window, the 0.35 s release after it, and the AAC priming.
    // Each time is the start of a 0.15 s analysis span. Window 2 sits inside the second music clip.
    private static let inWindow = [1.2, 1.5, 4.2, 4.5]
    private static let gap = [0.4, 2.7, 3.3]
    private static let afterWindow = [2.6, 5.6]

    private struct Levels { var musicInWindow: Double; var musicGap: Double; var musicAfter: Double; var voiceInWindow: Double; var voiceGap: Double }

    @MainActor func testMusicDucksUnderSpeechWindowsAndRisesInThePauses() async throws {
        let fixture = try await Fixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        for (label, gain, expected) in [("default level", Double?.none, 0.3), ("authored level", 0.25, 0.25)] {
            let levels = try await fixture.measure(windows: Self.windows, duckGain: gain, name: label)
            let duckDB = LevelTestMedia.decibels(levels.musicInWindow / levels.musicGap)
            print("TalkingMusicDuckLevelTests \(label): music in window \(levels.musicInWindow), gap \(levels.musicGap), after \(levels.musicAfter), duck \(duckDB) dB")
            XCTAssertGreaterThan(levels.musicGap, 0.1, "\(label): the bed must be audible in the pauses")
            XCTAssertLessThanOrEqual(duckDB, -9, "\(label): music must sit at least 9 dB lower under speech")
            XCTAssertEqual(levels.musicInWindow / levels.musicGap, expected, accuracy: 0.04, "\(label): the duck lands on the authored gain")
            XCTAssertEqual(LevelTestMedia.decibels(levels.musicAfter / levels.musicGap), 0, accuracy: 3,
                           "\(label): the bed is back within 3 dB of its pause level 0.6 s after the window ends")
            XCTAssertEqual(LevelTestMedia.decibels(levels.voiceInWindow / levels.voiceGap), 0, accuracy: 1,
                           "\(label): the duck never lowers the voice")
        }
    }

    /// The control: the same recipe with no windows must show no duck, so the 9 dB assertion above
    /// is something this measurement can fail.
    @MainActor func testNoWindowsLeaveTheMusicBedLevel() async throws {
        let fixture = try await Fixture.make()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        let levels = try await fixture.measure(windows: [], duckGain: nil, name: "control")
        let differenceDB = LevelTestMedia.decibels(levels.musicInWindow / levels.musicGap)
        print("TalkingMusicDuckLevelTests control: music in window \(levels.musicInWindow), gap \(levels.musicGap), difference \(differenceDB) dB")
        XCTAssertLessThan(abs(differenceDB), 1, "without speech windows the music must not move")
    }

    /// The song a creator uploads is a `song`-kind render asset, resolved through the verified library cache and the
    /// extension fixup the device resolver applies, then played on a `role: .music` track. It must duck exactly like
    /// the library bed above, through the real exporter (KRI-1578).
    @MainActor func testCreatorSongOnAMusicTrackDucksLikeTheLibraryBed() async throws {
        let fixture = try await Fixture.make(songKind: true)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        for (label, gain, expected) in [("song default level", Double?.none, 0.3), ("song authored level", 0.25, 0.25)] {
            let levels = try await fixture.measure(windows: Self.windows, duckGain: gain, name: label)
            let duckDB = LevelTestMedia.decibels(levels.musicInWindow / levels.musicGap)
            print("TalkingMusicDuckLevelTests \(label): music in window \(levels.musicInWindow), gap \(levels.musicGap), after \(levels.musicAfter), duck \(duckDB) dB")
            XCTAssertGreaterThan(levels.musicGap, 0.1, "\(label): the song must be audible in the pauses")
            XCTAssertEqual(levels.musicInWindow / levels.musicGap, expected, accuracy: 0.04, "\(label): the duck lands on the authored gain")
            XCTAssertEqual(LevelTestMedia.decibels(levels.voiceInWindow / levels.voiceGap), 0, accuracy: 1, "\(label): the duck never lowers the voice")
        }
    }

    @MainActor func testCreatorSongWithNoWindowsStaysFlat() async throws {
        let fixture = try await Fixture.make(songKind: true)
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        let levels = try await fixture.measure(windows: [], duckGain: nil, name: "song control")
        let differenceDB = LevelTestMedia.decibels(levels.musicInWindow / levels.musicGap)
        print("TalkingMusicDuckLevelTests song control: music in window \(levels.musicInWindow), gap \(levels.musicGap), difference \(differenceDB) dB")
        XCTAssertLessThan(abs(differenceDB), 1, "without speech windows the song must not move")
    }

    private struct Fixture {
        let directory: URL, urls: [String: URL]
        /// Nil for the plain two-file recipe; otherwise the schema-2 manifest that pins the music file as a `song`.
        let manifest: RenderAssetManifest?

        @MainActor static func make(songKind: Bool = false) async throws -> Fixture {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let picture = try await LevelTestMedia.blackVideo(directory, "picture", seconds: TalkingMusicDuckLevelTests.seconds)
            // Sine peak 0.1 is about -23 dBFS RMS, a phone talk-to-camera level; the bed is a catalog-mastered 0.28.
            let speaker = try await LevelTestMedia.mux(picture, try LevelTestMedia.tone(directory, "speech", TalkingMusicDuckLevelTests.speechHz, amplitude: 0.1, seconds: TalkingMusicDuckLevelTests.seconds),
                                                       directory.appendingPathComponent("speaker.mov"))
            let music = try LevelTestMedia.tone(directory, "music", TalkingMusicDuckLevelTests.musicHz, amplitude: 0.28, seconds: 3)
            guard songKind else { return Fixture(directory: directory, urls: ["speaker": speaker, "music": music], manifest: nil) }
            // The device path: the download is installed in the verified cache under an extensionless content address,
            // then linked under a playable name (`AuthorizedDeviceSourceResolver.resolve(for:)`).
            let hasher = SHA256Fingerprinter()
            let songReference = RenderAssetReference(id: "music", fingerprint: try RenderFingerprint(hasher.fingerprint(file: music)),
                                                     source: .song(planItemID: "item-1", generation: "3"))
            let library = RenderLibraryCache(root: directory.appendingPathComponent("library"))
            _ = try await library.install(downloadedFile: music, for: songReference)
            let cached = try await library.resolve(songReference)
            XCTAssertTrue(cached.pathExtension.isEmpty, "the cache address has no extension, so AVFoundation needs the playable link")
            let playable = try PlayableAudioFile.prepare(source: cached, fingerprint: songReference.fingerprint, directory: directory.appendingPathComponent("playable-audio"))
            let speakerReference = RenderAssetReference(id: "speaker", fingerprint: try RenderFingerprint(hasher.fingerprint(file: speaker)), source: .original(mediaID: "speaker"))
            return Fixture(directory: directory, urls: ["speaker": speaker, "music": playable],
                           manifest: RenderAssetManifest(assets: [speakerReference, songReference]))
        }

        @MainActor func measure(windows: [SpeechWindow], duckGain: Double?, name: String) async throws -> Levels {
            let recipe = EditRecipe(schemaVersion: manifest == nil ? 1 : 2, rendererVersion: manifest == nil ? "kria-ios-1" : "kria-ios-2", canvas: Canvas(width: 96, height: 160),
                assets: try manifest == nil ? urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) }
                    : manifest!.assets.map { MediaAsset(id: $0.id, relativePath: $0.id, fingerprint: $0.fingerprint.assetFingerprint) },
                tracks: [
                    TimelineTrack(id: "subtitled", kind: .video, clips: [
                        TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceStart: 0, sourceDuration: Double(TalkingMusicDuckLevelTests.seconds), timelineStart: 0)]),
                    TimelineTrack(id: "music", kind: .audio, clips: [
                        TimelineClip(id: "music-0", sourceAssetID: "music", sourceStart: 0, sourceDuration: 3, timelineStart: 0),
                        TimelineClip(id: "music-1", sourceAssetID: "music", sourceStart: 0, sourceDuration: 3, timelineStart: 3)], role: .music),
                ],
                audio: AudioMixRecipe(originalVolume: 1, speechWindows: windows, speechDuckGain: duckGain),
                assetManifest: manifest)
            try recipe.validate()
            let output = directory.appendingPathComponent("duck-\(name).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(name)")), branding: .none)
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let pcm = try await LevelTestMedia.decode(AVURLAsset(url: output), seconds: TalkingMusicDuckLevelTests.seconds)
            return Levels(musicInWindow: try LevelTestMedia.mean(pcm, TalkingMusicDuckLevelTests.inWindow, TalkingMusicDuckLevelTests.musicHz),
                          musicGap: try LevelTestMedia.mean(pcm, TalkingMusicDuckLevelTests.gap, TalkingMusicDuckLevelTests.musicHz),
                          musicAfter: try LevelTestMedia.mean(pcm, TalkingMusicDuckLevelTests.afterWindow, TalkingMusicDuckLevelTests.musicHz),
                          voiceInWindow: try LevelTestMedia.mean(pcm, TalkingMusicDuckLevelTests.inWindow, TalkingMusicDuckLevelTests.speechHz),
                          voiceGap: try LevelTestMedia.mean(pcm, TalkingMusicDuckLevelTests.gap, TalkingMusicDuckLevelTests.speechHz))
        }
    }
}
#endif
