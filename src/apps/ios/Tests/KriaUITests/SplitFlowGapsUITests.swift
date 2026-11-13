import XCTest
import UIKit

// KRI-1335 split-flow gaps. Failure modes these journeys exist to catch (written before the journeys):
// - Whole video is not the default scope, so a creator who wanted a stacked video gets one stacked clip.
// - A member handle drag clamps at the section end (old), shrinks a fixed-length section, or fails to shrink a collapse one.
// - Dragging an unselected member's end handle selects it, moves a peer, or does nothing.
// - Extension passes the next member in its panel or the source footage, so members overlap or read past the file.
// - Add / Loop to fill land in the wrong panel, overlap, or reuse a slot identity; Loop leaves free time.
// - Full screen from / until the playhead cuts at the wrong time, drops footage, or keeps the wrong panel.
// - Extending to the whole video leaves ordinary clips behind or reorders them.
// - Remove split keeps another panel than the one chosen.
// - A ghost tap fills the first empty panel instead of the one tapped.
// - A timeline gap is drawn over footage, missing where a panel is free, or does not open the add flow.
// Every assertion reads the persisted document, not only what is on screen.
@MainActor
final class SplitFlowGapsUITests: XCTestCase {
    private struct Doc {
        let rows: [[String: Any]]
        let sections: [[String: Any]]

        var ordinary: [[String: Any]] { rows.filter { $0["split_section_id"] as? String == nil } }
        func members(_ panel: Int) -> [[String: Any]] { rows.filter { $0["split_panel_index"] as? Int == panel && $0["split_section_id"] as? String != nil } }
        func row(_ id: String) throws -> [String: Any] { try XCTUnwrap(rows.first { $0["slot_id"] as? String == id }) }
        func number(_ row: [String: Any], _ key: String) throws -> Double { try XCTUnwrap(row[key] as? Double) }
        var sectionDuration: Double { sections.first?["duration_s"] as? Double ?? -1 }
        func clipNumber(_ id: String) throws -> Int {
            try XCTUnwrap(rows.firstIndex { $0["slot_id"] as? String == id }) + 1
        }
    }

    private enum Scope { case clip, whole }

    // MARK: Journeys

    func testWholeVideoIsTheDefault() throws {
        let app = launch(["-ui-testing-editor-split-multi"])
        openSplitTool(app)
        let whole = app.buttons["native-editor-split-scope-whole"]
        XCTAssertTrue(whole.waitForExistence(timeout: 10))
        XCTAssertTrue(whole.isEnabled)
        XCTAssertTrue(whole.isSelected, "Whole video is the default scope")
        app.buttons["native-editor-split-create-2"].tap()
        let doc = try snapshot(app)
        XCTAssertEqual(doc.sections.count, 1)
        XCTAssertEqual(doc.sectionDuration, 12, accuracy: 0.05, "The section is the whole video")
        XCTAssertEqual(doc.rows.count, 4)
        XCTAssertTrue(doc.ordinary.isEmpty, "No ordinary clip is left outside the split")
        let top = doc.members(0)
        XCTAssertEqual(top.count, 4)
        XCTAssertEqual(try top.map { try doc.number($0, "split_offset_s") }, [0, 3, 6, 9].map { $0 as Double }, accuracy: 0.01)
        attach(app, "Whole video split by default")
    }

    func testSplitCanBeExtendedWithoutCollapse() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        let doc0 = try twoPanel(app)
        XCTAssertEqual(doc0.sectionDuration, 4, accuracy: 0.001)
        zoomOut(app)

