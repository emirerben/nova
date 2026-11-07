import AVFoundation
import SwiftUI
import UIKit
import XCTest
@testable import Kria

@MainActor final class NativeEditorPreviewWorkSessionTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testPlayingInPlaceTitleUpdateSettlesThroughTheRealPlayerSurfaceAndCompositor() async throws {
        let file = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let sourceDuration = try await AVURLAsset(url: file).load(.duration).seconds
        XCTAssertGreaterThanOrEqual(sourceDuration, 4)
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                          operations: LocalEditorOperations())
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .first(where: { $0.activationState == .foregroundActive }))
        let previousKeyWindow = scene.windows.first(where: \.isKeyWindow)
        let host = UIHostingController(rootView: NativeVideoPreview(session: session)
            .onAppear { session.beginPreviewPresentation() }
            .onDisappear { session.endPreviewPresentation() })
        let window = UIWindow(windowScene: scene)
        window.frame = scene.coordinateSpace.bounds
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.view.layoutIfNeeded()
        defer {
            session.pausePlayback()
            session.endPreviewPresentation()
            window.isHidden = true
            window.rootViewController = nil
            previousKeyWindow?.makeKey()
        }
        func waitFor(_ timeout: Duration, _ condition: () -> Bool) async throws -> Bool {
            let deadline = ContinuousClock.now + timeout
            while ContinuousClock.now < deadline {
                if condition() { return true }
                try await Task.sleep(for: .milliseconds(10))
            }
            return condition()
        }

        await session.prepareFixtureSourcePreview(url: file)
        let admitted = try await waitFor(.seconds(10)) {
            session.sourcePreviewState == .ready && session.admissiblePreviewDisplayTicket != nil
                && !session.isPreparingCurrentPreview && session.player?.currentItem?.status == .readyToPlay
        }
        XCTAssertTrue(admitted, "The mounted production surface must admit the initial composition")
        guard admitted else { return }
        let player = try XCTUnwrap(session.player)
        let item = try XCTUnwrap(player.currentItem)
        let composition = try XCTUnwrap(item.videoComposition)
        let compositor = try XCTUnwrap(item.customVideoCompositor)
        let initialTicket = try XCTUnwrap(session.admissiblePreviewDisplayTicket)
        let title = try XCTUnwrap(session.document.textElements.first)
        session.togglePlayback()
        let playing = try await waitFor(.seconds(2)) {
            session.isPlaying && player.rate > 0 && player.currentTime().seconds >= 0.05
        }
        XCTAssertTrue(playing, "The actual player clock must advance before the edit")
        guard playing else { return }
        let editTime = player.currentTime().seconds
        XCTAssertLessThan(editTime, 1, "Start the edit early in the four-second fixture")
        guard editTime < 1 else { return }
        session.updateTextContent(id: title.id, content: "A clearer story")
        XCTAssertEqual(session.document.textElements.first(where: { $0.id == title.id })?.text, "A clearer story")
        XCTAssertTrue(session.isPreparingCurrentPreview, "The edit must own pending frame work")
        XCTAssertTrue(session.isPlaying, "Editing must not pause playback")
        let settled = try await waitFor(.seconds(2)) {
            session.admissiblePreviewDisplayTicket != initialTicket
                && item.videoComposition !== composition && !session.isPreparingCurrentPreview
                && session.isPlaying && player.rate > 0 && player.currentTime().seconds > editTime + 0.1
        }
        XCTAssertTrue(settled, "The new playback revision and real surface must settle while playback advances")
        XCTAssertTrue(session.player === player)
        XCTAssertTrue(player.currentItem === item)
        let currentCompositor = try XCTUnwrap(item.customVideoCompositor)
        XCTAssertTrue((currentCompositor as AnyObject) === (compositor as AnyObject))
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertNil(session.previousPreviewPresentation)
        XCTAssertLessThan(player.currentTime().seconds, 4, "Completion must precede the fixture's end")
        let evidence = XCTAttachment(string: "initial=\(initialTicket); current=\(String(describing: session.admissiblePreviewDisplayTicket)); editTime=\(editTime); finalTime=\(player.currentTime().seconds); rate=\(player.rate); settled=\(settled)")
        evidence.name = "Playing in-place native frame admission"
        evidence.lifetime = .keepAlways
        add(evidence)
    }

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
