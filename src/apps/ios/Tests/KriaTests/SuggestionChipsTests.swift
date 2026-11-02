import XCTest
@testable import Kria

/// KRI-756. Cross-platform parity fixtures: the JSON below is shaped like the backend's `SuggestionSetOut`
/// (docs/pipelines/suggestion-chips.md), plus a future kind, a future key and a broken chip, which an older app
/// must tolerate.
final class SuggestionChipsTests: XCTestCase {
    private let firstPrompt = Data("""
    {"schema_version":1,"set_id":"sg_c17d74f31cc7783e","state":"first_prompt","status":"ok","understanding":"done",
     "retry_after_s":null,"wording":"agent",
     "anchor":{"media_digest":"065dfcfb4045d98ddfd115cb","manifest_hash":"x","render_generation_id":null,"variant_id":null},
     "brand_new_key":{"a":1},
     "chips":[
      {"id":"sc_e80c892d02e3","kind":"default_edit","label":"Let Kria cut the first version","message":"Suggest an edit.","rank":1,"default_edit":true,
       "anchor":{"media_id":null,"clip_ordinal":null,"moment":null,"text_bar_index":null},"capability":"dispatch_render",
       "why":null,"label_source":"template","requires_protocol":null},
      {"id":"sc_2","kind":"hook","label":"Add a hook to the opening","message":"Add a hook that matches the video","rank":2,
       "why":"the first shot is a quiet one","label_source":"agent","requires_protocol":null},
      {"id":"sc_3","kind":"a_kind_from_the_future","label":"Try the new thing","message":"Try the new thing for me","rank":3},
      {"id":"sc_bad","kind":"hook","rank":4},
      {"id":"sc_5","kind":"sfx","label":"Add sound effects","message":"Add sound effects","rank":4}]}
    """.utf8)

    private func decode(_ json: String) throws -> SuggestionSet {
        try JSONDecoder().decode(SuggestionSet.self, from: Data(json.utf8))
    }

    func testContractResponseDecodesInRankOrderAndDropsOnlyTheBrokenChip() throws {
        let set = try JSONDecoder().decode(SuggestionSet.self, from: firstPrompt)
        XCTAssertEqual(set.state, "first_prompt")
        XCTAssertEqual(set.status, "ok")
        XCTAssertEqual(set.wording, "agent")
        XCTAssertEqual(set.chips.map(\.label), ["Let Kria cut the first version", "Add a hook to the opening", "Try the new thing", "Add sound effects"])
        XCTAssertEqual(set.chips[0].message, ChatSubmission.mediaOnlyMessage, "Chip #1 must send the stock media-only message")
        XCTAssertTrue(set.chips[0].isDefaultEdit)
        XCTAssertNil(set.chips[0].why)
        XCTAssertEqual(set.chips[1].why, "the first shot is a quiet one")
        XCTAssertEqual(set.chips[1].accessibilityText, "Add a hook to the opening, the first shot is a quiet one")
        XCTAssertEqual(set.chips[1].labelSource, "agent")
        XCTAssertEqual(set.chips(forState: "first_prompt").count, 4)
        XCTAssertTrue(set.chips(forState: "ready").isEmpty, "A first-prompt set never feeds the ready card")
        XCTAssertNil(set.retryDelay)
    }

    func testAnUnknownKindStillRendersByLabelAndSendsItsMessage() throws {
        let set = try JSONDecoder().decode(SuggestionSet.self, from: firstPrompt)
        let future = try XCTUnwrap(set.chips(forState: "first_prompt").first { $0.kind == "a_kind_from_the_future" })
        XCTAssertEqual(future.label, "Try the new thing")
        XCTAssertEqual(future.message, "Try the new thing for me")
    }

