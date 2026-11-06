import XCTest
@testable import Kria

final class NativeSplitScreenRowsTests: XCTestCase {
    @MainActor
    func testRepeatedSpeakerFixtureRetainsMembershipThroughSessionHydration() {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.makeSplitSpeaker(restoringSavedState: false))
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.document.clips.map(\.id), ["split-speaker-top", "split-speaker-bottom"])
        XCTAssertEqual(session.document.clips.map(\.splitPanelIndex), [0, 1])
        XCTAssertEqual(session.timelineProjection.totalDuration, 4)
        XCTAssertNotNil(session.document.speakerBackground?.speakerFrame)
    }

    func testLegacyTimelineKeepsOneVideoRow() {
        let rows = NativeSplitScreenRows(document: EditorDocument())
        XCTAssertEqual(rows.titles, ["Video"])
        XCTAssertEqual(rows.row(slotID: "ordinary"), 0)
    }

    func testTwoPanelBottomDoesNotBecomeMiddleBesideThreePanelSection() {
        var document = EditorDocument(clips: [
            EditorTimelineSlot(id: "full", clipIndex: 0, inS: 0, durationS: 2),
            member("two-top", section: "two", panel: 0),
            member("two-bottom", section: "two", panel: 1),
            member("three-top", section: "three", panel: 0),
            member("three-middle", section: "three", panel: 1),
            member("three-bottom", section: "three", panel: 2)
        ])
        document.splitScreenSections = [
            EditorSplitScreenSection(id: "two", durationS: 2, panelCount: 2),
            EditorSplitScreenSection(id: "three", durationS: 2, panelCount: 3)
        ]
        let rows = NativeSplitScreenRows(document: document)
        XCTAssertEqual(rows.titles, ["Video", "Top", "Middle", "Bottom"])
        XCTAssertEqual(rows.row(slotID: "full"), 0)
        XCTAssertEqual(rows.row(slotID: "two-top"), 1)
        XCTAssertEqual(rows.row(slotID: "two-bottom"), 3)
        XCTAssertEqual(rows.row(slotID: "three-middle"), 2)
        XCTAssertEqual(rows.row(slotID: "three-bottom"), 3)
    }

    func testEmptyPanelsKeepTheirRows() {
        var document = EditorDocument(clips: [member("top", section: "stack", panel: 0)])
        document.splitScreenSections = [EditorSplitScreenSection(id: "stack", durationS: 2, panelCount: 3)]
        let rows = NativeSplitScreenRows(document: document)
        XCTAssertEqual(rows.titles, ["Top", "Middle", "Bottom"])
        XCTAssertEqual(rows.row(slotID: "top"), 0)
    }

    func testReorderTreatsAllPanelMembersAsOneSectionIncludingEmptyTail() {
        var document = EditorDocument(clips: [
            EditorTimelineSlot(id: "before", clipIndex: 0, inS: 0, durationS: 2),
            member("top", section: "stack", panel: 0),
            member("bottom", section: "stack", panel: 1),
            EditorTimelineSlot(id: "after", clipIndex: 0, inS: 0, durationS: 2)
        ])
        document.splitScreenSections = [EditorSplitScreenSection(id: "stack", durationS: 4, panelCount: 2)]
        let clips = [clip("before", start: 0, end: 2), clip("top", start: 2, end: 4),
                     clip("bottom", start: 2, end: 4), clip("after", start: 6, end: 8)]
        let groups = NativeSplitScreenReorderUnit.make(document: document, clips: clips)
        XCTAssertEqual(groups.count, 3)
        XCTAssertEqual(groups[1].clipIDs, [clips[1].id, clips[2].id])
        XCTAssertEqual(groups[1].start, 2)
        XCTAssertEqual(groups[1].end, 6)
        XCTAssertEqual(groups[2].start, 6)
        let moved = NativeSplitScreenReorderUnit.reorderedWindows(document: document, clips: clips,
                                                                 moving: clips[2].id, to: clips[3].id)
        XCTAssertEqual(moved.map(\.start), [0, 2, 4])
        XCTAssertEqual(moved.map(\.end), [2, 4, 8])
    }

    private func clip(_ slotID: String, start: Double, end: Double) -> EditorClip {
        EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: start, end: end,
                   trimIn: 0, trimOut: end - start, sourceDuration: 20, muted: false, slotID: slotID)
    }

    private func member(_ id: String, section: String, panel: Int) -> EditorTimelineSlot {
        EditorTimelineSlot(id: id, clipIndex: 0, inS: 0, durationS: 2, raw: [
            "split_section_id": .string(section), "split_panel_index": .number(Double(panel)),
            "split_offset_s": .number(0)
        ])
    }
}
