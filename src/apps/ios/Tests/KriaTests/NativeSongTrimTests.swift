import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-561. Failure modes these pin (written before the code):
///
/// Song model
///  1. A natural end (the video's, or the song file's own) read as a creator end, which would then pin a
///     later video extension: end == start + video, end == song length, a lip-sync window, an unknown video length.
///  2. A creator end lost or wrongly cleared when the video length or the start changes.
///  3. Dragging the end back to the natural end must clear it, not store a coincidental number.
///  4. `window_end_s` sent when nothing changed, or together with a lip-sync start.
/// Lip-sync trim
///  5. A cut kept on the timeline that lies wholly outside the chosen range, or a cut inside it dropped.
///  6. A kept cut whose footage window moved though it was not on an edge.
///  7. A straddling cut whose in-point does not move by exactly what its output start moves, taking the
///     singer off the song (the derived start must equal the chosen start).
///  8. A trim below the 3 s minimum, or one that would leave a sliver shorter than a clip may be.
///  9. Cuts that disagree (a reorder) must not produce a start; B-roll must not vote.
@MainActor
final class NativeSongTrimTests: XCTestCase {
    // MARK: Song model

    private func variant(mode: String = "background", start: Double = 10, end: Double, duration: Double = 100,
                         video: Double? = 20, takes: [String: Double]? = nil, offersTrim: Bool = true) -> [String: JSONValue] {
        var song: [String: JSONValue] = ["mode": .string(mode), "duration_s": .number(duration),
                                         "window_start_s": .number(start), "window_end_s": .number(end)]
        if let takes { song["takes"] = .object(takes.mapValues { .number($0) }) }
        var variant: [String: JSONValue] = ["user_song": .object(song)]
        if let video { variant["duration_s"] = .number(video) }
        if offersTrim { variant["editor_capabilities"] = .object(["user_song": .object(["trim": .object(["editable": .bool(true)])])]) }
        return variant
    }

    func testCreatorEndIsInferredOnlyWhenTheWindowFallsShortOfWhatTheVideoAndSongGive() throws {
        XCTAssertNil(NativeUserSong(variant: variant(end: 30))?.creatorEndS, "end == start + video is the natural end")
        XCTAssertNil(NativeUserSong(variant: variant(end: 29.97))?.creatorEndS, "rounding noise is not a creator end")
        XCTAssertEqual(NativeUserSong(variant: variant(end: 25))?.creatorEndS, 25, "shorter than the video: the creator's")
        XCTAssertNil(NativeUserSong(variant: variant(start: 90, end: 100))?.creatorEndS, "the song ran out: natural")
        XCTAssertNil(NativeUserSong(variant: variant(end: 25, video: nil))?.creatorEndS, "no video length: cannot tell, assume natural")
        XCTAssertNil(NativeUserSong(variant: variant(end: 25, offersTrim: false))?.creatorEndS, "an older server always follows the video")
        XCTAssertNil(NativeUserSong(variant: variant(mode: "lipsync", end: 25))?.creatorEndS, "a lip-sync window is the cuts' business")
    }

    func testTakesAreLipsyncOnlyAndIgnoreMalformedValues() {
        let lip = NativeUserSong(variant: variant(mode: "lipsync", end: 30, takes: ["a": 12.5, "b": -3]))
        XCTAssertEqual(lip?.takes, ["a": 12.5, "b": -3], "a take's offset may be negative")
        XCTAssertEqual(NativeUserSong(variant: variant(end: 30, takes: ["a": 1]))?.takes, [:])
        XCTAssertEqual(NativeUserSong(variant: variant(mode: "lipsync", end: 30))?.takes, [:], "an older server sends none")
    }

    func testApplyingTheEndNeverOutlivesTheVideoOrTheSong() throws {
        let song = try XCTUnwrap(NativeUserSong(variant: variant(end: 25)))  // creator end 25, video 20
        XCTAssertEqual(song.applying(nil, videoLength: 20)?.windowEndS, 25, "the creator end beats the video that would go to 30")
        XCTAssertEqual(song.applying(nil, videoLength: 12)?.windowEndS, 22, "a shorter video still wins")
        XCTAssertEqual(song.applying(nil, videoLength: 40)?.windowEndS, 25, "a longer video keeps the creator end: it is absolute")
        // Moving the start later keeps the same end.
        XCTAssertEqual(song.applying(EditorUserSongState(windowStartS: 18), videoLength: 20)?.windowEndS, 25)
        // Clearing it (the song's own length) hands the window back to the video.
        let cleared = song.applying(EditorUserSongState(windowEndS: 100), videoLength: 20)
        XCTAssertEqual(cleared?.windowEndS, 30)
        XCTAssertNil(cleared?.creatorEndS)
        // Start earlier with a creator end set and the video unknown: the video length comes from the variant.
        XCTAssertEqual(song.applying(EditorUserSongState(windowStartS: 8))?.windowEndS, 25)
    }

