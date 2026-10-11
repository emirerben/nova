import XCTest
@testable import Kria

/// KRI-588: the chat question "Which part of the song did you film?". The contract rides in free-form event
/// payloads, so the failure modes that matter are: a malformed payload breaking the turn or hiding the message,
/// a whole-song answer losing its explicit nulls (absent and null mean different things to the server), a
/// relaunch asking again because the stored echo was unreadable, a card answering the wrong question, and a
/// card offering a range the server would refuse.
final class SongRangeQuestionTests: XCTestCase {
    private func payload(_ question: JSONValue) -> [String: JSONValue] { ["song_range_question": question] }

    private func question(_ fields: [String: JSONValue]) -> JSONValue { .object(fields) }

    // MARK: parsing the question

    func testParsesIdGenerationAndDuration() throws {
        let q = try XCTUnwrap(SongRangeQuestion.parse(payload: payload(question([
            "question_id": .string("q1"), "song_generation": .number(3), "song_duration_s": .number(92.5),
        ]))))
        XCTAssertEqual(q, SongRangeQuestion(questionID: "q1", songGeneration: 3, songDurationS: 92.5))
    }

    func testOnlyTheIdIsRequiredAndExtraKeysAreIgnored() throws {
        let q = try XCTUnwrap(SongRangeQuestion.parse(payload: payload(question([
            "question_id": .string("q1"), "version": .number(2), "future": .array([.string("x")]),
        ]))))
        XCTAssertEqual(q, SongRangeQuestion(questionID: "q1"))
    }

    func testAbsentOrMalformedPayloadsYieldNoQuestion() {
        XCTAssertNil(SongRangeQuestion.parse(payload: nil))
        XCTAssertNil(SongRangeQuestion.parse(payload: [:]))
        let bad: [JSONValue] = [
            .null, .string("q1"), .array([]), .number(4),
            question([:]),
            question(["question_id": .string("")]),
            question(["question_id": .number(7)]),
            question(["question_id": .null]),
            question(["song_duration_s": .number(90)]),
        ]
        for value in bad {
            XCTAssertNil(SongRangeQuestion.parse(payload: payload(value)), "\(value)")
            XCTAssertNil(SongRangeQuestion(json: value), "\(value)")
        }
    }

    func testBadOptionalFieldsAreDroppedAndNeverTheCard() throws {
        let durations: [JSONValue] = [.number(-30), .number(0), .number(.nan), .number(.infinity), .string("90"), .bool(true), .null]
        for duration in durations {
            let q = try XCTUnwrap(SongRangeQuestion.parse(payload: payload(question(["question_id": .string("q"), "song_duration_s": duration]))), "\(duration)")
            XCTAssertNil(q.songDurationS, "\(duration)")
        }
        let generations: [JSONValue] = [.number(1.5), .number(.nan), .number(1e12), .string("2"), .null]
        for generation in generations {
            let q = try XCTUnwrap(SongRangeQuestion.parse(payload: payload(question(["question_id": .string("q"), "song_generation": generation]))), "\(generation)")
            XCTAssertNil(q.songGeneration, "\(generation)")
        }
    }