        // Bottom is not the selected member: its own end handle still works and the section grows with it.
        let bottomID = try XCTUnwrap(doc0.members(1).first?["slot_id"] as? String)
        let bottomNumber = try doc0.clipNumber(bottomID)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        selectClip(try doc0.clipNumber(topID), in: app)
        let bottomClip = clip(bottomNumber, in: app)
        scrollClear(bottomClip, in: app)
        XCTAssertFalse(bottomClip.isSelected)
        let end = app.descendants(matching: .any)["native-editor-split-end-\(bottomNumber)"].firstMatch
        XCTAssertTrue(end.waitForExistence(timeout: 5), "Every split member carries its own end handle")
        let bottomDuration = try doc0.number(doc0.row(bottomID), "duration_s")
        drag(end, by: 1.5 * bottomClip.frame.width / CGFloat(bottomDuration))
        let doc1 = try snapshot(app)
        let grown = try doc1.row(bottomID)
        // The handle ends near the screen edge, where the timeline auto-scrolls, so the drag may carry past the finger.
        XCTAssertGreaterThan(try doc1.number(grown, "duration_s"), bottomDuration + 1)
        XCTAssertEqual(doc1.sectionDuration,
                       try doc1.number(grown, "split_offset_s") + doc1.number(grown, "duration_s"), accuracy: 0.001,
                       "A fixed section grows to its longest member")
        XCTAssertGreaterThan(doc1.sectionDuration, 4.5)
        XCTAssertEqual(NSDictionary(dictionary: try doc1.row(topID)), NSDictionary(dictionary: try doc0.row(topID)))

        // Shortening the longest member does not shrink a fixed section.
        let topNumber = try doc1.clipNumber(topID)
        scrollClear(clip(topNumber, in: app), in: app)
        selectClip(topNumber, in: app)
        let trailing = app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch
        XCTAssertTrue(trailing.waitForExistence(timeout: 5))
        let topClip = clip(topNumber, in: app)
        drag(trailing, by: -2 * topClip.frame.width / 4)
        let doc2 = try snapshot(app)
        XCTAssertEqual(doc2.sectionDuration, doc1.sectionDuration, accuracy: 0.001)
        let shortened = try doc2.number(doc2.row(topID), "duration_s")
        XCTAssertGreaterThan(shortened, 1)
        XCTAssertLessThan(shortened, 3)

        drag(trailing, by: -topClip.frame.width * 1.1)
        let doc3 = try snapshot(app)
        XCTAssertEqual(try doc3.number(doc3.row(topID), "duration_s"), 0.1, accuracy: 0.001, "The minimum still holds")
        XCTAssertEqual(doc3.sectionDuration, doc1.sectionDuration, accuracy: 0.001)

