@preconcurrency import AVFoundation
import XCTest
@testable import Kria

/// The video scenarios of `scripts/ios/talk-bench.sh` made their background with an encode that, when the phone's encoder
/// failed a session, waited on `isReadyForMoreMediaData` forever, so `load()` never finished, the camera stayed black and Record
/// stayed disabled (18 of 18 video runs). Failure modes written before the fix:
/// 1. the generated file is not a playable 720x1280 video of about ten seconds;
/// 2. the file plays but the app's own video background cannot decode a frame from it;
/// 3. the encode ends in a writer failure that is not reported (covered on a device: `bench.video.make` in `summary.json`).
final class TalkBenchVideoFixtureTests: XCTestCase {
    func testTheBenchVideoIsPlayableAndTheLiveBackgroundDecodesIt() async throws {
        let url = try await TalkBenchFixtures.makeVideo()
        defer { try? FileManager.default.removeItem(at: url) }

        let asset = AVURLAsset(url: url)
        let playable = try await asset.load(.isPlayable)
        let duration = try await asset.load(.duration).seconds
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let size = try await track.load(.naturalSize)
        XCTAssertTrue(playable)
        XCTAssertEqual(duration, 10, accuracy: 0.2)
        XCTAssertEqual(size, CGSize(width: 720, height: 1280))

        let background = try await LoopingVideoBackground(url: url)
        defer { background.stop() }
        var frame: CIImage?
        for _ in 0..<100 where frame == nil {
            frame = background.frame()
            if frame == nil { try await Task.sleep(for: .milliseconds(50)) }
        }
        XCTAssertNotNil(frame, "the app's video background shows a frame of the bench video within five seconds")
    }
}