    func testCommitBodyCarriesTheEndOnlyWhenItChanged() throws {
        func json(_ state: EditorUserSongState) throws -> [String: JSONValue] {
            let data = try JSONEncoder().encode(state.commit)
            return try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: data).objectValue)
        }
        XCTAssertNil(try json(EditorUserSongState(volume: 0.5))["window_end_s"])
        XCTAssertEqual(try json(EditorUserSongState(windowEndS: 42.5))["window_end_s"], .number(42.5))
        XCTAssertEqual(try json(EditorUserSongState(windowStartS: 4, windowEndS: 9)).keys.sorted(), ["removed", "window_end_s", "window_start_s"])
        XCTAssertEqual(try json(EditorUserSongState(windowEndS: 42.5, removed: true)).keys.sorted(), ["removed"], "a removal carries nothing else")
        XCTAssertEqual(EditorUserSongState.merged(EditorUserSongState(windowEndS: 40), EditorUserSongState(volume: 0.3))?.windowEndS, 40)
        XCTAssertEqual(EditorUserSongState.merged(EditorUserSongState(windowEndS: 40), EditorUserSongState(windowEndS: 50))?.windowEndS, 50)
        XCTAssertTrue(EditorUserSongState().isEmpty)
        XCTAssertFalse(EditorUserSongState(windowEndS: 3).isEmpty)
    }

    // MARK: Song start derivation

    func testDerivedStartIsTheAgreeingPinnedVotesAndIgnoresBrollAndDisagreement() {
        typealias Anchor = NativeLipsyncSongAnchor
        let deltas: [Int: Double] = [0: 100, 1: 102.5]
        // delta + inS - outputStart: 100 + 5 - 0 = 105 and 102.5 + 2.5 - 2.5 = 102.5 ... make them agree.
        let agree = [Anchor.Cut(clipIndex: 0, inS: 5, outputStartS: 0), Anchor.Cut(clipIndex: 1, inS: 5, outputStartS: 2.5)]
        XCTAssertEqual(try XCTUnwrap(Anchor.derivedStart(cuts: agree, deltaByClipIndex: deltas)), 105, accuracy: 1e-9)
        let withBroll = agree + [Anchor.Cut(clipIndex: 9, inS: 77, outputStartS: 5)]
        XCTAssertEqual(try XCTUnwrap(Anchor.derivedStart(cuts: withBroll, deltaByClipIndex: deltas)), 105, accuracy: 1e-9, "B-roll does not vote")
        let reordered = [Anchor.Cut(clipIndex: 0, inS: 5, outputStartS: 0), Anchor.Cut(clipIndex: 1, inS: 0, outputStartS: 2.5)]
        XCTAssertNil(Anchor.derivedStart(cuts: reordered, deltaByClipIndex: deltas), "disagreeing cuts are a reorder")
        XCTAssertNil(Anchor.derivedStart(cuts: [Anchor.Cut(clipIndex: 9, inS: 1, outputStartS: 0)], deltaByClipIndex: deltas), "nothing pinned, no vote")
        XCTAssertNil(Anchor.derivedStart(cuts: agree, deltaByClipIndex: [:]))
        let withinAFrame = [Anchor.Cut(clipIndex: 0, inS: 5, outputStartS: 0), Anchor.Cut(clipIndex: 1, inS: 5.02, outputStartS: 2.5)]
        XCTAssertNotNil(Anchor.derivedStart(cuts: withinAFrame, deltaByClipIndex: deltas), "within one editor frame still agrees")
    }

    // MARK: Lip-sync trim plan

    /// Four 2.5 s cuts on four takes. Take k plays song seconds `100 + 2.5k ...`, filmed from second `k` of its file,
    /// so the delta that puts them all on a 100 s song start is `100 + 2.5k - k`.
    private func slots() -> [EditorTimelineSlot] {
        (0..<4).map { index in
            EditorTimelineSlot(id: "slot-\(index)", clipIndex: index, inS: Double(index), durationS: 2.5, raw: ["source_duration_s": .number(30)])
        }
    }
    private let deltas: [Int: Double] = [0: 100, 1: 101.5, 2: 103, 3: 104.5]

    private func apply(_ plan: NativeLipsyncSongTrim.Plan, to slots: [EditorTimelineSlot]) -> [EditorTimelineSlot] {
        var result = slots
        for kept in plan.kept where kept.trimmed {
            result[kept.index].inS = kept.inS
            result[kept.index].durationS = kept.durationS
        }
        for index in plan.dropped.sorted(by: >) { result.remove(at: index) }
        return result
    }

    func testCleanFixtureAgreesOnTheSongStart() {
        XCTAssertEqual(NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: slots()), deltaByClipIndex: deltas) ?? -1, 100, accuracy: 1e-9)
    }

    func testDropsOnlyCutsOutsideTheRangeAndLeavesMiddleCutsUntouched() throws {
        // Keep output 2.5 ... 7.5: cut 0 and cut 3 are outside, 1 and 2 fill the range exactly.
        let plan = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: slots(), t0: 2.5, t1: 7.5, minimumClipS: 0.1))
        XCTAssertEqual(plan.dropped, [0, 3])
        XCTAssertEqual(plan.kept.map(\.index), [1, 2])
        XCTAssertEqual(plan.kept.map(\.trimmed), [false, false], "cuts on the boundary are not trimmed")
        XCTAssertEqual(plan.originS, 2.5)
        XCTAssertEqual(plan.totalS, 5)
    }

    func testStraddlingCutsLoseTheirHeadAndTailAndTheSingerStaysOnTheSong() throws {
        let original = slots()
        // Start at output 1.0 (inside cut 0) and end at 8.0 (inside cut 3).
        let plan = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: original, t0: 1, t1: 8, minimumClipS: 0.1))
        XCTAssertEqual(plan.dropped, [])
        let head = try XCTUnwrap(plan.kept.first), tail = try XCTUnwrap(plan.kept.last)
        XCTAssertEqual(head.inS, 0 + 1, accuracy: 1e-9, "the head cut's in-point moves by what it lost")
        XCTAssertEqual(head.durationS, 1.5, accuracy: 1e-9)
        XCTAssertEqual(tail.inS, 3, accuracy: 1e-9, "the tail cut keeps its in-point")
        XCTAssertEqual(tail.durationS, 0.5, accuracy: 1e-9)
        XCTAssertEqual(plan.kept.dropFirst().dropLast().map(\.trimmed), [false, false])
        // The server's formula over the result lands exactly on the chosen start: 100 + 1.
        let trimmed = apply(plan, to: original)
        XCTAssertEqual(try XCTUnwrap(NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: trimmed), deltaByClipIndex: deltas)), 101, accuracy: 1e-9)
        XCTAssertEqual(trimmed.compactMap(\.durationS).reduce(0, +), 7, accuracy: 1e-9)
    }

    func testDroppingTheOpeningCutsAndTrimmingTheNextOneKeepsTheStartConsistent() throws {
        let original = slots()
        let plan = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: original, t0: 3.5, t1: 10, minimumClipS: 0.1))
        XCTAssertEqual(plan.dropped, [0])
        let trimmed = apply(plan, to: original)
        XCTAssertEqual(try XCTUnwrap(NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: trimmed), deltaByClipIndex: deltas)), 103.5, accuracy: 1e-9)
        XCTAssertEqual(NativeEditorInteraction.timelineProjection(slots: trimmed, carousel: nil).totalDuration, 6.5, accuracy: 1e-9)
    }

    func testRefusesUnderThreeSecondsAndNoOpTrims() {
        XCTAssertNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: 4, t1: 6.5, minimumClipS: 0.1), "2.5 s is under the minimum")
        XCTAssertNotNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: 4, t1: 7, minimumClipS: 0.1), "exactly 3 s is allowed")
        XCTAssertNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: 0, t1: 10, minimumClipS: 0.1), "the whole video is no change")
        XCTAssertNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: 6, t1: 5, minimumClipS: 0.1), "an inverted range")
        XCTAssertNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: .nan, t1: 5, minimumClipS: 0.1))
    }

    func testASliverShorterThanAClipSnapsToTheCutBoundary() throws {
        // t0 = 2.45 leaves 0.05 s of cut 0: shorter than a clip may be, so the start snaps to 2.5 and cut 0 goes.
        let head = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: slots(), t0: 2.45, t1: 10, minimumClipS: 0.1))
        XCTAssertEqual(head.dropped, [0])
        XCTAssertEqual(head.originS, 2.5)
        // t1 = 7.55 would leave 0.05 s of cut 3 on the tail: snap back to 7.5 and drop it.
        let tail = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: slots(), t0: 0, t1: 7.55, minimumClipS: 0.1))
        XCTAssertEqual(tail.dropped, [3])
        XCTAssertEqual(tail.endS, 7.5)
        // A sliver that would push the total under the minimum is refused outright.
        XCTAssertNil(NativeLipsyncSongTrim.plan(slots: slots(), t0: 4.97, t1: 7.5, minimumClipS: 0.1))
    }

    func testTrimOfABrollCutRipplesLikeAnyOtherAndNeverVotes() throws {
        var original = slots()
        original[2].clipIndex = 9  // B-roll: no delta
        let plan = try XCTUnwrap(NativeLipsyncSongTrim.plan(slots: original, t0: 2.5, t1: 10, minimumClipS: 0.1))
        let trimmed = apply(plan, to: original)
        XCTAssertEqual(try XCTUnwrap(NativeLipsyncSongAnchor.derivedStart(cuts: NativeLipsyncSongTrim.cuts(of: trimmed), deltaByClipIndex: deltas)), 102.5, accuracy: 1e-9)
    }

    // MARK: Compiler

    func testSongBedEndStopsTheSongAndFadesOverOneAndAHalfSecondsOnlyWhenItBinds() throws {
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original", asset: MediaAsset(id: "original", relativePath: "original.mp4", fingerprint: fingerprint, duration: 30), url: URL(fileURLWithPath: "/original.mp4"))
        func program(end: Double?, video: Double = 10, songLength: Double = 200) throws -> TimelineClip? {
            let document = EditorDocument(clips: [.init(id: "shot", clipIndex: 0, inS: 0, durationS: video)])
            let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: video, trimIn: 0, trimOut: video, sourceDuration: 30, slotID: "shot")
            let song = ResolvedEditorSource(clipIndex: -1, mediaID: "song-item", asset: MediaAsset(id: "song-item", relativePath: "song.wav", fingerprint: fingerprint, duration: songLength), url: URL(fileURLWithPath: "/song.wav"))
            let bed = NativeEditorSongBed(assetID: "song-item", sourceStart: 100, sourceDuration: 4, volume: 1, fadeIn: 0.5, fadeOut: 0.5, endS: end)
            return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source],
                                        audioSources: [NativeEditorRenderCompiler.songSourceKey: song], songBed: bed)
                .recipe.tracks.first { $0.id == "song" }?.clips.first
        }
        let stopped = try XCTUnwrap(program(end: 106))
        XCTAssertEqual(stopped.sourceDuration, 6, accuracy: 1e-9, "stops at the creator's end")
        XCTAssertEqual(try XCTUnwrap(stopped.audioFadeOut), 1.5, accuracy: 1e-9)
        let tiny = try XCTUnwrap(program(end: 102))
        XCTAssertEqual(tiny.sourceDuration, 2, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(tiny.audioFadeOut), 1, accuracy: 1e-9, "a fade is capped at half the length")
        let beyond = try XCTUnwrap(program(end: 150))
        XCTAssertEqual(beyond.sourceDuration, 10, accuracy: 1e-9, "an end past the video does not bind")
        XCTAssertEqual(try XCTUnwrap(beyond.audioFadeOut), 0.5, accuracy: 1e-9, "and keeps the recipe's own fade")
        let runsOut = try XCTUnwrap(program(end: 105, video: 10, songLength: 105))
        XCTAssertEqual(runsOut.sourceDuration, 5, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(runsOut.audioFadeOut), 0.5, accuracy: 1e-9, "the song's own end is not a creator stop")
        let none = try XCTUnwrap(program(end: nil))
        XCTAssertEqual(none.sourceDuration, 10, accuracy: 1e-9)
    }
}
