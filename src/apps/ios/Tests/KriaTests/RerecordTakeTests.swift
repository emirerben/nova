import XCTest
@testable import Kria

/// The re-record state is read from the thread and the uploads, so a stale or killed session must still land on the
/// right phase. Ways this goes wrong, written first: the action shows on a server that does not offer it; the owed
/// plan turn is lost after a relaunch, or sent twice; a failed swap hides behind a stale "planning" state; a swap that
/// is still in flight shows as ready to start again.
final class RerecordTakeTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(type, from: Data(text.utf8))
    }

    private func thread(replace: Bool?, media: String, extra: String = "", activeJob: String? = "job-old") throws -> CreationThread {
        let clips = replace.map { #","media_capabilities":{"clips":{"current":1,"replace":\#($0)}}"# } ?? #","media_capabilities":{"clips":{"current":1}}"#
        let job = activeJob.map { #","active_job_id":"\#($0)""# } ?? ""
        let text = #"{"id":"t","title":"t","status":"active","revision":5,"runtime_version":1,"updated_at":"2026-09-09T12:00:00Z","events":[],"state":{"media":\#(media)\#(extra)}\#(clips)\#(job)}"#
        return try decode(CreationThread.self, text)
    }

    private let oneClip = #"[{"media_id":"clip-a","kind":"video","filename":"A.mov"}]"#

    // MARK: offer

    func testActionIsOfferedOnlyWhenTheServerSaysReplaceIsAvailable() throws {
        XCTAssertEqual(RerecordOffer.resolve(thread: try thread(replace: true, media: oneClip)), .one(RerecordTarget(mediaID: "clip-a", displayName: "A.mov")))
        XCTAssertEqual(RerecordOffer.resolve(thread: try thread(replace: nil, media: oneClip)), .none, "an old server leaves the key out")
        XCTAssertEqual(RerecordOffer.resolve(thread: try thread(replace: false, media: oneClip)), .none)
        XCTAssertEqual(RerecordOffer.resolve(thread: nil), .none)
    }

    func testOnlyVideoClipsCanBeReplacedAndSeveralAskWhichOne() throws {
        let audioOnly = #"[{"media_id":"vo","kind":"audio","filename":"v.m4a"},{"media_id":"song","kind":"audio","role":"song","filename":"s.mp3"}]"#
        XCTAssertEqual(RerecordOffer.resolve(thread: try thread(replace: true, media: audioOnly)), .none)
        let two = #"[{"media_id":"a","kind":"video","filename":"A.mov"},{"media_id":"b","kind":"video","filename":"B.mov"},{"media_id":"vo","kind":"audio","filename":"v.m4a"}]"#
        guard case let .choose(targets) = RerecordOffer.resolve(thread: try thread(replace: true, media: two)) else { return XCTFail("two clips must ask which one") }
        XCTAssertEqual(targets.map(\.mediaID), ["a", "b"])
    }

    // MARK: the plan turn the swap owes

    private func events(_ rows: [(Int, String, String)]) throws -> [ThreadEvent] {
        let text = "[" + rows.map { #"{"id":"e\#($0.0)","sequence":\#($0.0),"revision":\#($0.0),"role":"\#($0.1)","event_type":"\#($0.2)","content":"","payload":{},"created_at":"2026-09-09T12:00:00Z"}"# }.joined(separator: ",") + "]"
        return try decode([ThreadEvent].self, text)
    }

    private let marker = #","source_replacement":{"replaced_media_id":"clip-a","media_id":"clip-b","superseded_job_id":"JOB-OLD","revision":6}"#

    func testPlanIsOwedUntilTheCreatorMessagesAfterTheSwap() throws {
        let state = try thread(replace: true, media: oneClip, extra: marker).state
        let swapped = try events([(1, "assistant", "generation_ready"), (2, "user", "media_replaced")])
        let owed = RerecordPlanDebt.owed(state: state, activeJobID: "job-old", events: swapped)
        XCTAssertEqual(owed, SourceReplacement(replacedMediaID: "clip-a", mediaID: "clip-b", supersededJobID: "JOB-OLD"), "owed across a relaunch, and the job id compares case-insensitively")

        let followed = try events([(1, "assistant", "generation_ready"), (2, "user", "media_replaced"), (3, "user", "user_message")])
        XCTAssertNil(RerecordPlanDebt.owed(state: state, activeJobID: "job-old", events: followed), "a message after the swap settles it, so it is never sent twice")
    }

    func testPlanIsNotOwedOnceANewJobExistsOrWhenNoVideoWasMade() throws {
        let swapped = try events([(2, "user", "media_replaced")])
        let state = try thread(replace: true, media: oneClip, extra: marker).state
        XCTAssertNil(RerecordPlanDebt.owed(state: state, activeJobID: "job-new", events: swapped), "the marker goes inert once a new job is active")
        let beforeAnyVideo = try thread(replace: true, media: oneClip, extra: #","source_replacement":{"replaced_media_id":"a","media_id":"b","superseded_job_id":null,"revision":6}"#).state
        XCTAssertNil(RerecordPlanDebt.owed(state: beforeAnyVideo, activeJobID: nil, events: swapped), "a swap before any video leaves the creator's own first message to plan")
        XCTAssertNil(RerecordPlanDebt.owed(state: nil, activeJobID: "job-old", events: swapped))
    }

    // MARK: one phase from every fact

    private let target = RerecordTarget(mediaID: "clip-a", displayName: "A.mov")
    private let debt = SourceReplacement(replacedMediaID: "clip-a", mediaID: "clip-b", supersededJobID: "job-old")
    private var facts: RerecordPhase.Facts { .init(offer: .one(target), replaceInFlight: false, failure: nil, debt: nil) }

    func testPhaseFollowsTheFactsInPriorityOrder() {
        let failure = RerecordFailure.known(code: "retake_has_no_audio", transient: false)
        XCTAssertEqual(RerecordPhase.resolve(local: .none, facts: facts), .ready(.one(target)))
        XCTAssertEqual(RerecordPhase.resolve(local: .none, facts: { var f = facts; f.offer = .none; return f }()), .unavailable)
        XCTAssertEqual(RerecordPhase.resolve(local: .confirming([target]), facts: facts), .confirming([target]))

        var swapping = facts; swapping.replaceInFlight = true; swapping.failure = failure; swapping.debt = debt
        XCTAssertEqual(RerecordPhase.resolve(local: .swapping, facts: swapping), .replacing, "a swap in flight is never offered again")
        var failed = facts; failed.failure = failure; failed.debt = debt
        XCTAssertEqual(RerecordPhase.resolve(local: .planning(debt), facts: failed), .failed(failure), "a failure is shown, not hidden behind planning")
        var owed = facts; owed.debt = debt
        XCTAssertEqual(RerecordPhase.resolve(local: .none, facts: owed), .planOwed(debt), "a relaunch with no local state still shows the owed turn")
        XCTAssertEqual(RerecordPhase.resolve(local: .swapping, facts: owed), .planOwed(debt))
        XCTAssertEqual(RerecordPhase.resolve(local: .planning(debt), facts: owed), .planning(debt))
        let other = SourceReplacement(replacedMediaID: "x", mediaID: "y", supersededJobID: "z")
        XCTAssertEqual(RerecordPhase.resolve(local: .planning(other), facts: owed), .planOwed(debt), "a turn sent for an earlier swap does not cover this one")
    }

    // MARK: failures

    func testEveryRefusalSaysTheCurrentTakeIsUnchangedAndOnlyRetriesWhatCanSucceed() {
        let codes: [(String, Bool)] = [
            ("render_in_progress", true), ("upload_not_finished", true), ("identity_unavailable", true), ("revision_mismatch", true),
            ("retake_has_no_audio", false), ("retake_same_source", false), ("retake_target_not_speaker", false), ("retake_unsupported_format", false),
            ("media_replace_unavailable", false), ("retake_source_unresolved", false),
        ]
        for (code, retryable) in codes {
            let failure = RerecordFailure.known(code: code, transient: false)
            XCTAssertTrue(failure.message.hasSuffix(RerecordFailure.unchanged), code)
            XCTAssertEqual(failure.retryable, retryable, code)
        }
        XCTAssertTrue(RerecordFailure.known(code: nil, transient: true).retryable, "an unlabelled server error or lost connection can be retried")
        XCTAssertFalse(RerecordFailure.known(code: nil, transient: false).retryable)
    }

    func testFailureReadsTheServersProblemCodeFromEveryStatus() {
        XCTAssertEqual(RerecordFailure(APIError.conflict(detail: ConflictDetail("x", code: "render_in_progress"))).code, "render_in_progress")
        XCTAssertEqual(RerecordFailure(APIError.requestFailed(status: 422, detail: RequestFailureDetail(nil, code: "retake_has_no_audio"))).code, "retake_has_no_audio")
        XCTAssertTrue(RerecordFailure(APIError.requestFailed(status: 503, detail: RequestFailureDetail(nil))).retryable)
        XCTAssertTrue(RerecordFailure(APIError.offline).retryable)
    }
}
