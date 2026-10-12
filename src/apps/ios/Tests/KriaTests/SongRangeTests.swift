import XCTest
@testable import Kria

/// KRI-580: where in the song the creator filmed. The range is optional and the server is the authority, so the
/// failure modes that matter are: a malformed value breaking the thread, a range the server would refuse being
/// sent, a picker leaving the song, a retry applying twice, and a feature-off server showing an alert.
final class SongRangeTests: XCTestCase {
    // MARK: reading the range off the thread

    private func songEntry(_ range: JSONValue?) -> [String: JSONValue] {
        var entry: [String: JSONValue] = ["media_id": .string("s"), "kind": .string("audio"), "role": .string("song"), "duration_s": .number(90)]
        if let range { entry["song_range"] = range }
        return ["media": .array([.object(entry)])]
    }

    private func range(_ start: JSONValue?, _ end: JSONValue?) -> JSONValue {
        var fields: [String: JSONValue] = [:]
        if let start { fields["start_s"] = start }
        if let end { fields["end_s"] = end }
        return .object(fields)
    }

    func testValidRangeIsReadOffTheSongEntry() throws {
        let song = try XCTUnwrap(CreationAttachedMedia.song(songEntry(range(.number(40), .number(110.5)))))
        XCTAssertEqual(song.filmingRange, SongFilmingRange(start: 40, end: 110.5))
    }

    func testMissingPartialNegativeReversedOrNonNumericRangeMeansNoRangeAndNeverBreaksTheSong() throws {
        let cases: [JSONValue?] = [
            nil, .null, .string("0:40-1:10"), .array([]),
            range(.number(40), nil), range(nil, .number(60)), range(nil, nil),
            range(.number(-1), .number(30)),
            range(.number(60), .number(60)), range(.number(60), .number(20)),
            range(.string("40"), .number(60)), range(.number(40), .bool(true)), range(.number(40), .null),
            range(.number(.nan), .number(60)), range(.number(40), .number(.infinity)),
        ]
        for value in cases {
            let song = try XCTUnwrap(CreationAttachedMedia.song(songEntry(value)), "\(String(describing: value))")
            XCTAssertNil(song.filmingRange, "\(String(describing: value))")
            XCTAssertEqual(song.durationS, 90, "the song itself must still parse")
        }
    }

    func testFootageAndVoiceoverNeverCarryARange() throws {
        let state: [String: JSONValue] = ["media": .array([.object(["media_id": .string("c"), "kind": .string("video"), "song_range": range(.number(1), .number(9))])])]
        XCTAssertNil(try XCTUnwrap(CreationAttachedMedia.parse(state).first).filmingRange)
    }

    // MARK: capability

