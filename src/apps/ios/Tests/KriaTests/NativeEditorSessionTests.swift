import AVFoundation
import KriaMediaEngine
import UIKit
import XCTest
@testable import Kria

@MainActor
final class NativeEditorSessionTests: XCTestCase {
    func testDeviceNarrationRequestUsesPublishedGenerationAndExactTarget() throws {
        let jobID = UUID()
        let base = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a")
        var recipe = base.recipe
        recipe.audio.narrationAssetID = "voiceover-item"
        let request = DeviceRenderRequest(identity: base.identity, recipe: recipe)
        let status = DeviceRenderStatusResponse(
            phase: "published", request: request, publishedGeneration: "generation-1"
        )

        XCTAssertEqual(
            try NativeEditorSession.currentDeviceNarrationRequest(
                status, jobID: jobID, variantID: "variant", generation: "generation-1"
            ),
            request
        )
        XCTAssertThrowsError(try NativeEditorSession.currentDeviceNarrationRequest(
            status, jobID: jobID, variantID: "variant", generation: "generation-2"
        )) { XCTAssertEqual($0 as? APIError, .conflict) }
        XCTAssertThrowsError(try NativeEditorSession.currentDeviceNarrationRequest(
            status, jobID: UUID(), variantID: "variant", generation: "generation-1"
        )) { XCTAssertEqual($0 as? APIError, .conflict) }
        XCTAssertThrowsError(try NativeEditorSession.currentDeviceNarrationRequest(
            status, jobID: jobID, variantID: "another-variant", generation: "generation-1"
        )) { XCTAssertEqual($0 as? APIError, .conflict) }
    }

    func testDeviceRecipeWithoutNarrationDoesNotInventAnAudioSource() throws {
        let jobID = UUID()
        let request = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a")
        let status = DeviceRenderStatusResponse(
            phase: "published", request: request, publishedGeneration: "generation-1"
        )
        XCTAssertNil(try NativeEditorSession.currentDeviceNarrationRequest(
            status, jobID: jobID, variantID: "variant", generation: "generation-1"
        ))
    }

