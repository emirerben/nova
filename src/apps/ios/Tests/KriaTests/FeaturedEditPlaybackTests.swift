import AVFoundation
import XCTest
@testable import Kria

/// A format card whose preview video is missing from the app silently falls back to its still poster.
/// These play the real bundled file through the same controller the cards use.
@MainActor
final class FeaturedEditPlaybackTests: XCTestCase {
    func testEveryFormatCardPreviewVideoIsBundledAndPlays() async throws {
        for format in [CreationFormat.montage, .lipsync, .talkingToCamera] {
            let edit = try XCTUnwrap(format.previewEdit, "\(format.title) has no preview video")
            XCTAssertNotNil(Bundle.main.url(forResource: edit.resource, withExtension: "mp4"), "\(edit.resource).mp4 is not in the app")
            XCTAssertNotNil(Bundle.main.url(forResource: edit.resource, withExtension: "jpg"), "\(edit.resource).jpg poster is not in the app")

            let playback = FeaturedEditPlayback(edit: edit)
            playback.update(active: true, allowed: true)
            try await waitUntil(5) { playback.isPlaying }
            XCTAssertTrue(playback.isPlaying, "\(format.title) preview did not start playing")
            let started = playback.player.currentTime().seconds
            try await Task.sleep(for: .seconds(1.2))
            XCTAssertGreaterThan(playback.player.currentTime().seconds, started + 0.5, "\(format.title) preview is not advancing")
            playback.update(active: false, allowed: false)
        }
    }

    private func waitUntil(_ seconds: Double, _ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition(), Date() < deadline { try await Task.sleep(for: .milliseconds(50)) }
    }
}