    func testTranscriptMessageCarriesTheQuestionOnAssistantEventsAndTheAnswerOnUserEventsOnly() {
        func event(role: String, type: String, payload: [String: JSONValue]) -> ThreadEvent {
            ThreadEvent(id: type, sequence: 1, revision: 1, role: role, eventType: type, content: "Which part of the song did you film?", payload: payload, createdAt: .now)
        }
        let asked = payload(question(["question_id": .string("q1")]))
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_question", payload: asked))?.songRangeQuestion?.questionID, "q1")
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "user", type: "user_message", payload: asked))?.songRangeQuestion, "a user message never carries a card")
        let answered: [String: JSONValue] = ["song_range": .object(["question_id": .string("q1"), "start_s": .null, "end_s": .null])]
        XCTAssertEqual(ChatTranscriptMessage.from(event: event(role: "user", type: "user_message", payload: answered))?.songRangeAnswer?.questionID, "q1")
        XCTAssertNil(ChatTranscriptMessage.from(event: event(role: "assistant", type: "assistant_response", payload: answered))?.songRangeAnswer)
    }

    func testTurnResponseDecodesTheQuestionLeniently() throws {
        let good = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted","song_range_question":{"question_id":"q9","song_duration_s":90}}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(TurnAccepted.self, from: good).songRangeQuestion, SongRangeQuestion(questionID: "q9", songDurationS: 90))
        let plain = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted"}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(TurnAccepted.self, from: plain).songRangeQuestion)
        for garbage in [#"{"question_id":7}"#, #""q9""#, #"[1]"#, #"null"#] {
            let data = Data(#"{"turn_id":"t","thread_revision":3,"status":"accepted","song_range_question":\#(garbage)}"#.utf8)
            let decoded = try JSONDecoder().decode(TurnAccepted.self, from: data)
            XCTAssertNil(decoded.songRangeQuestion, "a malformed question must not fail the turn: \(garbage)")
            XCTAssertEqual(decoded.threadRevision, 3)
        }
    }

    // MARK: capability

    func testCapabilityNeedsTheFlagAndTheSongAffordance() throws {
        func caps(_ json: String) throws -> CreationCapabilities { try JSONDecoder().decode(CreationCapabilities.self, from: Data(json.utf8)) }
        let song = #""media":{"song":{"max":1,"max_file_bytes":52428800,"content_types":["audio/mpeg"]}}"#
        XCTAssertTrue(try caps(#"{"formats":[],"song_range_questions":true,\#(song)}"#).songRangeQuestionsEnabled)
        XCTAssertFalse(try caps(#"{"formats":[],"song_range_questions":true}"#).songRangeQuestionsEnabled, "no media.song, no card")
        XCTAssertFalse(try caps(#"{"formats":[],"song_range_questions":false,\#(song)}"#).songRangeQuestionsEnabled)
        XCTAssertFalse(try caps(#"{"formats":[],\#(song)}"#).songRangeQuestionsEnabled, "an older server omits it")
    }

    // MARK: the answer on the wire

    private func encoded(_ answer: SongRangeAnswer) throws -> [String: Any] {
        let data = try JSONEncoder().encode(answer)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testWholeSongSendsExplicitNullsNotAbsentKeys() throws {
        let json = try encoded(.wholeSong(questionID: "q1"))
        XCTAssertEqual(Set(json.keys), ["question_id", "start_s", "end_s"], "absent keys are not the same as null")
        XCTAssertTrue(json["start_s"] is NSNull)
        XCTAssertTrue(json["end_s"] is NSNull)
        XCTAssertEqual(json["question_id"] as? String, "q1")
    }

    func testAPartSendsWholeSecondsUnderTheContractKeys() throws {
        let json = try encoded(.part(questionID: "q1", range: SongFilmingRange(start: 40, end: 80)))
        XCTAssertEqual(json["start_s"] as? Double, 40)
        XCTAssertEqual(json["end_s"] as? Double, 80)
        XCTAssertEqual(Set(json.keys), ["question_id", "start_s", "end_s"])
    }

    func testTheTurnBodyCarriesSongRangeOnlyWhenThereIsOne() throws {
        func body(_ songRange: SongRangeAnswer?) throws -> [String: Any]? {
            let request = SubmitTurnRequest(message: "m", clientEventID: "c", expectedThreadRevision: 4, songRange: songRange)
            return try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any]
        }
        let with = try body(.wholeSong(questionID: "q1"))
        XCTAssertNotNil(with?["song_range"] as? [String: Any])
        let without = try body(nil)
        XCTAssertNil(without?["song_range"], "an ordinary message must not grow a song_range key")
    }

    func testAnUnreadableEchoCanNeverBeSent() {
        XCTAssertThrowsError(try JSONEncoder().encode(SongRangeAnswer(questionID: "q", choice: .unreadable)))
    }

    func testMessageTextMatchesTheContract() {
        XCTAssertEqual(SongRangeAnswer.part(questionID: "q", range: SongFilmingRange(start: 40, end: 80)).message, "I filmed from 0:40 to 1:20")
        XCTAssertEqual(SongRangeAnswer.part(questionID: "q", range: SongFilmingRange(start: 0, end: 5)).message, "I filmed from 0:00 to 0:05")
        XCTAssertEqual(SongRangeAnswer.part(questionID: "q", range: SongFilmingRange(start: 3_599, end: 3_600)).message, "I filmed from 59:59 to 1:00:00")
        XCTAssertEqual(SongRangeAnswer.wholeSong(questionID: "q").message, "Use the whole song")
    }

    func testSummariesRead() {
        XCTAssertEqual(SongRangeAnswer.part(questionID: "q", range: SongFilmingRange(start: 40, end: 80)).summary, "Filmed 0:40 - 1:20")
        XCTAssertEqual(SongRangeAnswer.part(questionID: "q", range: SongFilmingRange(start: 40, end: 80)).spokenSummary, "Filmed from 0:40 to 1:20")
        XCTAssertEqual(SongRangeAnswer.wholeSong(questionID: "q").summary, "Whole song")
    }

    // MARK: parsing the stored echo

    private func echo(_ fields: [String: JSONValue]) -> [String: JSONValue] { ["song_range": .object(fields)] }

    func testEchoParsesBackToWhatWasSent() {
        XCTAssertEqual(SongRangeAnswer.parse(payload: echo(["question_id": .string("q1"), "start_s": .number(40), "end_s": .number(80)])),
                       .part(questionID: "q1", range: SongFilmingRange(start: 40, end: 80)))
        XCTAssertEqual(SongRangeAnswer.parse(payload: echo(["question_id": .string("q1"), "start_s": .null, "end_s": .null])),
                       .wholeSong(questionID: "q1"))
        XCTAssertEqual(SongRangeAnswer.parse(payload: echo(["question_id": .string("q1")])),
                       .wholeSong(questionID: "q1"), "a server that drops the null keys still means the whole song")
    }

    func testEchoWithoutAQuestionIdIsNotAnAnswer() {
        XCTAssertNil(SongRangeAnswer.parse(payload: nil))
        XCTAssertNil(SongRangeAnswer.parse(payload: [:]))
        XCTAssertNil(SongRangeAnswer.parse(payload: ["song_range": .string("x")]))
        XCTAssertNil(SongRangeAnswer.parse(payload: echo(["start_s": .number(1), "end_s": .number(9)])))
        XCTAssertNil(SongRangeAnswer.parse(payload: echo(["question_id": .string("")])))
    }

    func testAnUnreadableRangeStillClosesTheQuestion() {
        let cases: [[String: JSONValue]] = [
            ["start_s": .number(40)],                                    // one end only
            ["end_s": .number(80)],
            ["start_s": .number(40), "end_s": .null],
            ["start_s": .string("40"), "end_s": .string("80")],
            ["start_s": .number(80), "end_s": .number(40)],              // reversed
            ["start_s": .number(40), "end_s": .number(40)],              // empty
            ["start_s": .number(-4), "end_s": .number(40)],
            ["start_s": .number(.nan), "end_s": .number(40)],
        ]
        for fields in cases {
            var all = fields; all["question_id"] = .string("q1")
            let answer = SongRangeAnswer.parse(payload: echo(all))
            XCTAssertEqual(answer?.choice, .unreadable, "\(fields)")
            XCTAssertEqual(answer?.questionID, "q1")
        }
    }

    // MARK: folding the transcript

    private func asked(_ message: String, _ id: String) -> SongRangeFold.Entry {
        .init(messageID: message, question: SongRangeQuestion(questionID: id), isUser: false)
    }
    private func reply(_ message: String, answer: SongRangeAnswer? = nil) -> SongRangeFold.Entry {
        .init(messageID: message, question: nil, isUser: true, answer: answer)
    }

    func testAnOpenQuestionIsActive() {
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1")])["a1"], .active)
    }

    func testOnlyAMatchingAnswerClosesTheCard() {
        let answer = SongRangeAnswer.wholeSong(questionID: "q1")
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1"), reply("u1", answer: answer)])["a1"], .answered(answer))
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1"), reply("u1")])["a1"], .active, "a plain message does not close it")
        let other = SongRangeAnswer.wholeSong(questionID: "other")
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1"), reply("u1", answer: other)])["a1"], .active, "an answer to another question does not close it")
    }

    func testANewerQuestionSupersedesAnUnansweredOne() {
        let entries = [asked("a1", "q1"), reply("u1"), asked("a2", "q2")]
        let phases = SongRangeFold.phases(entries)
        XCTAssertEqual(phases["a1"], .superseded)
        XCTAssertEqual(phases["a2"], .active)
    }

    func testAnAnsweredQuestionStaysAnsweredWhenAnotherIsAskedLater() {
        let answer = SongRangeAnswer.part(questionID: "q1", range: SongFilmingRange(start: 40, end: 80))
        let phases = SongRangeFold.phases([asked("a1", "q1"), reply("u1", answer: answer), reply("u2"), asked("a2", "q2")])
        XCTAssertEqual(phases["a1"], .answered(answer))
        XCTAssertEqual(phases["a2"], .active)
    }

    func testTheServerSayingStaleClosesAnOtherwiseOpenCardButNeverAnAnsweredOne() {
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1")], closed: ["q1"])["a1"], .superseded)
        let answer = SongRangeAnswer.wholeSong(questionID: "q1")
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1"), reply("u1", answer: answer)], closed: ["q1"])["a1"], .answered(answer))
        XCTAssertEqual(SongRangeFold.phases([asked("a1", "q1")], closed: ["other"])["a1"], .active)
    }

    func testMessagesWithoutAQuestionHaveNoPhase() {
        let phases = SongRangeFold.phases([reply("u1"), asked("a1", "q1")])
        XCTAssertNil(phases["u1"])
    }

    func testTwoMessagesAskingTheSameQuestionBothFollowTheAnswer() {
        let answer = SongRangeAnswer.wholeSong(questionID: "q1")
        let phases = SongRangeFold.phases([asked("a1", "q1"), asked("a2", "q1"), reply("u1", answer: answer)])
        XCTAssertEqual(phases["a1"], .answered(answer))
        XCTAssertEqual(phases["a2"], .answered(answer))
    }

    // MARK: pickers and validation

    func testPickersDefaultToTheWholeSong() throws {
        let draft = try XCTUnwrap(SongRangeQuestion(questionID: "q", songDurationS: 92.7).draft(fallbackDurationS: nil))
        XCTAssertEqual(draft.from, 0)
        XCTAssertEqual(draft.to, 92, "whole seconds, never past the song")
        XCTAssertEqual(draft.range, SongFilmingRange(start: 0, end: 92))
        XCTAssertNil(draft.message)
    }

    func testNoPickersForAnUnknownOrTinySong() {
        XCTAssertNil(SongRangeQuestion(questionID: "q").draft(fallbackDurationS: nil), "unknown length")
        XCTAssertNil(SongRangeQuestion(questionID: "q", songDurationS: 9.99).draft(fallbackDurationS: nil))
        XCTAssertNil(SongRangeQuestion(questionID: "q", songDurationS: 4).draft(fallbackDurationS: nil))
        XCTAssertNotNil(SongRangeQuestion(questionID: "q", songDurationS: 10).draft(fallbackDurationS: nil), "ten seconds is the smallest that is cut up")
        XCTAssertNil(SongRangeQuestion(questionID: "q").draft(fallbackDurationS: .nan))
        XCTAssertNil(SongRangeQuestion(questionID: "q").draft(fallbackDurationS: -5))
    }

    func testTheQuestionsOwnLengthWinsOverTheThreadsAndTheThreadFillsAGap() throws {
        XCTAssertEqual(try XCTUnwrap(SongRangeQuestion(questionID: "q", songDurationS: 60).draft(fallbackDurationS: 200)).limit, 60)
        XCTAssertEqual(try XCTUnwrap(SongRangeQuestion(questionID: "q").draft(fallbackDurationS: 200)).limit, 200)
        XCTAssertNil(SongRangeQuestion(questionID: "q", songDurationS: 5).draft(fallbackDurationS: 200), "a short question length is not overridden")
    }

    func testFiveSecondBoundaryAndFromPushingTo() throws {
        var draft = try XCTUnwrap(SongRangeQuestion(questionID: "q", songDurationS: 90).draft(fallbackDurationS: nil))
        draft.setTo(45); draft.setFrom(40)
        XCTAssertNotNil(draft.range, "exactly five seconds is allowed")
        draft.setTo(44)
        XCTAssertNil(draft.range)
        XCTAssertEqual(draft.message, "Pick at least five seconds of your song.")
        draft.setTo(30); draft.setFrom(50)
        XCTAssertEqual(draft.to, 55, "moving From past To pushes To out to keep five seconds")
        XCTAssertNotNil(draft.range)
        draft.setFrom(500)
        XCTAssertEqual(draft.from, 85, "From stops five seconds before the song ends")
        XCTAssertEqual(draft.to, 90)
    }

    // MARK: the 422 shown on the card

    func testOnlyTheServersInvalidRangeAnswerIsShownOnTheCard() {
        func failed(_ status: Int, _ code: String?, _ text: String?) -> APIError {
            .requestFailed(status: status, detail: RequestFailureDetail(nil, code: code, problemMessage: text))
        }
        XCTAssertTrue(SongRangeProblem.isInvalid(failed(422, "song_range_invalid", "Pick at least five seconds of your song.")))
        XCTAssertFalse(SongRangeProblem.isInvalid(failed(422, "something_else", "x")), "another 422 keeps the generic handling")
        XCTAssertFalse(SongRangeProblem.isInvalid(failed(500, "song_range_invalid", "x")))
        XCTAssertFalse(SongRangeProblem.isInvalid(.offline))
        XCTAssertEqual(SongRangeProblem(questionID: "q", error: failed(422, "song_range_invalid", "That starts after your song ends.")).message,
                       "That starts after your song ends.")
        XCTAssertEqual(SongRangeProblem(questionID: "q", error: failed(422, "song_range_invalid", nil)).message,
                       "Pick at least five seconds of your song.", "no server copy still reads as a sentence")
    }
}