    func testNoNarrationDeviceRecipeIsMemoizedUntilItsGenerationRefreshes() async throws {
        let threadID = UUID(), jobID = UUID()
        let request = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a")
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        // The failed fixture pool is enough to initialize the production
        // resolver; fixture composition then exercises preview audio directly.
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(
            phase: "published", request: request, publishedGeneration: "generation-1"
        )
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))

        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(fake.deviceRenderCallCount, 1, "An authoritative no-narration recipe is memoized for its generation")
        XCTAssertNil(session.yourSong, "a recipe without a song track connects nothing to the Sounds tab")

        let nextRequest = deviceRenderRequest(jobID: jobID, revision: 2, digest: "b")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(
            phase: "published", request: nextRequest, publishedGeneration: "generation-2"
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-2")
        let refreshRequest = expectation(description: "Refresh generation-two source pool")
        fake.sourcePoolExpectation = refreshRequest
        var nextVariant = Self.variant(duration: 2, generation: "generation-2")
        nextVariant["render_destination"] = .string("device")
        XCTAssertTrue(session.rebaseCleanDraft(from: nextVariant))
        await fulfillment(of: [refreshRequest], timeout: 3)
        for _ in 0..<100 {
            if case .failed = session.sourcePreviewState { break }
            await Task.yield()
        }
        guard case .failed = session.sourcePreviewState else {
            return XCTFail("The generation-two refresh must finish before fixture composition")
        }
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(fake.deviceRenderCallCount, 2, "A generation refresh must resolve its current device recipe again")
    }

    /// KRI-374: a creator-song montage has no music lane, so the live preview used to be silent. The pinned
    /// device recipe's `song` clip now plays, camera audio is forced to 0, and the Sounds row stays connected
    /// to it even when an older server sends no `user_song`.
    func testDeviceRecipeSongPlaysInPreviewMutesCameraAndConnectsSoundsRow() async throws {
        let threadID = UUID(), jobID = UUID()
        let wav = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: wav) }
        try Self.silentWAV(seconds: 4).write(to: wav)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: wav)
        let song = RenderAssetReference(id: "song-item", fingerprint: try RenderFingerprint(fingerprint),
                                        source: .song(planItemID: "item", generation: "3"))
        var recipe = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a").recipe
        recipe.assets.append(MediaAsset(id: song.id, relativePath: song.id, fingerprint: fingerprint, duration: 4))
        var bedClip = TimelineClip(id: "song-bed", sourceAssetID: song.id, sourceStart: 1, sourceDuration: 2, volume: 0.8)
        bedClip.audioFadeIn = 0.25; bedClip.audioFadeOut = 0.5
        recipe.tracks.append(TimelineTrack(id: "song", kind: .audio, clips: [bedClip]))
        recipe.audio = AudioMixRecipe(musicAssetID: song.id, originalVolume: 0)
        recipe.assetManifest = RenderAssetManifest(assets: [song])
        let request = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: 1,
            recipeDigest: String(repeating: "a", count: 64)), recipe: recipe)
        // Seed the verified cache the editor's resolver reads, so no grant or download is needed.
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        _ = try await RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
            .install(downloadedFile: wav, for: song)

        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["music_playback_mode"] = .string("reference_only")
        func session(variant: [String: JSONValue]) async -> (NativeEditorSession, EditorCommitSpy) {
            let fake = EditorCommitSpy(
                draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                    snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                    snapshot: [:], canUndo: false, createdAt: .now),
                authoritativeVariant: variant
            )
            fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
            fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "generation-1")
            let session = NativeEditorSession()
            await session.load(api: fake, threadID: threadID)
            return (session, fake)
        }
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))

        // An older server: no `user_song`, so the row comes from the recipe's clip alone.
        let (older, fake) = await session(variant: authoritative)
        await older.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(older.sourcePreviewState, .ready)
        XCTAssertEqual(fake.deviceRenderCallCount, 1)
        XCTAssertEqual(older.deviceSongBed?.sourceStart, 1)
        let displayed = try XCTUnwrap(older.displayedSourcePreviewRecipe)
        let track = try XCTUnwrap(displayed.tracks.first { $0.id == "song" && $0.kind == .audio })
        let clip = try XCTUnwrap(track.clips.first)
        XCTAssertEqual(clip.sourceStart, 1)
        XCTAssertEqual(clip.sourceDuration, 2, accuracy: 0.001)
        XCTAssertEqual(clip.volume, 0.8, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(clip.audioFadeIn), 0.25, accuracy: 0.001)
        XCTAssertEqual(try XCTUnwrap(clip.audioFadeOut), 0.5, accuracy: 0.001)
        let video = try XCTUnwrap(displayed.tracks.first { $0.kind == .video })
        XCTAssertFalse(video.clips.isEmpty)
        XCTAssertTrue(video.clips.allSatisfy { $0.volume == 0 }, "camera audio must never play over the song")
        XCTAssertEqual(older.yourSong, NativeEditorYourSong(title: "Your song", window: "Plays 0:01 – 0:03", mode: nil))
        // Preparing again is memoized: the recipe is not fetched twice for one generation.
        await older.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(fake.deviceRenderCallCount, 1)

        // The additive `user_song` field names the song and its mode.
        authoritative["user_song"] = .object(["title": .string("Midnight Drive"), "mode": .string("lipsync"),
            "duration_s": .number(214), "window_start_s": .number(108), "window_end_s": .number(123)])
        let (current, _) = await session(variant: authoritative)
        await current.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(current.yourSong, NativeEditorYourSong(title: "Midnight Drive", window: "Plays 1:48 – 1:50",
                                                              mode: "Lip-sync · master audio"))
    }

    /// KRI-428: volume, start and remove edit the song in the document (undoable, dirty, committed as `user_song`),
    /// and the live preview plays exactly what the server will render: the edited volume and start, or the
    /// camera's own audio once the song is removed.
    func testUserSongEditsDriveDirtyStateCommitUndoAndPreview() async throws {
        let (session, fake, sourceURL) = try await userSongSession(mode: "background", caps: [
            "volume": true, "window": true, "remove": true])
        await session.prepareFixtureSourcePreview(url: sourceURL)
        func song(_ session: NativeEditorSession) throws -> TimelineClip? {
            try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.id == "song" && $0.kind == .audio }?.clips.first
        }
        func cameraLevels(_ session: NativeEditorSession) throws -> [Double] {
            try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.kind == .video }?.clips.map(\.volume) ?? []
        }
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.yourSongControls?.volume, 1, "no `volume` on the wire reads as full volume")
        XCTAssertEqual(session.yourSongControls?.canEditStart, true)
        XCTAssertEqual(try cameraLevels(session), [0])

        session.setUserSongVolume(0.4)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(volume: 0.4))
        session.setUserSongStart(1.5)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(volume: 0.4, windowStartS: 1.5))
        XCTAssertEqual(session.yourSong?.window, "Plays 0:02 – 0:04", "the row shows the edited window")
        await session.prepareFixtureSourcePreview(url: sourceURL)
        let edited = try XCTUnwrap(song(session))
        XCTAssertEqual(edited.volume, 0.4, accuracy: 0.001, "preview plays the volume the server will write")
        XCTAssertEqual(edited.sourceStart, 1.5, accuracy: 0.001, "preview plays from the new start")
        XCTAssertEqual(try cameraLevels(session), [0], "camera stays muted while the song plays")

        // The start may go anywhere that leaves a second of song (the song stops early rather than the start being held back).
        session.setUserSongStart(10_000)
        XCTAssertEqual(session.document.userSong?.windowStartS ?? 0, 200 - NativeUserSong.minPlayableS, accuracy: 0.001)
        session.setUserSongStart(1.5)
        // Dragging back to the saved values is no change.
        session.setUserSongVolume(1)
        session.setUserSongStart(1)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertNil(session.document.userSong)
        session.setUserSongVolume(0.4)

        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(volume: 0.4, windowStartS: nil, removed: false))
        let wire = String(decoding: try JSONEncoder().encode(try XCTUnwrap(fake.lastRequest?.userSong)), as: UTF8.self)
        // Snake-case keys, and untouched fields are omitted rather than sent as null.
        XCTAssertTrue(wire.contains("\"volume\":0.4") && wire.contains("\"removed\":false") && !wire.contains("window_start_s"))
        XCTAssertFalse(session.hasUnsavedChanges, "a saved song edit is clean, even when the server does not echo the section")
        XCTAssertNil(fake.lastRequest?.timelineSlots, "untouched sections are not sent")

        // Remove: undoable until Save, un-mutes the camera in the preview, and sends only `removed`.
        session.removeUserSong()
        XCTAssertTrue(session.userSongRemoved)
        XCTAssertNil(session.yourSong)
        XCTAssertNil(session.yourSongControls)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertNil(try song(session), "no song track once removed")
        XCTAssertTrue(try cameraLevels(session).allSatisfy { $0 > 0 }, "camera audio plays again, like the server's recipe")
        session.undo()
        XCTAssertFalse(session.userSongRemoved)
        XCTAssertNotNil(session.yourSong)
        session.redo()
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(removed: true))
    }

    /// The Sounds tab's Original audio control and the per-clip audio button: a creator-song video plays the
    /// song alone until the creator turns the camera up, then both play at their own levels; each edit is dirty,
    /// undoable, saved as `mix.original_level` / the slot's `muted`, and heard in the live preview.
    func testOriginalAudioLevelAndClipAudioDrivePreviewDirtyStateAndCommit() async throws {
        let (session, fake, sourceURL) = try await userSongSession(mode: "lipsync", caps: [
            "volume": true, "window": false, "remove": true], originalAudio: true)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        func cameraLevels() throws -> [Double] {
            try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.kind == .video }?.clips.map(\.volume) ?? []
        }
        func songVolume() throws -> Double? {
            try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.id == "song" && $0.kind == .audio }?.clips.first?.volume
        }
        XCTAssertTrue(session.hasOriginalAudioControl)
        XCTAssertEqual(session.originalAudioLevel, 0, "a song video's camera is silent until the creator asks")
        XCTAssertEqual(try cameraLevels(), [0])
        let clipID = try XCTUnwrap(session.document.clips.first?.id)
        XCTAssertFalse(session.isClipAudioOn(clipID: clipID))

        // The whole-video level turns the camera up next to the song, which keeps its own level.
        session.setUserSongVolume(0.4)
        session.setOriginalAudioLevel(0.6)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.originalAudioLevel, 0.6, accuracy: 0.001)
        XCTAssertTrue(session.isDirty(.mix))
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertTrue(try cameraLevels().allSatisfy { $0 > 0 }, "the camera plays together with the song")
        XCTAssertEqual(try XCTUnwrap(songVolume()), 0.4, accuracy: 0.001)
        XCTAssertEqual(session.displayedSourcePreviewRecipe?.audio.originalVolume ?? 0, 0.6, accuracy: 0.001)

        // The per-clip button mutes just this clip, and works the other way too.
        XCTAssertTrue(session.isClipAudioOn(clipID: clipID))
        session.toggleClipAudio(clipID: clipID)
        XCTAssertFalse(session.isClipAudioOn(clipID: clipID))
        XCTAssertEqual(session.document.clips.first?.raw["muted"], .bool(true))
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(try cameraLevels(), [0], "a muted clip is silent in the preview")
        session.toggleClipAudio(clipID: clipID)
        XCTAssertTrue(session.isClipAudioOn(clipID: clipID))
        session.toggleClipAudio(clipID: clipID)

        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h2", expectedDuration: nil)
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertEqual(request.mix, ["original_level": .number(0.6)], "a guided Save carries only the footage's level, never a music level")
        XCTAssertEqual(request.timelineSlots?.first?.objectValue?["muted"], .bool(true))
        XCTAssertEqual(request.userSong, EditorCommitUserSong(volume: 0.4, windowStartS: nil, removed: false))

        // Back to zero is the silent default again, and Undo reverts a level edit.
        session.setOriginalAudioLevel(0)
        XCTAssertEqual(session.originalAudioLevel, 0)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(try cameraLevels(), [0])
        session.undo()
        XCTAssertEqual(session.originalAudioLevel, 0.6, accuracy: 0.001)
    }

    /// With the camera off for the whole video, turning one clip on raises the level and solos that clip.
    func testTurningAClipOnWhileOriginalAudioIsOffSolosThatClip() async throws {
        let (session, _, _) = try await userSongSession(mode: "background", caps: ["volume": true, "window": true, "remove": true], originalAudio: true)
        let clips = session.document.clips
        guard let target = clips.first?.id else { return XCTFail("fixture has a clip") }
        session.toggleClipAudio(clipID: target)
        XCTAssertEqual(session.originalAudioLevel, 1)
        XCTAssertTrue(session.isClipAudioOn(clipID: target))
        for other in session.document.clips.dropFirst() where !other.removed { XCTAssertEqual(other.raw["muted"], .bool(true)) }
        session.undo()
        XCTAssertEqual(session.originalAudioLevel, 0, "one undo step")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// Without the capabilities (older server, cloud render) the control is absent and edits are no-ops.
    func testOriginalAudioControlsStayClosedWithoutCapabilities() async throws {
        let (session, _, _) = try await userSongSession(mode: "background", caps: ["volume": true, "window": true, "remove": true], originalAudio: false)
        XCTAssertFalse(session.canEditOriginalAudio)
        XCTAssertFalse(session.canEditClipAudio)
        let clipID = try XCTUnwrap(session.document.clips.first?.id)
        session.setOriginalAudioLevel(0.5)
        session.toggleClipAudio(clipID: clipID)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// A reopened guided video shows the level the creator saved, and the camera plays with the song.
    func testSavedOriginalLevelIsRestoredOnReopen() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: ["volume": true, "window": true, "remove": true],
                                                                originalAudio: true, savedOriginalLevel: 0.3)
        XCTAssertEqual(session.originalAudioLevel, 0.3, accuracy: 0.001)
        XCTAssertFalse(session.hasUnsavedChanges)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        let levels = try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.kind == .video }?.clips.map(\.volume) ?? []
        XCTAssertTrue(levels.allSatisfy { $0 > 0 })
    }

    /// A lip-sync song keeps its start (each take's offset depends on it): the start setter is a no-op that
    /// never dirties the edit, while volume and remove still work.
    func testLipSyncSongLocksStartButKeepsVolumeAndRemove() async throws {
        let (session, _, _) = try await userSongSession(mode: "lipsync", caps: ["volume": true, "window": false, "remove": true])
        XCTAssertEqual(session.yourSongControls?.canEditStart, false)
        XCTAssertEqual(session.operationCapabilityReason("user_song.window"), "user_song_lipsync_locked")
        session.setUserSongStart(2)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertNil(session.document.userSong)
        session.setUserSongVolume(0.5)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(volume: 0.5))
        session.removeUserSong()
        XCTAssertEqual(session.document.userSong, EditorUserSongState(removed: true), "a removal drops the volume edit")
    }

    /// Without the `user_song.*` capabilities (an older server, or a closed edit) nothing about the song is editable.
    func testSongControlsStayClosedWithoutCapabilities() async throws {
        let (session, _, _) = try await userSongSession(mode: "background", caps: [:])
        XCTAssertEqual(session.yourSongControls?.canEditVolume, false)
        XCTAssertEqual(session.yourSongControls?.canEditStart, false)
        XCTAssertEqual(session.yourSongControls?.canRemove, false)
        session.setUserSongVolume(0.2); session.setUserSongStart(3); session.removeUserSong()
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertNil(session.document.userSong)
    }

    private static let allSongCaps = ["volume": true, "window": true, "remove": true]
    private func okResponse(_ generation: String) -> EditorCommitResponse {
        EditorCommitResponse(ok: true, generation: generation,
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "h", expectedDuration: nil)
    }

    /// KRI-457: the start may go up to songDuration - 1s, whatever the video length; the window shrinks to what is left.
    func testStartBoundLeavesOneSecondOfSongNotTheVideoLength() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps,
                                                            songDuration: 214, videoDuration: 14.347)
        XCTAssertEqual(session.duration, 14.347, accuracy: 0.001)
        for far in [10_000.0, 214, 213.4] {
            session.setUserSongStart(far)
            let start = try XCTUnwrap(session.document.userSong?.windowStartS)
            XCTAssertLessThanOrEqual(start, 214 - 1.0 + 0.0001, "\(far)")
        }
        session.setUserSongStart(10_000)
        XCTAssertEqual(session.document.userSong?.windowStartS ?? 0, 213, accuracy: 0.001)
        XCTAssertEqual(session.yourSongControls?.maxStartS ?? 0, 213, accuracy: 0.001)
        let controls = try XCTUnwrap(session.yourSongControls)
        XCTAssertEqual(controls.windowLengthS, 1, accuracy: 0.001, "the window shrinks to the song that is left")
        XCTAssertTrue(controls.songEndsBeforeVideo)
        XCTAssertEqual(session.yourSong?.window, "Plays 3:33 – 3:34", "the label ends where the song ends, not where the video would")
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        XCTAssertEqual(try XCTUnwrap(fake.lastRequest?.userSong?.windowStartS), 213, accuracy: 0.001)

        // A start that leaves room for the whole video keeps a full-length window and no note.
        session.setUserSongStart(100)
        let roomy = try XCTUnwrap(session.yourSongControls)
        XCTAssertEqual(roomy.windowLengthS, 14.347, accuracy: 0.001)
        XCTAssertFalse(roomy.songEndsBeforeVideo)
        XCTAssertEqual(session.yourSong?.window, "Plays 1:40 – 1:54")
    }

    /// The song follows the video as it changes, with no Save: the label's end tracks the video, and a pending
    /// start is held to the same one-second bound (it no longer moves with the video length).
    func testSongWindowFollowsTheVideoAndPendingStartKeepsTheSameBound() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.setUserSongStart(10_000)
        XCTAssertEqual(session.document.userSong?.windowStartS ?? 0, 200 - NativeUserSong.minPlayableS, accuracy: 0.001)
        session.setUserSongStart(50)
        XCTAssertEqual(session.yourSongControls?.windowLengthS ?? 0, 2, accuracy: 0.001)
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 3.5)
        XCTAssertEqual(session.duration, 3.5, accuracy: 0.001)
        XCTAssertEqual(session.yourSongControls?.windowLengthS ?? 0, 3.5, accuracy: 0.001, "extending the video extends the window")
        XCTAssertEqual(session.yourSongControls?.startS ?? 0, 50, accuracy: 0.001, "and never moves the start")
        XCTAssertEqual(session.yourSong?.window, "Plays 0:50 – 0:54")
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        XCTAssertEqual(try XCTUnwrap(fake.lastRequest?.userSong?.windowStartS), 50, accuracy: 0.001)
    }

    /// KRI-457: extending the video recompiles the preview (the in-place volume path needs an identical timeline) and the
    /// song clip follows the new length with no Save, up to the end of the song file (here 4s, from second 1).
    func testExtendingTheVideoExtendsThePreviewSongWithoutASave() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        func songLength() -> Double? { session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first?.sourceDuration }
        XCTAssertEqual(try XCTUnwrap(songLength()), 2, accuracy: 0.001)
        let compiles = session.sourcePreviewCompileCount
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 3.5)
        for _ in 0..<100 where session.sourcePreviewCompileCount == compiles { try await Task.sleep(for: .milliseconds(20)) }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertGreaterThan(session.sourcePreviewCompileCount, compiles, "a timeline change rebuilds, it is not absorbed as a volume change")
        XCTAssertEqual(try XCTUnwrap(songLength()), 3, accuracy: 0.001, "the song is as long as the video allows: 4s file from second 1")
        XCTAssertFalse(session.hasUnsavedChanges && session.document.userSong != nil, "no song edit was needed")
    }

    /// A video extended near the end of the song plays the song to its end only, and the audition follows.
    func testAuditionLengthIsWhatIsLeftOfTheSong() async throws {
        let (session, _, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps, songDuration: 20, videoDuration: 8)
        session.setUserSongStart(15)
        let song = try XCTUnwrap(session.effectiveUserSong)
        XCTAssertEqual(song.windowLengthS, 5, accuracy: 0.001, "min(video 8s, song 20s - start 15s)")
        session.setUserSongStart(5)
        XCTAssertEqual(try XCTUnwrap(session.effectiveUserSong).windowLengthS, 8, accuracy: 0.001)
    }

    /// An acknowledged removal is never sent again: not by a render retry, and not after a conflict rebase.
    func testSavedRemovalIsNotResentByRetryOrConflictRebase() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.removeUserSong()
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(removed: true))
        XCTAssertNil(session.document.userSong, "a saved edit is folded out of the unsaved state")
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.userSongRemoved, "and the song stays removed")
        XCTAssertNil(session.yourSong)

        let count = fake.commitCount
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, count + 1)
        XCTAssertNil(fake.lastRequest?.userSong, "a retry does not resend the removal")

        session.saveState = .conflict
        await session.rebaseAfterConflict()
        XCTAssertNil(session.document.userSong)
        XCTAssertFalse(session.hasUnsavedChanges)
        await session.retryRender()
        XCTAssertNil(fake.lastRequest?.userSong, "nor does a retry after a conflict rebase")
    }

    /// A render retry after a volume save re-sends the acknowledged volume (idempotent) so the render restarts.
    func testRenderRetryResendsAnAcknowledgedVolume() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.setUserSongVolume(0.4)
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        await session.retryRender()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(volume: 0.4))
        await session.save()
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// After a Save the saved song is the new baseline: dragging the volume back to the ORIGINAL value is a change.
    func testVolumeSetBackToTheOriginalAfterSaveIsSent() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.setUserSongVolume(0.4)
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        XCTAssertEqual(session.yourSongControls?.volume, 0.4, "the row shows the saved volume")
        XCTAssertFalse(session.hasUnsavedChanges)
        session.setUserSongVolume(1)
        XCTAssertTrue(session.hasUnsavedChanges)
        fake.commitResponse = okResponse("generation-3")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(volume: 1))
        XCTAssertEqual(session.yourSongControls?.volume, 1)
    }

    /// A removal the server already has (422 user_song_unavailable) is what the creator asked for: it is folded in, not a dead end.
    func testUnavailableSongOnARemovalCountsAsAlreadyRemoved() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.removeUserSong()
        fake.commitThrow = EditorSaveError.userSongUnavailable(reason: nil)
        await session.save()
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.userSongRemoved)
        XCTAssertNil(session.document.userSong)
        XCTAssertEqual(fake.commitCount, 1)
    }

    /// Any other 422 keeps the creator's song edit pending and retryable.
    func testRejectedSongEditStaysPending() async throws {
        let (session, fake, _) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        session.setUserSongStart(50)
        fake.commitThrow = EditorSaveError.userSongWindowOutOfRange(reason: nil)
        await session.save()
        XCTAssertEqual(session.saveState, .failed("That start point leaves less than a second of your song. Slide it earlier."))
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(windowStartS: 50))
        // Unavailable on a start edit (not a removal) is not treated as applied either.
        fake.commitThrow = EditorSaveError.userSongUnavailable(reason: nil)
        await session.save()
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    /// KRI-432: a volume change on a settled preview is a player-level change: same composition, same player,
    /// no recompile (no black frame, no restart), and the recipe on screen still carries the new level.
    /// The song is compiled at unity gain and `AVPlayer.volume` carries the level, because swapping the live
    /// item's audio mix silences the iPhone preview (KRI-241).
    func testSongVolumeChangeIsAppliedLiveWithoutRebuildingThePreview() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(session.liveSongVolumeActive, "the song is the only audible track")
        func songClip() throws -> TimelineClip? {
            try XCTUnwrap(session.displayedSourcePreviewRecipe).tracks.first { $0.id == "song" }?.clips.first
        }
        let player = try XCTUnwrap(session.player)
        let item = try XCTUnwrap(player.currentItem)
        let compiles = session.sourcePreviewCompileCount
        let time = session.currentTime
        XCTAssertEqual(player.volume, 1)

        for level in [0.4, 0.0, 1.0, 0.25] {
            session.setUserSongVolume(level)
            for _ in 0..<5 { await Task.yield() }
            try await Task.sleep(for: .milliseconds(150))
            XCTAssertTrue(session.player === player, "no player swap at \(level)")
            XCTAssertTrue(session.player?.currentItem === item, "no item swap at \(level)")
            XCTAssertEqual(session.sourcePreviewCompileCount, compiles, "no recompile at \(level)")
            XCTAssertEqual(Double(player.volume), level, accuracy: 0.001)
            XCTAssertEqual(try XCTUnwrap(songClip()).volume, level, accuracy: 0.001, "the recipe on screen carries the level")
            XCTAssertEqual(session.currentTime, time, "the playhead did not move")
        }
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(try XCTUnwrap(songClip()).sourceStart, 1, accuracy: 0.001, "fades and window are untouched")
    }

    /// KRI-432: dragging the start edits the document live (one undo step) but rebuilds the preview once, on release.
    func testStartDragRebuildsThePreviewOnceOnReleaseAndIsOneUndoStep() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: Self.allSongCaps)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        let compiles = session.sourcePreviewCompileCount
        session.beginSongStartDrag()
        for start in [0.25, 0.5, 1.0, 1.5] {
            session.moveSongStart(start)
            try await Task.sleep(for: .milliseconds(120))
        }
        XCTAssertEqual(session.sourcePreviewCompileCount, compiles, "no rebuild while the finger is down")
        XCTAssertEqual(session.document.userSong?.windowStartS, 1.5)
        session.endSongStartDrag()
        for _ in 0..<100 where session.sourcePreviewCompileCount == compiles { try await Task.sleep(for: .milliseconds(20)) }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(session.sourcePreviewCompileCount, compiles + 1, "one rebuild, at release")
        let song = try XCTUnwrap(session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first)
        XCTAssertEqual(song.sourceStart, 1.5, accuracy: 0.01, "the rebuilt preview starts at the new start")
        session.songAudition.cancel()
        session.undo()
        XCTAssertNil(session.document.userSong, "the whole drag is one undo step")
        XCTAssertFalse(session.canUndo)
    }

    /// Job 5a7f6c88: trimming the head of the opening cut moved the footage but not the song, so a lip-sync
    /// singer drifted off the audio by exactly the trim. The song start follows the first cut's head.
    func testTrimmingTheOpeningCutsHeadMovesALipsyncSongWithIt() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "lipsync", caps: ["volume": true, "window": false, "remove": true])
        await session.prepareFixtureSourcePreview(url: sourceURL)
        func songStart() throws -> Double {
            try XCTUnwrap(session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first).sourceStart
        }
        XCTAssertEqual(try songStart(), 1, accuracy: 0.001, "untouched: the pinned window")
        let clipID = try XCTUnwrap(session.document.clips.first?.id)
        session.setClipTiming(clipID: clipID, inS: 0.01)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(try songStart(), 1, accuracy: 0.001, "under a frame is rounding noise")
        session.setClipTiming(clipID: clipID, inS: 0.5)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(try songStart(), 1.5, accuracy: 0.001, "the singer starts 0.5s later in the take, so does the song")
    }

    func testTrimmingAHeadNeverMovesABackgroundSong() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: ["volume": true, "window": true, "remove": true])
        let clipID = try XCTUnwrap(session.document.clips.first?.id)
        session.setClipTiming(clipID: clipID, inS: 0.5)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        let start = try XCTUnwrap(session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first).sourceStart
        XCTAssertEqual(start, 1, accuracy: 0.001)
    }

    func testLipsyncAnchorShiftOnlyFollowsTheSameSourceBeyondAFrame() {
        typealias Anchor = NativeLipsyncSongAnchor
        XCTAssertEqual(Anchor.startShift(savedClipIndex: 0, savedInS: 0.3, currentClipIndex: 0, currentInS: 0), -0.3, accuracy: 1e-9)
        XCTAssertEqual(Anchor.startShift(savedClipIndex: 0, savedInS: 0.3, currentClipIndex: 0, currentInS: 0.32), 0)
        XCTAssertEqual(Anchor.startShift(savedClipIndex: 0, savedInS: 0.3, currentClipIndex: 1, currentInS: 0), 0, "another take opens the video")
        XCTAssertEqual(Anchor.startShift(savedClipIndex: nil, savedInS: nil, currentClipIndex: 0, currentInS: 0), 0)
    }


    // MARK: KRI-561 song trim

    private static let trimCaps = ["volume": true, "window": true, "remove": true, "trim": true]

    /// A background song 10...30 s over a 20 s video; the saved window follows the video (no creator end).
    private func backgroundTrimSession(trim: Bool = true, trimEditable: Bool = true) async throws -> (NativeEditorSession, EditorCommitSpy) {
        var caps = Self.allSongCaps
        if trim { caps["trim"] = trimEditable }
        let (session, fake, _) = try await userSongSession(mode: "background", caps: caps, songDuration: 200, videoDuration: 20) { variant in
            variant["user_song"] = .object(["title": .string("Midnight Drive"), "mode": .string("background"), "duration_s": .number(200),
                                            "window_start_s": .number(10), "window_end_s": .number(30)])
        }
        return (session, fake)
    }

    func testEndHandleClampsClearsAtTheNaturalEndAndCommitsOnlyWhatChanged() async throws {
        let (session, fake) = try await backgroundTrimSession()
        let initial = try XCTUnwrap(session.yourSongControls)
        XCTAssertTrue(initial.trimOffered)
        XCTAssertTrue(initial.canTrim)
        XCTAssertEqual(initial.endS, 30, accuracy: 1e-9)
        XCTAssertFalse(initial.hasCreatorEnd)

        session.setUserSongEnd(10.2)
        XCTAssertEqual(session.document.userSong?.windowEndS ?? 0, 11, accuracy: 1e-9, "never closer than a second to the start")
        session.setUserSongEnd(1_000)
        XCTAssertNil(session.document.userSong, "dragged back to the natural end: nothing to send")
        XCTAssertFalse(session.hasUnsavedChanges)
        session.setUserSongEnd(29.999)
        XCTAssertNil(session.document.userSong, "within rounding of the natural end is the natural end")

        session.setUserSongEnd(25)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(windowEndS: 25))
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 25, accuracy: 1e-9)
        XCTAssertEqual(session.yourSongControls?.hasCreatorEnd, true)
        XCTAssertEqual(session.yourSong?.window, "Plays 0:10 – 0:25")

        // Moving the start later keeps the end, and cannot pass it: at most a second before.
        session.setUserSongStart(40)
        XCTAssertEqual(session.document.userSong?.windowStartS ?? 0, 24, accuracy: 1e-9)
        XCTAssertEqual(session.document.userSong?.windowEndS ?? 0, 25, accuracy: 1e-9)
        session.setUserSongStart(12)
        XCTAssertEqual(session.yourSongControls?.startS ?? 0, 12, accuracy: 1e-9)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 25, accuracy: 1e-9)

        // The end can't pass where the video would stop it (start + video).
        session.setUserSongEnd(500)
        XCTAssertNil(session.document.userSong?.windowEndS, "past the natural end clears it")

        session.setUserSongEnd(25)
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(windowStartS: 12, windowEndS: 25))
        XCTAssertNil(session.document.userSong)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 25, accuracy: 1e-9, "the saved end is the baseline")

        // Dragging back to the saved end is no change; dragging to the natural end now CLEARS the saved one.
        session.setUserSongEnd(25)
        XCTAssertNil(session.document.userSong)
        session.setUserSongEnd(1_000)
        XCTAssertEqual(session.document.userSong, EditorUserSongState(windowEndS: 200), "the song's own length is how the wire clears it")
        fake.commitResponse = okResponse("generation-3")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.userSong, EditorCommitUserSong(windowEndS: 200))
        XCTAssertEqual(session.yourSongControls?.hasCreatorEnd, false)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 32, accuracy: 1e-9, "start 12 + the 20 s video")
    }

    func testEndEditIsOneUndoStepAndASavedEndSurvivesALongerVideo() async throws {
        let (session, _) = try await backgroundTrimSession()
        session.beginSongEndDrag()
        session.moveSongEnd(15)
        session.moveSongEnd(18)
        session.endSongEndDrag()
        XCTAssertEqual(session.document.userSong?.windowEndS ?? 0, 18, accuracy: 1e-9)
        session.undo()
        XCTAssertNil(session.document.userSong, "the whole drag is one undo step")
        XCTAssertFalse(session.canUndo)
        // A video made longer than the creator's end keeps it: the end is absolute.
        session.setUserSongEnd(18)
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 25)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 18, accuracy: 1e-9)
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 5)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 15, accuracy: 1e-9, "a shorter video still wins")
    }

    func testWithoutTheTrimCapabilityTheOldControlsStayAndTheEndCannotBeEdited() async throws {
        let (session, _) = try await backgroundTrimSession(trim: false)
        let controls = try XCTUnwrap(session.yourSongControls)
        XCTAssertFalse(controls.trimOffered, "an older server: today's single-handle bar")
        XCTAssertFalse(controls.canTrim)
        XCTAssertTrue(controls.canEditStart)
        session.setUserSongEnd(20)
        XCTAssertNil(session.document.userSong)
        XCTAssertFalse(session.applyLipsyncSongTrim(start: 11, end: 20))

        let (closed, _) = try await backgroundTrimSession(trimEditable: false)
        XCTAssertEqual(closed.yourSongControls?.trimOffered, true)
        XCTAssertEqual(closed.yourSongControls?.canTrim, false, "offered but closed: shown, not editable")
        closed.setUserSongEnd(20)
        XCTAssertNil(closed.document.userSong)
    }

    func testBackgroundPreviewStopsAtTheCreatorEndAndFollowsTheStart() async throws {
        let (session, _, sourceURL) = try await userSongSession(mode: "background", caps: Self.trimCaps, songDuration: 200, videoDuration: 2)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        func songClip() throws -> TimelineClip { try XCTUnwrap(session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first) }
        // The song file is 4 s and the bed plays it from second 1 for the 2 s video; the creator stops it at 2.
        session.setUserSongEnd(2)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertEqual(try songClip().sourceDuration, 1, accuracy: 0.01, "plays 1...2 only")
        XCTAssertEqual(try XCTUnwrap(try songClip().audioFadeOut), 0.5, accuracy: 0.01, "capped at half the length")
    }

    // Lip-sync: four 2.5 s cuts, take k filmed from second k, so delta_k = 100 + 1.5 k puts every cut on song second 100.
    private func lipsyncTrimSession(trim: Bool = true, withPool: Bool = true) async throws -> (NativeEditorSession, EditorCommitSpy, URL) {
        let pool = NativeEditorSourcePool(clips: (0..<4).map { index in
            .init(clipIndex: index, nativeSource: NativeTimelineSource(mediaID: "take-\(index)", sourceURL: nil, original: nil, localRequired: false))
        }, baseGeneration: "generation-1")
        var caps: [String: Bool] = ["volume": true, "window": false, "remove": true]
        if trim { caps["trim"] = true }
        return try await userSongSession(mode: "lipsync", caps: caps, songDuration: 200, videoDuration: 10, pool: withPool ? pool : nil) { variant in
            variant["user_song"] = .object(["title": .string("Midnight Drive"), "mode": .string("lipsync"), "duration_s": .number(200),
                "window_start_s": .number(100), "window_end_s": .number(110),
                "takes": .object(Dictionary(uniqueKeysWithValues: (0..<4).map { ("take-\($0)", JSONValue.number(100 + 1.5 * Double($0))) }))])
            variant["user_timeline"] = .object(["slots": .array((0..<4).map { index in
                .object(["slot_id": .string("slot-\(index)"), "clip_index": .number(Double(index)), "in_s": .number(Double(index)),
                         "duration_s": .number(2.5), "source_duration_s": .number(30), "removed": .bool(false)])
            })])
        }
    }

    func testLipsyncTrimDropsOutsideCutsInOneUndoStepAndTheSongFollowsTheSinger() async throws {
        let (session, fake, sourceURL) = try await lipsyncTrimSession()
        let controls = try XCTUnwrap(session.yourSongControls)
        XCTAssertTrue(controls.trimOffered)
        XCTAssertTrue(controls.canTrim)
        XCTAssertFalse(controls.canEditStart, "the start itself stays a derived value")
        XCTAssertEqual(controls.startS, 100, accuracy: 1e-9)
        XCTAssertEqual(controls.endS, 110, accuracy: 1e-9)
        let before = session.document

        XCTAssertTrue(session.applyLipsyncSongTrim(start: 102.5, end: 107.5))
        XCTAssertEqual(session.document.clips.map(\.id), ["slot-1", "slot-2"])
        XCTAssertEqual(session.document.clips.map(\.inS), [1, 2], "kept cuts keep their footage windows")
        XCTAssertEqual(session.document.clips.map(\.durationS), [2.5, 2.5])
        XCTAssertEqual(session.document.deletions.map(\.id).sorted(), ["slot-0", "slot-3"])
        XCTAssertNil(session.document.userSong, "a lip-sync trim edits the video, not a song field")
        XCTAssertEqual(session.duration, 5, accuracy: 1e-6)
        let after = try XCTUnwrap(session.yourSongControls)
        XCTAssertEqual(after.startS, 102.5, accuracy: 1e-6, "the song start follows the cuts")
        XCTAssertEqual(after.endS, 107.5, accuracy: 1e-6)
        XCTAssertEqual(session.yourSong?.window, "Plays 1:43 – 1:48")

        await session.prepareFixtureSourcePreview(url: sourceURL)
        let start = try XCTUnwrap(session.displayedSourcePreviewRecipe?.tracks.first { $0.id == "song" }?.clips.first).sourceStart
        // The harness's pinned recipe plays its 4 s song file from second 1; the trim moves that start by the 2.5 s the cuts moved.
        XCTAssertEqual(start, 1 + 2.5, accuracy: 0.01, "and so does the preview's song, so it stays on the singer")

        session.undo()
        XCTAssertEqual(session.document.clips, before.clips)
        XCTAssertEqual(session.document.deletions, before.deletions)
        XCTAssertEqual(session.document.tombstones, before.tombstones)
        XCTAssertFalse(session.canUndo, "cuts and window came back in ONE step")
        XCTAssertEqual(session.yourSongControls?.startS ?? 0, 100, accuracy: 1e-6)
        XCTAssertFalse(session.hasUnsavedChanges)

        // Save sends the cuts and deletions, and never a song window for a lip-sync song.
        XCTAssertTrue(session.applyLipsyncSongTrim(start: 101, end: 109))
        fake.commitResponse = okResponse("generation-2")
        await session.save()
        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertNil(request.userSong, "never window_start_s / window_end_s for lip-sync")
        XCTAssertEqual(request.timelineSlots?.count, 4)
    }

    func testLipsyncTrimStraddlesKeepsTheSingerOnTheSongAndRefusesTooShort() async throws {
        let (session, _, _) = try await lipsyncTrimSession()
        XCTAssertFalse(session.applyLipsyncSongTrim(start: 104, end: 106.5), "2.5 s is under the 3 s minimum")
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertTrue(session.applyLipsyncSongTrim(start: 101, end: 109))
        XCTAssertEqual(session.document.clips.count, 4, "both edge cuts straddle, nothing is dropped")
        XCTAssertEqual(session.document.clips.first?.inS ?? 0, 1, accuracy: 1e-9, "the head cut lost its first second")
        XCTAssertEqual(session.document.clips.first?.durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(session.document.clips.last?.durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(session.yourSongControls?.startS ?? 0, 101, accuracy: 1e-6)
        XCTAssertEqual(session.yourSongControls?.endS ?? 0, 109, accuracy: 1e-6)
        // The range is the current window: asking for more does nothing.
        XCTAssertFalse(session.applyLipsyncSongTrim(start: 90, end: 200))
        XCTAssertEqual(session.yourSongControls?.startS ?? 0, 101, accuracy: 1e-6)
    }

    func testLipsyncTrimNeedsTheTrimCapabilityAndFallsBackToTheFirstCutWithoutTakes() async throws {
        let (old, _, _) = try await lipsyncTrimSession(trim: false)
        XCTAssertEqual(old.yourSongControls?.trimOffered, false)
        XCTAssertFalse(old.applyLipsyncSongTrim(start: 102.5, end: 107.5))
        XCTAssertTrue(old.document.deletions.isEmpty)

        // Without the source pool no take's offset is known: the first cut's head is the anchor, as before.
        let (noPool, _, _) = try await lipsyncTrimSession(withPool: false)
        XCTAssertTrue(noPool.applyLipsyncSongTrim(start: 101, end: 109))
        XCTAssertEqual(noPool.yourSongControls?.startS ?? 0, 101, accuracy: 1e-6, "the head cut moved a second, so does the song")
    }

    /// A background or lip-sync creator-song edit on a device recipe whose song file is 4s long and whose
    /// bed plays 1s...3s of it; `caps` are the nested `user_song.{volume,window,remove}` editable flags.
    private func userSongSession(mode: String, caps: [String: Bool], songDuration: Double = 200, videoDuration: Double = 2,
                                 originalAudio: Bool = false, savedOriginalLevel: Double? = nil,
                                 pool: NativeEditorSourcePool? = nil,
                                 editVariant: ((inout [String: JSONValue]) -> Void)? = nil) async throws -> (NativeEditorSession, EditorCommitSpy, URL) {
        let threadID = UUID(), jobID = UUID()
        let wav = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).wav")
        addTeardownBlock { try? FileManager.default.removeItem(at: wav) }
        try Self.silentWAV(seconds: 4).write(to: wav)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: wav)
        let song = RenderAssetReference(id: "song-item", fingerprint: try RenderFingerprint(fingerprint),
                                        source: .song(planItemID: "item", generation: "3"))
        var recipe = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a").recipe
        recipe.assets.append(MediaAsset(id: song.id, relativePath: song.id, fingerprint: fingerprint, duration: 4))
        recipe.tracks.append(TimelineTrack(id: "song", kind: .audio, clips: [
            TimelineClip(id: "song-bed", sourceAssetID: song.id, sourceStart: 1, sourceDuration: 2, volume: 1)]))
        recipe.audio = AudioMixRecipe(musicAssetID: song.id, originalVolume: 0)
        recipe.assetManifest = RenderAssetManifest(assets: [song])
        let request = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: 1,
            recipeDigest: String(repeating: "a", count: 64)), recipe: recipe)
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        _ = try await RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
            .install(downloadedFile: wav, for: song)

        var authoritative = Self.variant(duration: videoDuration, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["music_playback_mode"] = .string("reference_only")
        authoritative["source_audio_preserved"] = .bool(false)
        authoritative["user_song"] = .object(["title": .string("Midnight Drive"), "mode": .string(mode),
            "duration_s": .number(songDuration), "window_start_s": .number(1), "window_end_s": .number(3)])
        if let savedOriginalLevel {
            authoritative["resolved_archetype"] = .string("guided_story")
            authoritative["original_audio_level"] = .number(savedOriginalLevel)
        }
        editVariant?(&authoritative)
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
            "original_audio": .object(["editable": .bool(originalAudio)]),
            "clips": .object(["audio": .object(["editable": .bool(originalAudio)])]),
            "user_song": .object(Dictionary(uniqueKeysWithValues: caps.map { key, editable in
                (key, JSONValue.object(["editable": .bool(editable)]
                    .merging(key == "window" && !editable ? ["reason": .string("user_song_lipsync_locked")] : [:]) { $1 }))
            })),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        fake.sourcePoolResult = pool ?? NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake, try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4")))
    }

    /// A recipe whose song cannot be resolved degrades to the finished-render fallback instead of a silent live preview.
    func testUnresolvableDeviceSongFailsLivePreviewSoItFallsBackToTheFinishedRender() async throws {
        let threadID = UUID(), jobID = UUID()
        let fingerprint = AssetFingerprint(hex: String(repeating: "c", count: 64), byteCount: 10)
        let song = RenderAssetReference(id: "song-item", fingerprint: try RenderFingerprint(fingerprint),
                                        source: .song(planItemID: "item", generation: "3"))
        var recipe = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a").recipe
        recipe.assets.append(MediaAsset(id: song.id, relativePath: song.id, fingerprint: fingerprint, duration: 4))
        recipe.tracks.append(TimelineTrack(id: "song", kind: .audio, clips: [
            TimelineClip(id: "song-bed", sourceAssetID: song.id, sourceStart: 1, sourceDuration: 2)]))
        recipe.audio = AudioMixRecipe(musicAssetID: "someone-else", originalVolume: 0)
        recipe.assetManifest = RenderAssetManifest(assets: [song])
        let request = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: 1,
            recipeDigest: String(repeating: "a", count: 64)), recipe: recipe)
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [], baseGeneration: "generation-1")
        fake.deviceRenderResponse = DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "generation-1")
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)
        guard case .failed = session.sourcePreviewState else { return XCTFail("a song that cannot be resolved must not preview silently") }
        XCTAssertNil(session.displayedSourcePreviewRecipe)
    }

    private static func silentWAV(seconds: Int, sampleRate: Int = 8000) -> Data {
        let samples = seconds * sampleRate
        func le32(_ v: Int) -> [UInt8] { (0..<4).map { UInt8((v >> (8 * $0)) & 0xff) } }
        func le16(_ v: Int) -> [UInt8] { (0..<2).map { UInt8((v >> (8 * $0)) & 0xff) } }
        var bytes: [UInt8] = Array("RIFF".utf8) + le32(36 + samples * 2) + Array("WAVEfmt ".utf8) + le32(16)
        bytes += le16(1) + le16(1) + le32(sampleRate) + le32(sampleRate * 2) + le16(2) + le16(16)
        bytes += Array("data".utf8) + le32(samples * 2) + [UInt8](repeating: 0, count: samples * 2)
        return Data(bytes)
    }

    /// Job 385e3b13: a phone Talking edit with photo cards opened to a black
    /// editor. The server keeps no timeline slots for it, so the preview had
    /// no video track, yet the cards made the recipe valid. The source clip now
    /// plays, locked, under the cards and captions.
    func testPhoneTalkingEditPreviewsItsSourceVideoAsALockedClip() async throws {
        let (session, _) = try await Self.phoneTalkingSession(lanesEditable: true)

        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertEqual(session.timelineClips.count, 1)
        XCTAssertEqual(session.timelineClips.first?.sourceClipIndex, 0)
        XCTAssertEqual(session.timelineClips.first?.start, 0)
        XCTAssertGreaterThan(try XCTUnwrap(session.timelineClips.first?.end), 0)
        XCTAssertFalse(session.canEditTimeline)
        XCTAssertFalse(session.hasUnsavedChanges, "The source clip is server state, never a user edit to Save")
    }

    /// KRI-211: a project whose originals aren't on this iPhone settles into its own state (no Retry
    /// can help), the relink targets come from the source pool that failed, a wrong file is refused, and
    /// the right one rebuilds the live preview.
    func testMissingOriginalsSettleIntoOriginalsUnavailableAndRelinkRebuildsThePreview() async throws {
        let (session, _) = try await Self.phoneTalkingSession(lanesEditable: true, bindOriginal: false)
        XCTAssertEqual(session.sourcePreviewState, .originalsUnavailable)
        XCTAssertTrue(session.sourcePreviewState.isFailure)

        let targets = await session.originalsNeedingRelink()
        XCTAssertEqual(targets.map(\.mediaID), ["source"])
        XCTAssertEqual(targets.first?.title, "Original 1")
        let target = try XCTUnwrap(targets.first)

        let wrong = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try Data("not the approved original".utf8).write(to: wrong)
        defer { try? FileManager.default.removeItem(at: wrong) }
        do {
            try await session.relinkOriginal(target, from: wrong)
            XCTFail("A different file must not be accepted as the approved original")
        } catch {}
        let stillMissing = await session.originalsNeedingRelink()
        XCTAssertEqual(stillMissing.count, 1)

        let right = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try FileManager.default.copyItem(at: XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4")), to: right)
        defer { try? FileManager.default.removeItem(at: right) }
        try await session.relinkOriginal(target, from: right)
        let remaining = await session.originalsNeedingRelink()
        XCTAssertTrue(remaining.isEmpty)
        await session.prepareSourcePreview()
        XCTAssertEqual(session.sourcePreviewState, .ready)
    }

    /// Cards and sounds closed (their rollout flag off): the server sends no
    /// lanes, so a live preview would drop what the finished MP4 shows. The
    /// editor keeps playing the finished MP4.
    func testPhoneTalkingEditWithClosedLanesKeepsTheFinishedMP4() async throws {
        let (session, _) = try await Self.phoneTalkingSession(lanesEditable: false)

        XCTAssertTrue(session.timelineClips.isEmpty)
        guard case .failed = session.sourcePreviewState else {
            return XCTFail("No video track must stay invalid so the finished MP4 plays")
        }
    }

    /// Lanes closed, yet the edit still carries a persisted card. The card
    /// alone made the recipe valid, so the editor played it over a black
    /// canvas. With no video track it keeps playing the finished MP4.
    func testPhoneTalkingEditWithClosedLanesAndACardKeepsTheFinishedMP4() async throws {
        let (session, _) = try await Self.phoneTalkingSession(lanesEditable: false, card: true)

        XCTAssertEqual(session.document.mediaOverlays.map(\.id), ["card"])
        XCTAssertTrue(session.timelineClips.isEmpty)
        guard case .failed(let message) = session.sourcePreviewState else {
            return XCTFail("A card over no video track must not become the live preview (state: \(session.sourcePreviewState))")
        }
        XCTAssertEqual(message, NativeEditorSession.sourcePreviewMessage(for: NativeEditorRenderError.missingVideoTrack))
        XCTAssertTrue(session.isShowingRenderedFallback)
        XCTAssertNil(session.displayedSourcePreviewRecipe)
    }

    /// KRI-287: Visuals → "Add photo or video" on a phone Voiceover edit. The
    /// server opens `phone_editor_media` + `visual_blocks` with the revision the
    /// app registers against; each import is admitted, placed at the playhead,
    /// and plays live in the preview over the locked voiceover cut.
    func testPhoneVoiceoverEditAddsAPhotoAndAVideoLiveInThePreview() async throws {
        let (session, fake, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: true)
        defer { _ = uploads }
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(session.canImportVisuals)
        XCTAssertNil(session.visualImportUnavailableMessage)

        await session.addLibraryVisual(try XCTUnwrap(fake.visualPool?.first { $0.kind == "image" }))
        XCTAssertNil(session.visualError)
        session.currentTime = 0.5
        await session.addLibraryVisual(try XCTUnwrap(fake.visualPool?.first { $0.kind == "video" }))
        XCTAssertNil(session.visualError)

        XCTAssertEqual(fake.registeredSourceIDs, [Self.voiceoverPhotoID, Self.voiceoverVideoID])
        XCTAssertEqual(fake.registeredRevisionNumbers, [1, 1])
        XCTAssertEqual(session.document.visualBlocks.map { $0.raw["media_kind"]?.stringValue }, ["image", "video"])
        XCTAssertEqual(session.document.visualBlocks.map { $0.raw["src_gcs_path"]?.stringValue },
                       [Self.voiceoverPhotoPath, Self.voiceoverVideoPath])
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertTrue(recipe.tracks.contains { $0.kind == .video && !$0.clips.isEmpty }, "The voiceover cut still plays underneath")
        let overlays = recipe.tracks.filter { $0.kind == .overlay }.flatMap(\.clips)
        XCTAssertEqual(overlays.count, 2)
        XCTAssertTrue(overlays.allSatisfy { $0.visualPlacement != nil })
    }

    /// KRI-287: reopening a saved Voiceover edit replays its added photo from
    /// the timeline's `visual_block` asset, so the preview matches the MP4.
    func testReopenedPhoneVoiceoverEditPreviewsItsSavedPhoto() async throws {
        let (session, _, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: true, savedPhoto: true)
        defer { _ = uploads }

        XCTAssertEqual(session.document.visualBlocks.map(\.id), ["photo-layer"])
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertEqual(recipe.tracks.filter { $0.kind == .overlay }.flatMap(\.clips).count, 1)
    }

    /// A server without the KRI-287 rollout (lanes only) keeps the import
    /// greyed out with the device copy.
    func testPhoneVoiceoverEditWithoutMediaRolloutKeepsImportClosed() async throws {
        let (session, _, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: false)
        defer { _ = uploads }

        XCTAssertFalse(session.canImportVisuals)
        XCTAssertEqual(session.visualImportUnavailableMessage, "Adding media isn’t available for this edit on this iPhone.")
    }

    static let voiceoverPhotoID = "5b3f6a1e-8f1c-4c55-9a8e-2f7d1c9b0a11"
    static let voiceoverVideoID = "c4e1d2f3-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
    static let voiceoverPhotoPath = "users/owner/plan/item/pool/photo.png"
    static let voiceoverVideoPath = "users/owner/plan/item/pool/clip.mp4"

    /// KRI-455: a phone Voiceover edit's opening title arrives read-only beside
    /// the caption mirrors. Since KRI-465 this is the server's kill-switch shape
    /// (`PHONE_NARRATED_TITLE_EDITS_ENABLED=false`); the editable shape is
    /// `testEditableVoiceoverTitleIsOnTheTimelineAndRidesASave`.
    /// The live preview draws it, but nothing in the editor
    /// can select, delete, retime or edit it, so a caption Save never sends
    /// `text_elements` (the narrated editor has no text lane: that Save 422s).
    func testReadOnlyTitleShowsInThePreviewButNeverReachesASave() async throws {
        let (session, fake, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: false, title: true)
        defer { _ = uploads }
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: false, mix: false, captionCues: true),
            revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        let title = try XCTUnwrap(recipe.textLayers.first { $0.id == "narrated-title" })
        XCTAssertEqual(title.runs.map(\.text), ["Cacio e pepe in", "10 minutes"])
        XCTAssertEqual(title.runs.first?.fontAssetID, "font-PlayfairDisplay-Bold.ttf")
        XCTAssertEqual(title.anchorY, 288, accuracy: 0.001)
        XCTAssertEqual(recipe.textLayers.filter { $0.runs.contains { $0.text.contains("First we pack") } }.count, 1,
                       "the caption still shows once")

        XCTAssertFalse(session.timelineItems.contains { $0.id == "narrated-title" }, "nothing to select or drag")
        XCTAssertTrue(session.document.textBlocks.isEmpty, "not in the Text list")
        XCTAssertFalse(session.textDeletion(id: "narrated-title").isAllowed)
        XCTAssertFalse(session.deleteText(id: "narrated-title"))
        session.setTextAlignment(id: "narrated-title", alignment: "left")
        XCTAssertFalse(session.hasUnsavedChanges)

        let cue = try XCTUnwrap(session.document.captionCues.first)
        session.updateCaptionCue(id: cue.id, text: "First we pack light")
        XCTAssertTrue(session.hasUnsavedChanges)
        await session.save()

        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertNil(request.textElements, "the title must never ride a narrated Save")
        XCTAssertEqual(request.captionCues?.count, 1)
    }

    /// KRI-465: the same title without `read_only`, on a variant whose
    /// `text_elements` capability is on. It goes through the ordinary text
    /// paths: timeline item, Text list, edits and deletion all ride a Save.
    func testEditableVoiceoverTitleIsOnTheTimelineAndRidesASave() async throws {
        let (session, fake, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: false, title: true, titleEditable: true)
        defer { _ = uploads }
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false, captionCues: false),
            revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        let title = try XCTUnwrap(recipe.textLayers.first { $0.id == "narrated-title" })
        XCTAssertEqual(title.runs.map(\.text), ["Cacio e pepe in", "10 minutes"])
        XCTAssertEqual(title.runs.first?.fontAssetID, "font-PlayfairDisplay-Bold.ttf")
        XCTAssertEqual(title.anchorY, 288, accuracy: 0.001)
        XCTAssertEqual(recipe.textLayers.filter { $0.runs.contains { $0.text.contains("First we pack") } }.count, 1,
                       "the caption still shows once")

        let item = try XCTUnwrap(session.timelineItems.first { $0.id == "narrated-title" }, "selectable and draggable")
        XCTAssertEqual(item.kind, .text)
        let block = try XCTUnwrap(session.document.textBlocks.first { $0.id == "narrated-title" }, "listed in the Text tab")
        XCTAssertEqual(block.kindLabel, "Title")
        XCTAssertEqual(block.text, "Cacio e pepe in 10 minutes")
        XCTAssertTrue(session.textDeletion(id: "narrated-title").isAllowed)
        XCTAssertFalse(session.hasUnsavedChanges)

        session.updateTextContent(id: "narrated-title", content: "Cacio e pepe, fast")
        session.updateTextTiming(id: "narrated-title", startS: 0.2, endS: 1.9)
        session.setTextPosition(id: "narrated-title", x: 0.4, y: 0.3)
        XCTAssertTrue(session.hasUnsavedChanges)
        func displayedTitle() -> String? {
            session.displayedSourcePreviewRecipe?.textLayers.first { $0.id == "narrated-title" }?.runs.map(\.text).joined(separator: " ")
        }
        for _ in 0..<200 where displayedTitle() != "Cacio e pepe, fast" { try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertEqual(displayedTitle(), "Cacio e pepe, fast", "the preview follows the edit")
        await session.save()

        let request = try XCTUnwrap(fake.lastRequest)
        let rows = try XCTUnwrap(request.textElements, "a title edit rides the text section")
        let sent = try XCTUnwrap(rows.compactMap(\.objectValue).first { $0["id"] == .string("narrated-title") })
        XCTAssertEqual(sent["text"], .string("Cacio e pepe, fast"))
        XCTAssertEqual(sent["start_s"], .number(0.2))
        XCTAssertEqual(sent["end_s"], .number(1.9))
        XCTAssertEqual(sent["x_frac"], .number(0.4))
        XCTAssertEqual(sent["y_frac"], .number(0.3))
        XCTAssertEqual(sent["position"], .string("custom"))
        XCTAssertNil(sent["source_params"]?.objectValue?["read_only"])
        XCTAssertTrue(rows.compactMap(\.objectValue).contains { $0["id"] == .string("mirror-0") },
                      "the caption mirror row rides along: text_elements is a full replacement")
        XCTAssertNil(request.deletions)
    }

    /// KRI-465: deleting the editable title drops it from the preview and the
    /// Save carries the `text`/`narrated-title` deletion beside the section.
    func testEditableVoiceoverTitleDeletionRidesASave() async throws {
        let (session, fake, uploads) = try await Self.phoneVoiceoverSession(mediaOpen: false, title: true, titleEditable: true)
        defer { _ = uploads }
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false, captionCues: false),
            revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertTrue(try XCTUnwrap(session.displayedSourcePreviewRecipe).textLayers.contains { $0.id == "narrated-title" })

        XCTAssertTrue(session.deleteText(id: "narrated-title"))
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertFalse(session.timelineItems.contains { $0.id == "narrated-title" })
        XCTAssertFalse(session.document.textBlocks.contains { $0.id == "narrated-title" })
        func titleDrawn() -> Bool { session.displayedSourcePreviewRecipe?.textLayers.contains { $0.id == "narrated-title" } ?? true }
        for _ in 0..<200 where titleDrawn() { try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertNotNil(session.displayedSourcePreviewRecipe)
        XCTAssertFalse(titleDrawn(), "the title layer is gone")
        await session.save()

        let request = try XCTUnwrap(fake.lastRequest)
        XCTAssertEqual(request.deletions, [EditorDeletion(kind: "text", id: "narrated-title")])
        let rows = try XCTUnwrap(request.textElements).compactMap(\.objectValue)
        XCTAssertFalse(rows.contains { $0["id"] == .string("narrated-title") })
        XCTAssertTrue(rows.contains { $0["id"] == .string("mirror-0") })
    }

    private static func phoneVoiceoverSession(mediaOpen: Bool, savedPhoto: Bool = false, title: Bool = false, titleEditable: Bool = false) async throws -> (NativeEditorSession, EditorCommitSpy, BackgroundUploadCoordinator) {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        let montage = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let input = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try FileManager.default.copyItem(at: montage, to: input)
        defer { try? FileManager.default.removeItem(at: input) }
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: input)
        try SourceAssetStore(project: project).bind(mediaID: "source", original: asset)
        let descriptor = OriginalMediaDescriptor(sha256: try XCTUnwrap(asset.fingerprint).hex, byteCount: try XCTUnwrap(asset.fingerprint).byteCount,
            durationS: 4.6, width: 1316, height: 740, orientationDegrees: 0, hasAudio: false)

        // Seed the preview cache the editor's resolver reads, so both visuals
        // resolve without a download.
        let cache = NativePreviewAssetCache(project: project, jobID: jobID)
        let photoURL = try XCTUnwrap(URL(string: "https://storage.example/photo.png"))
        let videoURL = try XCTUnwrap(URL(string: "https://storage.example/clip.mp4"))
        let photoFile = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).png")
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 96, height: 160)).image { context in
            UIColor.orange.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
        }.pngData()).write(to: photoFile)
        var photo = try await NativeDownloadedMedia.importAsset(from: photoFile, sourceURL: photoURL,
            response: URLResponse(url: photoURL, mimeType: "image/png", expectedContentLength: -1, textEncodingName: nil), project: cache.project)
        photo.naturalSize = MediaSize(width: 96, height: 160)
        try cache.store(photo, for: "media:generation-1:\(voiceoverPhotoID)")
        let videoFile = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try FileManager.default.copyItem(at: montage, to: videoFile)
        var video = try await NativeDownloadedMedia.importAsset(from: videoFile, sourceURL: videoURL,
            response: URLResponse(url: videoURL, mimeType: "video/mp4", expectedContentLength: -1, textEncodingName: nil), project: cache.project)
        video.naturalSize = MediaSize(width: 1316, height: 740)
        video.duration = 4.6
        try cache.store(video, for: "media:generation-1:\(voiceoverVideoID)")

        var capabilities: [String: JSONValue] = ["timeline": .bool(false), "text_elements": .bool(title && titleEditable), "mix": .bool(false),
            "overlays": .bool(true), "sfx": .bool(true), "visual_blocks": .bool(mediaOpen)]
        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"),
            "render_generation_id": .string("generation-1"),
            "render_status": .string("ready"),
            "render_destination": .string("device"),
            "resolved_archetype": .string("narrated"),
            "output_url": .string("file:///tmp/kria-editor-test.mp4"),
        ]
        if mediaOpen {
            capabilities["phone_editor_media"] = .object(["enabled": .bool(true), "source_registration": .bool(true),
                "visual_kinds": .array([.string("image"), .string("video")])])
            capabilities["visual_block_kinds"] = .array([.string("media")])
            variant["editor_revision_number"] = .number(1)
        }
        variant["editor_capabilities"] = .object(capabilities)
        if title {
            // What the status route sends a protocol-4 build: the read-only
            // title first, then the caption mirror of each cue.
            capabilities["caption_cues"] = .object(["editable": .bool(true), "reason": .null])
            capabilities["caption_meta"] = .object(["editable": .bool(true), "reason": .null])
            variant["editor_capabilities"] = .object(capabilities)
            variant["caption_cues"] = .array([.object([
                "text": .string("First we pack"), "start_s": .number(0), "end_s": .number(2),
            ])])
            variant["text_elements"] = .array([
                .object((titleEditable
                    ? NativeEditorRenderCompilerTests.editableTitle("Cacio e pepe in 10 minutes")
                    : NativeEditorRenderCompilerTests.readOnlyTitle("Cacio e pepe in 10 minutes")).raw),
                .object(["id": .string("mirror-0"), "text": .string("First we pack"), "start_s": .number(0), "end_s": .number(2),
                         "role": .string("generative_sequence"), "position": .string("bottom"),
                         "source_params": .object(["source": .string("caption_cue"), "key": .string("0")])]),
            ])
        }
        var nativeAssets: [NativeEditorAsset] = []
        if savedPhoto {
            variant["visual_blocks"] = .array([.object([
                "id": .string("photo-layer"), "kind": .string("media"), "asset_id": .string(voiceoverPhotoID),
                "src_gcs_path": .string(voiceoverPhotoPath), "media_kind": .string("image"), "start_s": .number(0.2),
                "end_s": .number(1.2), "display_mode": .string("overlay"), "scale": .number(0.4), "x_frac": .number(0.5),
                "y_frac": .number(0.5), "z": .number(1),
            ])])
            nativeAssets = [NativeEditorAsset(id: "photo-layer:photo-layer", kind: "visual_block", mediaID: voiceoverPhotoID,
                                              sourceURL: photoURL, preserveAlpha: nil)]
        }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [.init(clipIndex: 0, nativeSource: .init(mediaID: "source",
            sourceURL: nil, original: descriptor, localRequired: true))], baseGeneration: "generation-1", nativeAssets: nativeAssets)
        fake.deviceRenderResponse = DeviceRenderStatusResponse(
            phase: "published", request: deviceRenderRequest(jobID: jobID, revision: 1, digest: "a"), publishedGeneration: "generation-1"
        )
        fake.visualPool = [
            CreationVisual(id: voiceoverPhotoID, kind: "image", status: "ready", sourceFilename: "photo.png", displayURL: nil,
                previewURL: nil, retryable: nil, gcsPath: voiceoverPhotoPath, sourceURL: photoURL),
            CreationVisual(id: voiceoverVideoID, kind: "video", status: "ready", sourceFilename: "clip.mp4", displayURL: nil,
                previewURL: nil, retryable: nil, gcsPath: voiceoverVideoPath, sourceURL: videoURL, durationS: 4.6),
        ]
        func admitted(_ id: String, index: Int, source: [String: JSONValue]) -> EditorSourceRegistrationResponse {
            EditorSourceRegistrationResponse(importID: UUID(), status: "ready", sourceID: id, sourceIndex: index,
                source: source, error: nil, reasonCode: nil, retryable: false)
        }
        fake.registerEditorSourceResponses = [
            voiceoverPhotoID: admitted(voiceoverPhotoID, index: 1, source: ["gcs_path": .string(voiceoverPhotoPath), "kind": .string("image")]),
            voiceoverVideoID: admitted(voiceoverVideoID, index: 2, source: ["gcs_path": .string(voiceoverVideoPath), "kind": .string("video"),
                "duration_s": .number(4.6)]),
        ]
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let uploads = BackgroundUploadCoordinator(api: fake, defaultsKey: "voiceover-media-\(UUID().uuidString)", sessionConfiguration: .ephemeral)
        session.useMediaUploads(uploads)
        return (session, fake, uploads)
    }

    /// KRI-467: a phone Talking edit's hook title is an ordinary text row. It
    /// shows on the timeline and in the Text list, every edit previews and
    /// rides the Save as `text_elements` (caption mirrors untouched), and it
    /// can be deleted.
    func testPhoneTalkingTitleIsAnEditableTextRowThatReachesTheSave() async throws {
        let (session, fake) = try await Self.phoneTalkingSession(lanesEditable: true, title: true)
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "generation-2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false, captionCues: false),
            revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        XCTAssertTrue(session.timelineItems.contains { $0.id == "opening-title" }, "on the timeline")
        let block = try XCTUnwrap(session.document.textBlocks.first)
        XCTAssertEqual(block.id, "opening-title")
        XCTAssertEqual(block.kind, .title)
        XCTAssertEqual(session.document.textBlocks.count, 1, "the caption mirror is not a text block")
        XCTAssertTrue(session.textDeletion(id: "opening-title").isAllowed)

        let recipe = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        let title = try XCTUnwrap(recipe.textLayers.first { $0.runs.contains { $0.text.contains("sourdough") } })
        XCTAssertEqual(title.runs.first?.fontAssetID, "font-PlayfairDisplay-Bold.ttf")

        session.beginTransaction()
        session.updateTextContent(id: "opening-title", content: "Stop making these 3 mistakes")
        session.updateTextTiming(id: "opening-title", startS: 0, endS: 0.6)
        session.setTextPosition(id: "opening-title", x: 0.5, y: 0.5)
        session.endTransaction()
        XCTAssertTrue(session.hasUnsavedChanges)
        let edited = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertTrue(edited.textLayers.contains { $0.runs.contains { $0.text.contains("mistakes") } }, "previews live")
        await session.save()

        let request = try XCTUnwrap(fake.lastRequest)
        let rows = try XCTUnwrap(request.textElements).compactMap(\.objectValue)
        let saved = try XCTUnwrap(rows.first { $0["id"]?.stringValue == "opening-title" })
        XCTAssertEqual(saved["text"]?.stringValue, "Stop making these 3 mistakes")
        XCTAssertEqual(saved["end_s"]?.numberValue ?? -1, 0.6, accuracy: 0.001)
        XCTAssertEqual(saved["y_frac"]?.numberValue ?? -1, 0.5, accuracy: 0.001)
        XCTAssertTrue(rows.contains { $0["id"]?.stringValue == "caption-mirror-0" }, "mirrors ride along untouched")
        XCTAssertNil(request.captionCues)

        XCTAssertTrue(session.deleteText(id: "opening-title"))
        XCTAssertTrue(session.document.textBlocks.isEmpty)
        XCTAssertFalse(session.timelineItems.contains { $0.id == "opening-title" })
    }

    private static func phoneTalkingSession(lanesEditable: Bool, card: Bool = false, bindOriginal: Bool = true, title: Bool = false) async throws -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID(), jobID = UUID()
        let project = BackgroundUploadCoordinator.projectDirectory(threadID)
        let input = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mp4")
        try FileManager.default.copyItem(at: XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4")), to: input)
        defer { try? FileManager.default.removeItem(at: input) }
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: input)
        if bindOriginal { try SourceAssetStore(project: project).bind(mediaID: "source", original: asset) }
        let descriptor = OriginalMediaDescriptor(sha256: try XCTUnwrap(asset.fingerprint).hex, byteCount: try XCTUnwrap(asset.fingerprint).byteCount,
            durationS: 1, width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true)
        var nativeAssets: [NativeEditorAsset] = []
        if card {
            // Seed the preview cache the editor's resolver reads, so the card
            // resolves without a download.
            let cache = NativePreviewAssetCache(project: project, jobID: jobID)
            let remote = try XCTUnwrap(URL(string: "https://storage.example/card.png"))
            let download = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).png")
            try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 96, height: 160)).image { context in
                UIColor.orange.setFill()
                context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            }.pngData()).write(to: download)
            var image = try await NativeDownloadedMedia.importAsset(from: download, sourceURL: remote,
                response: URLResponse(url: remote, mimeType: "image/png", expectedContentLength: -1, textEncodingName: nil), project: cache.project)
            image.naturalSize = MediaSize(width: 96, height: 160)
            try cache.store(image, for: "media:generation-1:card-media")
            nativeAssets = [NativeEditorAsset(id: "card", kind: "media_overlay", mediaID: "card-media", sourceURL: remote, preserveAlpha: nil)]
        }

        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"),
            "render_generation_id": .string("generation-1"),
            "render_status": .string("ready"),
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "editor_capabilities": .object(["timeline": .bool(false), "text_elements": .bool(title), "mix": .bool(false),
                "overlays": .bool(lanesEditable), "sfx": .bool(lanesEditable)]),
            "caption_cues": .array([.object(["text": .string("Number three"), "start_s": .number(0.2), "end_s": .number(0.9)])]),
        ]
        if title {
            // As the status route serves it: the title row, then the caption mirrors.
            variant["text_elements"] = .array([
                .object(["id": .string("opening-title"), "text": .string("3 sourdough mistakes"), "start_s": .number(0),
                    "end_s": .number(0.8), "role": .string("generative_intro"), "position": .string("custom"),
                    "x_frac": .number(0.5), "y_frac": .number(0.15), "size_px": .number(120), "size_class": .string("large"),
                    "font_family": .string("Playfair Display"), "alignment": .string("center"), "effect": .string("fade-in"),
                    "source_params": .object(["source": .string("opening_title")])]),
                .object(["id": .string("caption-mirror-0"), "text": .string("Number three"), "start_s": .number(0.2),
                    "end_s": .number(0.9), "role": .string("generative_sequence"), "position": .string("bottom"),
                    "source_params": .object(["source": .string("caption_cue"), "key": .string("0")])]),
            ])
        }
        if card {
            variant["media_overlays"] = .array([.object(["id": .string("card"), "kind": .string("image"), "start_s": .number(0.2),
                "end_s": .number(0.8), "x_frac": .number(0.5), "y_frac": .number(0.3), "scale": .number(0.35), "z": .number(1)])])
        }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
                snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        fake.sourcePoolResult = NativeEditorSourcePool(clips: [.init(clipIndex: 0, nativeSource: .init(mediaID: "source",
            sourceURL: nil, original: descriptor, localRequired: true))], baseGeneration: "generation-1", nativeAssets: nativeAssets)
        fake.deviceRenderResponse = DeviceRenderStatusResponse(
            phase: "published", request: deviceRenderRequest(jobID: jobID, revision: 1, digest: "a"), publishedGeneration: "generation-1"
        )
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    func testDeviceTimelineDurationUsesShorterOriginalAndKeepsEOFMargin() throws {
        XCTAssertEqual(try XCTUnwrap(NativeEditorSession.deviceTimelineDuration(proxyDuration: 2.2, localDuration: 2.0, minimum: 0.1)),
                       1.95, accuracy: 0.0001)
        XCTAssertNil(NativeEditorSession.deviceTimelineDuration(proxyDuration: 0.14, localDuration: 0.14, minimum: 0.1))
        XCTAssertNil(NativeEditorSession.deviceTimelineDuration(proxyDuration: nil, localDuration: nil, minimum: 0.1))
    }

    func testAdmittedTimelinePhotoAppendsThreeSecondPlacementAndUndoRedo() async throws {
        let threadID = UUID()
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_revision_number"] = .number(7)
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "phone_editor_media": .object([
                "enabled": .bool(true), "source_registration": .bool(true),
                "visual_kinds": .array([.string("image")]),
            ]),
        ])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant",
            draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1",
            snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: authoritative)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: "generation-1", guidedRevisionNumber: 7, sourceKind: .visual)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: nil)
        let uploads = BackgroundUploadCoordinator(api: fake, defaultsKey: "timeline-placement-\(UUID().uuidString)", sessionConfiguration: .ephemeral)
        uploads.beginEditorPlacement(placement)
        session.useMediaUploads(uploads)
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "photo",
            sourceIndex: 4, source: nil, error: nil, reasonCode: nil, retryable: false)
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: response)
        let inserted = try XCTUnwrap(session.document.clips.last)
        XCTAssertEqual(session.document.clips.count, before + 1)
        XCTAssertEqual(inserted.durationS, 3)
        XCTAssertEqual(inserted.raw["editor_source_placement_id"], .string(placement.id.uuidString))
        session.undo(); XCTAssertEqual(session.document.clips.count, before)
        session.redo(); XCTAssertEqual(session.document.clips.count, before + 1)
    }

    func testAdmittedFootageUsesShorterOriginalWithEOFMarginAndDeduplicatesReadyCallback() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 1)
        uploads.beginEditorPlacement(placement)
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(1.2)], error: nil, reasonCode: nil, retryable: false)
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: response)
        XCTAssertEqual(session.document.clips.count, before + 1)
        XCTAssertEqual(try XCTUnwrap(session.document.clips.last?.durationS), 0.95, accuracy: 0.0001)
        // The acknowledgement removes the ledger intent; a duplicated ready
        // callback must not resurrect a user-visible slot.
        try await session.placeEditorSource(placement, response: response)
        XCTAssertEqual(session.document.clips.count, before + 1)
    }

    func testCanceledOrStalePlacementNeverAppends() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(2)], error: nil, reasonCode: nil, retryable: false)
        let canceled = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(canceled)
        await uploads.discardEditorPlacement(canceled.id)
        let before = session.document.clips.count
        try await session.placeEditorSource(canceled, response: response)
        XCTAssertEqual(session.document.clips.count, before)
        var staleTarget = target
        staleTarget = EditorSourceRegistrationTarget(itemID: staleTarget.itemID, variantID: staleTarget.variantID,
            clientImportID: UUID(), baseGeneration: "stale", guidedRevisionNumber: staleTarget.guidedRevisionNumber, sourceKind: .footage)
        let stale = PendingEditorSourcePlacement(id: UUID(), target: staleTarget, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(stale)
        try await session.placeEditorSource(stale, response: response)
        XCTAssertEqual(session.document.clips.count, before)
    }

    // KRI-166: an import left "preparing" with no live waiter (the app was
    // suspended/relaunched mid-import) must be picked up as soon as the editor
    // re-arms imports — the server finished long ago, only the polling died.
    func testResumeEditorImportsRearmsAnOrphanedPreparingImportAndPlacesIt() async throws {
        let (session, uploads, target, fake) = await Self.devicePlacementSessionWithSpy()
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(placement)
        XCTAssertEqual(uploads.editorPlacements(itemID: target.itemID, variantID: target.variantID, baseGeneration: nil).first?.status, "preparing")
        fake.editorSourceResponse = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready",
            sourceID: "proxy", sourceIndex: 9, source: ["duration_s": .number(2)], error: nil, reasonCode: nil, retryable: false)
        let before = session.document.clips.count

        await session.resumeEditorImports()
        for _ in 0..<100 where session.document.clips.count == before {
            try await Task.sleep(for: .milliseconds(50))
        }

        XCTAssertEqual(session.document.clips.count, before + 1)
    }

    func testLeavingEditorKeepsReadyImportPendingWithoutAppendingToHiddenDocument() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 2)
        uploads.beginEditorPlacement(placement)
        session.suspendEditorImports()
        let before = session.document.clips.count
        try await session.placeEditorSource(placement, response: .init(importID: target.clientImportID, status: "ready",
            sourceID: "proxy", sourceIndex: 9, source: ["duration_s": .number(2)], error: nil, reasonCode: nil, retryable: false))
        XCTAssertEqual(session.document.clips.count, before)
        XCTAssertTrue(uploads.containsEditorPlacement(placement.id))
    }

    func testReadyImportRechecksCapacityAfterOtherPlacementsFillTimeline() async throws {
        let (session, uploads, target) = await Self.devicePlacementSession()
        let response = EditorSourceRegistrationResponse(importID: target.clientImportID, status: "ready", sourceID: "proxy",
            sourceIndex: 9, source: ["duration_s": .number(1)], error: nil, reasonCode: nil, retryable: false)
        while session.document.clips.count < NativeEditorSession.maxTimelineClips {
            let nextTarget = EditorSourceRegistrationTarget(itemID: target.itemID, variantID: target.variantID,
                clientImportID: UUID(), baseGeneration: target.baseGeneration, guidedRevisionNumber: 7, sourceKind: .footage)
            let placement = PendingEditorSourcePlacement(id: UUID(), target: nextTarget, lane: .timeline, visual: nil, localDurationS: 1)
            uploads.beginEditorPlacement(placement)
            try await session.placeEditorSource(placement, response: response)
        }
        let late = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 1)
        uploads.beginEditorPlacement(late)
        do {
            try await session.placeEditorSource(late, response: response)
            XCTFail("A ready import must not exceed the clip limit")
        } catch {}
        XCTAssertEqual(session.document.clips.count, NativeEditorSession.maxTimelineClips)
        XCTAssertTrue(uploads.containsEditorPlacement(late.id))
    }

    func testFootageEditsPersistAndUndoWithoutChangingTimelineWindows() throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let original = session.document
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let windows = session.timelineClips.map { [$0.start, $0.end] }
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        let restored = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(restored.clips.first?.raw["playback_rate"], .number(0.5))
        XCTAssertEqual(restored.clips.first?.raw["source_crop"]?.objectValue?["y"], .number(0.2))
        XCTAssertEqual(session.timelineClips.map { [$0.start, $0.end] }, windows)
        session.undo()
        XCTAssertNil(session.footageCrop(for: selection))
        XCTAssertEqual(session.footagePlaybackRate(for: selection), 0.5)
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    /// The server closes `clips.source_crop/playback_rate/looks` on device
    /// variants because the phone compiler rejects them. A value saved before
    /// that clamp must still clear, as an explicit null the guided writer honors.
    func testDeviceVariantRefusesNewCropSpeedAndLookButClearsSavedOnes() async throws {
        let (session, fake) = await Self.footageSession(destination: "device", operationsEditable: false, slot: [
            "source_crop": .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.5), "height": .number(0.5)]),
            "playback_rate": .number(2),
            "look_preset": .string("golden_hour"),
        ])
        XCTAssertTrue(session.rendersOnDevice)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let slotID = try XCTUnwrap(session.document.clips.first?.id)
        let original = session.document

        session.setFootageCrop(selection, crop: .init(x: 0.2, y: 0.2, width: 0.6, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setClipLookPreset(clipID: slotID, preset: "olive_film")
        session.setClipLookAdjustments(clipID: slotID, adjustments: ["exposure": .number(0.2)])
        XCTAssertEqual(session.document, original, "A device variant can't take a new crop, speed or look")
        XCTAssertFalse(session.hasUnsavedChanges)

        session.setFootageCrop(selection, crop: nil)
        session.setFootagePlaybackRate(selection, rate: 1)
        session.setClipLookPreset(clipID: slotID, preset: "none")
        let slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertEqual(slot.raw["source_crop"], .null)
        XCTAssertEqual(slot.raw["playback_rate"], .null)
        XCTAssertEqual(slot.lookPreset, "none")
        XCTAssertNil(session.footageCrop(for: selection))
        XCTAssertEqual(session.footagePlaybackRate(for: selection), 1)
        XCTAssertTrue(session.hasUnsavedChanges)

        await session.save()
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)
        XCTAssertEqual(sent["source_crop"], .null, "An omitted key would keep the stored crop")
        XCTAssertEqual(sent["playback_rate"], .null)
    }

    /// KRI-132 journey fix: a device-rendered edit's Add-clip control is silently
    /// disabled (`NativeEditorTimelineView.canAddClip`) -- the session exposes why,
    /// mirroring `visualImportUnavailableMessage`'s `rendersOnDevice` case.
    func testAddClipUnavailableMessageExplainsDeviceRenderedEditsOnly() async throws {
        let (device, _) = await Self.footageSession(destination: "device", operationsEditable: false)
        XCTAssertNotNil(device.addClipUnavailableMessage)
        let (cloud, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertNil(cloud.addClipUnavailableMessage)
    }

    // KRI-166: the quick-add menu's Video row explains why it's disabled, and
    // the reason agrees with canAddTimelineMedia in both directions.
    func testAddClipUnavailableReasonAgreesWithCanAddTimelineMedia() async throws {
        let (device, _) = await Self.footageSession(destination: "device", operationsEditable: false)
        XCTAssertFalse(device.canAddTimelineMedia)
        XCTAssertNotNil(device.addClipUnavailableReason)
        let (cloud, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertEqual(cloud.canAddTimelineMedia, cloud.addClipUnavailableReason == nil)
    }

    func testDeviceOnlyModeMakesLegacyCloudEditorPlaybackOnly() async throws {
        let (session, fake) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        fake.creationMode = .deviceOnly
        await session.load(api: fake, threadID: UUID())

        let message = try XCTUnwrap(session.cloudEditorUnavailableMessage)
        XCTAssertTrue(message.localizedCaseInsensitiveContains("still play"))
        XCTAssertFalse(session.canEditTimeline)
        XCTAssertFalse(session.canEditText)
        XCTAssertFalse(session.canAddTimelineMedia)
        XCTAssertEqual(session.addClipUnavailableReason, message)
    }

    // KRI-166: the cap is 50 (matching server + creation), not the old 20 — an
    // edit with 20+ clips must still be able to add another.
    func testClipCapIsFiftyAndAddClipStaysAllowedPastTwenty() async throws {
        XCTAssertEqual(NativeEditorSession.maxTimelineClips, 50)
        let (cloud, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertLessThan(cloud.draft.clips.count, 20)
        XCTAssertTrue(cloud.canAddTimelineMedia)
        while cloud.draft.clips.count < 25 {
            let slot = try XCTUnwrap(cloud.document.clips.first)
            cloud.transactDocument(section: .timeline) { $0.clips.append(slot) }
        }
        XCTAssertGreaterThanOrEqual(cloud.draft.clips.count, 25)
        XCTAssertTrue(cloud.canAddTimelineMedia, "25 clips is under the 50 cap")
        XCTAssertNil(cloud.addClipUnavailableReason)
    }

    func testCloudVariantKeepsCropSpeedAndLookEditable() async throws {
        let (session, _) = await Self.footageSession(destination: "cloud", operationsEditable: true)
        XCTAssertFalse(session.rendersOnDevice)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let slotID = try XCTUnwrap(session.document.clips.first?.id)

        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        session.setClipLookPreset(clipID: slotID, preset: "olive_film")
        var slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertEqual(slot.raw["source_crop"]?.objectValue?["y"], .number(0.2))
        XCTAssertEqual(slot.raw["playback_rate"], .number(0.5))
        XCTAssertEqual(slot.lookPreset, "olive_film")

        // The saved slot never had these keys, so a reset drops them again.
        session.setFootageCrop(selection, crop: nil)
        session.setFootagePlaybackRate(selection, rate: 1)
        slot = try XCTUnwrap(session.document.clips.first)
        XCTAssertNil(slot.raw["source_crop"])
        XCTAssertNil(slot.raw["playback_rate"])
    }

    func testClosedClipCropCapabilityIsHonoredOnCloudVariants() async throws {
        let (session, _) = await Self.footageSession(destination: "cloud", operationsEditable: false)
        let clip = try XCTUnwrap(session.timelineClips.first)
        let selection = EditorSelection(kind: .clip, id: clip.id.uuidString)
        let original = session.document

        session.setFootageCrop(selection, crop: .init(x: 0.1, y: 0.2, width: 0.7, height: 0.6))
        session.setFootagePlaybackRate(selection, rate: 0.5)
        XCTAssertEqual(session.document, original, "`clips.*` wins over the broader `timeline` capability")
    }

    func testRenderedRebaseRefreshesGenerationSourcesButLocalEditsDoNot() async {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        XCTAssertEqual(fake.sourcePoolCallCount, 1)
        let clip = try! XCTUnwrap(session.timelineClips.first)
        session.setClipTiming(clipID: clip.id, durationS: 1.5)
        session.undo()
        XCTAssertEqual(fake.sourcePoolCallCount, 1, "Local edit and undo retain immutable inputs")

        let refresh = expectation(description: "Resolve sources for rendered generation")
        fake.sourcePoolExpectation = refresh
        let sourcePlayer = try! XCTUnwrap(session.player)
        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 2, generation: "g2")))
        XCTAssertEqual(session.sourcePreviewState, .preparing, "Old preview must not remain ready")
        let finishedURL = try! XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        session.installFinishedRenderPlayer(url: finishedURL)
        XCTAssertTrue(session.canDisplayCurrentPlayer, "The completed render stays visible while editable sources rebuild")
        XCTAssertFalse(session.player === sourcePlayer, "Authoritative playback replaces the stale source composition")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
        await fulfillment(of: [refresh], timeout: 3)
        XCTAssertEqual(fake.sourcePoolCallCount, 2)
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 2, generation: "g2")))
        XCTAssertEqual(fake.sourcePoolCallCount, 2, "Repeated authority keeps the same generation inputs")
    }

    func testLegacyConversationRefreshDoesNotUseRuntimeTwoDraftOrEraseLocalEdits() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(id: UUID(), title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let original = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.draftCallCount, 0)
        XCTAssertEqual(session.document, original)
        XCTAssertEqual(session.saveState, .idle)
        let clip = try XCTUnwrap(session.timelineClips.first)
        session.setClipTiming(clipID: clip.id, durationS: 1.5)
        let local = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, local)
        XCTAssertTrue(session.hasUnsavedChanges)
        fake.authoritativeVariant = Self.variant(duration: 2, generation: "g2")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, local)
        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertEqual(fake.draftCallCount, 0)
    }

    func testLegacyRefreshRejectsMissingOrUnknownSelectionWithoutLoadedTargetFallback() async throws {
        let jobID = UUID()
        let threadID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(id: threadID, title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let original = session.document
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        for state in ["{}", #"{"selected_variant_id":"unknown"}"#] {
            fake.refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
            {
              "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
              "revision":8,"runtime_version":1,
              "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
              "state":\#(state),
              "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[
                {"variant_id":"initial","render_status":"ready"},
                {"variant_id":"other","render_status":"ready"}
              ]},"updated_at":"2026-09-09T08:00:00Z"
            }
            """#.utf8))
            fake.lastVariantID = nil
            await session.synchronizePromptRevision()
            XCTAssertNil(fake.lastVariantID, "Invalid projection must not fetch the loaded variant")
            XCTAssertEqual(session.document, original)
            XCTAssertFalse(session.hasUnsavedChanges)
            XCTAssertEqual(fake.draftCallCount, 0)
            guard case .refreshFailed = session.saveState else {
                return XCTFail("Invalid selection must report a refresh failure")
            }
        }
    }

    func testLegacyVisualRemovalRefreshKeepsSelectedRenderingVariant() async throws {
        let jobID = UUID()
        let threadID = UUID()
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let projection = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
          "revision":8,"runtime_version":1,
          "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
          "state":{"selected_variant_id":"selected"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready_partial","variants":[
            {"variant_id":"other","render_status":"ready","output_url":"https://example.com/other.mp4"},
            {"variant_id":"selected","render_status":"rendering","render_generation_id":"g2"}
          ]},"updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        XCTAssertEqual(projection.summary.outputVariantID, "other", "Playback falls back while the selected edit renders")
        var variant = Self.variant(duration: 2, generation: "g1")
        variant["variant_id"] = .string("selected")
        // Stable source IDs let this assertion compare the complete clip lane.
        variant["user_timeline"] = .object(["slots": .array([.object([
            "slot_id": .string(UUID().uuidString), "clip_index": .number(0),
            "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4),
        ])])])
        variant["text_elements"] = .array([.object([
            "id": .string(UUID().uuidString), "text": .string("Keep this title"),
            "start_s": .number(0), "end_s": .number(2),
        ])])
        variant["audio_mix"] = .object(["original_level": .number(0.7)])
        variant["visual_blocks"] = .array([.object([
            "id": .string("uploaded-photo"), "kind": .string("media"),
            "start_s": .number(0), "end_s": .number(2),
            "src_gcs_path": .string("owned/photo.png"),
        ])])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "selected", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: variant, refreshedThread: projection)
        let project = ProjectSummary(id: threadID, title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "selected", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        let before = session.document
        XCTAssertEqual(before.visualBlocks.count, 1)
        variant["visual_blocks"] = .array([])
        variant["render_generation_id"] = .string("g2")
        variant["render_status"] = .string("rendering")
        fake.authoritativeVariant = variant
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.lastVariantID, "selected")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.document.visualBlocks.isEmpty)
        XCTAssertEqual(session.document.clips, before.clips)
        XCTAssertEqual(session.document.textElements, before.textElements)
        XCTAssertEqual(session.document.mix, before.mix)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertEqual(fake.draftCallCount, 0)

        let accepted = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, accepted)
        XCTAssertEqual(session.saveState, .idle)

        // A subsequent explicit selection is authoritative even if both cuts
        // happen to carry an identical editor document.
        fake.refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)","title":"Legacy","status":"active",
          "revision":9,"runtime_version":1,
          "active_job_id":"\#(jobID.uuidString)","active_plan_item_id":"item",
          "state":{"selected_variant_id":"other"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[
            {"variant_id":"other","render_status":"ready"},
            {"variant_id":"selected","render_status":"ready"}
          ]},"updated_at":"2026-09-09T08:00:01Z"
        }
        """#.utf8))
        await session.synchronizePromptRevision()
        XCTAssertEqual(fake.lastVariantID, "other")
    }

    func testLegacyRefreshAfterManualSaveAndConflictRebasePreservesNewLocalEdit() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "unused", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now), draftError: .conflict,
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"),
            commitResponse: EditorCommitResponse(ok: false, generation: "g2",
                sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false),
                revisionNumber: nil, revisionHash: nil, expectedDuration: nil))
        let project = ProjectSummary(id: UUID(), title: "Legacy", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", runtimeVersion: 1,
            activeJobID: jobID, activePlanItemID: "item")
        let session = NativeEditorSession(project: project)
        await session.load(project: project, api: fake)
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 1.5)
        await session.save()
        XCTAssertEqual(fake.commitCount, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
        let saved = Self.variant(duration: 1.5, generation: "g2")
        fake.authoritativeVariant = saved
        XCTAssertTrue(session.rebaseCleanDraft(from: saved))
        session.saveState = .saved
        session.setClipTiming(clipID: try XCTUnwrap(session.timelineClips.first?.id), durationS: 1.2)
        let afterSave = session.document
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, afterSave)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertNotEqual(session.saveState, .conflict)

        fake.authoritativeVariant = Self.variant(duration: 1.4, generation: "g3")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.saveState, .conflict)
        await session.rebaseAfterConflict()
        let rebased = session.document
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(rebased.revision.baseGeneration, "g3")
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, rebased)
        XCTAssertNotEqual(session.saveState, .conflict)
        XCTAssertEqual(fake.draftCallCount, 0)
    }

    func testPromptRefreshRecoveryClearsFailureForUnchangedDocument() async {
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let baseline = session.document
        fake.draftError = .requestFailed(status: 503)
        await session.synchronizePromptRevision()
        guard case .refreshFailed = session.saveState else { return XCTFail("Expected refresh failure") }
        // Repeated failures must retain the original state, not the last error.
        await session.synchronizePromptRevision()
        fake.draftError = nil
        await session.synchronizePromptRevision()
        XCTAssertEqual(session.document, baseline)
        XCTAssertEqual(session.saveState, .idle)
    }

    func testPromptRefreshRecoveryPreservesUnrelatedSaveAndRenderFailures() async {
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())
        let failures: [NativeEditorSaveState] = [.failed("Save failed"), .renderRetryNeeded("Render failed")]
        for failure in failures {
            session.saveState = failure
            fake.draftError = .requestFailed(status: 503)
            await session.synchronizePromptRevision()
            fake.draftError = nil
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.saveState, failure, "Restore the failure that preceded the refresh")

            session.saveState = .idle
            fake.draftError = .requestFailed(status: 503)
            await session.synchronizePromptRevision()
            session.saveState = failure
            fake.draftError = nil
            await session.synchronizePromptRevision()
            XCTAssertEqual(session.saveState, failure, "Keep a newer save/render failure")
        }
    }

    func testOrdinaryComposedPauseResumeDoesNotSeekOrRenderAnotherStill() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        let original = try XCTUnwrap(session.player?.currentItem)
        let item = AVPlayerItem(asset: original.asset)
        item.videoComposition = original.videoComposition
        let player = DelayedSeekPlayer(playerItem: item)
        session.player = player
        session.togglePlayback()
        XCTAssertEqual(player.targets.count, 1, "Initial scrub surface needs one handoff")
        player.completeSeek()
        await Task.yield()
        await Task.yield()
        for _ in 0..<3 {
            session.pausePlayback()
            XCTAssertFalse(session.isPlaying)
            XCTAssertNil(session.scrubPreviewFrame, "Pause retains the player surface")
            session.togglePlayback()
        }
        XCTAssertEqual(player.targets.count, 1, "Ordinary resume must not restart decoding with an exact seek")
        session.pausePlayback()
    }

    func testFixtureSourceFailureRetainsAndPlaysInitialFinishedRender() async throws {
        let renderURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: renderURL)
        let initialPlayer = try XCTUnwrap(session.player)

        await session.prepareFixtureSourcePreview(
            url: URL(fileURLWithPath: "/tmp/kria-invalid-source-fixture.mp4"),
            forceFailure: true
        )

        guard case .failed = session.sourcePreviewState else { return XCTFail("Fixture source failure must remain visible") }
        XCTAssertTrue(session.isShowingRenderedFallback)
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        XCTAssertTrue(session.player === initialPlayer, "The initial finished-render player remains available")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
    }

    func testDownloadUsesVisibleSourcePreviewInsteadOfOlderDeviceFile() async throws {
        let olderDeviceFile = URL(fileURLWithPath: "/tmp/kria-older-device-render.mp4")
        let session = NativeEditorSession(
            draft: NativeEditorUITestFixtures.sourceText,
            initialPlaybackURL: olderDeviceFile
        )
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))

        await session.prepareFixtureSourcePreview(url: sourceURL)

        XCTAssertTrue(session.hasSourcePreview)
        XCTAssertEqual(try session.videoDownloadRoute(deviceLocalFile: olderDeviceFile), .sourcePreview)
    }

    func testDownloadRejectsStaleFinishedRenderWhileCurrentPreviewPrepares() async throws {
        let olderRender = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(
            draft: NativeEditorUITestFixtures.sourceText,
            initialPlaybackURL: olderRender
        )

        let preparation = Task {
            await session.prepareFixtureSourcePreview(url: olderRender, delayedLoad: true)
        }
        await Task.yield()

        XCTAssertThrowsError(try session.videoDownloadRoute(deviceLocalFile: olderRender))
        await preparation.value
    }

    func testFirstSourcePreviewIncludesBrandOutroWithoutChangingEditableClips() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let originalClips = session.document.clips
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)

        XCTAssertTrue(session.hasSourcePreview)
        // KRI-166: the outro placeholder trusts a much smaller gap when this
        // is true, since the player is showing the always-branded preview.
        XCTAssertTrue(session.isPlayingBrandedSourcePreview)
        let item = try XCTUnwrap(session.player?.currentItem)
        let previewDuration = try await item.asset.load(.duration).seconds
        let outro = try await AVURLAsset(url: XCTUnwrap(KriaBranding.outroURL())).load(.duration).seconds
        XCTAssertEqual(previewDuration, session.duration + outro, accuracy: 0.01)
        XCTAssertEqual(session.playbackDuration, previewDuration, accuracy: 0.01)
        XCTAssertEqual(session.document.clips, originalClips)
        XCTAssertFalse(session.hasUnsavedChanges)

        // Scrubbing and resuming inside the outro must not clamp to the last
        // editable frame or restart playback at zero.
        let outroTime = session.duration + outro / 2
        session.seek(to: outroTime)
        XCTAssertEqual(session.currentTime, outroTime, accuracy: 0.001)
        XCTAssertEqual(session.timelineTime(for: 100, width: 100), previewDuration, accuracy: 0.01)
        // The compositor may still be finishing the initial frame on a busy
        // simulator. Wait for the requested outro frame, not a 2.5s render budget.
        let outroFrameReady = await waitUntil(timeout: .seconds(10)) {
            (session.scrubPreviewTime ?? 0) > session.duration
        }
        XCTAssertTrue(outroFrameReady, "The compositor must produce a frame inside the outro")
        XCTAssertGreaterThan(try XCTUnwrap(session.scrubPreviewTime), session.duration)
        session.togglePlayback()
        XCTAssertGreaterThan(session.currentTime, session.duration)
        session.pausePlayback()

        // Starting an authored text layer from the tail stays inside the edit.
        session.seek(to: outroTime)
        session.beginTextCreation()
        XCTAssertLessThanOrEqual(try XCTUnwrap(session.pendingText).endS, session.duration)
        session.cancelTextCreation()
    }

    func testIsPlayingBrandedSourcePreviewFalseBeforeAPreviewIsReady() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        XCTAssertFalse(session.isPlayingBrandedSourcePreview)
    }

    func testDisplayedSourcePreviewExportsAPlayableVideo() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)

        let exported = try await session.exportDisplayedSourcePreview()
        defer { try? FileManager.default.removeItem(at: exported.cleanupURL) }

        XCTAssertTrue(FileManager.default.fileExists(atPath: exported.fileURL.path))
        // This is the file the user saves to Photos, so it is a published
        // video and carries exactly the same outro as the first preview.
        // The editable duration stays separate from the branded playback.
        let outroURL = try XCTUnwrap(KriaBranding.outroURL())
        let outro = try await AVURLAsset(url: outroURL).load(.duration).seconds
        let duration = try await AVURLAsset(url: exported.fileURL).load(.duration).seconds
        XCTAssertEqual(duration, session.duration + outro, accuracy: 0.1)
        XCTAssertEqual(duration, session.playbackDuration, accuracy: 0.1)
    }

    func testProjectSessionUsesFreshPlaybackHandoffBeforeHydration() throws {
        let staleURL = URL(fileURLWithPath: "/tmp/kria-stale-project-render.mp4")
        let freshURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let project = ProjectSummary(
            id: UUID(), title: "Gallery edit", status: .ready, updatedAt: .now,
            posterURL: nil, outputURL: staleURL
        )

        let session = NativeEditorSession(project: project, initialPlaybackURL: freshURL)

        XCTAssertEqual(session.loadState, .idle)
        XCTAssertTrue(session.canDisplayCurrentPlayer, "The result screen's player is available before editor hydration")
        let asset = try XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(asset.url, freshURL)
    }

    /// KRI-91: opening a project (the shared chat-editor session's path) with
    /// no explicit playback URL falls back to `project.outputURL` — a
    /// possibly long-stale cached summary, not something anyone just
    /// fetched for this screen. That seed must not display until `load()`
    /// confirms it, unlike the explicit-URL case above.
    func testUncachedProjectOutputURLDoesNotDisplayBeforeHydration() throws {
        let cachedURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let project = ProjectSummary(
            id: UUID(), title: "Reopened project", status: .ready, updatedAt: .now,
            posterURL: nil, outputURL: cachedURL
        )

        let session = NativeEditorSession(project: project)

        XCTAssertEqual(session.loadState, .idle)
        XCTAssertNotNil(session.player, "The cached URL still seeds the player so playback can start the instant it's confirmed")
        XCTAssertFalse(session.canDisplayCurrentPlayer, "An unconfirmed cached seed must not display — it may already be stale")
    }

    // MARK: KRI-200 — a play tap must never be a silent no-op

    private func waitUntil(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(25))
        }
        return condition()
    }

    private static func failedStateMessage(_ session: NativeEditorSession) -> String? {
        if case .failed(let message) = session.sourcePreviewState { message } else { nil }
    }

    /// A finished render that cannot load used to leave a player that ignored `play()`: the icon flipped
    /// straight back and nothing said why. With nobody to refresh the link it must say so.
    func testUnplayableFinishedRenderSurfacesAMessageInsteadOfADeadPlayButton() async {
        let missing = URL(fileURLWithPath: "/tmp/kria-missing-\(UUID().uuidString).mp4")
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: missing)
        let settled = await waitUntil { Self.failedStateMessage(session) != nil }
        XCTAssertTrue(settled, "a failed item must surface, not sit silently paused")
        XCTAssertEqual(Self.failedStateMessage(session), NativeEditorSession.sourcePreviewMessage(for: NativeEditorPlaybackFailure.finishedItem))
    }

    /// The failed finished render gets exactly one fresh link, the failure is reported with its AVFoundation
    /// identity, and the recovered player plays.
    func testFailedFinishedRenderRefreshesItsLinkOnceReportsAndRecovers() async throws {
        let jobID = UUID()
        let good = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        var variant = Self.variant(duration: 2, generation: "g1")
        variant["output_url"] = .string("file:///tmp/kria-missing-\(UUID().uuidString).mp4")
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant)
        fake.playbackURLResult = good
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: UUID())

        let recovered = await waitUntil { (session.player?.currentItem?.asset as? AVURLAsset)?.url == good }
        XCTAssertTrue(recovered, "the refreshed link replaces the failed item")
        XCTAssertEqual(fake.playbackURLCallCount, 1, "one refresh, not a retry loop")
        let reported = await waitUntil { !fake.playbackFailureReports.isEmpty }
        XCTAssertTrue(reported)
        let report = try XCTUnwrap(fake.playbackFailureReports.first)
        XCTAssertEqual(report.playerKind, .finished)
        XCTAssertEqual(report.errorDomain, AVFoundationErrorDomain)
        XCTAssertEqual(fake.playbackFailureReports.count, 1, "one report per failed item")
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)
        session.pausePlayback()
    }

    /// The editable live composition failing must hand over to the finished render, and the tap that
    /// triggered it must not be lost.
    func testLiveItemFailureFallsBackToTheFinishedRenderAndKeepsThePlayTap() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText, initialPlaybackURL: url)
        let finished = try XCTUnwrap(session.player)
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let live = try XCTUnwrap(session.player)
        XCTAssertFalse(live === finished)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying)

        session.handlePlayerItemFailure(try XCTUnwrap(live.currentItem), error: NSError(domain: AVFoundationErrorDomain, code: -11800))

        XCTAssertEqual(Self.failedStateMessage(session), NativeEditorSession.sourcePreviewMessage(for: NativeEditorPlaybackFailure.liveItem))
        XCTAssertTrue(session.isShowingRenderedFallback)
        XCTAssertTrue(session.canDisplayCurrentPlayer)
        XCTAssertTrue(session.isPlaying, "the play tap carries over to the finished render")
        session.pausePlayback()
    }

    /// While the editable preview builds against a render that predates the document, there is nothing
    /// displayable. The tap is remembered and plays the moment a player can be shown.
    func testPlayTapDuringPreparationPlaysOnceThePreviewSettles() async throws {
        let session = try await Self.preparingSession()
        XCTAssertEqual(session.session.sourcePreviewState, .preparing)
        XCTAssertFalse(session.session.canDisplayCurrentPlayer)
        session.session.togglePlayback()
        XCTAssertFalse(session.session.isPlaying, "nothing to show yet")
        session.spy.resumeSourcePool()
        _ = await session.loading.value
        let playing = await waitUntil { session.session.isPlaying }
        XCTAssertTrue(playing, "the remembered tap starts playback once a player can be displayed")
        session.session.pausePlayback()
    }

    func testPauseCancelsARememberedPlayTap() async throws {
        let session = try await Self.preparingSession()
        session.session.togglePlayback()
        session.session.pausePlayback()
        session.spy.resumeSourcePool()
        _ = await session.loading.value
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertFalse(session.session.isPlaying)
    }

    private static func preparingSession() async throws -> (session: NativeEditorSession, spy: EditorCommitSpy, loading: Task<Void, Never>) {
        let jobID = UUID()
        let good = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        var stale = variant(duration: 2, generation: "g1")
        stale["output_url"] = .string(good.absoluteString)
        stale["render_status"] = .string("rendering")  // the render predates the loaded document → not displayable
        let spy = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: stale)
        spy.suspendNextSourcePool = true
        let session = NativeEditorSession()
        let loading = Task { @MainActor in await session.load(api: spy, threadID: UUID()) }
        for _ in 0..<200 where !spy.sourcePoolIsSuspended { try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertTrue(spy.sourcePoolIsSuspended)
        return (session, spy, loading)
    }

    func testNeedsReloadIsTrueBeforeAnyLoadAndFalseAfterMatchingRevision() async {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        let project = ProjectSummary(
            id: UUID(), title: "Revision check", status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", serverRevision: 4,
            activeJobID: jobID, activePlanItemID: "item"
        )
        let session = NativeEditorSession(project: project)
        XCTAssertTrue(session.needsReload(for: project), "A never-loaded session always needs its first load")

        await session.load(project: project, api: fake)
        XCTAssertFalse(session.needsReload(for: project), "The same revision the session just loaded does not need a reload")

        let newerProject = ProjectSummary(
            id: project.id, title: project.title, status: .ready, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial", serverRevision: 5,
            activeJobID: jobID, activePlanItemID: "item"
        )
        XCTAssertTrue(
            session.needsReload(for: newerProject),
            "A shared session left open across a server-side change must reload rather than keep showing the stale video indefinitely"
        )
    }

    /// The editor installs the last finished cloud render immediately on
    /// open so something is on screen right away, then swaps to a local,
    /// editable composition built straight from the current document. If
    /// the server's render hasn't caught up with the last save yet
    /// (render_status != "ready"), that finished render must not display
    /// during the swap window — otherwise a fresh title/text edit flashes
    /// its pre-edit styling for the few seconds the swap takes.
    func testUncaughtUpRenderStaysHiddenWhilePreviewPreparesUntilConfirmedCurrent() async throws {
        let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: jobID.uuidString,
            baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 2, generation: "g1"))
        // A save queued a re-render that hasn't finished yet: the render
        // this response's output_url points to predates the document the
        // rest of this same response describes.
        fake.authoritativeVariant?["render_status"] = .string("rendering")
        fake.suspendNextSourcePool = true
        let project = ProjectSummary(
            id: UUID(), title: "Rendering", status: .rendering, updatedAt: .now,
            posterURL: nil, outputVariantID: "initial",
            activeJobID: jobID, activePlanItemID: "item"
        )
        let session = NativeEditorSession(project: project)

        let loadTask = Task { await session.load(project: project, api: fake) }
        for _ in 0..<100 where !fake.sourcePoolIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(fake.sourcePoolIsSuspended, "the source-pool gate did not suspend in time")

        XCTAssertEqual(session.sourcePreviewState, .preparing)
        XCTAssertNotNil(session.player, "the not-yet-current render is still installed as a fallback")
        XCTAssertFalse(
            session.canDisplayCurrentPlayer,
            "A render that predates the current document must not display while the current-document preview is still preparing"
        )

        fake.resumeSourcePool()
        await loadTask.value
    }

    func testFixtureSourceFailureDoesNotPlayStaleEditablePreview() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertTrue(session.hasSourcePreview)

        await session.prepareFixtureSourcePreview(
            url: URL(fileURLWithPath: "/tmp/kria-invalid-source-fixture.mp4"),
            forceFailure: true
        )

        guard case .failed = session.sourcePreviewState else { return XCTFail("Fixture source failure must remain visible") }
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertFalse(session.canDisplayCurrentPlayer)
        session.togglePlayback()
        XCTAssertFalse(session.isPlaying, "A stale editable preview cannot play after source preparation fails")
    }

    func testPauseDuringScrubHandoffRetainsTargetWithoutRestartingPlayback() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 1.2)
        session.togglePlayback()
        session.reconcilePlaybackState(.paused)
        XCTAssertTrue(session.isPlaying, "Transient paused observation must not cancel play intent")
        session.pausePlayback()
        XCTAssertEqual(session.currentTime, 1.2, accuracy: 0.001)
        XCTAssertEqual(player.targets.count, 2)
        player.completeSeek() // Invalidated paused seek.
        player.completeSeek() // Actual playback handoff.
        await Task.yield()
        await Task.yield()
        XCTAssertFalse(session.isPlaying)
        XCTAssertEqual(player.rate, 0, "Late handoff completion must never restart a paused player")
    }

    /// KRI-95: the real editor's scrub path now records seek latency the same
    /// way MediaDiagnosticView's debug harness always did, so KRI-97's
    /// physical-device seek-p95 measurement has real numbers to read.
    func testScrubRecordsSeekLatencyIntoPreviewInstrumentation() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        XCTAssertTrue(session.previewInstrumentation.snapshot().isEmpty)
        session.seek(to: 0.5)
        for _ in 0..<100 {
            if session.scrubPreviewFrame != nil { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertNotNil(session.scrubPreviewFrame)
        let events = session.previewInstrumentation.snapshot()
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.name, .seekLatency)
        XCTAssertGreaterThanOrEqual(events.first?.value ?? -1, 0)
    }

    func testBufferingDuringPlayHandoffCannotLeaveScrubImageOverMovingVideo() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let url = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: url)
        session.seek(to: 0.5)
        for _ in 0..<100 {
            if session.scrubPreviewFrame != nil { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertNotNil(session.scrubPreviewFrame)
        let player = DelayedSeekPlayer()
        session.player = player
        session.togglePlayback()
        // Buffering arrives before the play-position seek completion.
        session.reconcilePlaybackState(.waitingToPlayAtSpecifiedRate)
        XCTAssertTrue(session.isPlaying, "Buffering must preserve the play request")
        player.completeSeek(finished: false)
        await Task.yield()
        await Task.yield()
        session.reconcilePlaybackState(.playing)
        XCTAssertNil(session.scrubPreviewFrame, "A moving player must never stay covered by its old scrub still")
        let seeks = player.targets.count
        session.pausePlayback()
        session.togglePlayback()
        XCTAssertEqual(player.targets.count, seeks, "A recovered player must not retain a stale scrub target")
        player.pause()
    }

    func testStalledPausedSeekRecoversNewestTargetAndIgnoresLateCompletion() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 18)
        let recovery = player.expectSeek(to: 17)
        session.seek(to: 17)
        await fulfillment(of: [recovery], timeout: 3)
        XCTAssertEqual(player.targets, [18, 17])
        session.seek(to: 16)
        let newest = player.expectSeek(to: 16)
        player.completeSeek() // The stalled request completes after replacement.
        player.completeSeek()
        await fulfillment(of: [newest], timeout: 3)
        XCTAssertEqual(player.targets, [18, 17, 16])
        XCTAssertEqual(session.currentTime, 16)
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
    }

    func testStalledFinalScrubSampleRetriesWithoutAnotherFingerEvent() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        session.seek(to: 18)
        let recovery = player.expectSeek(to: 18)
        await fulfillment(of: [recovery], timeout: 3)
        XCTAssertEqual(player.targets, [18, 18])
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
        XCTAssertEqual(player.targets, [18, 18])
    }

    func testRapidScrubbingCoalescesDecoderSeeksAndKeepsLatestClock() async {
        let session = NativeEditorSession()
        session.duration = 60
        let player = DelayedSeekPlayer()
        session.player = player
        for step in 1...100 { session.seek(to: Double(step) / 10) }
        XCTAssertEqual(session.currentTime, 10)
        XCTAssertEqual(player.targets, [0.1])
        let latest = player.expectSeek(to: 10)
        player.completeSeek()
        await fulfillment(of: [latest], timeout: 3)
        XCTAssertEqual(player.targets, [0.1, 10])
        XCTAssertEqual(session.currentTime, 10)
        let next = player.expectSeek(to: 2)
        player.completeSeek()
        session.seek(to: 2)
        await fulfillment(of: [next], timeout: 3)
        XCTAssertEqual(player.targets, [0.1, 10, 2])
        let noFurtherSeek = player.expectNoSeek()
        player.completeSeek()
        await fulfillment(of: [noFurtherSeek], timeout: 0.5)
    }

    func testMusicSourceSurvivesUUIDCaseRoundTripWithoutPublicCatalog() {
        let id = UUID().uuidString
        let url = "https://media.example.test/owned-track.m4a"
        XCTAssertEqual(NativeEditorSession.previewMusicURL(trackID: id, variant: [
            "music_track_id": .string(id.lowercased()), "music_preview_url": .string(url)
        ]), URL(string: url))
        XCTAssertEqual(NativeEditorSession.previewMusicURL(trackID: id, variant: [
            "background_music": .object(["track_id": .string(id.lowercased()), "preview_url": .string(url)])
        ]), URL(string: url))
        XCTAssertNil(NativeEditorSession.previewMusicURL(trackID: UUID().uuidString, variant: [
            "music_track_id": .string(id.lowercased()), "music_preview_url": .string(url)
        ]))
    }

    func testCanonicalTextIdentityPreservesTimedStoryLayersAcrossDraftBridge() throws {
        let rows: [JSONValue] = (0..<172).map { index in
            .object(["id": .string("story-label-\(index)"), "text": .string("Caption \(index)"),
                     "start_s": .number(Double(index) * 0.25), "end_s": .number(Double(index + 1) * 0.25),
                     "font_family": .string("Fraunces"), "size_px": .number(64), "effect": .string("pop-in")])
        }
        let snapshot = DraftSnapshot(draftID: "draft", itemID: "item", variantKey: "guided_story",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: ["editor_payload": .object(["sections": .object(["text_elements": .array(rows)])])],
            canUndo: false, createdAt: .now)
        let draft = snapshot.editorDraft(projectID: UUID())
        let session = NativeEditorSession(draft: draft)
        XCTAssertEqual(session.document.textElements.count, 172)
        XCTAssertEqual(session.document.textElements.filter { $0.startS <= 0 && $0.endS > 0 }.count, 1)
        for (index, layer) in session.document.textElements.enumerated() {
            XCTAssertEqual(layer.id, "story-label-\(index)")
            XCTAssertEqual(layer.startS, Double(index) * 0.25)
            XCTAssertEqual(layer.endS, Double(index + 1) * 0.25)
            XCTAssertEqual(layer.raw["size_px"], .number(64))
            XCTAssertEqual(layer.raw["effect"], .string("pop-in"))
        }
        let roundTrip = try JSONDecoder().decode(EditorDraft.self, from: JSONEncoder().encode(draft))
        XCTAssertEqual(roundTrip.text.first?.canonicalID, "story-label-0")
    }

    func testTextTransformUsesGestureBaselineAndSingleUndo() {
        let session = NativeEditorSession()
        session.addText(content: "Scale and rotate")
        let id = session.document.textElements[0].id
        session.setTextSize(id: id, sizePX: 80)
        session.setTextWidth(id: id, width: 0.5)
        let baseline = session.document.textElements[0]
        let before = session.document
        session.beginDirectManipulation()
        session.transformText(from: baseline, scale: 1.2, rotationDelta: 10)
        session.transformText(from: baseline, scale: 1.5, rotationDelta: 35)
        session.endDirectManipulation()
        let result = session.document.textElements[0]
        XCTAssertEqual(result.raw["size_px"], .number(120))
        XCTAssertEqual(result.raw["max_width_frac"], .number(0.75))
        XCTAssertEqual(result.raw["rotation_deg"], .number(35))
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        XCTAssertEqual(session.document.textElements[0], result)
    }

    func testTextScalingContinuesBeyondCanvasAndFormerSizeLimit() {
        let session = NativeEditorSession()
        session.addText(content: "Large text")
        let id = session.document.textElements[0].id
        session.setTextSize(id: id, sizePX: 100)
        let baseline = session.document.textElements[0]
        session.transformText(from: baseline, scale: 12, rotationDelta: 0)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1200, accuracy: 0.001)
        XCTAssertGreaterThan(session.document.textElements[0].raw["max_width_frac"]?.numberValue ?? 0, 1)
        session.setTextSize(id: id, sizePX: 1600)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1600, accuracy: 0.001)
        session.setTextSize(id: id, sizePX: .infinity)
        XCTAssertEqual(session.document.textElements[0].raw["size_px"]?.numberValue ?? 0, 1600, accuracy: 0.001)
    }

    func testPhaseEditsPreserveLegacyEntranceAndUndoTogether() {
        let session = NativeEditorSession()
        session.addText(content: "Motion")
        let id = session.document.textElements[0].id
        session.setTextAnimation(id: id, animation: "pop-in")
        let baseline = session.document
        session.beginTransaction()
        session.setTextPhase(id: id, phase: "exit", effect: "typewriter")
        session.setTextAnimationSpeed(id: id, speed: 2)
        session.endTransaction()
        let phases = NativeEditorSession.textPhases(for: session.document.textElements[0])
        XCTAssertEqual(phases["entrance"], .string("pop"))
        XCTAssertEqual(phases["exit"], .string("typewriter"))
        XCTAssertEqual(phases["speed"], .number(2))
        session.undo()
        XCTAssertEqual(session.document, baseline)
    }

    func testTextDragCrossingCarouselUsesOutputDeltaAndKeepsBaseDuration() {
        let clips: [EditorClip] = [
            EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 4),
            EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 8, trimIn: 0, trimOut: 4, sourceDuration: 4),
        ]
        let snapshot: [String: JSONValue] = ["carousel_moment": .object(["position": .string("middle"), "duration_s": .number(3)])]
        let draft = EditorDraft(projectID: UUID(), clips: clips, text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        session.addText(content: "Move")
        let id = session.document.textElements[0].id
        session.updateTextTiming(id: id, startS: 2, endS: 3)
        let baseline = session.document
        session.beginTimedBodyMove(kind: .text, id: id)
        session.updateTimedBodyMove(by: 2)
        session.updateTimedBodyMove(by: 6)
        session.endTimedBodyMove()
        XCTAssertEqual(session.document.textElements[0].startS, 5)
        XCTAssertEqual(session.document.textElements[0].endS, 6)
        session.undo()
        XCTAssertEqual(session.document, baseline)
    }

    func testPendingTextOnlyCommitsOnDoneAtPlayheadAsOneUndoStep() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 6, trimIn: 0, trimOut: 6, sourceDuration: 6)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.currentTime = 4.5
        session.beginTextCreation()
        session.updatePendingText("One")
        session.updatePendingText("One more game")
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
        XCTAssertFalse(session.canUndo)
        let selection = session.finishTextCreation()
        XCTAssertEqual(selection, session.selection)
        XCTAssertEqual(session.document.textElements.first?.text, "One more game")
        XCTAssertEqual(session.document.textElements.first?.startS, 4.5)
        XCTAssertEqual(session.document.textElements.first?.endS, 6)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.canUndo)
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "One more game")
    }

    func testCancelAndEmptyDonePreserveExistingTextAndHistory() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 6, trimIn: 0, trimOut: 6, sourceDuration: 6)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.addText(content: "Keep me")
        let baseline = session.document
        session.beginTextCreation()
        session.updatePendingText("Discard me")
        session.cancelTextCreation()
        XCTAssertEqual(session.document, baseline)
        session.beginTextCreation()
        session.updatePendingText(" \n ")
        XCTAssertNil(session.finishTextCreation())
        XCTAssertEqual(session.document, baseline)
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.canUndo)
    }

    func testProductionSessionStartsFailClosedUntilCapabilitiesLoad() {
        let project = ProjectSummary(id: UUID(), title: "Ready", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        XCTAssertFalse(session.canEditTimeline)
        XCTAssertFalse(session.canEditText)
        XCTAssertFalse(session.canEditCaptions)
        XCTAssertFalse(session.canEditMix)
        XCTAssertEqual(session.loadState, .idle)
    }

    func testFixtureSessionStartsLoaded() {
        let session = NativeEditorSession()
        XCTAssertEqual(session.loadState, .loaded)
    }

    func testTrimClampsToMinimumAndUndoRedo() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4, sourceDuration: 8)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 2))
        session.selectClip(first.id)
        session.trimSelected(edge: .trailing, to: 0)
        XCTAssertEqual(session.draft.clips[0].end - session.draft.clips[0].start, 0.1, accuracy: 0.0001)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo(); XCTAssertEqual(session.draft.clips[0].end, 4)
        session.redo(); XCTAssertEqual(session.draft.clips[0].end, 0.1, accuracy: 0.0001)
    }

    func testLeadingTrimAdvancesSourceWithoutLeavingTimelineGap() {
        let first = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 4, start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let second = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 7, start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(first.id)
        session.trimSelected(edge: .leading, to: 1.5)
        XCTAssertEqual(session.draft.clips[0].start, 0, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimIn, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 2.5, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 4.5, accuracy: 0.0001)
    }

    func testLeadingTrimUsesOneGestureBaselineAndOneUndoStep() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let original = EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: original)

        session.beginTrim(clipID: first.id, edge: .leading)
        for translation in [0.25, 0.5, 0.75, 1.0] { session.updateTrim(by: translation) }
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 2, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 3, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 3, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 5, accuracy: 0.0001)
        session.undo()
        XCTAssertEqual(session.draft, original)
        XCTAssertFalse(session.canUndo)
    }

    func testLeadingTrimCanExtendAndReverseWithinOneGesture() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))

        session.beginTrim(clipID: first.id, edge: .leading)
        session.updateTrim(by: 1)
        session.updateTrim(by: -0.5)
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 0.5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 4.5, accuracy: 0.0001)
    }

    func testTrailingTrimExtendsThroughRippleAndStopsAtSourceEnd() {
        let first = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 6)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 4, end: 6, trimIn: 0, trimOut: 2, sourceDuration: 3)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))

        session.beginTrim(clipID: first.id, edge: .trailing)
        session.updateTrim(by: 100)
        session.endTrim()

        XCTAssertEqual(session.draft.clips[0].trimIn, 1, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].trimOut, 6, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[0].end, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].start, 5, accuracy: 0.0001)
        XCTAssertEqual(session.draft.clips[1].end, 7, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 7, accuracy: 0.0001)
    }

    /// KRI-290: a phone Narrated clip the server slowed to fill its voiceover
    /// step can be trimmed, and the preview plays the trimmed window the way
    /// the phone renders it (`_fit_step_window`), not the span hydrated on load.
    func testTrimmedNarratedClipPreviewsLikeThePhoneRendersIt() async throws {
        let threadID = UUID()
        // A 4 s window over 2 s of footage, as hydrated on load (1.95 s slowed).
        let slot: JSONValue = .object([
            "slot_id": .string("s1"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(4),
            "source_duration_s": .number(2), "native_source_span_s": .number(1.95),
        ])
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([slot])]),
        ])]
        let variant: [String: JSONValue] = [
            "variant_id": .string("narrated"), "render_generation_id": .string("g1"), "render_status": .string("ready"),
            "resolved_archetype": .string("narrated"),
            "editor_capabilities": .object(["timeline": .bool(true)]),
            "user_timeline": .object(["slots": .array([slot])]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "narrated", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let loaded = try XCTUnwrap(session.timelineClips.first)
        XCTAssertEqual(loaded.trimOut - loaded.trimIn, 1.95, accuracy: 0.0001)

        session.beginTrim(clipID: try XCTUnwrap(session.draft.clips.first?.id), edge: .trailing)
        session.updateTrim(by: -0.5)
        // Shrinks from its own length instead of snapping to the 2 s of footage.
        XCTAssertEqual(session.timelineClips[0].end, 3.5, accuracy: 0.0001)
        XCTAssertEqual(session.timelineClips[0].trimOut - session.timelineClips[0].trimIn, 1.95, accuracy: 0.0001)
        session.updateTrim(by: 1)
        XCTAssertEqual(session.timelineClips[0].end, 4, accuracy: 0.0001)
        session.updateTrim(by: -2.5)
        session.endTrim()

        // Within its footage now: 1.5 s at 1x, not the load-time 1.95 s sped up.
        let trimmed = session.timelineClips[0]
        XCTAssertEqual(trimmed.end, 1.5, accuracy: 0.0001)
        XCTAssertEqual(trimmed.trimOut - trimmed.trimIn, 1.5, accuracy: 0.0001)
    }

    // REGRESSION: timelineScrubDuration must equal playbackDuration whenever
    // no preview rebuild is pending or in flight — auto-scroll and any seek
    // widen against timelineScrubDuration, so if this baseline case were
    // ever wrong (e.g. permanently widened, as it would be if gated on
    // `sourcePreviewTask != nil` — that field is never reset to nil after a
    // rebuild completes), every seek in the app would silently accept times
    // past the real end. This fixture has no source compiler installed, so
    // scheduleSourcePreviewUpdate's very first guard bails before any
    // rebuild is ever scheduled — sourcePreviewSequence and
    // sourcePreviewSettledSequence both stay at their initial 0.
    func testTimelineScrubDurationEqualsPlaybackDurationWithNoRebuildPending() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        XCTAssertEqual(session.timelineScrubDuration, session.playbackDuration, accuracy: 0.0001)

        // Also holds mid-gesture and after a trim that extends the clip —
        // the deferral machinery this test guards against only ever engages
        // when a real source compiler is installed (see
        // rebuildSourcePreview's first guard), which this fixture has none
        // of, so a timing gesture here never marks a rebuild pending either.
        session.beginTrim(clipID: clip.id, edge: .trailing)
        session.updateTrim(by: 3)
        XCTAssertEqual(session.timelineScrubDuration, session.playbackDuration, accuracy: 0.0001)
        session.endTrim()
        XCTAssertEqual(session.timelineScrubDuration, session.playbackDuration, accuracy: 0.0001)
    }

    func testReturningTrimGestureToBaselineLeavesNoUndoEntry() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 1, trimOut: 5, sourceDuration: 8)
        let original = EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: original)

        session.beginTrim(clipID: clip.id, edge: .trailing)
        session.updateTrim(by: -1)
        session.updateTrim(by: 0)
        session.endTrim()

        XCTAssertEqual(session.draft, original)
        XCTAssertFalse(session.canUndo)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSourceWindowSlideAndDelete() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 2, trimOut: 4, sourceDuration: 6)
        let second = EditorClip(id: UUID(), assetID: UUID(), start: 2, end: 4, trimIn: 0, trimOut: 2, sourceDuration: 4)
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(clip.id); session.slideSourceWindow(by: 100); XCTAssertEqual(session.draft.clips[0].trimIn, 4)
        session.deleteSelectedClip(); XCTAssertEqual(session.draft.clips.count, 1); XCTAssertEqual(session.draft.clips[0].start, 0)
    }

    func testDeletingLastClipIsExplicitEmptyAndUndoRestoresIt() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2, sourceDuration: 2, slotID: "server-slot")
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))

        session.selectClip(clip.id)
        session.deleteSelectedClip()

        XCTAssertEqual(session.document.editorState, "empty")
        XCTAssertTrue(session.document.clips.isEmpty)
        XCTAssertEqual(session.document.deletions, [EditorDeletion(kind: "clip", id: "server-slot")])
        XCTAssertFalse(session.canDownloadCurrentVideo)
        session.undo()
        XCTAssertEqual(session.document.editorState, "renderable")
        XCTAssertEqual(session.document.clips.count, 1)
        XCTAssertTrue(session.document.deletions.isEmpty)
    }

    func testFailedSaveOfEmptyTimelineRetainsIntentAndUndoRestoresRenderableDocument() async {
        let threadID = UUID()
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"),
            "sections": .object(["timeline_slots": .array([.object([
                "slot_id": .string("server-slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2),
            ])])]),
        ])]
        let variant: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("g1"), "render_status": .string("ready"),
            "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true)]),
            "user_timeline": .object(["slots": .array([.object([
                "slot_id": .string("server-slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2),
            ])])]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: variant,
            commitError: .offline
        )
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)

        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "server-slot")))
        await session.save()

        XCTAssertEqual(session.document.editorState, "empty")
        XCTAssertTrue(session.document.clips.isEmpty)
        XCTAssertEqual(session.document.deletions, [.init(kind: "clip", id: "server-slot")])
        XCTAssertEqual(fake.lastRequest?.deletions, [.init(kind: "clip", id: "server-slot")])
        session.undo()
        XCTAssertEqual(session.document.editorState, "renderable")
        XCTAssertEqual(session.document.clips.count, 1)
        XCTAssertTrue(session.document.deletions.isEmpty)
    }

    private func loadDeletionSaveFixture(clipCount: Int = 1, suspendCommit: Bool = false) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        let slots: [JSONValue] = (0..<clipCount).map { index in .object([
            "slot_id": .string("slot-\(index)"), "clip_index": .number(Double(index)),
            "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(8),
        ]) }
        let text: [JSONValue] = ["keep", "later"].map { id in .object([
            "id": .string(id), "text": .string(id), "start_s": .number(0), "end_s": .number(2),
        ]) }
        let snapshot: [String: JSONValue] = ["editor_payload": .object([
            "base_generation": .string("g1"),
            "sections": .object(["timeline_slots": .array(slots), "text_elements": .array(text)]),
        ])]
        let variant: [String: JSONValue] = [
            "variant_id": .string("variant"), "render_generation_id": .string("g1"), "render_status": .string("ready"),
            "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]),
            "user_timeline": .object(["slots": .array(slots)]), "text_elements": .array(text),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: variant, suspendNextCommit: suspendCommit
        )
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    private func emptyCommitResponse(_ session: NativeEditorSession, generation: String) -> EditorCommitResponse {
        var saved = session.document
        saved.editorState = "empty"
        saved.revision.baseGeneration = generation
        saved.deletions.removeAll()
        let draft = DraftSnapshot(draftID: "empty-\(generation)", itemID: "item", variantKey: "variant", draftRevision: 2, snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: generation, snapshot: saved.encodeSnapshot(), canUndo: true, createdAt: .now)
        return EditorCommitResponse(ok: true, generation: generation, sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "h", expectedDuration: 0, editorState: "empty", draft: draft)
    }

    func testSecondEmptySaveAdvertisesSupportWithoutDeletionIntents() async {
        let (session, fake) = await loadDeletionSaveFixture()
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        fake.commitResponse = emptyCommitResponse(session, generation: "g2")
        await session.save()
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)

        session.updateTextContent(id: "keep", content: "Edited while empty")
        fake.commitResponse = emptyCommitResponse(session, generation: "g3")
        await session.save()

        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(fake.lastRequest?.editorStateVersion, 1)
        XCTAssertNil(fake.lastRequest?.deletions)
        XCTAssertEqual(session.document.editorState, "empty")
        XCTAssertEqual(session.document.textElements.first?.text, "Edited while empty")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testClipAddedAfterEmptySaveAdvertisesSupportAtSavedGeneration() async throws {
        let (session, fake) = await loadDeletionSaveFixture()
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        fake.commitResponse = emptyCommitResponse(session, generation: "g2")
        await session.save()
        let file = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("fixture upload".utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/upload")!, gcsPath: "users/u/new.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: "job", clipIndex: 1, kind: "video")
        await session.addClip(fileURL: file)
        XCTAssertNil(session.addClipError)
        XCTAssertEqual(session.document.editorState, "renderable")
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 3, revisionHash: "h3", expectedDuration: 4)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(fake.lastRequest?.editorStateVersion, 1)
        XCTAssertNil(fake.lastRequest?.deletions)
        XCTAssertEqual(fake.lastRequest?.timelineSlots?.count, 1)
    }

    func testEditDuringEmptySaveKeepsNewGenerationAndUndo() async {
        let (session, fake) = await loadDeletionSaveFixture(suspendCommit: true)
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        session.markDirty(.timeline)
        fake.commitResponse = emptyCommitResponse(session, generation: "g2")
        let save = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(fake.commitIsSuspended)
        session.updateTextContent(id: "keep", content: "Later edit")
        fake.resumeCommit()
        await save.value

        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "Later edit")
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "keep")
        XCTAssertTrue(session.document.clips.isEmpty)
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "Later edit")
        fake.commitResponse = emptyCommitResponse(session, generation: "g3")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(fake.lastRequest?.editorStateVersion, 1)
    }

    func testEmptySaveRejectsPreviouslyPendingRenderCompletion() async {
        let (session, fake) = await loadDeletionSaveFixture()
        session.updateTextContent(id: "keep", content: "Submitted")
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "h2", expectedDuration: 2)
        await session.save()
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        fake.commitResponse = emptyCommitResponse(session, generation: "g3")
        await session.save()
        XCTAssertFalse(session.applyPreviewVariant([
            "render_generation_id": .string("g2"), "render_status": .string("ready"),
            "output_url": .string("https://storage.example/old.mp4"),
        ], generation: "g2"))
        XCTAssertEqual(session.document.editorState, "empty")
        XCTAssertEqual(session.document.revision.baseGeneration, "g3")
        XCTAssertFalse(session.canDownloadCurrentVideo)
    }

    func testClipAddedDuringEmptySaveRemainsRenderableAndUndoUsesSavedEmptyBaseline() async throws {
        let (session, fake) = await loadDeletionSaveFixture(suspendCommit: true)
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        fake.commitResponse = emptyCommitResponse(session, generation: "g2")
        let save = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(fake.commitIsSuspended)
        let file = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("fixture upload".utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/upload")!, gcsPath: "users/u/new.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: "job", clipIndex: 1, kind: "video")
        await session.addClip(fileURL: file)
        fake.resumeCommit()
        await save.value

        XCTAssertNil(session.addClipError)
        XCTAssertEqual(session.document.editorState, "renderable")
        XCTAssertEqual(session.document.clips.count, 1)
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undo()
        XCTAssertEqual(session.document.editorState, "empty")
        XCTAssertTrue(session.document.clips.isEmpty)
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
        session.redo()
        XCTAssertEqual(session.document.editorState, "renderable")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
    }

    func testDeleteSaveConsumesAcceptedIntentAndPreservesLaterDeletionAndUndo() async {
        let (session, fake) = await loadDeletionSaveFixture(clipCount: 2, suspendCommit: true)
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: "slot-0")))
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "h2", expectedDuration: 2)
        let save = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(fake.commitIsSuspended)
        XCTAssertTrue(session.deleteSelection(.init(kind: .text, id: "later")))
        session.updateTextContent(id: "keep", content: "Later edit")
        fake.resumeCommit()
        await save.value

        XCTAssertEqual(session.document.deletions, [.init(kind: "text", id: "later")])
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        session.undo()
        XCTAssertEqual(session.document.deletions, [.init(kind: "text", id: "later")])
        session.undo()
        XCTAssertTrue(session.document.deletions.isEmpty)
        XCTAssertEqual(session.document.clips.count, 1)
        session.redo(); session.redo()
        XCTAssertEqual(session.document.deletions, [.init(kind: "text", id: "later")])
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "h3", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(fake.lastRequest?.deletions, [.init(kind: "text", id: "later")])
    }

    func testVisibleGeneratedLanesDeleteWithoutEditCapabilitiesAndUndoRedoTheirIntents() {
        var draft = NativeEditorUITestFixtures.captionVisuals
        var document = EditorDocument(snapshot: draft.serverSnapshot)
        document.music = EditorMusic(trackID: "song-1")
        document.soundEffects = [EditorTimedEffect(id: "sfx-1", startS: 0, endS: 1)]
        document.mediaOverlays = [EditorTimedEffect(id: "overlay-1", startS: 0, endS: 1)]
        document.motionScenes = [EditorMotionScene(id: "motion-1", startS: 0, endS: 1)]
        document.cameraEffects = [EditorCameraEffect(id: "camera-1", startS: 0, endS: 1)]
        document.carouselMoment = ["id": .string("carousel-1"), "position": .string("middle")]
        draft.serverSnapshot = document.encodeSnapshot()
        draft.serverSnapshot["editor_capabilities"] = .object([
            "music": .bool(false), "sound_effects": .bool(false), "media_overlays": .bool(false),
            "visual_blocks": .bool(false), "motion_scenes": .bool(false), "camera_effects": .bool(false),
            "carousel_moment": .bool(false),
        ])
        let session = NativeEditorSession(draft: draft)
        let cases: [(EditorSelection, EditorDeletion)] = [
            (.init(kind: .music, id: "song-1"), .init(kind: "music", id: "song-1")),
            (.init(kind: .soundEffect, id: "sfx-1"), .init(kind: "sound_effect", id: "sfx-1")),
            (.init(kind: .mediaOverlay, id: "overlay-1"), .init(kind: "media_overlay", id: "overlay-1")),
            (.init(kind: .visualBlock, id: "paper-media"), .init(kind: "visual_block", id: "paper-media")),
            (.init(kind: .motionScene, id: "motion-1"), .init(kind: "motion_scene", id: "motion-1")),
            (.init(kind: .cameraEffect, id: "camera-1"), .init(kind: "camera_effect", id: "camera-1")),
            (.init(kind: .carousel, id: "carousel-1"), .init(kind: "carousel", id: "carousel-1")),
        ]

        for (selection, intent) in cases {
            XCTAssertTrue(session.deleteSelection(selection), "delete \(selection.kind)")
            XCTAssertTrue(session.document.deletions.contains(intent))
            session.undo()
            XCTAssertFalse(session.document.deletions.contains(intent), "undo restores \(selection.kind)")
            session.redo()
            XCTAssertTrue(session.document.deletions.contains(intent), "redo repeats \(selection.kind)")
        }
    }

    func testDeletingUnsavedAdditionDoesNotSendServerDeletionIntent() {
        var draft = NativeEditorUITestFixtures.captionVisuals
        draft.serverSnapshot["editor_capabilities"] = .object(["music": .bool(true)])
        let session = NativeEditorSession(draft: draft)

        session.setMusic(trackID: "new-local-song")
        XCTAssertTrue(session.deleteSelection(.init(kind: .music, id: "new-local-song")))
        XCTAssertTrue(session.document.deletions.isEmpty)
        session.undo()
        XCTAssertEqual(session.document.music?.trackID, "new-local-song")
        session.redo()
        XCTAssertNil(session.document.music)
        XCTAssertTrue(session.document.deletions.isEmpty)
    }

    func testSerializationKeepsUnknownServerKeysAndUsesProductionSections() {
        let projectID = UUID(); let clipID = UUID(); let assetID = UUID()
        let snapshot: [String: JSONValue] = ["schema_version": .number(2), "kind": .string("editor"), "future": .object(["keep": .bool(true)]), "editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string("slot-a"), "clip_index": .number(0), "in_s": .number(1), "duration_s": .number(2), "removed": .bool(false)])]), "future_section": .string("untouched")])])]
        let server = DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 3, snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now)
        let draft = server.editorDraft(projectID: projectID)
        XCTAssertEqual(draft.clips.count, 1)
        let persisted = draft.persistedSnapshot()
        let payload = Self.object(persisted["editor_payload"]); let sections = Self.object(payload?["sections"])
        XCTAssertEqual(Self.object(persisted["future"])?["keep"], .bool(true)); XCTAssertEqual(sections?["future_section"], .string("untouched")); XCTAssertNotNil(sections?["timeline_slots"])
        _ = clipID; _ = assetID
    }

    func testHydrationDoesNotSynthesizeAnUnmuteOverrideForAnUntouchedSlot() {
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2, trimIn: 0, trimOut: 2, slotID: "slot-a")
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["sections": .object([
            "timeline_slots": .array([.object(["slot_id": .string("slot-a"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])
        ])])]
        let draft = EditorDraft(projectID: UUID(), clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        XCTAssertNil(session.document.clips.first?.raw["muted"])
    }

    func testReorderingPreservesSourcePoolIndexes() {
        let first = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 7, start: 0, end: 2, trimIn: 0, trimOut: 2, slotID: "a")
        let second = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 2, start: 2, end: 4, trimIn: 0, trimOut: 2, slotID: "b")
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [first, second], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.selectClip(first.id)
        session.moveSelected(by: 1)
        let payload = Self.object(session.document.encodeSnapshot()["editor_payload"])
        let sections = Self.object(payload?["sections"])
        let slots = Self.array(sections?["timeline_slots"])
        XCTAssertEqual(Self.object(slots.first)?["slot_id"], .string("b"))
        XCTAssertEqual(Self.object(slots.last)?["slot_id"], .string("a"))
        XCTAssertEqual(Self.object(slots.first)?["clip_index"], .number(2))
        XCTAssertEqual(Self.object(slots.last)?["clip_index"], .number(7))
    }

    func testAuthoritativeVariantRebasesStaleRuntimeDraft() {
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("old"), "sections": .object(["captions_enabled": .bool(false), "caption_style": .string("word"), "timeline_slots": .array([.object(["slot_id": .string("old-slot"), "clip_index": .number(9), "in_s": .number(0), "duration_s": .number(5)])])])])]
        let server = DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "old", snapshot: snapshot, canUndo: false, createdAt: .now)
        let authoritative: [String: JSONValue] = [
            "render_generation_id": .string("live"),
            "captions_enabled": .bool(true),
            "voiceover_caption_style": .string("sentence"),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("live-slot"), "clip_index": .number(3), "in_s": .number(1.25), "duration_s": .number(2.5), "source_duration_s": .number(7)])])]),
        ]
        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)
        XCTAssertEqual(draft.clips.first?.slotID, "live-slot")
        XCTAssertEqual(draft.clips.first?.sourceClipIndex, 3)
        XCTAssertEqual(draft.clips.first?.sourceDuration, 7)
        XCTAssertTrue(draft.captions.enabled)
        XCTAssertEqual(draft.captions.style, "sentence")
        XCTAssertEqual(Self.object(draft.serverSnapshot["editor_payload"])?["base_generation"], .string("live"))
        for absentGeneration in [JSONValue.null, .string("")] {
            var legacy = authoritative
            legacy["render_generation_id"] = absentGeneration
            legacy["render_finished_at"] = .string("live-finished")
            let restored = server.editorDraft(projectID: UUID(), authoritativeVariant: legacy)
            XCTAssertEqual(Self.object(restored.serverSnapshot["editor_payload"])?["base_generation"], .string("live-finished"))
        }
    }

    func testNarratedVariantProjectsPersistedVisualCutIntoTimeline() {
        let server = DraftSnapshot(
            draftID: "job", itemID: "item", variantKey: "narrated",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: UUID().uuidString,
            baseGenerationID: "generation", snapshot: [:], canUndo: false, createdAt: .now
        )
        let authoritative: [String: JSONValue] = [
            "variant_id": .string("narrated"),
            "resolved_archetype": .string("narrated"),
            "render_status": .string("ready"),
            "ai_timeline": .null,
            "narrated_timings": .array([
                .object(["step_id": .string("shot_1"), "start_s": .number(0), "end_s": .number(12)]),
                .object(["step_id": .string("shot_2"), "start_s": .number(12), "end_s": .number(30)]),
            ]),
            "narrated_clip_assignments": .array([
                .object(["step_id": .string("shot_1"), "clip_id": .string("clip_2"), "source_start_s": .number(1.5)]),
                .object(["step_id": .string("shot_2"), "clip_id": .string("clip_0"), "source_start_s": .number(4)]),
            ]),
            "editor_capabilities": .object(["timeline": .bool(false)]),
        ]

        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)

        XCTAssertEqual(draft.clips.count, 2)
        XCTAssertEqual(draft.clips.map(\.sourceClipIndex), [2, 0])
        XCTAssertEqual(draft.clips.map(\.start), [0, 12])
        XCTAssertEqual(draft.clips.map(\.end), [12, 30])
        XCTAssertEqual(draft.clips.map(\.trimIn), [1.5, 4])
        XCTAssertEqual(draft.clips.map(\.trimOut), [13.5, 22])
    }

    /// KRI-281: exact shapes the server projects from the pinned device recipe (protocol 3).
    func testServerProjectedPhoneNarratedAndVoiceoverTimelinesHydrateClips() {
        let server = DraftSnapshot(
            draftID: "job", itemID: "item", variantKey: "v",
            draftRevision: 0, snapshotHash: "", etag: "", baseJobID: UUID().uuidString,
            baseGenerationID: "generation", snapshot: [:], canUndo: false, createdAt: .now
        )
        let narrated: [String: JSONValue] = [
            "variant_id": .string("narrated"), "resolved_archetype": .string("narrated"),
            "render_destination": .string("device"),
            "narrated_timings": .array([
                .object(["step_id": .string("s1"), "start_s": .number(0), "end_s": .number(5), "confidence": .number(1)]),
                .object(["step_id": .string("s2"), "start_s": .number(5), "end_s": .number(9), "confidence": .number(1)]),
            ]),
            "narrated_clip_assignments": .array([
                .object(["step_id": .string("s1"), "clip_id": .string("clip_3"), "participant_key": .string("clip:clip_3"), "source_start_s": .number(2)]),
                .object(["step_id": .string("s2"), "clip_id": .string("clip_1"), "participant_key": .string("clip:clip_1"), "source_start_s": .number(0)]),
            ]),
            "editor_capabilities": .object(["timeline": .bool(false)]),
        ]
        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: narrated)
        XCTAssertEqual(draft.clips.map(\.sourceClipIndex), [3, 1])
        XCTAssertEqual(draft.clips.map(\.end), [5, 9])

        func slot(_ id: String, _ index: Int, _ inS: Double, _ duration: Double, _ order: Int) -> JSONValue {
            .object(["slot_id": .string(id), "clip_index": .number(Double(index)), "source_duration_s": .number(inS + duration),
                     "in_s": .number(inS), "duration_s": .number(duration), "order": .number(Double(order)), "removed": .bool(false)])
        }
        let voiceover: [String: JSONValue] = [
            "variant_id": .string("voiceover_only"), "resolved_archetype": .string("voiceover"),
            "render_destination": .string("device"),
            "ai_timeline": .object(["beat_grid": .array([]), "slots": .array([slot("a", 2, 1, 4, 0), slot("b", 0, 0, 3, 1)])]),
            "editor_capabilities": .object(["timeline": .bool(false)]),
        ]
        let voiced = server.editorDraft(projectID: UUID(), authoritativeVariant: voiceover)
        XCTAssertEqual(voiced.clips.map(\.sourceClipIndex), [2, 0])
        XCTAssertEqual(voiced.clips.map(\.end), [4, 7])
        XCTAssertEqual(voiced.clips.map(\.trimIn), [1, 0])
    }

    func testAuthoritativeVariantReplacesAdvancedCapabilitiesAndRequiredMotionHash() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "motion_scenes": .bool(true),
                "overlays": .bool(false),
            ]),
            "editor_payload": .object([
                "base_generation": .string("old"),
                "sections": .object([
                    "motion_runtime_hash": .string("stale-required"),
                    "motion_scenes": .array([]),
                ]),
            ]),
        ]
        let server = DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: nil, baseGenerationID: "old",
            snapshot: snapshot, canUndo: false, createdAt: .now
        )
        let authoritative: [String: JSONValue] = [
            "render_generation_id": .string("live"),
            "motion_runtime_hash": .string("live-required"),
            "editor_capabilities": .object([
                "motion_scenes": .bool(false),
                "motion_scenes_reason": .string("motion_runtime_mismatch"),
                "motion_runtime_hash": .string("editor-runtime"),
                "motion_required_runtime_hash": .string("live-required"),
                "overlays": .bool(true),
            ]),
        ]

        let draft = server.editorDraft(projectID: UUID(), authoritativeVariant: authoritative)
        let document = EditorDocument.decode(snapshot: draft.serverSnapshot)
        XCTAssertEqual(document.motionRuntimeHash, "live-required")
        XCTAssertEqual(document.capabilities["motion_scenes"]?.editable, false)
        XCTAssertEqual(document.capabilities["motion_scenes"]?.reason, "motion_runtime_mismatch")
        XCTAssertEqual(document.capabilities["overlays"]?.editable, true)
        XCTAssertNil(document.capabilities["motion_runtime_hash"], "metadata is not an editable capability")
    }

    func testGalleryJobPromotesIntoEditorAndRebasesServerSnappedDuration() async {
        let jobID = UUID()
        let emptySnapshot = DraftSnapshot(
            draftID: "unused",
            itemID: "unused",
            variantKey: "initial",
            draftRevision: 0,
            snapshotHash: "",
            etag: "",
            baseJobID: nil,
            baseGenerationID: nil,
            snapshot: [:],
            canUndo: false,
            createdAt: .now
        )
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial"),
            authoritativeVariant: Self.variant(duration: 2, generation: "generation-1")
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        XCTAssertEqual(fake.openedJobID, jobID)
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 2, accuracy: 0.0001)
        XCTAssertNotNil(session.player)

        let clipID = try! XCTUnwrap(session.draft.clips.first?.id)
        session.selectClip(clipID)
        session.trimSelected(edge: .trailing, to: 1.3)
        await session.save()
        XCTAssertEqual(fake.lastItemID, "item-gallery")
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 1.3, accuracy: 0.0001)

        XCTAssertTrue(session.rebaseCleanDraft(from: Self.variant(duration: 1.5, generation: "next")))
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 1.5, accuracy: 0.0001)
        XCTAssertEqual(session.duration, 1.5, accuracy: 0.0001)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testPhoneGalleryJobUsesCreationThreadForLocalOriginals() async {
        let jobID = UUID(), threadID = UUID()
        let emptySnapshot = DraftSnapshot(
            draftID: "unused",
            itemID: "unused",
            variantKey: "initial",
            draftRevision: 0,
            snapshotHash: "",
            etag: "",
            baseJobID: nil,
            baseGenerationID: nil,
            snapshot: [:],
            canUndo: false,
            createdAt: .now
        )
        var variant = Self.variant(duration: 2, generation: "generation-1")
        variant["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial", creationThreadID: threadID),
            authoritativeVariant: variant
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        XCTAssertEqual(fake.openedJobID, jobID)
        XCTAssertEqual(try! XCTUnwrap(session.draft.clips.first?.end), 2, accuracy: 0.0001)
        XCTAssertNotNil(session.player)

        XCTAssertEqual(session.deviceRenderKey, DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "initial"))
    }

    /// `NativeEditorView.loadEditor()` calls `showDeviceOutput` with the
    /// device coordinator's local receipt right after a device-rendered
    /// variant loads. That local file must win over whatever
    /// server-signed `output_url` the load installed first — the remote
    /// signature is sized for a short-lived playback window (see
    /// `PLAYBACK_URL_TTL_MIN`), while the on-device export is immediate,
    /// free, and available offline. Only once the local receipt is gone
    /// (cleaned up, reinstalled app, other device) should playback fall
    /// back to the remote URL.
    func testDeviceLocalExportPreferredOverRemoteOutputURL() async {
        let jobID = UUID(), threadID = UUID()
        let emptySnapshot = DraftSnapshot(
            draftID: "unused", itemID: "unused", variantKey: "initial", draftRevision: 0,
            snapshotHash: "", etag: "", baseJobID: nil, baseGenerationID: nil,
            snapshot: [:], canUndo: false, createdAt: .now
        )
        var variant = Self.variant(duration: 2, generation: "generation-1")
        variant["render_destination"] = .string("device")
        let fake = EditorCommitSpy(
            draftSnapshot: emptySnapshot,
            openReceipt: OpenInEditorResponse(planItemID: "item-gallery", variantID: "initial", creationThreadID: threadID),
            authoritativeVariant: variant
        )
        let project = ProjectSummary(id: jobID, title: "Gallery cut", status: .ready, updatedAt: .now, posterURL: nil)
        let session = NativeEditorSession(project: project)

        await session.load(libraryJobID: jobID, api: fake)
        let remoteAsset = try! XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(remoteAsset.url, URL(string: "file:///tmp/kria-editor-test.mp4"))

        let localFile = try! XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        session.showDeviceOutput(localFile)

        let localAsset = try! XCTUnwrap(session.player?.currentItem?.asset as? AVURLAsset)
        XCTAssertEqual(localAsset.url, localFile, "The on-device export must win over the remote signed URL when both are available")
    }

    func testReadyCreationProjectHydratesListProjectionAndLoadsItsExistingPlanItem() async throws {
        let threadID = UUID()
        let jobID = UUID()
        let slots: [JSONValue] = (0..<6).map { index in
            .object([
                "slot_id": .string("slot-\(index)"),
                "clip_index": .number(Double(index)),
                "in_s": .number(0),
                "duration_s": .number(1),
                "source_duration_s": .number(2),
                "removed": .bool(false),
            ])
        }
        var variant = Self.variant(duration: 6, generation: "legacy-finished-at")
        variant["variant_id"] = .string("song_text")
        variant["user_timeline"] = .object([:])
        variant["ai_timeline"] = .object(["slots": .array(slots)])
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)",
          "title":"Music montage",
          "status":"active",
          "revision":8,
          "runtime_version":2,
          "active_job_id":"\#(jobID.uuidString)",
          "active_plan_item_id":"item-montage",
          "state":{},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[]},
          "updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            draftError: .conflict,
            openReceipt: OpenInEditorResponse(planItemID: "item-montage", variantID: "original_text"),
            authoritativeVariant: variant,
            refreshedThread: refreshedThread
        )
        let project = ProjectSummary(
            id: threadID,
            title: "Music montage",
            status: .ready,
            updatedAt: .now,
            posterURL: nil,
            activeJobID: jobID
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        XCTAssertLessThanOrEqual(fake.draftCallCount, 1, "the runtime draft is best-effort (staging only); its failure must never block opening")
        XCTAssertEqual(session.loadState, .loaded)
        XCTAssertEqual(fake.projectCallCount, 1, "URL-free project-list summaries must hydrate before editor loading")
        XCTAssertNil(fake.openedJobID, "creation projects already own a plan item and must not be promoted again")
        XCTAssertEqual(fake.editorVariantsCallCount, 1, "the job status is authoritative when project projections omit variant identity")
        XCTAssertEqual(session.timelineClips.count, 6)
        XCTAssertEqual(session.duration, 6, accuracy: 0.0001)
        XCTAssertEqual(session.saveState, .idle)
    }

    func testDraftLoadFailureIsNotReportedAsSaveFailure() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            draftError: .requestFailed(status: 500)
        )
        let project = ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        XCTAssertEqual(
            session.saveState,
            .loadFailed("Kria hit a problem on its side. Your chat and footage are safe. Try again in a moment.")
        )
    }

    func testDraftLoadFailsWhenAuthoritativeVariantCannotBeFetched() async {
        let threadID = UUID()
        let jobID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "draft", itemID: "item", variantKey: "selected",
                draftRevision: 1, snapshotHash: "hash", etag: "etag",
                baseJobID: jobID.uuidString, baseGenerationID: "generation-1",
                snapshot: ["editor_payload": .object([:])], canUndo: false, createdAt: .now
            ),
            editorVariantError: .requestFailed(status: 404)
        )
        let session = NativeEditorSession(project: ProjectSummary(
            id: threadID, title: "Draft", status: .draft, updatedAt: .now, posterURL: nil
        ))

        await session.load(api: fake, threadID: threadID)

        XCTAssertEqual(session.loadState, .failed("Kria couldn’t complete that request."))
        XCTAssertEqual(session.saveState, .loadFailed("Kria couldn’t complete that request."))
    }

    func testHydratedProjectRetainsFinishedOutputWhenSourcePreviewFails() async throws {
        let threadID = UUID()
        let jobID = UUID()
        let fallbackURL = URL(fileURLWithPath: "/tmp/kria-refreshed-fallback.mp4")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let refreshedThread = try decoder.decode(CreationThread.self, from: Data(#"""
        {
          "id":"\#(threadID.uuidString)",
          "title":"Hydrated",
          "status":"active",
          "revision":8,
          "runtime_version":2,
          "active_job_id":"\#(jobID.uuidString)",
          "active_plan_item_id":"item-hydrated",
          "state":{"selected_variant_id":"original_text"},
          "job":{"id":"\#(jobID.uuidString)","status":"variants_ready","variants":[{"variant_id":"original_text","render_status":"ready","output_url":"\#(fallbackURL.absoluteString)"}]},
          "updated_at":"2026-09-09T08:00:00Z"
        }
        """#.utf8))
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative.removeValue(forKey: "output_url")
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(
                draftID: "unused", itemID: "unused", variantKey: "initial",
                draftRevision: 0, snapshotHash: "", etag: "", baseJobID: nil,
                baseGenerationID: nil, snapshot: [:], canUndo: false, createdAt: .now
            ),
            authoritativeVariant: authoritative,
            refreshedThread: refreshedThread
        )
        let project = ProjectSummary(
            id: threadID, title: "Hydrated", status: .ready, updatedAt: .now,
            posterURL: nil, activeJobID: jobID
        )
        let session = NativeEditorSession(project: project)

        await session.load(project: project, api: fake)

        let player = try XCTUnwrap(session.player)
        guard case .failed = session.sourcePreviewState else { return XCTFail("Missing sources must remain explicit") }
        XCTAssertTrue(session.isShowingRenderedFallback)
        session.togglePlayback()
        XCTAssertTrue(session.isPlaying, "A finished render remains playable while source preview is unavailable")
        XCTAssertTrue(session.player === player, "The failed source preview must not discard the finished render")
        session.pausePlayback()
        XCTAssertEqual(fake.lastVariantID, "original_text")
    }

    func testAddClipUploadsReservesAndAppendsTimelineSlot() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditTimeline)

        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        // The fixture's default `editorVariant` already seeds one timeline
        // slot at clip_index 0 (see EditorCommitSpy.editorVariant) — the newly
        // added clip mints the next pool index, 1.
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")

        await session.addClip(fileURL: tempURL)

        XCTAssertNil(session.addClipError)
        XCTAssertEqual(fake.uploadFileCalls.count, 1)
        XCTAssertEqual(fake.uploadFileCalls.first?.reservation.gcsPath, "users/u/generative/abc123def456/clip.mp4")
        XCTAssertEqual(fake.addClipCalls.map(\.gcsPath), ["users/u/generative/abc123def456/clip.mp4"])
        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.document.clips.last?.clipIndex, 1)
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testAddClipUsesGuidedVariantSourceIndexWhenProvided() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video", variantClipIndices: ["variant": 7])

        await session.addClip(fileURL: tempURL)

        XCTAssertNil(session.addClipError)
        XCTAssertEqual(session.document.clips.last?.clipIndex, 7)
    }

    func testAddClipResultDecodesGuidedVariantSourceIndices() throws {
        let data = Data(#"{"job_id":"job","clip_index":1,"kind":"video","variant_clip_indices":{"song_lyrics":7}}"#.utf8)
        let result = try JSONDecoder().decode(AddClipResult.self, from: data)
        XCTAssertEqual(result.variantClipIndices?["song_lyrics"], 7)
    }

    func testAddClipSurfacesUploadFailureWithoutMutatingTimeline() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)

        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        // reserveUploadResult left nil — the fake throws .unsupported, matching
        // a reservation failure (e.g. the network call itself failing).

        await session.addClip(fileURL: tempURL)

        XCTAssertNotNil(session.addClipError)
        // Only the fixture's pre-existing slot (see EditorCommitSpy.editorVariant)
        // — the failed upload must not have staged anything new.
        XCTAssertEqual(session.document.clips.count, 1)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// KRI-125: the add-clip sheet closes as soon as a file is chosen, so the session — not the
    /// sheet — must report "adding" for the whole time, including while the file is still being
    /// fetched out of Photos (seconds for an iCloud asset). It also has to finish with no view attached.
    func testAddClipReportsAddingWhileTheFileIsStillBeingFetched() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")
        let latch = FetchLatch()

        let adding = Task { @MainActor in await session.addClip { await latch.wait(); return tempURL } }
        while !latch.isWaiting { await Task.yield() }

        XCTAssertTrue(session.isAddingClip, "the editor must show progress while Photos is still handing the file over")
        XCTAssertTrue(fake.uploadFileCalls.isEmpty, "nothing uploads until the file exists")

        latch.open()
        await adding.value

        XCTAssertFalse(session.isAddingClip)
        XCTAssertNil(session.addClipError)
        XCTAssertEqual(session.document.clips.count, 2)
    }

    /// iOS terminates an app whose background-task expiration handler does not end the task. With the
    /// sheet gone the user backgrounds the app mid-upload, so a handler that only "reports" would kill
    /// it and lose the editor's unsaved timeline. The handler must end the assertion, exactly once.
    func testAddClipsExpirationHandlerEndsTheBackgroundAssertionExactlyOnce() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let activity = RecordingEditorActivity()
        session.backgroundActivity = activity
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.addClipResult = AddClipResult(jobID: jobID.uuidString, clipIndex: 1, kind: "video")
        let latch = FetchLatch()

        let adding = Task { @MainActor in await session.addClip { await latch.wait(); return tempURL } }
        while !latch.isWaiting { await Task.yield() }
        XCTAssertEqual(activity.beginCount, 1)
        XCTAssertEqual(activity.endCount, 0)

        activity.expire()
        XCTAssertEqual(activity.endCount, 1, "the handler must end the assertion or iOS terminates the app")

        latch.open()
        await adding.value
        XCTAssertEqual(activity.endCount, 1, "the normal exit must not end it a second time")
    }

    func testAddClipBalancesItsBackgroundAssertionOnEveryExit() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let activity = RecordingEditorActivity()
        session.backgroundActivity = activity

        await session.addClip { throw AddClipSourceUnreadable() }   // fails before any upload

        XCTAssertEqual(activity.beginCount, 1)
        XCTAssertEqual(activity.endCount, 1)
    }

    /// The sheet is dismissed before `addClip` runs, so a silent early return would drop the user's pick
    /// with no signal at all.
    func testAddClipReportsWhyItCannotProceedInsteadOfSilentlyDroppingThePick() async throws {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        // Never loaded: no API, no job. `canEditTimeline` is false.
        await session.addClip { URL(fileURLWithPath: "/nonexistent.mp4") }

        XCTAssertNotNil(session.addClipError, "a dropped pick must say so")
        XCTAssertFalse(session.isAddingClip)
    }

    func testAddClipExplainsAnUnreadablePhotoWithoutTouchingTheTimeline() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)

        await session.addClip { throw AddClipSourceUnreadable() }

        XCTAssertEqual(session.addClipError, "This file couldn’t be read. Try Files or choose it again.")
        XCTAssertFalse(session.isAddingClip)
        XCTAssertTrue(fake.uploadFileCalls.isEmpty)
        XCTAssertEqual(session.document.clips.count, 1)
    }

    /// The sheet is gone by the time the upload runs, so the user will background the app. When the
    /// connection then drops, "This file couldn't be added" plus a raw URLError is no help.
    func testAddClipExplainsAnInterruptedUpload() async throws {
        let threadID = UUID(); let jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 0, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let tempURL = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).mp4")
        try Data("clip-bytes".utf8).write(to: tempURL)
        defer { try? FileManager.default.removeItem(at: tempURL) }
        fake.reserveUploadResult = UploadReservation(uploadURL: URL(string: "https://storage.example/signed-put")!, gcsPath: "users/u/generative/abc123def456/clip.mp4", kind: "video", contentType: "video/mp4", uploadHeaders: [:], purpose: nil, reservationID: nil, retentionExpiresAt: nil)
        fake.uploadFileError = URLError(.networkConnectionLost)

        await session.addClip(fileURL: tempURL)

        XCTAssertEqual(session.addClipError, "The upload was interrupted. Your edit is unchanged. Keep Kria open while it uploads and try again.")
        XCTAssertTrue(fake.addClipCalls.isEmpty, "a clip that never finished uploading must not be minted into the pool")
        XCTAssertEqual(session.document.clips.count, 1)
    }

    func testSaveCallsExplicitEditorCommitOnlyAfterEdit() async {
        let threadID = UUID(); let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2)
        let snapshot: [String: JSONValue] = ["schema_version": .number(2), "kind": .string("editor"), "editor_payload": .object(["base_generation": .string("generation-1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(false)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1", snapshot: snapshot, canUndo: false, createdAt: .now))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id); session.trimSelected(edge: .trailing, to: 1.5); await session.save()
        XCTAssertEqual(fake.commitCount, 1); XCTAssertEqual(fake.lastRequest?.baseGeneration, "generation-1"); XCTAssertEqual(session.saveState, .previewPending); XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testTimelineSaveAdoptsAuthoritativeServerTextWithoutMakingEditorDirty() async throws {
        let threadID = UUID(); let jobID = UUID(); let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2)
        let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object([
            "timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(false)])]),
            "text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])]),
        ])])]
        let capabilities: JSONValue = .object(["timeline": .bool(true), "text_elements": .bool(true)])
        let initialVariant: [String: JSONValue] = ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": capabilities]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: initialVariant,
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(clip.id); session.trimSelected(edge: .trailing, to: 1.5)
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": capabilities, "text_elements": .array([.object(["id": .string(textID), "text": .string("Server projection"), "start_s": .number(0), "end_s": .number(1)])])]

        await session.save()

        XCTAssertEqual(session.document.textElements.first?.text, "Server projection")
        XCTAssertFalse(session.isDirty(.text))
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testAuthoritativeTextRequiresExplicitArrayAndHonorsEmptyArray() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let capabilities: JSONValue = .object(["timeline": .bool(true), "text_elements": .bool(true)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": capabilities],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.markDirty(.timeline)
        await session.save()
        XCTAssertEqual(session.document.textElements.first?.text, "Original")

        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g3"), "editor_capabilities": capabilities, "text_elements": .array([])]
        session.markDirty(.timeline)
        await session.save()
        XCTAssertTrue(session.document.textElements.isEmpty)
    }

    func testSaveReconcilesServerTextWhilePreservingConcurrentTextUndoAndRedo() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let capabilities: JSONValue = .object(["timeline": .bool(true), "text_elements": .bool(true)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": capabilities],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: UUID(uuidString: textID)!, content: "Submitted")
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "editor_capabilities": capabilities, "text_elements": .array([.object(["id": .string(textID), "text": .string("Server"), "start_s": .number(0), "end_s": .number(1)])])]
        fake.suspendNextEditorVariant = true

        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.editorVariantIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(fake.editorVariantIsSuspended)
        session.updateTextContent(id: UUID(uuidString: textID)!, content: "Local")
        fake.resumeEditorVariant()
        await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Local")
        XCTAssertTrue(session.isDirty(.text))
        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "Server")
        session.redo()
        XCTAssertEqual(session.document.textElements.first?.text, "Local")
    }

    func testOtherLaneEditDuringSaveAdoptsServerTextAndStaysDirtyOnlyForThatLane() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString; let trackID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])]), "music_track_id": .string(trackID), "music_window": .object(["start_s": .number(0), "alignment": .string("preserve_cuts")])])])]
        let capabilities: JSONValue = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(true)])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": capabilities], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: MusicSelection(trackID: UUID(uuidString: trackID)!, title: "Music", start: 0), revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.markDirty(.timeline)
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "editor_capabilities": capabilities, "text_elements": .array([.object(["id": .string(textID), "text": .string("Server"), "start_s": .number(0), "end_s": .number(1)])])]
        fake.suspendNextEditorVariant = true
        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.editorVariantIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        session.setOriginalMixLevel(0.25)
        fake.resumeEditorVariant(); await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Server")
        XCTAssertFalse(session.isDirty(.text))
        XCTAssertTrue(session.isDirty(.mix))
        XCTAssertTrue(session.hasUnsavedChanges)
    }

    func testMismatchedGenerationPollDoesNotAdoptServerText() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": .object(["text_elements": .bool(true)])], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID); session.updateTextContent(id: UUID(uuidString: textID)!, content: "Submitted"); await session.save()
        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("old"), "render_status": .string("rendering"), "text_elements": .array([.object(["id": .string(textID), "text": .string("Stale")])])], generation: "g2"))
        XCTAssertEqual(session.document.textElements.first?.text, "Submitted")
    }

    func testFetchFailureLeavesSaveDurableAndPollReconcilesLater() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": .object(["text_elements": .bool(true)])], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID); session.updateTextContent(id: UUID(uuidString: textID)!, content: "Submitted")
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "editor_capabilities": .object(["text_elements": .bool(true)]), "text_elements": .array([.object(["id": .string(textID), "text": .string("Server")])])]
        fake.editorVariantError = .requestFailed(status: 503)
        await session.save()
        XCTAssertEqual(session.saveState, .previewPending)
        fake.editorVariantError = nil
        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("rendering"), "text_elements": .array([.object(["id": .string(textID), "text": .string("Server")])])], generation: "g2"))
        XCTAssertEqual(session.document.textElements.first?.text, "Server")
    }

    func testSaveDoesNotResumeOldResponseAfterTargetSwitchDuringVariantFetch() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": .object(["text_elements": .bool(true)])], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID); session.updateTextContent(id: UUID(uuidString: textID)!, content: "Submitted")
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "editor_capabilities": .object(["text_elements": .bool(true)]), "text_elements": .array([.object(["id": .string(textID), "text": .string("Server")])])]
        fake.suspendNextEditorVariant = true
        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.editorVariantIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        let replacementJobID = UUID(); let replacement = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d2", itemID: "new-item", variantKey: "new-variant", draftRevision: 1, snapshotHash: "h2", etag: "e2", baseJobID: replacementJobID.uuidString, baseGenerationID: "new-g1", snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("new-variant"), "render_generation_id": .string("new-g1"), "editor_capabilities": .object(["text_elements": .bool(true)])])
        await session.load(api: replacement, threadID: UUID())
        fake.resumeEditorVariant(); await saveTask.value

        XCTAssertEqual(session.visualItemID, "new-item")
        XCTAssertFalse(session.saveState == .previewPending, "the old save must not start a poll on the replacement target")
    }

    func testActiveTrimBaselineRebasesAuthoritativeTextDuringSave() async throws {
        let threadID = UUID(); let jobID = UUID(); let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "removed": .bool(false)])]), "text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "user_timeline": .object(["slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)])], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.beginTrim(clipID: clip.id, edge: .trailing); session.updateTrim(by: -0.25)
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "user_timeline": .object(["slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]), "text_elements": .array([.object(["id": .string(textID), "text": .string("Server")])])]
        await session.save()
        XCTAssertEqual(fake.editorVariantJobIDs.last, jobID)
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertEqual(session.document.textElements.first?.text, "Server")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        session.updateTrim(by: -0.25)
        XCTAssertEqual(session.document.clips.first?.durationS, 1.75, "cumulative drag must not apply twice after Save")
        XCTAssertFalse(session.canUndo)
        session.updateTrim(by: 0); session.endTrim()
        XCTAssertEqual(session.document.textElements.first?.text, "Server")
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertTrue(session.canUndo)
        session.undo()
        XCTAssertEqual(session.document.clips.first?.durationS, 1.75)
        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testDurableAcknowledgementInvalidatesPreviousPollDuringReplacementFetch() async throws {
        let threadID = UUID(); let jobID = UUID(); let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let capabilities: JSONValue = .object(["text_elements": .bool(true)])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": capabilities], commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil))
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [TextLayer(id: UUID(uuidString: textID)!, content: "Original", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1, serverSnapshot: snapshot))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: UUID(uuidString: textID)!, content: "First")
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g2"), "editor_capabilities": capabilities, "text_elements": .array([.object(["id": .string(textID), "text": .string("First")])])]
        await session.save()

        session.updateTextContent(id: UUID(uuidString: textID)!, content: "Second")
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        fake.authoritativeVariant = ["variant_id": .string("variant"), "render_generation_id": .string("g3"), "editor_capabilities": capabilities, "text_elements": .array([.object(["id": .string(textID), "text": .string("Third")])])]
        fake.suspendNextEditorVariant = true
        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.editorVariantIsSuspended { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(fake.editorVariantIsSuspended)
        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("rendering"), "text_elements": .array([.object(["id": .string(textID), "text": .string("Stale")])])], generation: "g2"))
        fake.resumeEditorVariant(); await saveTask.value
        XCTAssertEqual(session.document.textElements.first?.text, "Third")
    }

    func testPhoneSaveReconcilesAppOwnedRendererWithCreationProjectIdentity() async {
        let threadID = UUID(), jobID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now))
        fake.phoneDestination = true
        let renderSessions = DeviceRenderSessions(fetch: { job, variant in
            XCTAssertEqual(job, jobID)
            XCTAssertEqual(variant, "variant")
            fake.deviceFetchCount += 1
            throw APIError.requestFailed(status: 503)
        }, factory: { _, _ in throw APIError.unsupported })
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        session.useDeviceRendering(renderSessions)
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id)
        session.trimSelected(edge: .trailing, to: 1.5)
        await session.save()
        let key = DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "variant")
        XCTAssertEqual(session.deviceRenderKey, key)
        XCTAssertEqual(fake.deviceFetchCount, 1)
        XCTAssertEqual(fake.lastRequest?.guidedRevisionNumber, 7)
        XCTAssertEqual(renderSessions.presentations[key]?.phase, .needsAttention)
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    /// KRI-227: a save acknowledgement moves the document to the commit generation (`g2`) before the
    /// phone publishes under its own id (`published-g2`). Device narration is a generation-owned input,
    /// so the check must stay keyed on the generation the preview was prepared for.
    func testNarrationCheckStaysOnPreviewGenerationWhileSaveRenderPends() {
        XCTAssertEqual(NativeEditorSession.narrationOwnerGeneration(previewGeneration: "g1", documentGeneration: "g2"), "g1")
        XCTAssertEqual(NativeEditorSession.narrationOwnerGeneration(previewGeneration: nil, documentGeneration: "g2"), "g2")
    }

    func testDevicePublishedGenerationAndIdentityFenceReadyPreview() async throws {
        let threadID = UUID(), jobID = UUID()
        let request = deviceRenderRequest(jobID: jobID, revision: 1, digest: "a")
        let statusBox = SessionTestStatus(DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "published-g2"))
        let output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let renderSessions = DeviceRenderSessions(fetch: { _, _ in await statusBox.response }, factory: { _, request in
            try DeviceRenderCoordinator(directory: output, exporter: SessionTestExport(), sources: SessionTestSources(), publisher: SessionTestPublisher())
        })
        var authoritative = Self.variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: jobID.uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative,
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.useDeviceRendering(renderSessions)
        await session.load(api: fake, threadID: threadID)
        session.selectClip(session.draft.clips[0].id)
        session.trimSelected(edge: .trailing, to: 1.5)
        await session.save()

        let key = DeviceRenderKey(projectID: threadID, jobID: jobID, variantID: "variant")
        XCTAssertEqual(renderSessions.presentations[key]?.publishedGeneration, "published-g2")
        await statusBox.set(DeviceRenderStatusResponse(phase: "published", request: deviceRenderRequest(jobID: jobID, revision: 2, digest: "b"), publishedGeneration: "published-g2"))
        await renderSessions.reconcile(key, capabilities: .disabled)
        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("published-g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertFalse(session.showsEditApplied, "Edit applied only appears once the render lands")
        await statusBox.set(DeviceRenderStatusResponse(phase: "published", request: request, publishedGeneration: "published-g2"))
        await renderSessions.reconcile(key, capabilities: .disabled)
        session.trimSelected(edge: .trailing, to: 1.25)
        let localDuration = try XCTUnwrap(session.document.clips.first?.durationS)
        let refreshedSources = expectation(description: "Dirty device rebase invalidates generation-owned sources")
        fake.sourcePoolExpectation = refreshedSources
        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("published-g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertTrue(session.showsEditApplied, "KRI-227: a pending save that lands confirms the edit")
        XCTAssertEqual(session.document.revision.baseGeneration, "published-g2")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertEqual(session.document.clips.first?.durationS, localDuration, "Refreshing generation-owned inputs must retain the follow-up edit")
        await fulfillment(of: [refreshedSources], timeout: 3)
        XCTAssertEqual(fake.sourcePoolCallCount, 2, "The dirty rebase must not reuse sources, including narration, from the prior generation")

        // A subsequent save must use the published device generation.
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        await session.save()
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "published-g2")
        XCTAssertEqual(session.saveState, .previewPending)
    }

    func testSaveKeepsNewerSameSectionEditDirtyWhileCommitIsInFlight() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let response = EditorCommitResponse(
            ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil
        )
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: response,
            suspendNextCommit: true
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        XCTAssertTrue(session.canEdit(.text))
        session.updateTextContent(id: loadedTextID, content: "Submitted")
        XCTAssertTrue(session.hasUnsavedChanges)

        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(fake.commitIsSuspended, "the commit gate did not suspend in time")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Submitted"))

        session.updateTextContent(id: loadedTextID, content: "Newer local edit")
        fake.resumeCommit()
        await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Newer local edit")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)

        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Newer local edit"))
    }

    func testSaveKeepsInFlightRevertDirtyAgainstAcknowledgedServerValue() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let response = EditorCommitResponse(
            ok: true, generation: "g2",
            sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false),
            revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil
        )
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: response,
            suspendNextCommit: true
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        session.updateTextContent(id: loadedTextID, content: "Submitted")

        let saveTask = Task { await session.save() }
        for _ in 0..<100 where !fake.commitIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        session.updateTextContent(id: loadedTextID, content: "Original")
        fake.resumeCommit()
        await saveTask.value

        XCTAssertEqual(session.document.textElements.first?.text, "Original")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertTrue(session.hasUnsavedChanges)

        await session.save()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Original"))
    }

    func testPersistedCommitWithFailedEnqueueCanRetryRender() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: false, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        session.updateTextContent(id: loadedTextID, content: "Submitted")

        await session.save()

        XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely."))
        XCTAssertFalse(session.hasUnsavedChanges)
        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)

        await session.retryRender()

        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(Self.object(fake.lastRequest?.textElements?.first)?["text"], .string("Submitted"))
        XCTAssertEqual(session.saveState, .previewPending)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testPreviewPollIgnoresMismatchedGenerationAndAcceptsMatchingReadyGeneration() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "Submitted")
        await session.save()

        XCTAssertFalse(session.applyPreviewVariant(["render_generation_id": .string("old"), "render_status": .string("ready"), "output_url": .string("https://storage.example/old.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .previewPending)
        session.updateTextContent(id: textID, content: "Follow-up")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("ready"), "output_url": .string("https://storage.example/g2.mp4")], generation: "g2"))
        XCTAssertEqual(session.saveState, .saved)
        XCTAssertTrue(session.hasUnsavedChanges, "A follow-up edit must survive installation of the ready render")
    }

    func testFailedPreviewPollKeepsAcknowledgedSectionsRetryable() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 2, revisionHash: "revision-2", expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "Submitted")
        await session.save()

        XCTAssertTrue(session.applyPreviewVariant(["render_generation_id": .string("g2"), "render_status": .string("failed")], generation: "g2"))
        XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved, but its preview render failed. You can retry it safely."))

        fake.commitResponse = EditorCommitResponse(ok: true, generation: "g3", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: 3, revisionHash: "revision-3", expectedDuration: nil)
        await session.retryRender()
        XCTAssertEqual(fake.commitCount, 2)
        XCTAssertEqual(fake.lastRequest?.baseGeneration, "g2")
        XCTAssertEqual(session.saveState, .previewPending)
    }

    func testSaveConflictPreservesLocalDocumentSelectionAndUndo() async {
        let threadID = UUID()
        let clipID = UUID()
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clipID.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true)])],
            commitError: .conflict
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let selected = try! XCTUnwrap(session.timelineClips.first?.id)
        session.selectClip(selected)
        session.trimSelected(edge: .trailing, to: 1.5)
        let editedDocument = session.document

        await session.save()

        XCTAssertEqual(session.saveState, .conflict)
        XCTAssertEqual(session.document, editedDocument)
        XCTAssertEqual(session.selectedClipID, selected)
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
    }

    func testConflictRebaseKeepsLocalSectionsOnLatestServerBaseline() async {
        let threadID = UUID()
        let textID = UUID().uuidString
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object([
                "base_generation": .string("g1"),
                "sections": .object([
                    "text_elements": .array([.object([
                        "id": .string(textID), "text": .string("Original"),
                        "start_s": .number(0), "end_s": .number(2),
                    ])]),
                ]),
            ]),
        ]
        let latestVariant: [String: JSONValue] = [
            "render_generation_id": .string("g2"),
            "resolved_archetype": .string("narrated"),
            "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["text_elements": .bool(true)]),
            "text_elements": .array([.object([
                "id": .string(textID), "text": .string("Changed elsewhere"),
                "start_s": .number(0), "end_s": .number(2),
            ])]),
        ]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: latestVariant.merging(["render_generation_id": .string("g1")]) { _, new in new },
            commitError: .conflict
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateTextContent(id: textID, content: "My local edit")

        await session.save()
        XCTAssertEqual(session.saveState, .conflict)

        fake.authoritativeVariant = latestVariant
        let sourceRefresh = expectation(description: "Conflict rebase resolves new generation sources")
        fake.sourcePoolExpectation = sourceRefresh
        await session.rebaseAfterConflict()
        await fulfillment(of: [sourceRefresh], timeout: 3)
        XCTAssertEqual(fake.sourcePoolCallCount, 2)

        XCTAssertEqual(session.document.revision.baseGeneration, "g2")
        XCTAssertEqual(session.document.textElements.first?.text, "My local edit")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
        XCTAssertEqual(session.saveState, .idle)

        session.undo()
        XCTAssertEqual(session.document.textElements.first?.text, "Changed elsewhere")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testSaveRequestFailurePreservesLocalEdits() async {
        let threadID = UUID()
        let textID = "text-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["text_elements": .array([.object(["id": .string(textID), "text": .string("Original"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["text_elements": .bool(true)])],
            commitError: .offline
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        let loadedTextID = try! XCTUnwrap(session.document.textElements.first?.id)
        XCTAssertTrue(session.deleteText(id: loadedTextID))

        await session.save()

        XCTAssertEqual(session.saveState, .failed("Kria couldn’t complete that request. Check your connection and try again."))
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertEqual(fake.lastRequest?.deletions, [EditorDeletion(kind: "text", id: textID)])
        XCTAssertEqual(session.document.deletions, [EditorDeletion(kind: "text", id: textID)], "a failed request must retain the deletion intent for the next save")
        XCTAssertTrue(session.hasUnsavedChanges)
        XCTAssertTrue(session.canUndo)
    }

    func testSaveUsesExpectedRenderedDurationUntilPreviewRefreshes() async {
        let threadID = UUID()
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 2, trimIn: 0, trimOut: 2)
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string(clip.id.uuidString), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2)])])])])]
        let response = EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: 1.5)
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            commitResponse: response
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [clip], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 1))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(try! XCTUnwrap(session.timelineClips.first?.id))
        session.trimSelected(edge: .trailing, to: 1.25)
        await session.save()

        XCTAssertEqual(session.duration, 1.5, accuracy: 0.0001)
        XCTAssertEqual(session.saveState, .previewPending)
    }

    func testCanonicalDocumentTracksTextEditAndDirtySection() {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        session.addText(content: "Hook")
        XCTAssertEqual(session.document.textElements.first?.text, "Hook")
        XCTAssertTrue(session.isDirty(.text))
        session.undo()
        XCTAssertTrue(session.document.textElements.isEmpty)
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testLongTextEditDoesNotChangeTransportDuration() {
        let textID = UUID()
        let clip = EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4, trimIn: 0, trimOut: 4)
        let text = TextLayer(id: textID, content: "Short", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [clip], text: [text], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        let originalDuration = session.duration

        session.updateTextContent(id: textID, content: String(repeating: "Long text ", count: 100))

        XCTAssertEqual(session.duration, originalDuration, accuracy: 0.0001)
    }

    func testUndoingTrimRestoresRenderedDurationBoundary() async {
        let threadID = UUID()
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["timeline_slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4)])])])])]
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "initial", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now),
            authoritativeVariant: Self.variant(duration: 4, generation: "g1")
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.selectClip(try! XCTUnwrap(session.timelineClips.first?.id))

        session.trimSelected(edge: .trailing, to: 3)
        XCTAssertEqual(session.duration, 3, accuracy: 0.0001)

        session.undo()
        XCTAssertEqual(session.duration, 4, accuracy: 0.0001)
    }

    func testCaptionMetadataUsesCaptionMetaCommitSection() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "g1", snapshot: ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["captions_enabled": .bool(false), "caption_style": .string("word")])])], canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]), "captions_enabled": .bool(false), "caption_style": .string("word")],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: true, timeline: false, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.setCaptionStyle("sentence")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionMeta?["style"], JSONValue.string("sentence"))
        XCTAssertNil(fake.lastRequest?.captionCues)
        XCTAssertFalse(session.isDirty(.captionMeta))
    }

    // KRI-110: guided-story captions are caption_cue-tagged TextElements, not
    // caption_cues rows. canEditCaptions must open for them via the
    // capability the guided revision actually advertises, without extending
    // the narrated/subtitled archetype allowlist.
    func testGuidedStoryCaptionTaggedTextElementsEnableCaptionEditing() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertEqual(session.document.captionUnits.map(\.id), ["narration-caption-1"])
    }

    func testGuidedStoryWithoutCaptionTaggedTextElementsLeavesCaptionsClosed() async {
        let threadID = UUID()
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertFalse(session.canEditCaptions)
    }

    func testGuidedStoryCaptionEditRoutesThroughTextElementsNotCaptionCues() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ],
            commitResponse: EditorCommitResponse(ok: true, generation: "g2", sections: EditorCommitSections(textElements: true, captionMeta: false, timeline: false, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        XCTAssertTrue(session.isDirty(.text))
        XCTAssertFalse(session.isDirty(.captions))
        // Timing is pinned server-side for guided captions — a timing-only
        // call with no text must be a no-op, not silently accepted.
        session.updateCaptionCue(id: "narration-caption-1", startS: 5)
        XCTAssertEqual(session.document.textElements.first(where: { $0.id == "narration-caption-1" })?.startS, 0)
        await session.save()
        XCTAssertNotNil(fake.lastRequest?.textElements)
        XCTAssertNil(fake.lastRequest?.captionCues)
    }

    // KRI-110: the render compiler now overlays caption_meta onto
    // caption_cue-tagged text elements' raw fields (applyingCaptionMeta),
    // so appearance is open regardless of render destination — cloud or
    // phone-pilot, the caption's style now actually reaches the burn.
    func testCaptionAppearanceOpenForTextLaneCaptionsRegardlessOfRenderDestination() async {
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        for renderDestination: JSONValue? in [nil, .string("device")] {
            let threadID = UUID()
            var variant: [String: JSONValue] = [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
            if let renderDestination { variant["render_destination"] = renderDestination }
            let fake = EditorCommitSpy(
                draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
                authoritativeVariant: variant
            )
            let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
            await session.load(api: fake, threadID: threadID)
            XCTAssertTrue(session.canEditCaptions)
            XCTAssertTrue(session.canEditCaptionAppearance)
        }
    }

    // KRI-216: phone (`render_destination == "device"`) renders never carry
    // `base_video_path`, so the legacy archetype allowlist in
    // `configureCapabilities` always read a device subtitled variant as
    // caption-closed. The server now sends explicit `caption_cues`/
    // `caption_meta` capability objects, which must take priority over that
    // heuristic and open both line editing and appearance editing.
    func testDeviceSubtitledVariantWithExplicitCaptionCapabilitiesOpensCaptionEditing() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object([
                    "caption_cues": .object(["editable": .bool(true)]),
                    "caption_meta": .object(["editable": .bool(true)]),
                    "caption_editor_style": .bool(true),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertTrue(session.canEditCaptionLines)
        XCTAssertTrue(session.canEditCaptionMeta)
        XCTAssertTrue(session.canEditCaptionAppearance)
    }

    // KRI-280: a phone Narrated (recorded voiceover) render gets the same
    // explicit caption capabilities as phone Talking, so the Captions panel
    // opens for line edits and Style/Settings, and a Save carries both
    // sections to the server's phone recompile.
    func testDeviceNarratedVariantOpensCaptionEditingAndSavesBothSections() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("First we pack"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "narrated", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("narrated"),
                "caption_cues": .array([cue]),
                "voiceover_caption_style": .string("sentence"),
                "editor_capabilities": .object([
                    "text_elements": .bool(false),
                    "caption_cues": .object(["editable": .bool(true)]),
                    "caption_meta": .object(["editable": .bool(true)]),
                    "caption_editor_style": .bool(true),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertTrue(session.canEditCaptionLines)
        XCTAssertTrue(session.canEditCaptionMeta)
        XCTAssertTrue(session.canEditCaptionAppearance)

        session.updateCaptionCue(id: "cue-1", text: "First we pack light")
        session.setCaptionStyle("word")
        XCTAssertTrue(session.isDirty(.captions))
        XCTAssertTrue(session.isDirty(.captionMeta))
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionCues?.first?.objectValue?["text"], .string("First we pack light"))
        XCTAssertEqual(fake.lastRequest?.captionMeta?["style"], .string("word"))
    }

    // KRI-216: cues open, meta closed — line edits stay available while the
    // Style/Settings writes (`caption_meta`) are locked, even though the
    // coarse `canEditCaptions` is true from the cues lane alone.
    func testExplicitCaptionCuesOnlyKeepsCaptionMetaLocked() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object([
                    "caption_cues": .object(["editable": .bool(true)]),
                    "caption_meta": .object(["editable": .bool(false), "reason": .string("device_render_locked")]),
                    "caption_editor_style": .bool(false),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertTrue(session.canEditCaptionLines)
        XCTAssertFalse(session.canEditCaptionMeta)
        XCTAssertFalse(session.canEditCaptionAppearance)
    }

    // KRI-216: Show captions and the display style write `caption_meta`, not
    // the appearance keys, so they stay open without `caption_editor_style`.
    func testCaptionMetaOpensWithoutCaptionEditorStyle() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object([
                    "caption_cues": .object(["editable": .bool(true)]),
                    "caption_meta": .object(["editable": .bool(true)]),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptionMeta)
        XCTAssertFalse(session.canEditCaptionAppearance)
        session.setCaptionEnabled(false)
        XCTAssertEqual(session.document.captionMeta["enabled"], .bool(false))
    }

    // KRI-216: the same device subtitled shape, but the server explicitly
    // closes both lanes — must not fall back to any legacy heuristic.
    func testDeviceSubtitledVariantWithExplicitCaptionCapabilitiesBothFalseClosesCaptionEditing() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object([
                    "caption_cues": .object(["editable": .bool(false), "reason": .string("device_render_locked")]),
                    "caption_meta": .object(["editable": .bool(false), "reason": .string("device_render_locked")]),
                ]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertFalse(session.canEditCaptions)
        XCTAssertFalse(session.canEditCaptionLines)
        XCTAssertFalse(session.canEditCaptionMeta)
        XCTAssertFalse(session.canEditCaptionAppearance)
    }

    // KRI-216: a legacy cloud variant that never sends the new capability
    // keys must keep working off the old base_video_path heuristic.
    func testLegacyCloudVariantWithBaseVideoPathAndNoExplicitCaptionKeysStillOpensCaptionEditing() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("subtitled"),
                "base_video_path": .string("base.mp4"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        XCTAssertTrue(session.canEditCaptionLines)
        XCTAssertTrue(session.canEditCaptionMeta)
    }

    // KRI-216: a device variant from an old server that never sends the new
    // capability keys must stay closed (the pre-fix, still-correct behavior
    // for a server that genuinely cannot honor a phone caption edit).
    func testDeviceVariantWithNoExplicitCaptionKeysFromOldServerClosesCaptionEditing() async {
        let threadID = UUID()
        let cue: JSONValue = .object([
            "id": .string("cue-1"), "text": .string("Hello"), "start_s": .number(0), "end_s": .number(2),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "render_destination": .string("device"),
                "resolved_archetype": .string("subtitled"),
                "caption_cues": .array([cue]),
                "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true)]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertFalse(session.canEditCaptions)
        XCTAssertFalse(session.canEditCaptionLines)
        XCTAssertFalse(session.canEditCaptionMeta)
    }

    // KRI-110: a variant that never carries the narrated-only
    // `captions_enabled` field (guided_story never does) must not load as
    // captions-off. The backend treats missing as enabled; deriving `false`
    // here manufactured an explicit `caption_meta.enabled == false` that the
    // render compiler honors by hiding every caption in the on-device preview.
    func testVariantWithoutCaptionsEnabledFieldLoadsAsCaptionsOn() async {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(2), "role": .string("generative_sequence"),
            "source_params": .object(["source": .string("caption_cue")]),
        ])
        for captionsEnabled: JSONValue? in [nil, .null] {
            var variant: [String: JSONValue] = [
                "resolved_archetype": .string("guided_story"),
                "editor_capabilities": .object(["text_elements": .bool(true)]),
                "text_elements": .array([captionElement]),
            ]
            if let captionsEnabled { variant["captions_enabled"] = captionsEnabled }
            let fake = EditorCommitSpy(
                draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
                authoritativeVariant: variant
            )
            let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
            await session.load(api: fake, threadID: threadID)
            XCTAssertNotEqual(session.document.captionMeta["enabled"], .bool(false), "captions_enabled=\(String(describing: captionsEnabled)) must not manufacture an explicit captions-off")
            XCTAssertTrue(session.draft.captions.enabled)
            XCTAssertFalse(session.hasUnsavedChanges)
        }
        // An explicit server-side off still loads as off.
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "captions_enabled": .bool(false)]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: true, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertEqual(session.document.captionMeta["enabled"], .bool(false))
        XCTAssertFalse(session.draft.captions.enabled)
    }

    // KRI-110: editing a guided-story caption's text must reach the video
    // the canvas is actually showing, through the same live text update any
    // ordinary text edit takes. Uses the exact element shape guided_story.py
    // persists (word_timings, static effect, custom position, pinned
    // source_params).
    func testGuidedStoryCaptionTextEditReachesTheDisplayedSourcePreview() async throws {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(1.5), "role": .string("generative_sequence"),
            "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.82),
            "font_family": .string("Inter-Bold"), "size_px": .number(58), "color": .string("#FFFFFF"),
            "highlight_color": .string("#FFFFFF"), "stroke_width": .number(6), "shadow_enabled": .bool(true),
            "effect": .string("static"), "alignment": .string("center"), "max_width_frac": .number(0.84),
            "word_timings": .array([
                .object(["text": .string("Spoken"), "start_s": .number(0), "end_s": .number(0.7)]),
                .object(["text": .string("words"), "start_s": .number(0.7), "end_s": .number(1.5)]),
            ]),
            "source_params": .object(["source": .string("caption_cue"), "key": .string("0"), "identity": .string("pinned-narration-caption-0")]),
        ])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: [
                "resolved_archetype": .string("guided_story"),
                "render_generation_id": .string("g1"),
                "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
                "text_elements": .array([captionElement]),
                "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]),
            ]
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        XCTAssertTrue(session.canEditCaptions)
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: sourceURL)
        XCTAssertTrue(session.hasSourcePreview)
        let before = try XCTUnwrap(session.displayedSourcePreviewRecipe)
        XCTAssertEqual(before.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }, ["Spoken words"])

        session.beginTransaction()
        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        session.endTransaction()
        XCTAssertEqual(session.document.captionUnits.first?.text, "Edited words")

        for _ in 0..<200 {
            if session.displayedSourcePreviewRecipe?.textLayers.first?.runs.map(\.text).joined(separator: " ") == "Edited words" { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertEqual(session.sourcePreviewState, .ready, "A caption text edit must never fail the source preview over to the stale finished render")
        let after = try XCTUnwrap(session.displayedSourcePreviewRecipe, "The canvas must still be showing the editable source composition after a caption edit")
        XCTAssertEqual(after.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }, ["Edited words"])
    }

    /// A loaded guided-story session with one caption element and, when
    /// `finishedRender` is given, a completed cloud render installed as the
    /// finished-render fallback — the state a real production video opens in.
    private func loadGuidedStorySession(finishedRender: URL? = nil) async -> NativeEditorSession {
        let threadID = UUID()
        let captionElement: JSONValue = .object([
            "id": .string("narration-caption-1"), "text": .string("Spoken words"),
            "start_s": .number(0), "end_s": .number(1.5), "role": .string("generative_sequence"),
            "position": .string("custom"), "x_frac": .number(0.5), "y_frac": .number(0.82),
            "font_family": .string("Inter-Bold"), "size_px": .number(58), "color": .string("#FFFFFF"),
            "effect": .string("static"), "alignment": .string("center"), "max_width_frac": .number(0.84),
            "source_params": .object(["source": .string("caption_cue"), "key": .string("0"), "identity": .string("pinned-narration-caption-0")]),
        ])
        var variant: [String: JSONValue] = [
            "resolved_archetype": .string("guided_story"),
            "render_generation_id": .string("g1"),
            "render_status": .string("ready"),
            "editor_capabilities": .object(["text_elements": .bool(true), "caption_editor_style": .bool(true)]),
            "text_elements": .array([captionElement]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])]),
        ]
        if let finishedRender { variant["output_url"] = .string(finishedRender.absoluteString) }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        return session
    }

    private func displayedCaptionTexts(_ session: NativeEditorSession) -> [String]? {
        session.displayedSourcePreviewRecipe?.textLayers.map { $0.runs.map(\.text).joined(separator: " ") }
    }

    private func waitForDisplayedCaptionTexts(_ session: NativeEditorSession, _ expected: [String]) async throws {
        for _ in 0..<200 where displayedCaptionTexts(session) != expected {
            try await Task.sleep(for: .milliseconds(25))
        }
    }

    // KRI-110: retyping a caption passes through the empty string. That is
    // nothing to draw, not a broken edit — the live composition must stay on
    // the canvas rather than fail over to the finished cloud render.
    func testCaptionClearedMidEditKeepsTheLiveCompositionOnTheCanvas() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession(finishedRender: sourceURL)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])

        session.beginTransaction()
        session.updateCaptionCue(id: "narration-caption-1", text: "")
        try await waitForDisplayedCaptionTexts(session, [])
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertEqual(displayedCaptionTexts(session), [], "An empty caption draws nothing; it must not take the composition down")

        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        session.endTransaction()
        try await waitForDisplayedCaptionTexts(session, ["Edited words"])
        XCTAssertEqual(displayedCaptionTexts(session), ["Edited words"])
    }

    // KRI-110: a compile failure hands the canvas to the finished render as a
    // fallback. When the very next edit compiles again, the recovered
    // composition must take the canvas back — otherwise the user keeps
    // watching the stale cloud render while every edit lands off-screen.
    func testRecoveredCompositionTakesTheCanvasBackFromTheFinishedRenderFallback() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession(finishedRender: sourceURL)
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])

        // Over the layout's 500-character ceiling: the one text-element input
        // that still hard-fails a compile.
        session.updateCaptionCue(id: "narration-caption-1", text: String(repeating: "x", count: 600))
        for _ in 0..<200 where !session.isShowingRenderedFallback {
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertTrue(session.isShowingRenderedFallback, "precondition: the failure fell back to the finished render")
        XCTAssertNil(session.displayedSourcePreviewRecipe)

        session.updateCaptionCue(id: "narration-caption-1", text: "Edited words")
        try await waitForDisplayedCaptionTexts(session, ["Edited words"])
        XCTAssertEqual(session.sourcePreviewState, .ready)
        XCTAssertFalse(session.isShowingRenderedFallback)
        XCTAssertEqual(displayedCaptionTexts(session), ["Edited words"], "The canvas must show the recovered composition, not the stale render")
    }

    // KRI-110: a slider drag delivers many samples per second; each one used
    // to recompile and repaint the entire composition (~170 caption layouts
    // on a real guided story). Samples inside one transaction coalesce into
    // a single rebuild once the finger pauses, and the result is the final
    // value.
    func testGestureSamplesInsideOneTransactionCoalesceIntoOneRebuild() async throws {
        let sourceURL = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let session = await loadGuidedStorySession()
        await session.prepareFixtureSourcePreview(url: sourceURL)
        try await waitForDisplayedCaptionTexts(session, ["Spoken words"])
        let compiles = session.sourcePreviewCompileCount

        session.beginTransaction()
        for size in stride(from: 60, through: 74, by: 2) {
            session.setCaptionSize(Double(size))
            try await Task.sleep(for: .milliseconds(10))
        }
        session.endTransaction()
        try await Task.sleep(for: .milliseconds(NativeEditorSession.previewCoalesceMilliseconds * 4))
        for _ in 0..<100 where session.sourcePreviewCompileCount == compiles {
            try await Task.sleep(for: .milliseconds(25))
        }

        XCTAssertEqual(session.sourcePreviewCompileCount, compiles + 1, "Eight samples inside one drag must produce one rebuild")
        let run = try XCTUnwrap(session.displayedSourcePreviewRecipe?.textLayers.first?.runs.first)
        XCTAssertEqual(run.fontSize, 74)
    }

    func testTypedTextMutationsKeepWireKeysAndOneGestureUndo() {
        let textID = UUID()
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "old", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: draft)
        session.beginTransaction()
        session.updateTextContent(id: textID, content: "new")
        session.updateTextTiming(id: textID, startS: 1, endS: 3)
        session.setTextSize(id: textID.uuidString, sizePX: 88)
        session.setTextWidth(id: textID.uuidString, width: 0)
        session.setTextAlignment(id: textID.uuidString, alignment: "center")
        session.setTextAnimation(id: textID.uuidString, animation: "pop-in")
        session.setTextColor(id: textID.uuidString, color: "#fff")
        session.setTextHighlightColor(id: textID.uuidString, color: "#0f0")
        session.setTextShadow(id: textID.uuidString, enabled: true)
        session.setTextStroke(id: textID.uuidString, width: 4)
        session.setTextBehindSubject(id: textID.uuidString, behind: true)
        session.setTextPosition(id: textID, x: 0.2, y: 0.8)
        session.endTransaction()
        let payload = Self.object(session.document.encodeSnapshot()["editor_payload"]); let sections = Self.object(payload?["sections"])
        let text = Self.object(Self.array(sections?["text_elements"]).first)
        XCTAssertEqual(text?["text"], .string("new")); XCTAssertEqual(text?["start_s"], .number(1)); XCTAssertEqual(text?["end_s"], .number(3))
        XCTAssertEqual(text?["size_px"], .number(88)); XCTAssertEqual(text?["max_width_frac"], .number(0.2)); XCTAssertEqual(text?["effect"], .string("pop-in")); XCTAssertNil(text?["font_size_px"]); XCTAssertNil(text?["width"]); XCTAssertNil(text?["animation"])
        XCTAssertEqual(text?["behind_subject"], .bool(true)); XCTAssertTrue(session.canUndo)
        session.undo(); XCTAssertFalse(session.hasUnsavedChanges)
        session.redo(); session.beginTransaction(); session.setTextColor(id: textID.uuidString, color: "#000"); session.endTransaction(); XCTAssertFalse(session.canRedo)
    }

    func testUndoHistoryIsBoundedForLongEditingSessions() {
        let textID = UUID()
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "Initial", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
        let session = NativeEditorSession(draft: draft)

        for index in 0..<150 {
            session.updateTextContent(id: textID, content: "Edit \(index)")
        }

        var undoCount = 0
        while session.canUndo {
            session.undo()
            undoCount += 1
        }
        XCTAssertEqual(undoCount, 100)
    }

    func testCaptionCueMetadataAndMusicMutationsUseSeparateDirtySections() async {
        let threadID = UUID(); let trackID = UUID(); let cueID = "cue-1"
        let snapshot: [String: JSONValue] = ["editor_payload": .object(["base_generation": .string("g1"), "sections": .object(["caption_meta": .object(["enabled": .bool(true), "style": .string("word")]), "caption_cues": .array([.object(["id": .string(cueID), "start_s": .number(0), "end_s": .number(1), "text": .string("old")])]), "music_track_id": .string(trackID.uuidString), "music_window": .object(["start_s": .number(0), "alignment": .string("preserve_cuts")]), "audio_mix": .object(["music_level": .number(0.5), "original_level": .number(1)])])])]
        let response = EditorCommitResponse(ok: false, generation: "g2", sections: EditorCommitSections(textElements: false, captionMeta: true, timeline: false, mix: true, captionCues: true, music: true), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: snapshot, canUndo: false, createdAt: .now), authoritativeVariant: ["resolved_archetype": .string("subtitled"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(true)])], commitResponse: response)
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        session.updateCaptionCue(id: cueID, text: "new", startS: 0.25, endS: 1.25); session.setCaptionHighlightColor("#0f0")
        session.setMusicWindow(startS: 2, alignment: "resync_beats"); session.setMusicLevel(0.8); session.setOriginalMixLevel(0.6)
        XCTAssertTrue(session.isDirty(.captions)); XCTAssertTrue(session.isDirty(.captionMeta)); XCTAssertTrue(session.isDirty(.music)); XCTAssertTrue(session.isDirty(.mix))
        await session.save()
        XCTAssertEqual(fake.lastRequest?.captionCues?.count, 1); XCTAssertEqual(fake.lastRequest?.captionMeta?["highlight_color"], .string("#0f0")); XCTAssertEqual(fake.lastRequest?.musicWindow?.startS, 2); XCTAssertEqual(fake.lastRequest?.musicWindow?.alignment, .resyncBeats); XCTAssertEqual(fake.lastRequest?.mix?["music_level"], .number(0.8)); XCTAssertEqual(fake.lastRequest?.mix?["original_level"], .number(0.6)); XCTAssertEqual(session.saveState, .renderRetryNeeded("Your edit is saved. Its preview render did not start, so you can retry it safely."))
    }

    func testCapabilityReasonIsExposedAndDisablesTypedMutation() {
        let textID = UUID(); let snapshot: [String: JSONValue] = ["editor_capabilities": .object(["text_elements": .object(["editable": .bool(false), "reason": .string("renderer locked")])]), "editor_payload": .object(["sections": .object(["text_elements": .array([.object(["id": .string(textID.uuidString), "text": .string("old"), "start_s": .number(0), "end_s": .number(1)])])])])]
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [TextLayer(id: textID, content: "old", position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces")], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        XCTAssertEqual(session.capabilityReason("text_elements"), "renderer locked"); XCTAssertFalse(session.canEdit(.text)); session.updateTextContent(id: textID, content: "new"); XCTAssertEqual(session.document.textElements.first?.text, "old")
    }

    func testTimedLanesMutateWithOneGestureAndHonorWireKeys() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "sfx": .bool(true), "overlays": .bool(true), "visual_blocks": .bool(true),
                "motion_scenes": .bool(true), "camera_effects": .bool(true), "carousel": .bool(true), "layer_order": .bool(true),
            ]),
            "editor_payload": .object(["sections": .object([
                "sound_effects": .array([.object(["id": .string("sfx"), "at_s": .number(1), "gain": .number(1), "trim_start_s": .number(0), "trim_end_s": .number(0.5), "duration_s": .number(1)])]),
                "media_overlays": .array([.object(["id": .string("overlay"), "start_s": .number(0), "end_s": .number(2), "x_frac": .number(0.5), "y_frac": .number(0.5), "scale": .number(0.5), "z": .number(1)])]),
                "visual_blocks": .array([.object(["id": .string("visual"), "kind": .string("media"), "start_s": .number(0), "end_s": .number(2)])]),
                "motion_scenes": .array([.object(["id": .string("motion"), "start_frame": .number(0), "end_frame_exclusive": .number(60), "preset_id": .string("push"), "runtime_hash": .string("runtime")])]),
                "motion_runtime_hash": .string("runtime"),
                "camera_effects": .array([.object(["id": .string("camera"), "start_s": .number(0), "end_s": .number(2)])]),
                "carousel_moment": .object(["position": .string("intro")]),
            ])]),
        ]
        let draft = EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot)
        let session = NativeEditorSession(draft: draft)
        session.beginTransaction()
        session.setSoundEffectTiming(id: "sfx", atS: 2)
        session.setSoundEffectTrim(id: "sfx", trimStartS: 0.1, trimEndS: 0.8)
        session.setSoundEffectGain(id: "sfx", gain: 0.75)
        session.setMediaOverlayPosition(id: "overlay", x: 2, y: -1)
        session.setMediaOverlayScale(id: "overlay", scale: 2)
        session.setMediaOverlayDisplayMode(id: "overlay", mode: "fullscreen")
        session.setCameraEffectIntensity(id: "camera", intensity: 0.8)
        session.setCameraEffectEasing(id: "camera", easing: "sine_pulse")
        session.setCarouselPosition("outro")
        session.endTransaction()
        XCTAssertEqual(session.document.soundEffects.first?.pointS, 2)
        XCTAssertEqual(session.document.soundEffects.first?.raw["gain"], .number(0.75))
        XCTAssertEqual(session.document.mediaOverlays.first?.raw["x_frac"], .number(1))
        XCTAssertEqual(session.document.mediaOverlays.first?.raw["scale"], .number(1))
        XCTAssertEqual(session.document.cameraEffects.first?.raw["intensity"], .number(0.08))
        XCTAssertEqual(session.document.cameraEffects.first?.raw["easing"], .string("sine_pulse"))
        XCTAssertEqual(session.document.carouselMoment?["position"], .string("outro"))
        XCTAssertTrue(session.canUndo)
    }

    func testMotionRuntimeMismatchIsAlwaysReadOnly() {
        let snapshot: [String: JSONValue] = [
            "editor_capabilities": .object([
                "motion_scenes": .object(["editable": .bool(false), "reason": .string("motion_runtime_mismatch")]),
                "motion_runtime_hash": .string("current-runtime"),
            ]),
            "editor_payload": .object(["sections": .object([
                "motion_runtime_hash": .string("editor"),
                "motion_scenes": .array([.object(["id": .string("motion"), "start_frame": .number(0), "end_frame_exclusive": .number(60), "preset_id": .string("route_trace")])]),
            ])]),
        ]
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0, serverSnapshot: snapshot))
        XCTAssertTrue(session.isMotionSceneReadOnly(id: "motion"))
        XCTAssertNotNil(session.motionRuntimeMismatchReason(id: "motion"))
        session.setMotionSceneTiming(id: "motion", startS: 1)
        XCTAssertEqual(session.document.motionScenes.first?.startS, 0)
        XCTAssertEqual(session.motionRuntimeMismatchReason(id: "motion"), "motion_runtime_mismatch")
    }


    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }

    private static func variant(duration: Double, generation: String) -> [String: JSONValue] {
        [
            "variant_id": .string("initial"),
            "render_generation_id": .string(generation),
            "render_status": .string("ready"),
            "duration_s": .number(duration),
            "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "resolved_archetype": .string("narrated"),
            "base_video_path": .string("base.mp4"),
            "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]),
            "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(duration), "source_duration_s": .number(4), "removed": .bool(false)])])]),
        ]
    }

    private static func devicePlacementSession() async -> (NativeEditorSession, BackgroundUploadCoordinator, EditorSourceRegistrationTarget) {
        let (session, uploads, target, _) = await devicePlacementSessionWithSpy()
        return (session, uploads, target)
    }

    private static func devicePlacementSessionWithSpy() async -> (NativeEditorSession, BackgroundUploadCoordinator, EditorSourceRegistrationTarget, EditorCommitSpy) {
        let threadID = UUID()
        var authoritative = variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string("device")
        authoritative["editor_revision_number"] = .number(7)
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true),
            "phone_editor_media": .object(["enabled": .bool(true), "source_registration": .bool(true),
                "visual_kinds": .array([.string("image")])]),
        ])
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant",
            draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1",
            snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: authoritative)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        let uploads = BackgroundUploadCoordinator(api: fake, defaultsKey: "placement-\(UUID().uuidString)", sessionConfiguration: .ephemeral)
        session.useMediaUploads(uploads)
        return (session, uploads, .init(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: "generation-1", guidedRevisionNumber: 7, sourceKind: .footage), fake)
    }

    /// Loads one clip slot (plus `extras`) under the server's per-clip
    /// `clips.*` crop, speed and look capabilities.
    private static func footageSession(destination: String, operationsEditable: Bool, slot extras: [String: JSONValue] = [:]) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        var authoritative = variant(duration: 2, generation: "generation-1")
        authoritative["render_destination"] = .string(destination)
        let operation: JSONValue = .object(["editable": .bool(operationsEditable)])
        authoritative["editor_capabilities"] = .object([
            "timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false),
            "clips": .object(["source_crop": operation, "playback_rate": operation, "looks": operation]),
        ])
        var slot: [String: JSONValue] = ["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)]
        slot.merge(extras) { _, extra in extra }
        authoritative["user_timeline"] = .object(["slots": .array([.object(slot)])])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }
}

private func deviceRenderRequest(jobID: UUID, revision: Int, digest: String) -> DeviceRenderRequest {
    DeviceRenderRequest(
        identity: DeviceRenderIdentity(jobID: jobID, variantID: "variant", recipeRevision: revision, recipeDigest: String(repeating: digest, count: 64)),
        recipe: KriaMediaEngine.EditRecipe(
            assets: [MediaAsset(id: "source", relativePath: "source")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 2)])]
        )
    )
}

private actor SessionTestExport: LocalExporting {
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}

private struct SessionTestSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}

private actor SessionTestPublisher: DeviceRenderPublishing {
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
}

private actor SessionTestStatus {
    private(set) var response: DeviceRenderStatusResponse
    init(_ response: DeviceRenderStatusResponse) { self.response = response }
    func set(_ response: DeviceRenderStatusResponse) { self.response = response }
}

final class EditorCommitSpy: KriaAPIClient, @unchecked Sendable {
    var draftSnapshot: DraftSnapshot
    var draftError: APIError?
    let openReceipt: OpenInEditorResponse?
    var authoritativeVariant: [String: JSONValue]?
    var editorVariantError: APIError?
    var commitResponse: EditorCommitResponse?
    let commitError: APIError?
    /// Any other error `editorCommit` should throw (e.g. a typed 422 `EditorSaveError`).
    var commitThrow: Error?
    var refreshedThread: CreationThread?
    private(set) var commitIsSuspended = false
    private var commitContinuation: CheckedContinuation<Void, Never>?
    private var commitResumeRequested = false
    private var suspendNextCommit: Bool
    var suspendNextEditorVariant = false
    private(set) var editorVariantIsSuspended = false
    private var editorVariantContinuation: CheckedContinuation<Void, Never>?
    private var editorVariantResumeRequested = false
    var suspendNextSourcePool = false
    private(set) var sourcePoolIsSuspended = false
    private var sourcePoolContinuation: CheckedContinuation<Void, Never>?
    private var sourcePoolResumeRequested = false
    var phoneDestination = false
    var creationMode: CreationMode?
    /// What `editorSource` polls return (nil ⇒ the default unsupported error).
    var editorSourceResponse: EditorSourceRegistrationResponse?
    var deviceFetchCount = 0
    var commitCount = 0
    var lastRequest: EditorCommitRequest?
    var openedJobID: UUID?
    var lastItemID: String?
    var draftCallCount = 0
    var lastVariantID: String?
    var editorVariantJobIDs: [UUID] = []
    var supersededJobIDs: Set<UUID> = []
    var projectCallCount = 0
    var editorVariantsCallCount = 0
    var sourcePoolCallCount = 0
    var sourcePoolExpectation: XCTestExpectation?
    var sourcePoolResult: NativeEditorSourcePool?
    var deviceRenderResponse: DeviceRenderStatusResponse?
    var deviceRenderCallCount = 0
    var reserveUploadResult: UploadReservation?
    /// Makes the PUT itself fail (after a successful reservation), e.g. a dropped connection.
    var uploadFileError: (any Error)?
    var addClipResult: AddClipResult?
    var addClipCalls: [(jobID: UUID, gcsPath: String)] = []
    var uploadFileCalls: [(reservation: UploadReservation, fileURL: URL)] = []
    init(draftSnapshot: DraftSnapshot, draftError: APIError? = nil, openReceipt: OpenInEditorResponse? = nil, authoritativeVariant: [String: JSONValue]? = nil, editorVariantError: APIError? = nil, commitResponse: EditorCommitResponse? = nil, commitError: APIError? = nil, refreshedThread: CreationThread? = nil, suspendNextCommit: Bool = false) {
        self.draftSnapshot = draftSnapshot
        self.draftError = draftError
        self.openReceipt = openReceipt
        self.authoritativeVariant = authoritativeVariant
        self.editorVariantError = editorVariantError
        self.commitResponse = commitResponse
        self.commitError = commitError
        self.refreshedThread = refreshedThread
        self.suspendNextCommit = suspendNextCommit
    }
    func projects() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func creationCapabilities() async throws -> CreationCapabilities { CreationCapabilities(formats: [], creationMode: creationMode) }
    func project(threadID: UUID) async throws -> CreationThread {
        projectCallCount += 1
        guard let refreshedThread else { throw APIError.unsupported }
        return refreshedThread
    }
    func library() async throws -> [ProjectSummary] { throw APIError.unsupported }
    func createThread(message: String?) async throws -> CreationThread { throw APIError.unsupported }
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession { throw APIError.unsupported }
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession { throw APIError.unsupported }
    func revokeMobileSession(_ refreshToken: String) async throws { throw APIError.unsupported }
    func reviewerSignIn(email: String, password: String) async throws -> MobileSession { throw APIError.unsupported }
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted { throw APIError.unsupported }
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta { throw APIError.unsupported }
    func draft(threadID: UUID) async throws -> DraftSnapshot {
        draftCallCount += 1
        if let draftError { throw draftError }
        return draftSnapshot
    }
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot { throw APIError.unsupported }
    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse {
        openedJobID = jobID
        guard let openReceipt else { throw APIError.unsupported }
        return openReceipt
    }
    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue] {
        lastVariantID = variantID; editorVariantJobIDs.append(jobID)
        if supersededJobIDs.contains(jobID) { throw APIError.contentPlanUnavailable }
        if suspendNextEditorVariant {
            suspendNextEditorVariant = false
            editorVariantIsSuspended = true
            await withCheckedContinuation { continuation in
                if editorVariantResumeRequested {
                    editorVariantResumeRequested = false
                    continuation.resume()
                } else {
                    editorVariantContinuation = continuation
                }
            }
            editorVariantIsSuspended = false
        }
        if let editorVariantError { throw editorVariantError }
        return authoritativeVariant ?? ["editor_revision_number": phoneDestination ? .number(7) : .null, "render_destination": .string(phoneDestination ? "device" : "cloud"), "variant_id": .string(variantID), "render_generation_id": .string("generation-1"), "resolved_archetype": .string("narrated"), "base_video_path": .string("base.mp4"), "editor_capabilities": .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false)]), "user_timeline": .object(["slots": .array([.object(["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(2), "source_duration_s": .number(2), "removed": .bool(false)])])])]
    }
    func resumeEditorVariant() {
        if let editorVariantContinuation {
            editorVariantContinuation.resume()
            self.editorVariantContinuation = nil
        } else {
            editorVariantResumeRequested = true
        }
    }
    func editorSource(itemID: String, variantID: String, importID: UUID) async throws -> EditorSourceRegistrationResponse {
        guard let editorSourceResponse else { throw APIError.unsupported }
        return editorSourceResponse
    }
    /// What `registerEditorSource` answers per source id; a missing id fails.
    var registerEditorSourceResponses: [String: EditorSourceRegistrationResponse] = [:]
    var registeredSourceIDs: [String] = []
    var registeredRevisionNumbers: [Int] = []
    func registerEditorSource(_ target: EditorSourceRegistrationTarget, sourceID: String) async throws -> EditorSourceRegistrationResponse {
        registeredSourceIDs.append(sourceID)
        registeredRevisionNumbers.append(target.guidedRevisionNumber)
        guard let response = registerEditorSourceResponses[sourceID] else { throw APIError.unsupported }
        return response
    }
    func editorSourcePool(jobID: UUID, variantID: String) async throws -> NativeEditorSourcePool {
        sourcePoolCallCount += 1
        sourcePoolExpectation?.fulfill()
        sourcePoolExpectation = nil
        if suspendNextSourcePool {
            suspendNextSourcePool = false
            sourcePoolIsSuspended = true
            await withCheckedContinuation { continuation in
                if sourcePoolResumeRequested {
                    sourcePoolResumeRequested = false
                    continuation.resume()
                } else {
                    sourcePoolContinuation = continuation
                }
            }
            sourcePoolIsSuspended = false
        }
        guard let sourcePoolResult else { throw APIError.unsupported }
        return sourcePoolResult
    }
    func resumeSourcePool() {
        if let sourcePoolContinuation {
            sourcePoolContinuation.resume()
            self.sourcePoolContinuation = nil
        } else {
            sourcePoolResumeRequested = true
        }
    }
    func editorVariants(jobID: UUID) async throws -> [[String: JSONValue]] {
        editorVariantsCallCount += 1
        return [authoritativeVariant ?? ["variant_id": .string("initial"), "render_status": .string("ready")]]
    }
    func editorCommit(itemID: String, variantID: String, request: EditorCommitRequest) async throws -> EditorCommitResponse {
        commitCount += 1; lastRequest = request; lastItemID = itemID
        if suspendNextCommit {
            suspendNextCommit = false
            commitIsSuspended = true
            await withCheckedContinuation { continuation in
                if commitResumeRequested {
                    commitResumeRequested = false
                    continuation.resume()
                } else {
                    commitContinuation = continuation
                }
            }
            commitIsSuspended = false
        }
        if let commitError { throw commitError }
        if let commitThrow { throw commitThrow }
        return commitResponse ?? EditorCommitResponse(ok: true, generation: "next", sections: EditorCommitSections(textElements: false, captionMeta: false, timeline: true, mix: false), revisionNumber: nil, revisionHash: nil, expectedDuration: nil)
    }
    func resumeCommit() {
        if let commitContinuation {
            commitContinuation.resume()
            self.commitContinuation = nil
        } else {
            commitResumeRequested = true
        }
    }
    func undoDraft(threadID: UUID, expectedRevision: Int) async throws -> DraftSnapshot { throw APIError.unsupported }
    func approval(threadID: UUID, approvalID: UUID) async throws -> ApprovalSnapshot { throw APIError.unsupported }
    func decideApproval(threadID: UUID, approvalID: UUID, decision: String, expectedThreadRevision: Int, expectedDraftRevision: Int, fingerprint: String, speechCleanupAware: Bool, speechCleanupAnalysisID: String?, speechCleanupChoice: String?, outputOrientation: String?, landscapeFit: String?) async throws { throw APIError.unsupported }
    var playbackURLResult: URL?
    var playbackURLCallCount = 0
    var playbackFailureReports: [PlaybackFailureReport] = []
    func playbackURL(jobID: UUID) async throws -> URL {
        playbackURLCallCount += 1
        guard let playbackURLResult else { throw APIError.unsupported }
        return playbackURLResult
    }
    func reportPlaybackFailure(jobID: UUID, report: PlaybackFailureReport) async throws { playbackFailureReports.append(report) }
    func deviceRender(jobID: UUID, variantID: String) async throws -> DeviceRenderStatusResponse {
        deviceRenderCallCount += 1
        guard let deviceRenderResponse else { throw APIError.unsupported }
        return deviceRenderResponse
    }
    // Qualified: this file now imports KriaMediaEngine, which has its own
    // `EditRecipe` (the render recipe). The API client returns Kria's DTO.
    func editRecipe(jobID: UUID, variantID: String?) async throws -> Kria.EditRecipe { throw APIError.unsupported }
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose?) async throws -> UploadReservation {
        guard let reserveUploadResult else { throw APIError.unsupported }
        return reserveUploadResult
    }
    func cancelUpload(reservationID: UUID) async throws { throw APIError.unsupported }
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation { throw APIError.unsupported }
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread { throw APIError.unsupported }
    func addClip(jobID: UUID, gcsPath: String) async throws -> AddClipResult {
        addClipCalls.append((jobID, gcsPath))
        guard let addClipResult else { throw APIError.unsupported }
        return addClipResult
    }
    func uploadFile(to reservation: UploadReservation, fileURL: URL) async throws {
        uploadFileCalls.append((reservation, fileURL))
        if let uploadFileError { throw uploadFileError }
    }
    /// The Visuals library `visuals(itemID:)` serves; nil fails the load.
    var visualPool: [CreationVisual]?
    /// What reanalyze answers per asset; a missing id fails the request.
    var retryVisualResults: [String: CreationVisual] = [:]
    var retriedVisualIDs: [String] = []
    /// Holds the next reanalyze on the wire until `resumeRetryVisual()`.
    var suspendNextRetryVisual = false
    private(set) var retryVisualIsSuspended = false
    private var retryVisualContinuation: CheckedContinuation<Void, Never>?
    func visuals(itemID: String) async throws -> CreationVisuals {
        guard let visualPool else { throw APIError.unsupported }
        return CreationVisuals(assets: visualPool, maxAssets: 20, occupiedAssets: visualPool.count)
    }
    func retryVisual(itemID: String, assetID: String) async throws -> CreationVisual {
        retriedVisualIDs.append(assetID)
        if suspendNextRetryVisual {
            suspendNextRetryVisual = false
            await withCheckedContinuation { continuation in
                retryVisualContinuation = continuation
                retryVisualIsSuspended = true
            }
            retryVisualIsSuspended = false
        }
        guard let result = retryVisualResults[assetID] else { throw APIError.unsupported }
        return result
    }
    func resumeRetryVisual() {
        retryVisualContinuation?.resume()
        retryVisualContinuation = nil
    }
}

private final class DelayedSeekPlayer: AVPlayer, @unchecked Sendable {
    var targets: [Double] = []
    private var completions: [@Sendable (Bool) -> Void] = []
    private var seekExpectation: (target: Double?, expectation: XCTestExpectation)?

    func expectSeek(to target: Double) -> XCTestExpectation {
        let expectation = XCTestExpectation(description: "Seek to \(target)")
        seekExpectation = (target, expectation)
        return expectation
    }

    func expectNoSeek() -> XCTestExpectation {
        let expectation = XCTestExpectation(description: "Completed seek must not retry")
        expectation.isInverted = true
        seekExpectation = (nil, expectation)
        return expectation
    }
    override func seek(to time: CMTime, toleranceBefore: CMTime, toleranceAfter: CMTime,
                       completionHandler: @escaping @Sendable (Bool) -> Void) {
        targets.append(time.seconds)
        completions.append(completionHandler)
        if let pending = seekExpectation,
           pending.target == nil || abs(pending.target! - time.seconds) < 0.001 {
            seekExpectation = nil
            pending.expectation.fulfill()
        }
    }
    func completeSeek(finished: Bool = true) {
        guard !completions.isEmpty else { return }
        completions.removeFirst()(finished)
    }
}

/// Records background-task begin/end and lets a test fire the expiration handler.
@MainActor private final class RecordingEditorActivity: BackgroundActivityAssertion, @unchecked Sendable {
    private(set) var beginCount = 0
    private(set) var endCount = 0
    private var handler: (@Sendable () -> Void)?
    func begin(name: String, expirationHandler: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier {
        beginCount += 1
        handler = expirationHandler
        return UIBackgroundTaskIdentifier(rawValue: 7)
    }
    func end(_ identifier: UIBackgroundTaskIdentifier) { endCount += 1 }
    func expire() { handler?() }
}

/// Holds a simulated Photos export open so a test can observe the session while the file is still
/// being fetched, then lets it finish.
@MainActor private final class FetchLatch {
    private var continuation: CheckedContinuation<Void, Never>?
    private var opened = false
    var isWaiting: Bool { continuation != nil }

    func wait() async {
        if opened { return }
        await withCheckedContinuation { continuation = $0 }
    }

    func open() {
        opened = true
        continuation?.resume()
        continuation = nil
    }
}
