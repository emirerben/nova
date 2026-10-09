import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-443: the live plan feed reducer and its wire decoding. The contract is frozen in
/// `docs/pipelines/live-plan-blocks.md`; these pin the rules the client owns.
final class PlanBlockFeedTests: XCTestCase {
    private func block(_ section: String, _ state: String, summary: String? = nil, skipped: Bool = false, intent: Bool = false) -> JSONValue {
        var fields: [String: JSONValue] = ["section_id": .string(section), "state": .string(state), "intent": .bool(intent), "skipped": .bool(skipped)]
        fields["summary"] = summary.map { .string($0) } ?? .null
        fields["detail"] = .null
        fields["decided_at"] = state == "decided" ? .string("2026-10-05T10:00:00.123Z") : .null
        return .object(fields)
    }

    private func event(_ sequence: Int, job: String = "job-1", turn: String? = "turn-1", _ blocks: [JSONValue], type: String = "plan_block") -> ThreadEvent {
        ThreadEvent(
            id: "e\(sequence)-\(job)", sequence: sequence, revision: sequence + 1, role: "system", eventType: type, content: nil,
            payload: ["turn_id": turn.map { .string($0) } ?? .null, "job_id": .string(job), "blocks": .array(blocks)],
            createdAt: Date(timeIntervalSince1970: 0)
        )
    }

    /// What an older (feed-only) server sends: seven sections, no post caption.
    private let legacySections = PlanSectionID.allCases.filter { $0 != .postCaption }

    private var allWaiting: [JSONValue] { legacySections.map { block($0.rawValue, "waiting") } }