    func testAnUnknownStateOrStatusRendersNothing() throws {
        let chip = #"{"id":"a","kind":"hook","label":"Add a hook","message":"Add a hook that matches the video","rank":1}"#
        let future = try decode(#"{"state":"mid_render","status":"ok","chips":[\#(chip)]}"#)
        XCTAssertEqual(future.chips.count, 1, "Decodes fine, draws nowhere")
        XCTAssertTrue(future.chips(forState: "first_prompt").isEmpty)
        XCTAssertTrue(future.chips(forState: "ready").isEmpty)
        let futureStatus = try decode(#"{"state":"ready","status":"degraded","chips":[\#(chip)]}"#)
        XCTAssertTrue(futureStatus.chips(forState: "ready").isEmpty)
    }

    func testUnavailableAndEmptyBodiesRenderNothing() throws {
        let off = try decode(#"{"schema_version":1,"status":"unavailable","state":"none","chips":[]}"#)
        XCTAssertTrue(off.chips(forState: "first_prompt").isEmpty)
        XCTAssertTrue(off.chips(forState: "ready").isEmpty)
        let bare = try decode("{}")
        XCTAssertEqual(bare.status, "unavailable")
        XCTAssertTrue(bare.chips.isEmpty)
        // Unavailable never draws even if a server (wrongly) sent chips with it.
        let odd = try decode(#"{"state":"first_prompt","status":"unavailable","chips":[{"id":"a","label":"Add captions","message":"Add captions","rank":1}]}"#)
        XCTAssertTrue(odd.chips(forState: "first_prompt").isEmpty)
    }

    func testChipsAreSortedByRankAndCappedAtFourWithoutDuplicates() throws {
        func chip(_ id: String, rank: Int, message: String? = nil) -> String {
            #"{"id":"\#(id)","kind":"k","label":"L \#(id)","message":"\#(message ?? "m " + id)","rank":\#(rank)}"#
        }
        let set = try decode("""
        {"state":"ready","status":"ok","chips":[\(chip("d", rank: 4)),\(chip("b", rank: 2)),\(chip("a", rank: 1)),\(chip("a", rank: 9)),
         \(chip("dup", rank: 3, message: "m b")),\(chip("c", rank: 3)),\(chip("e", rank: 5))]}
        """)
        XCTAssertEqual(set.chips.map(\.id), ["a", "b", "c", "d"], "rank order, repeated id and repeated message dropped, at most four")
    }

    func testOverlongOrBadWhyIsDroppedButTheChipSurvives() throws {
        let long = String(repeating: "x", count: SuggestionChip.maxWhyLength + 1)
        let set = try decode("""
        {"state":"ready","status":"ok","chips":[
         {"id":"a","label":"Add captions","message":"Add captions","rank":1,"why":"\(long)"},
         {"id":"b","label":"Add a hook","message":"Add a hook that matches the video","rank":2,"why":42,"label_source":7,"requires_protocol":"x"}]}
        """)
        XCTAssertEqual(set.chips.count, 2)
        XCTAssertNil(set.chips[0].why)
        XCTAssertNil(set.chips[1].why)
        XCTAssertNil(set.chips[1].requiresProtocol)
    }

    func testChipsThatNeedANewerProtocolAreDroppedLocally() throws {
        let set = try decode("""
        {"state":"ready","status":"ok","chips":[
         {"id":"a","label":"Add captions","message":"Add captions","rank":1,"requires_protocol":null},
         {"id":"b","label":"Bend the title","message":"Bend the title","rank":2,"requires_protocol":9},
         {"id":"c","label":"Newer thing","message":"Do the newer thing","rank":3,"requires_protocol":33}]}
        """)
        XCTAssertEqual(set.chips(forState: "ready").map(\.id), ["a", "b"], "Protocol 33 is above this app")
        XCTAssertEqual(set.chips(forState: "ready", appProtocol: 8).map(\.id), ["a"])
        XCTAssertEqual(set.chips(forState: "ready", appProtocol: 33).map(\.id), ["a", "b", "c"])
        XCTAssertEqual(KriaClientProtocolContract.version, 32, "Update this fixture when the app's protocol moves")
    }

    func testCapabilityDefaultsOffWhenKeyIsMissing() throws {
        let missing = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(missing.suggestionChipsEnabled)
        XCTAssertFalse(SuggestionChipsPolicy.isEnabled(missing))
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"suggestion_chips":true}"#.utf8))
        XCTAssertTrue(SuggestionChipsPolicy.isEnabled(on))
        XCTAssertFalse(SuggestionChipsPolicy.isEnabled(nil))
    }

    func testSurfaceFollowsStageAndHidesOnceTheCreatorHasSpoken() {
        func surface(_ stage: WorkspaceStage, enabled: Bool = true, media: Int = 2, pending: Int = 0, said: Bool = false,
                     slides: Bool = false, thinking: Bool = false, question: Bool = false) -> SuggestionSurface? {
            SuggestionChipsPolicy.surface(enabled: enabled, stage: stage, isSlides: slides, isFormatKnown: true, readyMediaCount: media,
                                          pendingUploadCount: pending, hasUserMessage: said, hasPendingMessage: thinking, hasOpenQuestion: question)
        }
        XCTAssertEqual(surface(.footage), .firstPrompt)
        XCTAssertEqual(surface(.ready), .ready)
        XCTAssertNil(surface(.footage, enabled: false), "Capability off: no row and no request")
        XCTAssertNil(surface(.footage, media: 0), "No media: no chips")
        XCTAssertNil(surface(.footage, pending: 1), "Clips still uploading")
        XCTAssertNil(surface(.footage, said: true))
        XCTAssertNil(surface(.footage, slides: true))
        XCTAssertNil(surface(.footage, thinking: true))
        XCTAssertNil(surface(.ready, thinking: true), "A turn or send in flight")
        XCTAssertNil(surface(.footage, question: true))
        XCTAssertNil(surface(.ready, question: true), "An open question owns the screen")
        for stage in [WorkspaceStage.format, .direction, .rendering, .failed] { XCTAssertNil(surface(stage)) }
    }

    func testOnlyAnUnansweredAssistantQuestionSuppressesChips() throws {
        func event(role: String, type: String, content: String = "Which?", payload: [String: JSONValue]? = nil) -> ThreadEvent {
            ThreadEvent(id: UUID().uuidString, sequence: 1, revision: 1, role: role, eventType: type, content: content, payload: payload, createdAt: .now)
        }
        let withChips = ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_response",
            payload: ["turn_value": .string("question"), "options": .array([.string("About 1 second"), .string("About 0.5 seconds")])]))
        XCTAssertEqual(withChips?.asksAQuestion, true, "Reply options are an open question")
        let legacy = ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_question",
            payload: ["options": .array([.string("A"), .string("B")])]))
        XCTAssertEqual(legacy?.asksAQuestion, true)
        let plain = ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_response", content: "Done."))
        XCTAssertEqual(plain?.asksAQuestion, false)
        let user = ChatTranscriptMessage.from(event: event(role: "user", type: "user_message", content: "Add captions"))
        XCTAssertEqual(user?.asksAQuestion, false)
    }

    func testADeliveredSetIsNeverReRankedOrRelabelledUnderTheSameSetID() throws {
        let lead = SuggestionChip(id: "d", kind: "default_edit", label: "Let Kria cut the first version", message: "Suggest an edit.", rank: 1, isDefaultEdit: true)
        let pending = SuggestionSet(setID: "sg_1", state: "first_prompt", status: "pending", understanding: "pending", retryAfterSeconds: 2, wording: "pending", chips: [lead])
        let delivered = SuggestionSet(setID: "sg_1", state: "first_prompt", status: "ok", wording: "agent",
                                      chips: [lead, SuggestionChip(id: "h", kind: "hook", label: "Add a hook", message: "Add a hook that matches the video", rank: 2)])
        let reworded = SuggestionSet(setID: "sg_1", state: "first_prompt", status: "ok", wording: "fallback",
                                     chips: [SuggestionChip(id: "h", kind: "hook", label: "Hook it", message: "Add a hook that matches the video", rank: 1), lead])
        let next = SuggestionSet(setID: "sg_2", state: "first_prompt", status: "ok", chips: [lead])
        let none = SuggestionSet(setID: "sg_1", state: "none", status: "ok", chips: [])

        XCTAssertEqual(SuggestionChipsPolicy.merge(current: nil, incoming: pending), pending)
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: pending, incoming: delivered), delivered, "pending is replaced by the final set")
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: delivered, incoming: reworded), delivered, "same set_id: frozen")
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: delivered, incoming: next), next, "a new set_id replaces")
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: delivered, incoming: none), none, "the server saying none hides the chips")
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: delivered, incoming: nil), delivered, "a failed ask keeps what is held")
    }

    @MainActor func testPendingIsPolledUntilItSettlesAndNoLongerThanTheCeiling() async {
        let lead = SuggestionChip(id: "d", kind: "default_edit", label: "Let Kria cut the first version", message: "Suggest an edit.", rank: 1, isDefaultEdit: true)
        let pending = SuggestionSet(setID: "a", state: "first_prompt", status: "pending", understanding: "pending", retryAfterSeconds: 3, wording: "pending", chips: [lead])
        let done = SuggestionSet(setID: "a", state: "first_prompt", status: "ok", chips: [lead])
        XCTAssertEqual(pending.chips(forState: "first_prompt").map(\.id), ["d"], "Pending shows chip #1 alone")

        let answers = Counter()
        let shown = Recorder()
        await SuggestionChipsPolicy.poll(
            fetch: { await answers.next() < 3 ? pending : done },
            onSet: { shown.record($0) },
            sleep: { _ in }
        )
        XCTAssertEqual(shown.sets.map { $0?.status }, ["pending", "pending", "pending", "ok"])

        // Never settles: stops at 60 s of waiting (20 waits of 3 s), not forever.
        let stuck = Counter()
        await SuggestionChipsPolicy.poll(fetch: { _ = await stuck.next(); return pending }, onSet: { _ in }, sleep: { _ in })
        let asked = await stuck.next()
        XCTAssertEqual(asked, 21, "The first ask plus 20 three-second retries, then it gives up")
    }

    func testRetryDelayIsClampedAndOnlyExistsWhilePending() throws {
        XCTAssertEqual(try decode(#"{"state":"ready","status":"pending","retry_after_s":2,"chips":[]}"#).retryDelay, .milliseconds(2000))
        XCTAssertEqual(try decode(#"{"state":"ready","status":"pending","retry_after_s":0,"chips":[]}"#).retryDelay, .milliseconds(1000))
        XCTAssertEqual(try decode(#"{"state":"ready","status":"pending","retry_after_s":900,"chips":[]}"#).retryDelay, .milliseconds(10000))
        XCTAssertEqual(try decode(#"{"state":"ready","status":"pending","chips":[]}"#).retryDelay, .milliseconds(3000))
        XCTAssertNil(try decode(#"{"state":"ready","status":"ok","retry_after_s":2,"chips":[]}"#).retryDelay)
    }

    func testTheTurnBodyCarriesTheTapOnlyWhenThereIsOne() throws {
        func body(_ tap: SuggestionTap?) throws -> [String: Any] {
            let request = SubmitTurnRequest(message: "Add captions", clientEventID: "c", expectedThreadRevision: 4, suggestion: tap)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        }
        let with = try body(SuggestionTap(setID: "sg_1", chipID: "sc_2"))
        let tap = try XCTUnwrap(with["suggestion"] as? [String: String])
        XCTAssertEqual(tap, ["set_id": "sg_1", "chip_id": "sc_2"])
        XCTAssertEqual(with["message"] as? String, "Add captions", "The message is the chip's message, never its label")
        XCTAssertNil(try body(nil)["suggestion"], "An ordinary message must not grow a suggestion key")
    }

    private actor Counter {
        private var count = 0
        func next() -> Int { count += 1; return count - 1 }
    }
    @MainActor private final class Recorder {
        var sets: [SuggestionSet?] = []
        func record(_ set: SuggestionSet?) { sets.append(set) }
    }
}
