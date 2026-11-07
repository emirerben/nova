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
}
