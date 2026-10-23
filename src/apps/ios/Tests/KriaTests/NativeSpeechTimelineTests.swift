import XCTest
@testable import Kria

final class NativeSpeechTimelineTests: XCTestCase {
    func testDeleteTrimSlideReorderKeepsWordsAndLanesOnTheirSource() throws {
        let a = EditorTimelineSlot(id: "native-phone-talking-source", clipIndex: 0, inS: 0, durationS: 2)
        let b = EditorTimelineSlot(id: "native-phone-talking-source-1", clipIndex: 0, inS: 3, durationS: 2)
        let c = EditorTimelineSlot(id: "native-phone-talking-source-2", clipIndex: 0, inS: 8, durationS: 4)
        let cue = EditorCaptionCue(id: "c", startS: 4, endS: 8, text: "Charlie Delta", raw: ["words": .array([
            .object(["text": .string("Charlie"), "start_s": .number(4.2), "end_s": .number(5)]),
            .object(["text": .string("Delta"), "start_s": .number(6.2), "end_s": .number(7.5)])])])
        let initial = EditorDocument(clips: [a, b, c], textElements: [EditorTextElement(id: "title", text: "At Delta", startS: 6.2, endS: 7.5)],
            captionCues: [cue], soundEffects: [EditorTimedEffect(id: "sfx", startS: 6.2, endS: 6.2, pointS: 6.2)])
        var deleted = initial; deleted.clips.remove(at: 1)
        NativeSpeechTimelineProjection.rebase(&deleted, from: initial)
        XCTAssertEqual(deleted.captionCues[0].startS, 2)
        XCTAssertEqual(deleted.soundEffects[0].pointS, 4.2)
        var edited = deleted
        edited.clips[1].inS = 10; edited.clips[1].durationS = 2
        NativeSpeechTimelineProjection.rebase(&edited, from: deleted)
        XCTAssertEqual(edited.captionCues[0].text, "Delta")
        XCTAssertEqual(edited.soundEffects[0].pointS, 2.2)
        var reordered = edited; reordered.clips.reverse()
        NativeSpeechTimelineProjection.rebase(&reordered, from: edited)
        XCTAssertEqual(reordered.captionCues[0].text, "Delta")
        let word = try XCTUnwrap(reordered.captionCues[0].raw["words"]?.arrayValue?.first?.objectValue)
        XCTAssertEqual(try XCTUnwrap(word["start_s"]?.numberValue), 0.2, accuracy: 0.001)
        XCTAssertEqual(reordered.textElements[0].startS, 0.2, accuracy: 0.001)
        XCTAssertEqual(reordered.soundEffects[0].pointS, 0.2)
        // Save/reopen followed by extending restores the original source words.
        var reopened = EditorDocument(snapshot: edited.encodeSnapshot())
        let shortened = reopened
        reopened.clips = deleted.clips
        NativeSpeechTimelineProjection.rebase(&reopened, from: shortened)
        XCTAssertEqual(reopened.captionCues.first?.text, "Charlie Delta")
        XCTAssertEqual(reopened.captionCues.first?.raw["words"]?.arrayValue?.count, 2)
        // An unchanged document and the independent music bed are byte-identical.
        var unchanged = initial; NativeSpeechTimelineProjection.rebase(&unchanged, from: initial)
        XCTAssertEqual(unchanged, initial)
    }
    func testFractionalOverlapAndRepeatedSourceFixtureMatchesTheRenderer() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "speech_editor_projection", withExtension: "json"))
        let root = try JSONDecoder().decode([String: JSONValue].self, from: Data(contentsOf: url))
        func slots(_ key: String) -> [EditorTimelineSlot] {
            EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": root[key]!])])]).clips
        }
        for side in ["old", "new"] {
            let windows = NativeEditorInteraction.timelineProjection(slots: slots(side + "_slots"), carousel: nil).baseClipWindows
            let expected = try XCTUnwrap(root[side + "_windows"]?.arrayValue)
            for (window, row) in zip(windows, expected) {
                let values = try XCTUnwrap(row.arrayValue)
                XCTAssertEqual(window.start, try XCTUnwrap(values[0].numberValue), accuracy: 1e-10)
                XCTAssertEqual(window.end, try XCTUnwrap(values[1].numberValue), accuracy: 1e-10)
            }
        }
        let result = NativeSpeechTimelineProjection.records(try XCTUnwrap(root["records"]?.arrayValue), oldSlots: slots("old_slots"), newSlots: slots("new_slots"))
        XCTAssertEqual(result, root["expected"]?.arrayValue)
        let relative = try XCTUnwrap(root["relative_word_timings"]?.objectValue)
        func relativeSlots(_ key: String) -> [EditorTimelineSlot] {
            EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": relative[key]!])])]).clips
        }
        XCTAssertEqual(NativeSpeechTimelineProjection.records(try XCTUnwrap(relative["records"]?.arrayValue), oldSlots: relativeSlots("old_slots"), newSlots: relativeSlots("new_slots")), relative["expected"]?.arrayValue)
        let exact = try XCTUnwrap(root["exact_source_end"]?.objectValue)
        func exactSlots(_ key: String) -> [EditorTimelineSlot] {
            EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": exact[key]!])])]).clips
        }
        let bounded = NativeSpeechTimelineProjection.records(try XCTUnwrap(exact["records"]?.arrayValue), oldSlots: exactSlots("old_slots"), newSlots: exactSlots("new_slots"), captions: true)
        XCTAssertEqual(bounded, exact["expected"]?.arrayValue)
    }

    func testMissingSourceWordsRetainTheRangeUntilCaptionRecovery() {
        let old = EditorDocument(clips: [EditorTimelineSlot(id: "native-phone-talking-source", clipIndex: 0, inS: 0, durationS: 2)],
            captionCues: [EditorCaptionCue(id: "a", startS: 0.2, endS: 1.5, text: "Alpha")])
        var next = old; next.clips[0].inS = 1
        NativeSpeechTimelineProjection.rebase(&next, from: old)
        XCTAssertEqual(next.clips[0].inS, 1)
        XCTAssertTrue(NativeSpeechTimelineProjection.requiresCaptionTiming(next))
        var empty = next; empty.clips[0].inS = 5
        NativeSpeechTimelineProjection.rebase(&empty, from: next)
        XCTAssertTrue(empty.captionCues.isEmpty)
        XCTAssertTrue(NativeSpeechTimelineProjection.requiresCaptionTiming(empty))
        var disabled = next; disabled.captionMeta["enabled"] = .bool(false)
        XCTAssertFalse(NativeSpeechTimelineProjection.requiresCaptionTiming(disabled))
        var own = next; own.captionCues = [EditorCaptionCue(id: "own", startS: 0, endS: 2, text: "My caption")]
        NativeSpeechTimelineProjection.acknowledgeCaptionEdits(&own, from: next)
        XCTAssertFalse(NativeSpeechTimelineProjection.requiresCaptionTiming(own))
    }

    func testSharedSourcePoolSurvivesReopenWithoutCaptionMirrors() throws {
        let slot = EditorTimelineSlot(id: "native-phone-talking-source", clipIndex: 0, inS: 0, durationS: 2)
        let full: JSONValue = .object(["slot_id": .string(slot.id!), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(12)])
        let group: JSONValue = .object(["source_captions": .array([.object(["start_s": .number(3), "end_s": .number(4), "text": .string("Hidden"), "words": .array([.object(["text": .string("Hidden"), "start_s": .number(3), "end_s": .number(4)])])])]), "source_groups": .array([.object(["slot": full, "initial_start_s": .number(0), "initial_end_s": .number(2)])])])
        var old = EditorDocument(clips: [slot])
        old.speechTimingBasis = ["caption_cues": .array([group])]
        var next = old; next.clips[0].inS = 3
        NativeSpeechTimelineProjection.rebase(&next, from: old)
        XCTAssertEqual(next.captionCues.first?.text, "Hidden")
        XCTAssertEqual(next.captionCues.first?.startS, 0)
        var reopened = EditorDocument(snapshot: next.encodeSnapshot())
        let previous = reopened
        reopened.clips[0].inS = 0
        NativeSpeechTimelineProjection.rebase(&reopened, from: previous)
        XCTAssertTrue(reopened.captionCues.isEmpty)
        next = reopened; next.clips[0].inS = 3
        NativeSpeechTimelineProjection.rebase(&next, from: reopened)
        XCTAssertEqual(next.captionCues.first?.text, "Hidden")
        XCTAssertEqual(next.speechTimingBasis["caption_cues"]?.arrayValue?.count, 1)
    }

}