    func testBlocksComeBackInDisplayOrderRegardlessOfArrival() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, [block("look", "waiting"), block("sfx", "waiting"), block("title", "waiting")]),
            event(2, [block("music", "waiting"), block("captions", "waiting"), block("overlays", "waiting"), block("clips", "waiting")]),
        ])
        XCTAssertEqual(feed.blocks.map(\.section), [.title, .clips, .captions, .music, .sfx, .overlays, .look])
    }

    func testCountsAndCompletion() {
        var events = [event(1, allWaiting)]
        var feed = PlanBlockFeedState.reduce(events: events)
        XCTAssertEqual(feed.totalCount, 7)
        XCTAssertEqual(feed.decidedCount, 0)
        XCTAssertFalse(feed.isComplete)
        events.append(event(2, [block("title", "decided", summary: "Hello"), block("clips", "deciding")]))
        feed = PlanBlockFeedState.reduce(events: events)
        XCTAssertEqual(feed.decidedCount, 1)
        XCTAssertEqual(feed.newestDecided, .title)
        events.append(event(3, legacySections.map { block($0.rawValue, "decided", summary: $0.label) }))
        feed = PlanBlockFeedState.reduce(events: events)
        XCTAssertTrue(feed.isComplete)
        XCTAssertEqual(feed.progress, 1)
    }

    func testStateOnlyMovesForwardSoOutOfOrderEventsAreHarmless() {
        // The decided event is applied last by sequence-sorted reduce, but a late "deciding" with a LOWER
        // sequence must not matter either way; and a stale waiting that arrives later in sequence is ignored.
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, allWaiting),
            event(2, [block("music", "decided", summary: "Golden Hour")]),
            event(3, [block("music", "deciding")]),
            event(4, [block("music", "waiting")]),
        ])
        let music = feed.blocksBySection[.music]
        XCTAssertEqual(music?.state, .decided)
        XCTAssertEqual(music?.summary, "Golden Hour")
    }

    func testArrivalOrderOfTheArrayDoesNotMatterOnlySequenceDoes() {
        let events = [
            event(3, [block("music", "decided", summary: "Golden Hour")]),
            event(1, allWaiting),
            event(2, [block("music", "deciding")]),
        ]
        XCTAssertEqual(PlanBlockFeedState.reduce(events: events).blocksBySection[.music]?.state, .decided)
        XCTAssertEqual(PlanBlockFeedState.reduce(events: events.reversed()).blocksBySection[.music]?.state, .decided)
    }

    func testDuplicateEventsAreIdempotent() {
        let once = PlanBlockFeedState.reduce(events: [event(1, allWaiting), event(2, [block("title", "decided", summary: "Hi")])])
        let twice = PlanBlockFeedState.reduce(events: [
            event(1, allWaiting), event(2, [block("title", "decided", summary: "Hi")]),
            event(3, [block("title", "decided", summary: "Hi")]), event(4, allWaiting),
        ])
        XCTAssertEqual(once.decidedCount, twice.decidedCount)
        XCTAssertEqual(twice.blocksBySection[.title]?.summary, "Hi")
        XCTAssertEqual(twice.blocksBySection[.title]?.state, .decided)
    }

    func testRepeatedDecidedKeepsTextWhenLaterEventHasNone() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, [block("title", "decided", summary: "Hi")]),
            event(2, [block("title", "decided", summary: nil)]),
        ])
        XCTAssertEqual(feed.blocksBySection[.title]?.summary, "Hi")
    }

    func testANewJobIDResetsTheFeed() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, job: "job-1", allWaiting),
            event(2, job: "job-1", [block("title", "decided", summary: "Old")]),
            event(3, job: "job-2", turn: "turn-2", [block("title", "waiting"), block("clips", "waiting")]),
        ])
        XCTAssertEqual(feed.jobID, "job-2")
        XCTAssertEqual(feed.turnID, "turn-2")
        XCTAssertEqual(feed.totalCount, 2)
        XCTAssertEqual(feed.decidedCount, 0)
        XCTAssertNil(feed.newestDecided)
    }

    func testSkippedSectionsCountAsDecidedAndReadNotUsed() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, allWaiting),
            event(2, [block("overlays", "decided", skipped: true)]),
        ])
        XCTAssertEqual(feed.decidedCount, 1)
        let overlays = feed.blocksBySection[.overlays]
        XCTAssertEqual(overlays?.displaySummary, "Not used")
        XCTAssertEqual(overlays?.skipped, true)
    }

    func testIntentAndDecidedAtDecode() throws {
        let feed = PlanBlockFeedState.reduce(events: [event(1, [block("music", "deciding", summary: "A calm track", intent: true)])])
        let music = try XCTUnwrap(feed.blocksBySection[.music])
        XCTAssertTrue(music.intent)
        let decided = PlanBlockFeedState.reduce(events: [event(1, [block("music", "decided", summary: "X")])])
        XCTAssertNotNil(decided.blocksBySection[.music]?.decidedAt)
    }

    func testUnknownSectionsStatesAndMalformedBlocksAreIgnored() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, [
                block("hologram", "decided"), block("title", "teleporting"), .string("junk"),
                .object(["state": .string("decided")]), block("clips", "waiting"),
            ]),
        ])
        XCTAssertEqual(feed.blocks.map(\.section), [.clips])
    }

    func testEventsWithoutAJobIDOrOtherTypesAreIgnored() {
        var noJob = event(1, allWaiting)
        noJob = ThreadEvent(id: "x", sequence: 1, revision: 1, role: "system", eventType: "plan_block", content: nil, payload: ["blocks": .array(allWaiting)], createdAt: Date(timeIntervalSince1970: 0))
        let other = event(2, allWaiting, type: "render_queued")
        XCTAssertTrue(PlanBlockFeedState.reduce(events: [noJob, other]).isEmpty)
    }

    func testNullTurnIDKeepsAnEarlierTurnID() {
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, turn: "turn-1", allWaiting),
            event(2, turn: nil, [block("title", "deciding")]),
        ])
        XCTAssertEqual(feed.turnID, "turn-1")
    }

    func testRenderCancelledMarksOnlyItsJob() {
        let cancelled = ThreadEvent(id: "c", sequence: 5, revision: 6, role: "system", eventType: "render_cancelled", content: nil,
                                    payload: ["turn_id": .string("turn-1"), "job_id": .string("job-1")], createdAt: Date(timeIntervalSince1970: 0))
        XCTAssertTrue(PlanBlockFeedState.reduce(events: [event(1, allWaiting), cancelled]).isCancelled)
        let other = ThreadEvent(id: "c2", sequence: 5, revision: 6, role: "system", eventType: "render_cancelled", content: nil,
                                payload: ["turn_id": .string("t"), "job_id": .string("job-9")], createdAt: Date(timeIntervalSince1970: 0))
        XCTAssertFalse(PlanBlockFeedState.reduce(events: [event(1, allWaiting), other]).isCancelled)
    }

    // MARK: visibility + settle + decoding

    func testVisibilityFailsClosedWithoutTheCapabilityOrV2() {
        let feed = PlanBlockFeedState.reduce(events: [event(1, allWaiting)])
        XCTAssertTrue(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: feed, activeJobID: "job-1"))
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: nil, runtimeVersion: 2, feed: feed, activeJobID: "job-1"))
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: false, runtimeVersion: 2, feed: feed, activeJobID: "job-1"))
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 1, feed: feed, activeJobID: "job-1"))
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: .empty, activeJobID: "job-1"))
    }

    func testAFeedFromAnOlderJobIsNotShownDuringARerender() {
        let feed = PlanBlockFeedState.reduce(events: [event(1, job: "job-1", allWaiting)])
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: feed, activeJobID: "job-2"))
        XCTAssertTrue(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: feed, activeJobID: "JOB-1"))
        XCTAssertTrue(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: feed, activeJobID: nil))
    }

    func testACancelledFeedIsHidden() {
        let cancelled = ThreadEvent(id: "c", sequence: 5, revision: 6, role: "system", eventType: "render_cancelled", content: nil,
                                    payload: ["job_id": .string("job-1")], createdAt: Date(timeIntervalSince1970: 0))
        let feed = PlanBlockFeedState.reduce(events: [event(1, allWaiting), cancelled])
        XCTAssertFalse(PlanFeedVisibility.shows(capabilityEnabled: true, runtimeVersion: 2, feed: feed, activeJobID: "job-1"))
    }

    func testPlanBlockEventsSettleTheThinkingRowButAreNotChatMessages() {
        let planBlock = event(9, allWaiting)
        XCTAssertTrue(ChatThinkingSettlement.settles(planBlock))
        XCTAssertNil(ChatTranscriptMessage.from(event: planBlock))
    }

    func testCapabilityDecodesAndFailsClosedWhenMissing() throws {
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertNotEqual(off.livePlanReviewEnabled, true)
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"live_plan_review_enabled":true}"#.utf8))
        XCTAssertEqual(on.livePlanReviewEnabled, true)
        let explicitOff = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"live_plan_review_enabled":false}"#.utf8))
        XCTAssertNotEqual(explicitOff.livePlanReviewEnabled, true)
    }

    func testTurnCancelledDecodesWithAndWithoutApprovalIDs() throws {
        let full = try JSONDecoder().decode(TurnCancelled.self, from: Data(#"{"turn_id":"t","thread_revision":12,"status":"cancelled","approval_ids":["a"]}"#.utf8))
        XCTAssertEqual(full, TurnCancelled(turnID: "t", threadRevision: 12, status: "cancelled", approvalIDs: ["a"]))
        let bare = try JSONDecoder().decode(TurnCancelled.self, from: Data(#"{"turn_id":"t","thread_revision":1,"status":"cancelled"}"#.utf8))
        XCTAssertEqual(bare.approvalIDs, [])
    }

    func testPlanBlockEventDecodesFromTheWireShape() throws {
        let json = #"""
        {"id":"1","sequence":4,"revision":5,"role":"system","event_type":"plan_block","content":"","created_at":"2026-10-05T10:00:00Z",
         "payload":{"turn_id":null,"job_id":"j","blocks":[{"section_id":"music","state":"decided","summary":"Song","detail":null,"intent":false,"skipped":false,"decided_at":"2026-10-05T10:00:01+00:00"}]}}
        """#
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        let event = try decoder.decode(ThreadEvent.self, from: Data(json.utf8))
        let feed = PlanBlockFeedState.reduce(events: [event])
        XCTAssertEqual(feed.blocksBySection[.music]?.summary, "Song")
        XCTAssertNil(feed.turnID)
        XCTAssertNotNil(feed.blocksBySection[.music]?.decidedAt)
    }

    // MARK: Live plan contract v2 (KRI-439 / KRI-450): decode with fallback, revision-aware reducer

    private func v2Block(_ section: String, _ state: String, summary: String? = nil, revision: Int = 0, changed: Bool = false,
                         payload: JSONValue? = nil, previous: JSONValue? = nil) -> JSONValue {
        guard case .object(var fields) = block(section, state, summary: summary) else { return .null }
        fields["revision"] = .number(Double(revision)); fields["changed"] = .bool(changed)
        if let payload { fields["payload"] = payload }
        if let previous { fields["previous"] = previous }
        return .object(fields)
    }

    func testPostCaptionIsTheEighthSectionAndDisplayOnly() {
        XCTAssertEqual(PlanSectionID.allCases.last, .postCaption)
        XCTAssertFalse(PlanSectionID.postCaption.isScopable)
        XCTAssertTrue(PlanSectionID.allCases.dropLast().allSatisfy(\.isScopable))
        let feed = PlanBlockFeedState.reduce(events: [event(1, PlanSectionID.allCases.map { block($0.rawValue, "waiting") })])
        XCTAssertEqual(feed.totalCount, 8)
        XCTAssertEqual(feed.blocks.last?.section, .postCaption)
    }

    func testAnOldSevenSectionEventStillWorks() {
        let feed = PlanBlockFeedState.reduce(events: [event(1, allWaiting), event(2, legacySections.map { block($0.rawValue, "decided", summary: "x") })])
        XCTAssertEqual(feed.totalCount, 7)
        XCTAssertTrue(feed.isComplete)
        XCTAssertTrue(feed.blocks.allSatisfy { $0.payload == nil && $0.revision == 0 && !$0.changed })
    }

    func testStructuredPayloadsDecodeAndAMalformedOneFallsBackToTheSummary() throws {
        let clips: JSONValue = .object(["total_duration_s": .number(24), "clips": .array([
            .object(["index": .number(0), "start_s": .number(0), "end_s": .number(4), "transition": .string("dissolve")]),
            .object(["index": .string("bad")]),                                        // a bad row is skipped, not fatal
            .object(["index": .number(2), "start_s": .number(8), "end_s": .number(12), "transition": .string("banana")]),
        ])])
        let feed = PlanBlockFeedState.reduce(events: [event(1, [
            v2Block("clips", "decided", summary: "3 clips", revision: 1, payload: clips),
            v2Block("title", "decided", summary: "Hi", revision: 1, payload: .string("garbage")),
            v2Block("captions", "decided", summary: "4 lines", revision: 1, payload: .object(["lines": .string("nope")])),
            v2Block("music", "decided", summary: "Song", revision: 1, payload: .object(["source": .string("???"), "bpm": .number(112)])),
        ])])
        guard case .clips(let payload)? = feed.blocksBySection[.clips]?.payload else { return XCTFail("clips payload") }
        XCTAssertEqual(payload.clips.map(\.index), [0, 2])
        XCTAssertEqual(payload.clips.first?.transition, .dissolve)
        XCTAssertNil(payload.clips.last?.transition, "an unmapped transition is nil, never a guess")
        XCTAssertNil(feed.blocksBySection[.title]?.payload, "a non-object payload falls back")
        XCTAssertEqual(feed.blocksBySection[.title]?.displaySummary, "Hi")
        guard case .captions(let captions)? = feed.blocksBySection[.captions]?.payload else { return XCTFail("captions payload") }
        XCTAssertTrue(captions.lines.isEmpty)
        guard case .music(let music)? = feed.blocksBySection[.music]?.payload else { return XCTFail("music payload") }
        XCTAssertEqual(music.source, .catalog, "an unknown source decodes to the documented default")
        XCTAssertEqual(music.bpm, 112)
    }

    func testAHigherRevisionReplacesTheWholeBlockAndALowerOneIsIgnored() {
        let previous: JSONValue = .object(["revision": .number(1), "job_id": .string("job-0"), "summary": .string("Old"), "skipped": .bool(false),
                                           "payload": .object(["text": .string("Old title")])])
        let feed = PlanBlockFeedState.reduce(events: [
            event(1, [v2Block("title", "decided", summary: "Old", revision: 1, payload: .object(["text": .string("Old title")]))]),
            event(2, [v2Block("title", "decided", summary: "New", revision: 2, changed: true, payload: .object(["text": .string("New title")]), previous: previous)]),
            event(3, [v2Block("title", "decided", summary: "Stale", revision: 1)]),
        ])
        let title = feed.blocksBySection[.title]
        XCTAssertEqual(title?.summary, "New")
        XCTAssertEqual(title?.revision, 2)
        XCTAssertEqual(title?.changed, true)
        XCTAssertEqual(title?.previous?.summary, "Old")
        XCTAssertEqual(title?.previous?.jobID, "job-0")
        guard case .title(let payload)? = title?.payload else { return XCTFail("payload") }
        XCTAssertEqual(payload.text, "New title")
        guard case .title(let was)? = title?.previous?.payload else { return XCTFail("previous payload") }
        XCTAssertEqual(was.text, "Old title")
    }

    func testScopeAndPreviousJobComeFromTheEventTopLevel() {
        var scoped = event(1, [v2Block("captions", "deciding", revision: 1)])
        scoped = ThreadEvent(id: "s", sequence: 1, revision: 2, role: "system", eventType: "plan_block", content: nil,
                             payload: ["turn_id": .string("t"), "job_id": .string("job-2"), "scope": .array([.string("captions"), .string("hologram")]),
                                       "previous_job_id": .string("job-1"), "blocks": .array([v2Block("captions", "deciding", revision: 1)])],
                             createdAt: Date(timeIntervalSince1970: 0))
        let feed = PlanBlockFeedState.reduce(events: [scoped])
        XCTAssertEqual(feed.scope, [.captions])
        XCTAssertEqual(feed.previousJobID, "job-1")
    }

    func testReviewIsOfferedOnlyOnAContractV2Server() throws {
        func caps(_ extra: String) throws -> CreationCapabilities {
            try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"live_plan_review_enabled":true\#(extra)}"#.utf8))
        }
        XCTAssertFalse(try caps("").livePlanReviewAvailable)
        XCTAssertFalse(try caps(#","live_plan_review_version":1"#).livePlanReviewAvailable)
        XCTAssertTrue(try caps(#","live_plan_review_version":2"#).livePlanReviewAvailable)
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"live_plan_review_enabled":false,"live_plan_review_version":2}"#.utf8))
        XCTAssertFalse(off.livePlanReviewAvailable)
    }

    // MARK: Device-build pacing (KRI-443 follow-up)

    /// What the server sends an iPhone account: everything decided at once, overlays skipped.
    private var serverAllDecided: PlanBlockFeedState {
        PlanBlockFeedState.reduce(events: [event(1, allWaiting), event(2, legacySections.map {
            $0 == .overlays ? block($0.rawValue, "decided", skipped: true) : block($0.rawValue, "decided", summary: "\($0.label) value")
        })])
    }

    private func states(_ feed: PlanBlockFeedState) -> [PlanSectionID: PlanBlockState] {
        Dictionary(uniqueKeysWithValues: feed.blocks.map { ($0.section, $0.state) })
    }

    func testNoDeviceStageLeavesTheServerFeedUntouched() {
        let feed = serverAllDecided
        XCTAssertEqual(feed.paced(by: nil), feed)
        XCTAssertTrue(feed.paced(by: nil).isComplete)
    }

    func testPreparingShowsOnlyClipsDecidingEvenWhenServerDecidedEverything() {
        let paced = serverAllDecided.paced(by: .preparing)
        XCTAssertEqual(paced.decidedCount, 0)
        XCTAssertEqual(states(paced)[.clips], .deciding)
        XCTAssertEqual(states(paced)[.title], .waiting)
        XCTAssertEqual(states(paced)[.overlays], .waiting, "a skipped section waits for the compose stage")
        XCTAssertNil(paced.blocksBySection[.clips]?.decidedAt)
    }

    func testRenderingAdvancesSectionsInDeviceWorkOrderAsTheExporterProgresses() {
        // 6 active sections (overlays skipped): clips, music, title, captions, look, sfx.
        let early = serverAllDecided.paced(by: .rendering(fraction: 0))
        XCTAssertEqual(states(early)[.overlays], .decided, "skipped resolves at the compose boundary")
        XCTAssertEqual(states(early)[.clips], .deciding)
        XCTAssertEqual(states(early)[.music], .waiting)

        let mid = serverAllDecided.paced(by: .rendering(fraction: 0.5))  // 3 of 6 done
        XCTAssertEqual(states(mid)[.clips], .decided)
        XCTAssertEqual(states(mid)[.music], .decided)
        XCTAssertEqual(states(mid)[.title], .decided)
        XCTAssertEqual(states(mid)[.captions], .deciding)
        XCTAssertEqual(states(mid)[.look], .waiting)
        XCTAssertEqual(mid.newestDecided, .title)
        XCTAssertEqual(mid.blocksBySection[.clips]?.summary, "Clips value", "values still come from the server")

        let late = serverAllDecided.paced(by: .rendering(fraction: 1))
        XCTAssertTrue(late.isComplete)
    }

    func testRenderingWithoutProgressStaysOnTheFirstSection() {
        let paced = serverAllDecided.paced(by: .rendering(fraction: nil))
        XCTAssertEqual(states(paced)[.clips], .deciding)
        XCTAssertEqual(states(paced)[.sfx], .waiting)
    }

    func testFinishedBuildDecidesEverything() {
        let paced = serverAllDecided.paced(by: .finished)
        XCTAssertTrue(paced.isComplete)
        XCTAssertEqual(paced.newestDecided, .sfx)
    }

    func testDisplayedStateIsNeverAheadOfTheServer() {
        // Server has only decided title; the device is already done composing. min() keeps the others where the server is.
        let server = PlanBlockFeedState.reduce(events: [event(1, allWaiting), event(2, [block("title", "decided", summary: "T"), block("clips", "deciding")])])
        let paced = server.paced(by: .finished)
        XCTAssertEqual(states(paced)[.title], .decided)
        XCTAssertEqual(states(paced)[.clips], .deciding)
        XCTAssertEqual(states(paced)[.music], .waiting)
        XCTAssertFalse(paced.isComplete)
    }

    func testStageMappingFromDevicePhase() {
        XCTAssertEqual(DeviceBuildStage(phase: .preparing, exportProgress: 0.4), .preparing)
        XCTAssertEqual(DeviceBuildStage(phase: .rendering, exportProgress: 0.4), .rendering(fraction: 0.4))
        for phase in [DeviceRenderPhase.localReady, .syncing, .synced] {
            XCTAssertEqual(DeviceBuildStage(phase: phase, exportProgress: nil), .finished)
        }
        for phase in [DeviceRenderPhase.cancelled, .needsAttention, .superseded] {
            XCTAssertNil(DeviceBuildStage(phase: phase, exportProgress: nil), "fails closed to the server states")
        }
    }

    func testEmptyFeedStaysEmptyWhenPaced() {
        XCTAssertTrue(PlanBlockFeedState.empty.paced(by: .preparing).isEmpty)
    }
}