        for expected in [doc2, doc1, doc0] {
            app.buttons["native-editor-undo"].tap()
            let restored = try snapshot(app)
            XCTAssertEqual(NSArray(array: restored.rows), NSArray(array: expected.rows), "Each handle gesture is one undo step")
            XCTAssertEqual(NSArray(array: restored.sections), NSArray(array: expected.sections))
        }
        attach(app, "Split extended without collapse and undone")
    }

    func testSplitCanBeExtendedWithCollapse() throws {
        let app = launch(["-ui-testing-editor-split-screen", "-ui-testing-editor-split-collapse"])
        let doc0 = try twoPanel(app)
        XCTAssertEqual(doc0.sections.first?["collapse_empty_panels"] as? Bool, true, "The collapse capability makes new sections collapse")
        XCTAssertEqual(doc0.sectionDuration, 4, accuracy: 0.001)
        zoomOut(app)
        let bottomID = try XCTUnwrap(doc0.members(1).first?["slot_id"] as? String)
        let number = try doc0.clipNumber(bottomID)
        selectClip(try doc0.clipNumber(XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)), in: app)
        let bottomClip = clip(number, in: app)
        scrollClear(bottomClip, in: app)
        XCTAssertFalse(bottomClip.isSelected)
        let end = app.descendants(matching: .any)["native-editor-split-end-\(number)"].firstMatch
        XCTAssertTrue(end.waitForExistence(timeout: 5))
        let duration = try doc0.number(doc0.row(bottomID), "duration_s")
        let pps = bottomClip.frame.width / CGFloat(duration)
        drag(end, by: 1.5 * pps)
        let doc1 = try snapshot(app)
        let longest = try doc1.row(bottomID)
        XCTAssertGreaterThan(try doc1.number(longest, "duration_s"), 4.5)
        XCTAssertEqual(doc1.sectionDuration,
                       try doc1.number(longest, "split_offset_s") + doc1.number(longest, "duration_s"), accuracy: 0.001,
                       "A collapse section follows its longest member")
        // Pulling it back below the Top member makes the section follow Top again: collapse sections shrink.
        let widenedDuration = try doc1.number(longest, "duration_s")
        let widenedPPS = clip(number, in: app).frame.width / CGFloat(widenedDuration)
        drag(app.descendants(matching: .any)["native-editor-split-end-\(number)"].firstMatch, by: -3.5 * widenedPPS)
        let doc2 = try snapshot(app)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        let topEnd = try doc2.number(doc2.row(topID), "split_offset_s") + doc2.number(doc2.row(topID), "duration_s")
        let bottomEnd = try doc2.number(doc2.row(bottomID), "split_offset_s") + doc2.number(doc2.row(bottomID), "duration_s")
        XCTAssertLessThan(bottomEnd, topEnd)
        XCTAssertEqual(doc2.sectionDuration, topEnd, accuracy: 0.001)
        attach(app, "Collapse section follows the longest member")
    }

    func testSplitPanelHandleClampsAtNextMemberAndSourceFootage() throws {
        let app = launch(["-ui-testing-editor-split-multi", "-ui-testing-editor-split-short-source"])
        createSplit(panels: 2, scope: .clip, in: app)
        fill("Bottom", withFootage: "Clip 1", in: app)
        // A fill is as long as the section, so shorten Top to open free time after it, then add a second Top clip there.
        let filled = try snapshot(app)
        let topFirst = try XCTUnwrap(filled.members(0).first?["slot_id"] as? String)
        app.buttons["native-editor-split-done"].tap()
        let topNumber = try filled.clipNumber(topFirst)
        selectClip(topNumber, in: app)
        let topClip = clip(topNumber, in: app)
        let fullLength = try filled.number(filled.row(topFirst), "duration_s")
        drag(app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch, by: -1 * topClip.frame.width / CGFloat(fullLength))
        openSplitTool(app)
        panelControl("native-editor-split-add-0", in: app).tap()
        source("Clip 2", in: app).tap()
        app.buttons["native-editor-split-done"].tap()
        let doc0 = try snapshot(app)
        let top = doc0.members(0)
        XCTAssertEqual(top.count, 2)
        let firstID = try XCTUnwrap(top.first?["slot_id"] as? String)
        let secondID = try XCTUnwrap(top.last?["slot_id"] as? String)
        let firstDuration = try doc0.number(doc0.row(firstID), "duration_s")
        let secondDuration = try doc0.number(doc0.row(secondID), "duration_s")
        XCTAssertEqual(try doc0.number(doc0.row(secondID), "split_offset_s"), firstDuration, accuracy: 0.01)
        XCTAssertLessThan(secondDuration, 3.5, "The second clip has source footage left to extend into")
        XCTAssertLessThan(firstDuration, fullLength - 0.3, "Top was shortened to make room")

        // Source has 3.6 s, so the first clip could grow, but the second one starts where it ends.
        let firstNumber = try doc0.clipNumber(firstID)
        selectClip(firstNumber, in: app)
        let firstClip = clip(firstNumber, in: app)
        drag(app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch,
             by: 0.5 * firstClip.frame.width / CGFloat(firstDuration))
        let doc1 = try snapshot(app)
        XCTAssertEqual(NSDictionary(dictionary: try doc1.row(firstID)), NSDictionary(dictionary: try doc0.row(firstID)),
                       "Extension stops at the next member in the panel")
        XCTAssertEqual(NSArray(array: doc1.sections), NSArray(array: doc0.sections))

        let secondNumber = try doc0.clipNumber(secondID)
        selectClip(secondNumber, in: app)
        let secondClip = clip(secondNumber, in: app)
        drag(app.descendants(matching: .any)["native-editor-trim-trailing"].firstMatch,
             by: 3 * secondClip.frame.width / CGFloat(secondDuration))
        let doc2 = try snapshot(app)
        let extended = try doc2.row(secondID)
        XCTAssertEqual(try doc2.number(extended, "duration_s"), 3.6, accuracy: 0.001, "Extension stops at the end of the source footage")
        XCTAssertEqual(doc2.sectionDuration, try doc2.number(extended, "split_offset_s") + 3.6, accuracy: 0.001)
        XCTAssertEqual(try doc2.number(extended, "in_s"), try doc0.number(doc0.row(secondID), "in_s"), accuracy: 0.001)
    }

    func testAddClipToNonEmptyPanelAndLoopFill() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        let doc0 = try twoPanel(app)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        let bottomID = try XCTUnwrap(doc0.members(1).first?["slot_id"] as? String)
        zoomOut(app)
        // A fill is as long as the section; shorten Bottom (not the selected member) so it has free time to loop into.
        let topNumber = try doc0.clipNumber(topID)
        selectClip(topNumber, in: app)
        let bottomNumber = try doc0.clipNumber(bottomID)
        let bottomClip = clip(bottomNumber, in: app)
        scrollClear(bottomClip, in: app)
        let bottomBefore = try doc0.number(doc0.row(bottomID), "duration_s")
        drag(app.descendants(matching: .any)["native-editor-split-end-\(bottomNumber)"].firstMatch,
             by: -1.5 * bottomClip.frame.width / CGFloat(bottomBefore))
        let shortened = try snapshot(app)
        let bottomLength = try shortened.number(shortened.row(bottomID), "duration_s")
        XCTAssertEqual(bottomLength, bottomBefore - 1.5, accuracy: 0.3)
        XCTAssertEqual(shortened.sectionDuration, 4, accuracy: 0.001, "A fixed section never shrinks")
        openSplitTool(app)
        XCTAssertFalse(panelControl("native-editor-split-loop-0", in: app).isEnabled, "Top has no free time to loop into")
        XCTAssertFalse(panelControl("native-editor-split-add-0", in: app).isEnabled, "A full panel cannot take another clip")

        // Add: a second Bottom clip goes into the free time after its last one, without lengthening the section.
        panelControl("native-editor-split-add-1", in: app).tap()
        source("Clip 2", in: app).tap()
        let added = try snapshot(app)
        let bottomClips = added.members(1)
        XCTAssertEqual(bottomClips.count, 2, "Add puts a second clip in a panel that already has footage")
        let second = try XCTUnwrap(bottomClips.last)
        XCTAssertEqual(try added.number(second, "split_offset_s"), bottomLength, accuracy: 0.01, "It lands after Bottom's last clip")
        XCTAssertLessThanOrEqual(try added.number(second, "split_offset_s") + added.number(second, "duration_s"), 4.01)
        XCTAssertEqual(second["clip_index"] as? Int, 1)
        XCTAssertEqual(added.sectionDuration, 4, accuracy: 0.001)
        XCTAssertEqual(added.members(0).count, 1)
        undo(app)
        XCTAssertEqual(NSArray(array: try snapshot(app).rows), NSArray(array: shortened.rows), "Add is one undo step")

        // Loop: Bottom's own clip repeats into the same free time, clipped at the section end.
        ensureSplitPanel(app)
        panelControl("native-editor-split-loop-1", in: app).tap()
        let doc1 = try snapshot(app)
        let bottom = doc1.members(1)
        XCTAssertEqual(bottom.count, 2, "Loop adds one clipped copy")
        let copy = try XCTUnwrap(bottom.last)
        XCTAssertEqual(try doc1.number(copy, "split_offset_s"), bottomLength, accuracy: 0.01)
        XCTAssertEqual(try doc1.number(copy, "duration_s"), 4 - bottomLength, accuracy: 0.01, "The last copy is clipped at the section end")
        XCTAssertEqual(copy["clip_index"] as? Int, doc1.members(1).first?["clip_index"] as? Int)
        XCTAssertNotEqual(copy["slot_id"] as? String, bottomID)
        XCTAssertNotEqual(copy["slot_id"] as? String, topID)
        XCTAssertEqual(doc1.sectionDuration, 4, accuracy: 0.001)
        let looped = panelControl("native-editor-split-loop-1", in: app)
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: looped)
        waitForExpectations(timeout: 5)
        attach(app, "Looped Bottom to fill")
    }

    func testFullScreenFromPlayheadAndUntilPlayhead() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        let doc0 = try twoPanel(app)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        let topIn = try doc0.number(doc0.row(topID), "in_s")
        let serialID = try XCTUnwrap(doc0.ordinary.first?["slot_id"] as? String)
        let topNumber = try doc0.clipNumber(topID)
        selectClip(topNumber, in: app)
        scrub(by: 2, pixelsPerSecond: clip(topNumber, in: app).frame.width / 4, in: app)
        XCTAssertTrue(clip(topNumber, in: app).isSelected, "Scrubbing keeps the Top panel selected, which is the panel that stays")
        ensureSplitPanel(app)

        panelControl("native-editor-split-full-from", in: app).tap()
        let doc1 = try snapshot(app)
        let t = doc1.sectionDuration
        XCTAssertGreaterThan(t, 1.5)
        XCTAssertLessThan(t, 2.5)
        XCTAssertEqual(doc1.ordinary.count, 2, "The rest of the video is ordinary clips")
        let rest = try XCTUnwrap(doc1.ordinary.first)
        XCTAssertEqual(try doc1.number(rest, "duration_s"), 4 - t, accuracy: 0.02)
        XCTAssertEqual(try doc1.number(rest, "in_s"), topIn + t, accuracy: 0.02, "The source clock continues from the cut")
        XCTAssertEqual(doc1.ordinary.last?["slot_id"] as? String, serialID)
        XCTAssertEqual(doc1.rows.first?["split_section_id"] as? String, doc1.sections.first?["id"] as? String, "The split stays before the playhead")

        undo(app)
        let undone = try snapshot(app)
        XCTAssertEqual(NSArray(array: undone.rows), NSArray(array: doc0.rows))
        XCTAssertEqual(NSArray(array: undone.sections), NSArray(array: doc0.sections))

        ensureSplitPanel(app)
        panelControl("native-editor-split-full-until", in: app).tap()
        let doc2 = try snapshot(app)
        XCTAssertEqual(doc2.ordinary.count, 2)
        let before = try XCTUnwrap(doc2.rows.first)
        XCTAssertNil(before["split_section_id"] as? String, "Until the playhead the video is ordinary clips")
        let cut = try doc2.number(before, "duration_s")
        XCTAssertGreaterThan(cut, 1.5)
        XCTAssertLessThan(cut, 2.5)
        XCTAssertEqual(doc2.sectionDuration + cut, 4, accuracy: 0.02, "Nothing is lost at the cut")
        let topAfter = try XCTUnwrap(doc2.members(0).first)
        XCTAssertEqual(try doc2.number(topAfter, "in_s"), topIn + cut, accuracy: 0.03)
        XCTAssertEqual(doc2.ordinary.last?["slot_id"] as? String, serialID)
        attach(app, "Full screen until the playhead")
    }

    func testExtendSplitToWholeVideo() throws {
        let app = launch(["-ui-testing-editor-split-multi"])
        zoomOut(app)
        createSplit(panels: 2, scope: .clip, in: app)
        fill("Bottom", withFootage: "Clip 1", in: app)
        app.buttons["native-editor-split-done"].tap()
        let doc0 = try snapshot(app)
        XCTAssertEqual(doc0.sectionDuration, 3, accuracy: 0.01)
        XCTAssertEqual(doc0.ordinary.count, 3)

        // Outside the split the panel explains why Whole video is unavailable.
        let firstOrdinary = try doc0.clipNumber(XCTUnwrap(doc0.ordinary.first?["slot_id"] as? String))
        selectClip(firstOrdinary, in: app)
        openSplitTool(app)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-split-scope-reason"].firstMatch.waitForExistence(timeout: 10),
                      "A split elsewhere in the video explains why Whole video is off")
        attach(app, "Scope reason outside the split")
        app.buttons["native-editor-split-done"].tap()

        let topNumber = try doc0.clipNumber(XCTUnwrap(doc0.members(0).first?["slot_id"] as? String))
        selectClip(topNumber, in: app)
        openSplitTool(app)
        panelControl("native-editor-split-extend-whole", in: app).tap()
        let doc1 = try snapshot(app)
        XCTAssertEqual(doc1.sections.count, 1)
        XCTAssertEqual(doc1.sectionDuration, 12, accuracy: 0.05, "The split covers the whole video")
        XCTAssertTrue(doc1.ordinary.isEmpty)
        let top = doc1.members(0)
        XCTAssertEqual(top.count, 4, "Every ordinary clip joins the Top panel")
        XCTAssertEqual(try top.map { try doc1.number($0, "split_offset_s") }, [0, 3, 6, 9].map { $0 as Double }, accuracy: 0.05)
        XCTAssertEqual(top.compactMap { $0["slot_id"] as? String },
                       doc0.rows.filter { $0["split_panel_index"] as? Int != 1 }.compactMap { $0["slot_id"] as? String },
                       "Timeline order and identities are kept")
        XCTAssertEqual(doc1.members(1).count, 1)
    }

    func testRemoveSplitKeepsTheChosenPanel() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        let doc0 = try twoPanel(app)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        let bottomID = try XCTUnwrap(doc0.members(1).first?["slot_id"] as? String)
        openSplitTool(app)
        panelControl("native-editor-split-remove", in: app).tap()
        let keep = app.buttons.matching(identifier: "native-editor-split-keep-1").firstMatch
        XCTAssertTrue(keep.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons.matching(identifier: "native-editor-split-keep-0").firstMatch.exists, "Every panel is offered")
        keep.tap()
        let doc1 = try snapshot(app)
        XCTAssertTrue(doc1.sections.isEmpty)
        XCTAssertEqual(doc1.ordinary.count, doc1.rows.count, "Every clip is ordinary again")
        XCTAssertEqual(doc1.rows.first?["slot_id"] as? String, bottomID, "The Bottom panel's footage stays")
        XCTAssertNil(doc1.rows.first { $0["slot_id"] as? String == topID }, "The Top panel's footage is removed")
        XCTAssertEqual(doc1.rows.count, 2)
    }

    func testGhostTapOpensTheFillFlowForThatPanel() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        createSplit(panels: 3, scope: .clip, in: app)
        XCTAssertTrue(app.staticTexts["Add to Middle"].waitForExistence(timeout: 10), "Without a ghost tap the first empty panel leads")
        let ghost = app.descendants(matching: .any)["native-editor-split-ghost-2"].firstMatch
        XCTAssertTrue(ghost.waitForExistence(timeout: 10), "Every empty panel shows a ghost in the preview")
        ghost.tap()
        XCTAssertTrue(app.staticTexts["Add to Bottom"].waitForExistence(timeout: 10), "The tapped panel is the target")
        XCTAssertFalse(app.staticTexts["Add to Middle"].exists)
        source("Clip 2", in: app).tap()
        let doc = try snapshot(app)
        XCTAssertEqual(doc.members(2).count, 1, "The pick lands in the tapped Bottom panel")
        XCTAssertTrue(doc.members(1).isEmpty, "Middle stays empty")
        attach(app, "Ghost tap targets the Bottom panel")
    }

    func testGapInAPanelLaneOpensTheAddFlow() throws {
        let app = launch(["-ui-testing-editor-split-screen"])
        let doc0 = try twoPanel(app)
        XCTAssertEqual(doc0.sectionDuration, 4, accuracy: 0.001)
        zoomOut(app)
        let topID = try XCTUnwrap(doc0.members(0).first?["slot_id"] as? String)
        let bottomID = try XCTUnwrap(doc0.members(1).first?["slot_id"] as? String)
        let bottomNumber = try doc0.clipNumber(bottomID)
        selectClip(try doc0.clipNumber(topID), in: app)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-split-gap-0-0"].firstMatch.exists, "Top is full, so it has no gap")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-split-gap-1-0"].firstMatch.exists, "A full panel has no gap")
        let bottomClip = clip(bottomNumber, in: app)
        scrollClear(bottomClip, in: app)
        let before = try doc0.number(doc0.row(bottomID), "duration_s")
        let pps = bottomClip.frame.width / CGFloat(before)
        drag(app.descendants(matching: .any)["native-editor-split-end-\(bottomNumber)"].firstMatch, by: -1 * pps)
        let doc1 = try snapshot(app)
        let length = try doc1.number(doc1.row(bottomID), "duration_s")
        XCTAssertEqual(length, before - 1, accuracy: 0.3)

        let gap = app.descendants(matching: .any)["native-editor-split-gap-1-0"].firstMatch
        XCTAssertTrue(gap.waitForExistence(timeout: 5), "Bottom now ends before the section does")
        XCTAssertEqual(gap.frame.width, CGFloat(4 - length) * pps, accuracy: 4, "The gap is the free time after the Bottom clip")
        attach(app, "Hatched gap after the Bottom clip")
        gap.tap()
        XCTAssertTrue(app.staticTexts["Add to Bottom"].waitForExistence(timeout: 10), "The gap opens Bottom's add flow")
        let time = try currentTime(app)
        XCTAssertGreaterThanOrEqual(time, length - 0.1)
        XCTAssertLessThan(time, length + 0.3, "The playhead moved into the gap")
        XCTAssertEqual(NSArray(array: try snapshot(app).rows), NSArray(array: doc1.rows), "Opening the flow edits nothing")
    }

    // MARK: Setup

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-live-preview",
                               "-ui-testing-editor-document-snapshot"] + extra
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(clip(1, in: app).waitForExistence(timeout: 30))
        return app
    }

    private func openSplitTool(_ app: XCUIApplication) {
        let tool = app.buttons["native-editor-tool-split"]
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        for _ in 0..<3 where !tool.isHittable { rail.swipeLeft() }
        XCTAssertTrue(tool.isHittable, "Split is the last tool on the rail")
        tool.tap()
    }

    /// The Split panel covers the undo button; close it first.
    private func undo(_ app: XCUIApplication) {
        let done = app.buttons["native-editor-split-done"]
        if done.exists { done.tap() }
        let button = app.buttons["native-editor-undo"]
        XCTAssertTrue(button.waitForExistence(timeout: 5))
        button.tap()
    }

    private func ensureSplitPanel(_ app: XCUIApplication) {
        if !app.buttons["native-editor-split-done"].exists { openSplitTool(app) }
    }

    private func createSplit(panels: Int, scope: Scope, in app: XCUIApplication) {
        openSplitTool(app)
        if scope == .clip {
            let tab = app.buttons["native-editor-split-scope-clip"]
            XCTAssertTrue(tab.waitForExistence(timeout: 10))
            tab.tap()
        }
        let card = app.buttons["native-editor-split-create-\(panels)"]
        XCTAssertTrue(card.waitForExistence(timeout: 10))
        card.tap()
    }

    private func fill(_ panel: String, withFootage label: String, in app: XCUIApplication) {
        XCTAssertTrue(app.staticTexts["Add to \(panel)"].waitForExistence(timeout: 10))
        source(label, in: app).tap()
    }

    private func source(_ label: String, in app: XCUIApplication) -> XCUIElement {
        let button = app.buttons.matching(NSPredicate(
            format: "identifier BEGINSWITH 'native-editor-split-source-' AND label == %@", label)).firstMatch
        XCTAssertTrue(button.waitForExistence(timeout: 10), "The picker offers \(label) from the edit")
        return button
    }

    /// Clip 1 split into Top, Bottom filled from Clip 1 again, panel closed. Bottom is 3 s, Top and the section 4 s.
    private func twoPanel(_ app: XCUIApplication) throws -> Doc {
        clip(1, in: app).tap()
        createSplit(panels: 2, scope: .clip, in: app)
        fill("Bottom", withFootage: "Clip 1", in: app)
        app.buttons["native-editor-split-done"].tap()
        let doc = try snapshot(app)
        XCTAssertEqual(doc.sections.count, 1)
        XCTAssertEqual(doc.rows.count, 3)
        return doc
    }

    private func panelControl(_ id: String, in app: XCUIApplication) -> XCUIElement {
        let control = app.buttons[id]
        XCTAssertTrue(control.waitForExistence(timeout: 10), id)
        for _ in 0..<4 where !control.isHittable { app.swipeUp() }
        return control
    }

    // MARK: Timeline

    private func clip(_ number: Int, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-clip-\(number)"].firstMatch
    }

    private func zoomOut(_ app: XCUIApplication) {
        clip(1, in: app).pinch(withScale: 0.5, velocity: -1)
    }

    private func scrollClear(_ target: XCUIElement, in app: XCUIApplication) {
        let lanes = app.scrollViews["native-editor-lane-scroll"]
        let toolbar = app.descendants(matching: .any)["native-editor-clip-context"].firstMatch
        let rail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        let bottom = toolbar.exists ? toolbar.frame.minY : rail.frame.minY
        for _ in 0..<6 where target.frame.maxY >= bottom || target.frame.minY < lanes.frame.minY {
            let direction: CGFloat = target.frame.minY < lanes.frame.minY ? 90 : -90
            let start = app.coordinate(withNormalizedOffset: .zero)
                .withOffset(CGVector(dx: lanes.frame.minX + 24, dy: min(bottom - 16, lanes.frame.maxY - 16)))
            start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 0, dy: direction)))
        }
        XCTAssertTrue(target.waitForExistence(timeout: 3))
        XCTAssertTrue(target.isHittable)
    }

    /// `at` is the horizontal tap point inside the block; a very narrow block keeps its end handle on the right.
    private func selectClip(_ number: Int, at x: CGFloat = 0.5, in app: XCUIApplication) {
        let target = clip(number, in: app)
        scrollClear(target, in: app)
        target.coordinate(withNormalizedOffset: CGVector(dx: x, dy: 0.5)).tap()
        XCTAssertTrue(target.isSelected)
    }

    /// A held press first, so the handle's zero-distance drag begins.
    private func drag(_ handle: XCUIElement, by dx: CGFloat) {
        let from = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        from.press(forDuration: 0.1, thenDragTo: from.withOffset(CGVector(dx: dx, dy: 0)), withVelocity: 100, thenHoldForDuration: 0.2)
    }

    private func scrub(by seconds: Double, pixelsPerSecond: CGFloat, in app: XCUIApplication) {
        let playhead = app.descendants(matching: .any)["native-editor-playhead"].firstMatch
        XCTAssertTrue(playhead.isHittable)
        let start = playhead.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: CGFloat(seconds) * pixelsPerSecond, dy: 0)),
                    withVelocity: 20, thenHoldForDuration: 0.3)
    }

    private func currentTime(_ app: XCUIApplication) throws -> Double {
        let raw = try XCTUnwrap(app.descendants(matching: .any)["native-editor-current-time"].firstMatch.value as? String)
        let parts = raw.split(separator: ":").compactMap { Double($0) }
        XCTAssertEqual(parts.count, 2)
        return (parts.first ?? 0) * 60 + (parts.last ?? 0)
    }

    // MARK: Document

    private func snapshot(_ app: XCUIApplication) throws -> Doc {
        let probe = app.descendants(matching: .any)["native-editor-test-document"].firstMatch
        XCTAssertTrue(probe.waitForExistence(timeout: 5))
        let value = try XCTUnwrap(probe.value as? String)
        let attachment = XCTAttachment(string: value)
        attachment.name = "Split flow persisted document"
        attachment.lifetime = .keepAlways
        add(attachment)
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any])
        let payload = try XCTUnwrap(root["editor_payload"] as? [String: Any])
        let sections = try XCTUnwrap(payload["sections"] as? [String: Any])
        return Doc(rows: try XCTUnwrap(sections["timeline_slots"] as? [[String: Any]]),
                   sections: sections["split_screen_sections"] as? [[String: Any]] ?? [])
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

private func XCTAssertEqual(_ values: [Double], _ expected: [Double], accuracy: Double,
                            file: StaticString = #filePath, line: UInt = #line) {
    XCTAssertEqual(values.count, expected.count, file: file, line: line)
    for (value, wanted) in zip(values, expected) { XCTAssertEqual(value, wanted, accuracy: accuracy, file: file, line: line) }
}
