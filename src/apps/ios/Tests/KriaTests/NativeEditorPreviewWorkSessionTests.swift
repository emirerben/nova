import AVFoundation
import XCTest
@testable import Kria

@MainActor final class NativeEditorPreviewWorkSessionTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testUntrustedInitialSeedCannotBecomeAnAdmittedSourcePreview() throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations(), initialPlaybackURL: file, trustsInitialPlaybackURL: false)
        session.beginPreviewPresentation()
        XCTAssertFalse(session.canDisplayCurrentPlayer)
        XCTAssertNil(session.admissiblePreviewDisplayTicket)
        if let player = session.player { session.previewFrameWasShown(player: player, ticket: nil) }
        XCTAssertNil(session.previousPreviewPresentation)
        XCTAssertFalse(session.isPreparingCurrentPreview)
    }

    func testReadyCompositionStillWaitsForTheOwnedSurfaceAdmissionCallback() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations())
        session.beginPreviewPresentation()
        await session.prepareFixtureSourcePreview(url: file)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(session.isPreparingCurrentPreview)
        let player = try XCTUnwrap(session.player)
        let ticket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)
        for _ in 0..<100 where player.currentItem?.status != .readyToPlay {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(player.currentItem?.status, .readyToPlay)
        // This is the surface boundary callback, not proof of an inspected native image.
        session.previewFrameWasShown(player: player, ticket: ticket)
        XCTAssertFalse(session.isPreparingCurrentPreview)
        XCTAssertNil(session.previousPreviewPresentation)
    }

    func testSameTargetReloadInvalidatesDelayedWorkAndItsStaleCleanup() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations())
        session.beginPreviewPresentation()
        let oldLifetime = session.editorLifetimeID
        let oldBuild = Task { await session.prepareFixtureSourcePreview(url: file, delayedLoad: true) }
        for _ in 0..<50 where !session.isPreparingCurrentPreview { await Task.yield() }
        XCTAssertTrue(session.isPreparingCurrentPreview)
        NativeEditorURLProtocol.handler = { _ in (404, Data("{}".utf8)) }
        await session.load(project: PreviewFixtures.editorProject, api: NativeEditorTestSupport.api())
        XCTAssertGreaterThan(session.editorLifetimeID, oldLifetime)
        XCTAssertFalse(session.isPreparingCurrentPreview)
        let replacementState = session.sourcePreviewState
        let replacementLoadState = session.loadState
        let compileCount = session.sourcePreviewCompileCount
        await oldBuild.value
        XCTAssertEqual(session.sourcePreviewState, replacementState)
        XCTAssertEqual(session.loadState, replacementLoadState)
        XCTAssertEqual(session.sourcePreviewCompileCount, compileCount)
        XCTAssertFalse(session.isPreparingCurrentPreview)
        XCTAssertNil(session.previousPreviewPresentation)
        XCTAssertNil(session.admissiblePreviewDisplayTicket)
    }

    func testDismissalCannotBeUndoneByDelayedPreparation() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations())
        session.beginPreviewPresentation()
        let build = Task { await session.prepareFixtureSourcePreview(url: file, delayedLoad: true) }
        await Task.yield()
        session.endPreviewPresentation()
        await build.value
        XCTAssertFalse(session.isPreparingCurrentPreview)
        XCTAssertNil(session.previousPreviewPresentation)
    }
    private func session(retainingAdmittedPreviewOf file: URL) async throws -> NativeEditorSession {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations())
        session.beginPreviewPresentation()
        await session.prepareFixtureSourcePreview(url: file)
        try await waitForReadyItem(of: session)
        let ticket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)
        XCTAssertTrue(session.previewFrameWasShown(player: try XCTUnwrap(session.player), ticket: ticket))
        session.retainAdmittedPreview()
        return session
    }

    private func waitForReadyItem(of session: NativeEditorSession) async throws {
        for _ in 0..<250 where session.player?.currentItem?.status != .readyToPlay {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(session.player?.currentItem?.status, .readyToPlay)
    }

    func testRetainedHandoffThatMissesItsPrerollFallsBackToThePlainSwapAndStaysPlayable() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = try await session(retainingAdmittedPreviewOf: file)
        defer { session.endPreviewPresentation() }
        let retainedPlayer = try XCTUnwrap(session.player)
        // A real miss (timeout or failed seek) leaves the item on the warm-up's player (KRI-1404).
        var missedHolder: AVPlayer?
        session.prerolledPlayerProvider = { item, _ in
            let holder = AVPlayer(playerItem: item)
            missedHolder = holder
            return NativeEditorSession.WarmedLivePlayer(player: holder, isPrerolled: false)
        }

        await session.prepareFixtureSourcePreview(url: file)

        XCTAssertEqual(session.sourcePreviewState, .ready, "A preroll miss is not a preview failure")
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        XCTAssertNotEqual(session.previousPreviewPresentation?.status, .failed)
        XCTAssertFalse(session.player === retainedPlayer, "The plain swap installs the new composition")
        XCTAssertTrue(session.player === missedHolder, "The plain swap runs on the player already holding the item")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying, "Play must start the live preview")
        try await waitForReadyItem(of: session)
        let ticket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)
        XCTAssertTrue(session.previewFrameWasShown(player: try XCTUnwrap(session.player), ticket: ticket))
        XCTAssertNil(session.previousPreviewPresentation)
        XCTAssertFalse(session.isPreparingCurrentPreview)
        session.pausePlayback()
    }

    /// KRI-1404: two TestFlight 1977 aborts on 2026-11-13, both `-[AVPlayerItem _attachToPlayer:]` under
    /// `-[AVPlayer initWithPlayerItem:]` while another player was still in `RemoveFromPlayQueue`.
    ///
    /// Ways the editor can hand one live item to two players (written before the fix):
    /// 1. The finished video plays, the warm-up attaches the live item to its own player, the creator pauses or
    ///    scrubs meanwhile, the warm player is dropped with `replaceCurrentItem(with: nil)` and the plain swap
    ///    calls `AVPlayer(playerItem:)` on the same item. That detach is asynchronous, so the attach throws.
    /// 2. The warm-up takes longer than its ~2 s budget (a long, many-cut Talking edit on an older phone) and
    ///    detaches the item itself before the plain swap re-attaches it.
    /// 3. A retained preview drifts past the hand-off limit and the corrective seek misses: same detach + attach.
    /// What must still hold afterwards: one player only (no second audio track left playing), the live preview
    /// installed and playable, and a stale warm-up (the document changed meanwhile) still discarded.
    /// This test drives case 1 through the production warm-up; the other two share its swap.
    func testPausingWhileTheLivePreviewWarmsOverTheFinishedVideoDoesNotAttachItsItemTwice() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations(), trustsInitialPlaybackURL: true)
        session.beginPreviewPresentation()
        defer { session.endPreviewPresentation() }
        session.installFinishedRenderPlayer(url: file)
        try await waitForReadyItem(of: session)
        let finishedPlayer = try XCTUnwrap(session.player)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying, "The finished video plays while the live preview builds")

        let productionWarmUp = session.prerolledPlayerProvider
        var warmUps = 0
        session.prerolledPlayerProvider = { item, time in
            warmUps += 1
            let warmed = await productionWarmUp(item, time)
            session.pausePlayback()  // the creator pauses while the live item warms
            return warmed
        }

        await session.prepareFixtureSourcePreview(url: file)

        XCTAssertEqual(warmUps, 1, "The live preview must have been warmed over the playing finished video")
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isPlaying, "The pause is kept")
        let livePlayer = try XCTUnwrap(session.player)
        XCTAssertFalse(livePlayer === finishedPlayer, "The live preview replaced the finished video")
        XCTAssertNil(finishedPlayer.currentItem, "The finished video's player is detached, so it cannot keep playing")
        try await waitForReadyItem(of: session)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying, "Play starts the live preview")
        session.pausePlayback()
    }

    func testRetainedPreviewRetiresWithinTheBoundWhenNoFrameReportArrives() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = try await session(retainingAdmittedPreviewOf: file)
        defer { session.endPreviewPresentation() }

        await session.prepareFixtureSourcePreview(url: file)
        XCTAssertEqual(session.previousPreviewPresentation?.status, .updating,
                       "The previous picture still covers the candidate until its frame or the bound")

        var retired = false
        for _ in 0..<200 where !retired {
            try await Task.sleep(for: .milliseconds(20))
            retired = session.previousPreviewPresentation == nil
        }
        XCTAssertTrue(retired, "The live preview must become visible within the bound")
        XCTAssertFalse(session.isPreparingCurrentPreview)
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
    }

    /// #2221's bound only started once the live item was ready. A rebuild stuck before that left
    /// "Updating preview" over the old frame for good, with no Retry.
    func testRetainedPreviewOffersRetryWhenTheUpdateIsStuckBeforeItsItemIsReady() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = try await session(retainingAdmittedPreviewOf: file)
        defer { session.endPreviewPresentation() }
        session.retainedPreviewPreparationBound = .milliseconds(50)

        let stuck = Task { await session.prepareFixtureSourcePreview(url: file, delayedLoad: true) }
        for _ in 0..<50 where session.sourcePreviewState != .preparing { await Task.yield() }
        XCTAssertEqual(session.previousPreviewPresentation?.status, .updating)

        var offersRetry = false
        for _ in 0..<20 where !offersRetry {
            try await Task.sleep(for: .milliseconds(20))
            offersRetry = session.previousPreviewPresentation.map { $0.status != .updating } ?? false
        }
        XCTAssertTrue(offersRetry, "past the bound the old frame must offer Retry instead of updating forever")
        XCTAssertEqual(session.sourcePreviewState, .preparing, "checked before the slow build lands")
        await stuck.value
        XCTAssertEqual(session.sourcePreviewState, .ready, "a late build still takes over")
    }

    func testRejectedFrameReportIsNotConsumedAndTheMatchingReportStillAdmits() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = try await session(retainingAdmittedPreviewOf: file)
        defer { session.endPreviewPresentation() }
        await session.prepareFixtureSourcePreview(url: file)
        try await waitForReadyItem(of: session)
        let player = try XCTUnwrap(session.player)
        let ticket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)

        XCTAssertFalse(session.previewFrameWasShown(player: player, ticket: nil), "A report without the current ticket is rejected")
        XCTAssertNotNil(session.previousPreviewPresentation)
        XCTAssertTrue(session.previewFrameWasShown(player: player, ticket: ticket))
        XCTAssertNil(session.previousPreviewPresentation)
    }
}
