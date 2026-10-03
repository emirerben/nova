import XCTest
@testable import Kria

@MainActor
final class NativeSfxPanelTests: XCTestCase {
    func testWaveformNormalizeScalesToLoudestBarAndFixedBucketCount() {
        let bars = NativeSfxWaveformPeaks.normalize([0.1, 0.5, 0.25, 0.05], buckets: 4)
        XCTAssertEqual(bars.count, 4)
        XCTAssertEqual(bars.max(), 1)
        XCTAssertEqual(bars[0], 0.2, accuracy: 0.0001)
    }

    func testWaveformNormalizeOfSilenceOrEmptyYieldsNoBars() {
        XCTAssertTrue(NativeSfxWaveformPeaks.normalize([]).isEmpty)
        XCTAssertTrue(NativeSfxWaveformPeaks.normalize([0, 0, 0]).isEmpty)
    }

    func testAuditionWithoutAURLMarksTheSoundFailedAndNeverPlays() {
        let player = NativeSfxAuditionPlayer()
        player.play(id: "a", url: nil)
        XCTAssertTrue(player.failedIDs.contains("a"))
        XCTAssertNil(player.playingID)
        XCTAssertNil(player.loadingID)
    }

    func testStartingASecondAuditionStopsTheFirstAndRetryClearsTheFailure() {
        let player = NativeSfxAuditionPlayer()
        player.play(id: "a", url: URL(string: "file:///nonexistent-a.m4a"))
        XCTAssertEqual(player.loadingID, "a")
        player.play(id: "b", url: URL(string: "file:///nonexistent-b.m4a"))
        XCTAssertEqual(player.loadingID, "b", "only one audition at a time")
        player.stop()
        XCTAssertNil(player.loadingID)
        player.play(id: "c", url: nil)
        XCTAssertTrue(player.failedIDs.contains("c"))
        player.play(id: "c", url: URL(string: "file:///nonexistent-c.m4a"))
        XCTAssertFalse(player.failedIDs.contains("c"))
    }

    func testToggleStopsTheSoundThatIsAlreadyLoadingOrPlaying() {
        let player = NativeSfxAuditionPlayer()
        let url = URL(string: "file:///nonexistent.m4a")
        player.toggle(id: "a", url: url)
        XCTAssertEqual(player.loadingID, "a")
        player.toggle(id: "a", url: url)
        XCTAssertNil(player.loadingID)
    }

    func testSelectedEffectFollowsTheSessionSelection() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.allLanes)
        XCTAssertNil(NativeSoundEffectsPanelBody.selectedEffect(session))
        session.select(EditorSelection(kind: .soundEffect, id: "sfx-1"), seekToStart: false)
        XCTAssertEqual(NativeSoundEffectsPanelBody.selectedEffect(session)?.id, "sfx-1")
        session.removeSoundEffect(id: "sfx-1")
        XCTAssertNil(NativeSoundEffectsPanelBody.selectedEffect(session), "removing the sound returns to the list")
    }

    func testClockFormatsMinutesAndTenths() {
        XCTAssertEqual(NativeSfxFormat.clock(4.2), "0:04.2")
        XCTAssertEqual(NativeSfxFormat.clock(75.5), "1:15.5")
    }
}