    func testCapabilityNeedsTheFlagAndTheSongAffordance() throws {
        func caps(_ json: String) throws -> CreationCapabilities { try JSONDecoder().decode(CreationCapabilities.self, from: Data(json.utf8)) }
        let song = #""media":{"song":{"max":1,"max_file_bytes":52428800,"content_types":["audio/mpeg"]}}"#
        XCTAssertTrue(try caps(#"{"formats":[],"song_filming_range":true,\#(song)}"#).songFilmingRangeEnabled)
        XCTAssertFalse(try caps(#"{"formats":[],"song_filming_range":true}"#).songFilmingRangeEnabled, "no media.song, no control")
        XCTAssertFalse(try caps(#"{"formats":[],"song_filming_range":false,\#(song)}"#).songFilmingRangeEnabled)
        XCTAssertFalse(try caps(#"{"formats":[],\#(song)}"#).songFilmingRangeEnabled, "an older server omits it")
    }

    // MARK: time math

    func testSongsTooShortToHoldARangeOfferNothing() {
        XCTAssertNil(SongRangeDraft(duration: nil, existing: nil))
        XCTAssertNil(SongRangeDraft(duration: 0, existing: nil))
        XCTAssertNil(SongRangeDraft(duration: 4.99, existing: nil))
        XCTAssertNil(SongRangeDraft(duration: -30, existing: nil))
        XCTAssertNil(SongRangeDraft(duration: .nan, existing: nil))
        XCTAssertNil(SongRangeDraft(duration: .infinity, existing: nil))
        XCTAssertNotNil(SongRangeDraft(duration: 5, existing: nil))
    }

    func testExactlyFiveSecondsIsOneValidRange() throws {
        var draft = try XCTUnwrap(SongRangeDraft(duration: 5, existing: nil))
        XCTAssertEqual(draft.range, SongFilmingRange(start: 0, end: 5))
        XCTAssertEqual(draft.maxFrom, 0)
        draft.setFrom(3)
        XCTAssertEqual(draft.from, 0, "From cannot leave room for less than five seconds")
        XCTAssertEqual(draft.to, 5)
    }

    func testOpeningWithNothingSetIsTheWholeSongToItsLastWholeSecond() throws {
        let draft = try XCTUnwrap(SongRangeDraft(duration: 90.7, existing: nil))
        XCTAssertEqual(draft.range, SongFilmingRange(start: 0, end: 90))
    }

    func testAnExistingRangeReopensWithItsValuesAndAnEndPastTheSongIsClamped() throws {
        let draft = try XCTUnwrap(SongRangeDraft(duration: 90, existing: SongFilmingRange(start: 40, end: 80)))
        XCTAssertEqual(draft.range, SongFilmingRange(start: 40, end: 80))
        let clamped = try XCTUnwrap(SongRangeDraft(duration: 90, existing: SongFilmingRange(start: 40, end: 400)))
        XCTAssertEqual(clamped.to, 90)
        let nothingLeft = try XCTUnwrap(SongRangeDraft(duration: 90, existing: SongFilmingRange(start: 88, end: 120)))
        XCTAssertEqual(nothingLeft.range, SongFilmingRange(start: 0, end: 90), "a range with under five seconds left of it falls back to the whole song")
    }

    func testMovingFromPastToPushesToToKeepFiveSecondsAndNeverLeavesTheSong() throws {
        var draft = try XCTUnwrap(SongRangeDraft(duration: 90, existing: SongFilmingRange(start: 10, end: 30)))
        draft.setFrom(50)
        XCTAssertEqual(draft.from, 50)
        XCTAssertEqual(draft.to, 55)
        draft.setFrom(10_000)
        XCTAssertEqual(draft.from, 85, "clamped so five seconds still fit")
        XCTAssertEqual(draft.to, 90)
        draft.setFrom(-20)
        XCTAssertEqual(draft.from, 0)
        XCTAssertEqual(draft.to, 90, "moving From back leaves To where it was")
    }

    func testToBelowFromPlusFiveIsReportedNotSilentlyMovedAndSaveHasNothingToSend() throws {
        var draft = try XCTUnwrap(SongRangeDraft(duration: 90, existing: SongFilmingRange(start: 40, end: 80)))
        draft.setTo(43)
        XCTAssertEqual(draft.to, 43)
        XCTAssertEqual(draft.message, "Pick at least five seconds of your song.")
        XCTAssertNil(draft.range)
        draft.setTo(45)
        XCTAssertNil(draft.message)
        XCTAssertEqual(draft.range, SongFilmingRange(start: 40, end: 45))
        draft.setTo(9_999)
        XCTAssertEqual(draft.to, 90)
        draft.setTo(-5)
        XCTAssertEqual(draft.to, 0)
        XCTAssertNil(draft.range)
    }

    func testTimecodeHandlesBadInputAndLongSongs() {
        XCTAssertEqual(SongRangeMath.timecode(0), "0:00")
        XCTAssertEqual(SongRangeMath.timecode(-3), "0:00")
        XCTAssertEqual(SongRangeMath.timecode(.nan), "0:00")
        XCTAssertEqual(SongRangeMath.timecode(.infinity), "0:00")
        XCTAssertEqual(SongRangeMath.timecode(59.4), "0:59")
        XCTAssertEqual(SongRangeMath.timecode(59.6), "1:00")
        XCTAssertEqual(SongRangeMath.timecode(110), "1:50")
        XCTAssertEqual(SongRangeMath.timecode(3599.6), "1:00:00", "rounding must carry into the hour")
        XCTAssertEqual(SongRangeMath.timecode(3725), "1:02:05", "an hour or more reads h:mm:ss")
    }

    func testSummaryAndSpokenText() {
        let range = SongFilmingRange(start: 40, end: 110)
        XCTAssertEqual(range.summary, "Filmed 0:40 - 1:50")
        XCTAssertEqual(range.spokenSummary, "Filmed from 0:40 to 1:50")
    }

    // MARK: wire

    private func thread(revision: Int = 5) -> Data {
        Data("""
        {"id":"\(PreviewFixtures.projectID.uuidString)","title":"T","status":"active","revision":\(revision),"runtime_version":2,"updated_at":"2026-09-10T10:00:00Z","state":{},"events":[]}
        """.utf8)
    }

    func testSetterSendsPutWithRangeRevisionAndEventId() async throws {
        var seen: (method: String?, path: String?, body: [String: Any])?
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            seen = (request.httpMethod, request.url?.path, body)
            return (200, self.thread(revision: 6))
        }
        let updated = try await NativeEditorTestSupport.api().setSongRange(threadID: PreviewFixtures.projectID, range: SongFilmingRange(start: 40, end: 80), expectedRevision: 5, clientEventID: "evt-1")
        XCTAssertEqual(updated.revision, 6)
        XCTAssertEqual(seen?.method, "PUT")
        XCTAssertEqual(seen?.path, "/creation-threads/\(PreviewFixtures.projectID.uuidString)/song-range")
        XCTAssertEqual(seen?.body["start_s"] as? Double, 40)
        XCTAssertEqual(seen?.body["end_s"] as? Double, 80)
        XCTAssertEqual(seen?.body["expected_revision"] as? Int, 5)
        XCTAssertEqual(seen?.body["client_event_id"] as? String, "evt-1")
    }

    func testClearingSendsExplicitNullsSoTheServerClearsInsteadOfRejecting() async throws {
        var body: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any])
            return (200, self.thread())
        }
        _ = try await NativeEditorTestSupport.api().setSongRange(threadID: PreviewFixtures.projectID, range: nil, expectedRevision: 5, clientEventID: "evt-2")
        XCTAssertTrue(body["start_s"] is NSNull, "the key must be present and null")
        XCTAssertTrue(body["end_s"] is NSNull)
    }

    private func problem(_ code: String, _ message: String) -> Data {
        Data(#"{"problem":{"code":"\#(code)","message":"\#(message)","phase":"accept","retryable":false,"recovery":"none"}}"#.utf8)
    }

    func testProblemEnvelopeCodeAndMessageSurviveOnNon409Failures() async throws {
        NativeEditorURLProtocol.handler = { _ in (422, self.problem("song_range_invalid", "That starts after your song ends.")) }
        do {
            _ = try await NativeEditorTestSupport.api().setSongRange(threadID: PreviewFixtures.projectID, range: SongFilmingRange(start: 99, end: 120), expectedRevision: 1, clientEventID: "e")
            XCTFail("expected a failure")
        } catch let APIError.requestFailed(status, detail) {
            XCTAssertEqual(status, 422)
            XCTAssertEqual(detail.code, "song_range_invalid")
            XCTAssertEqual(detail.problemMessage, "That starts after your song ends.")
            XCTAssertNil(detail.message, "the legacy detail copy is untouched")
        }
    }

    // MARK: save flow

    private final class FakeClient: SongRangeClient, @unchecked Sendable {
        var failures: [Error]
        var revisions: [Int]
        private(set) var sent: [(revision: Int, eventID: String, range: SongFilmingRange?)] = []
        private(set) var reads = 0
        init(failures: [Error] = [], revisions: [Int] = [5, 6, 7]) { self.failures = failures; self.revisions = revisions }
        func project(threadID: UUID) async throws -> CreationThread {
            defer { reads += 1 }
            return try Self.decode(revision: revisions[min(reads, revisions.count - 1)])
        }
        func setSongRange(threadID: UUID, range: SongFilmingRange?, expectedRevision: Int, clientEventID: String) async throws -> CreationThread {
            sent.append((expectedRevision, clientEventID, range))
            if !failures.isEmpty { throw failures.removeFirst() }
            return try Self.decode(revision: expectedRevision + 1)
        }
        static func decode(revision: Int) throws -> CreationThread {
            let json = #"{"id":"t","title":"T","status":"active","revision":\#(revision),"updated_at":"2026-10-04T10:00:00Z"}"#
            let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(CreationThread.self, from: Data(json.utf8))
        }
    }

    private let target = SongFilmingRange(start: 40, end: 80)
    private func conflict(_ code: String? = nil, _ message: String? = "Creation thread changed") -> APIError {
        .conflict(detail: ConflictDetail(message, code: code))
    }
    private func failed(_ status: Int, code: String?, message: String?) -> APIError {
        .requestFailed(status: status, detail: RequestFailureDetail(nil, code: code, problemMessage: message))
    }

    func testRevisionConflictRereadsTheThreadAndRetriesOnceWithTheSameEventId() async throws {
        let client = FakeClient(failures: [conflict()], revisions: [5, 8])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .saved(let thread) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(thread.revision, 9)
        XCTAssertEqual(client.sent.map(\.revision), [5, 8], "the second try uses the fresh revision")
        XCTAssertEqual(client.sent.map(\.eventID), ["evt", "evt"], "one user action, one idempotency key")
    }

    func testASecondConflictIsReportedInsteadOfLooping() async {
        let client = FakeClient(failures: [conflict(), conflict(), conflict()])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .failed(let message) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(message, SongRangeSubmission.genericFailure)
        XCTAssertEqual(client.sent.count, 2)
    }

    func testMissingSongIsNeverRetriedAndSaysWhy() async {
        let client = FakeClient(failures: [conflict("song_missing", "Attach a song first.")])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .failed(let message) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(message, "Attach a song first.")
        XCTAssertEqual(client.sent.count, 1)
    }

    func testFeatureOffIsSilentUnavailableNotAnError() async {
        let client = FakeClient(failures: [failed(404, code: "song_range_unavailable", message: "Song range unavailable")])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .unavailable = result else { return XCTFail("\(result)") }
    }

    func testAnOrdinary404IsNotTreatedAsFeatureOff() async {
        let client = FakeClient(failures: [failed(404, code: "thread_not_found", message: "Creation thread not found")])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .failed = result else { return XCTFail("\(result)") }
    }

    func testInvalidRangeShowsTheServersOwnWords() async {
        let client = FakeClient(failures: [failed(422, code: "song_range_invalid", message: "That starts after your song ends.")])
        let result = await SongRangeSubmission.submit(client: client, threadID: UUID(), range: target, clientEventID: "evt")
        guard case .failed(let message) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(message, "That starts after your song ends.")
    }

    func testOfflineAndUnknownFailuresReadAsPlainRetryableCopy() async {
        let offline = await SongRangeSubmission.submit(client: FakeClient(failures: [APIError.offline]), threadID: UUID(), range: nil, clientEventID: "e")
        guard case .failed(let offlineText) = offline else { return XCTFail("\(offline)") }
        XCTAssertEqual(offlineText, SongRangeSubmission.offlineFailure)
        let odd = await SongRangeSubmission.submit(client: FakeClient(failures: [failed(500, code: nil, message: "boom stack trace")]), threadID: UUID(), range: nil, clientEventID: "e")
        guard case .failed(let oddText) = odd else { return XCTFail("\(odd)") }
        XCTAssertEqual(oddText, SongRangeSubmission.genericFailure, "server internals are never shown")
    }

    func testEventIdIsReusedForARetryOfTheSameRangeAndFreshForAnotherOne() {
        let first = SongRangeEvent.reusing(nil, range: target)
        XCTAssertEqual(SongRangeEvent.reusing(first, range: target).id, first.id)
        XCTAssertNotEqual(SongRangeEvent.reusing(first, range: SongFilmingRange(start: 41, end: 80)).id, first.id)
        XCTAssertNotEqual(SongRangeEvent.reusing(first, range: nil).id, first.id)
    }
}
