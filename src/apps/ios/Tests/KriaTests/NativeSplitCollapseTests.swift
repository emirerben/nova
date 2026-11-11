import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1335 collapse mode (`collapse_empty_panels`, protocol 47). Each test is written against a listed failure mode:
/// the active-panel function off by one at a window's exact start or end, an overlap inside one panel counted twice,
/// an empty or foreign or removed member making a panel active, a non-collapse section losing panels, a divider drag
/// that moves a panel that has ended or a pair that is not adjacent on screen, a section that does not follow its
/// longest member (the tail cut off after an extension, a member dropped after a shrink), a trim whose section change
/// and clip change are separate undo steps, a legacy section whose JSON or trim behaviour changed, a flag dropped or
/// emitted when false in JSON, and a section made collapsing before the server offers it.
final class NativeSplitCollapseTests: XCTestCase {
    private static var editable: [String: EditorCapability] { ["timeline": .init(editable: true), "split_screen": .init(editable: true)] }
    private static var collapsing: [String: EditorCapability] {
        editable.merging(["split_collapse": .init(editable: true)]) { _, new in new }
    }

    private func member(_ panel: Int, offset: Double, duration: Double, section: String = "s", clipIndex: Int = 0) -> EditorTimelineSlot {
        var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: clipIndex, inS: 0, durationS: duration, raw: ["source_duration_s": .number(30)])
        slot.setSplitMembership(sectionID: section, panelIndex: panel, offset: offset)
        return slot
    }

    private func fullScreen(duration: Double) -> EditorTimelineSlot {
        EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 0, durationS: duration, raw: ["source_duration_s": .number(30)])
    }

    private func section(panels: Int = 2, duration: Double, collapse: Bool = true, weights: [Double]? = nil, gutter: Double = 0) -> EditorSplitScreenSection {
        EditorSplitScreenSection(id: "s", durationS: duration, panelCount: panels, panelWeights: weights, gutter: gutter, collapseEmptyPanels: collapse)
    }

    @MainActor
    private func makeSession(_ document: EditorDocument) -> NativeEditorSession {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        return session
    }

    @MainActor
    private func clipID(_ session: NativeEditorSession, _ slot: EditorTimelineSlot) throws -> UUID {
        try XCTUnwrap(session.timelineClips.first { $0.slotID == slot.id }?.id)
    }

    @MainActor
    private func trimTrailing(_ session: NativeEditorSession, _ slot: EditorTimelineSlot, by delta: Double) throws {
        XCTAssertTrue(session.beginTrim(clipID: try clipID(session, slot), edge: .trailing))
        session.updateTrim(by: delta)
        session.endTrim()
    }

    // MARK: Pure

    func testActivePanelsUseHalfOpenWindowsAtExactBoundaries() {
        let split = section(panels: 3, duration: 6)
        let members = [member(0, offset: 0, duration: 4), member(1, offset: 2, duration: 1)]
        func active(_ time: Double) -> [Int] { NativeSplitPanelLayout.activePanels(in: split, members: members, at: time) }
        XCTAssertEqual(active(0), [0], "A window includes its start")
        XCTAssertEqual(active(1.999999), [0])
        XCTAssertEqual(active(2), [0, 1], "A member starting this instant is active this instant")
        XCTAssertEqual(active(2.999999), [0, 1])
        XCTAssertEqual(active(3), [0], "A window excludes its end")
        XCTAssertEqual(active(3.999999), [0])
        XCTAssertEqual(active(4), [], "Nothing is active once every member has ended; panel 2 never was")
    }

    func testOverlapInOnePanelCountsOnceAndForeignOrRemovedMembersNeverActivate() {
        let split = section(panels: 3, duration: 6)
        var removed = member(2, offset: 0, duration: 6)
        removed.removed = true
        let members = [member(0, offset: 0, duration: 2), member(0, offset: 1.8, duration: 2.2),
                       member(1, offset: 0, duration: 6, section: "other"), removed]
        for time in [0.5, 1.9, 2.0, 3.0] {
            XCTAssertEqual(NativeSplitPanelLayout.activePanels(in: split, members: members, at: time), [0], "at \(time)")
        }
    }

    func testNonCollapseSectionAlwaysShowsEveryPanel() {
        let split = section(panels: 3, duration: 6, collapse: false)
        let members = [member(0, offset: 0, duration: 1)]
        for time in [0, 0.5, 1, 5.9] {
            XCTAssertEqual(NativeSplitPanelLayout.activePanels(in: split, members: members, at: time), [0, 1, 2])
        }
    }

    func testCollapseLayoutFollowsTheContractFormula() {
        let rects = NativeSplitPanelLayout.panelRects(weights: [1, 2, 3], gutter: 0.05, active: [0, 2])
        XCTAssertEqual(rects.count, 2)
        XCTAssertEqual(rects[0].minY, 0, accuracy: 1e-12)
        XCTAssertEqual(rects[0].height, 0.95 * 1 / 4, accuracy: 1e-12)
        XCTAssertEqual(rects[1].minY, rects[0].maxY + 0.05, accuracy: 1e-12, "The gutter sits only between drawn panels")
        XCTAssertEqual(rects[1].height, 0.95 * 3 / 4, accuracy: 1e-12)
        XCTAssertEqual(rects[1].maxY, 1, accuracy: 1e-12)
        XCTAssertEqual(NativeSplitPanelLayout.dividerYs(weights: [1, 2, 3], gutter: 0.05, active: [0, 2]), [rects[0].maxY + 0.025])
        XCTAssertEqual(NativeSplitPanelLayout.panelRects(weights: [1, 2, 3], gutter: 0.05, active: [1]), [CGRect(x: 0, y: 0, width: 1, height: 1)])
        XCTAssertEqual(NativeSplitPanelLayout.panelRects(weights: [1, 2, 3], gutter: 0.05, active: []), [])
        XCTAssertEqual(NativeSplitPanelLayout.panelRects(weights: [1, 2, 3], gutter: 0.05, active: [2, 0, 0, 7]), rects, "Order, repeats and unknown panels do not matter")
        XCTAssertEqual(NativeSplitPanelLayout.panelRects(weights: [2, 1], gutter: 0.1, active: [0, 1]), NativeSplitPanelLayout.panelRects(weights: [2, 1], gutter: 0.1))
    }

    func testDividerBetweenDrawnNeighboursKeepsTheirSumAndLeavesTheEndedPanelAlone() {
        let moved = NativeSplitPanelLayout.draggedWeights([1, 2, 3], between: 0, and: 2, active: [0, 2], gutter: 0.05, deltaFraction: 0.1)
        XCTAssertEqual(moved[1], 2, "The ended panel keeps its weight")
        XCTAssertEqual(moved[0] + moved[2], 4, accuracy: 1e-12)
        XCTAssertEqual(moved[0], 1 + 0.1 * 4 / 0.95, accuracy: 1e-9, "The drag is measured on the collapsed layout")
        let clamped = NativeSplitPanelLayout.draggedWeights([1, 2, 3], between: 0, and: 2, active: [0, 2], gutter: 0.05, deltaFraction: 9)
        XCTAssertEqual(clamped[0], 4 * 0.85, accuracy: 1e-9)
        XCTAssertEqual(clamped[2], 4 * 0.15, accuracy: 1e-9)
        for (a, b, active) in [(0, 2, [0, 1, 2]), (2, 0, [0, 2]), (0, 1, [0, 2]), (0, 0, [0, 2])] {
            XCTAssertEqual(NativeSplitPanelLayout.draggedWeights([1, 2, 3], between: a, and: b, active: active, gutter: 0, deltaFraction: 0.2), [1, 2, 3])
        }
    }

    func testFlagRoundTripsAndFalseKeepsTheLegacyJSON() throws {
        let off = section(duration: 4, collapse: false)
        XCTAssertEqual(Set(try XCTUnwrap(off.json.objectValue).keys), ["id", "duration_s", "panel_count", "panel_weights", "gutter", "background_color"])
        XCTAssertEqual(EditorSplitScreenSection(json: off.json), off)
        let on = section(duration: 4)
        XCTAssertEqual(on.json.objectValue?["collapse_empty_panels"], .bool(true))
        XCTAssertEqual(EditorSplitScreenSection(json: on.json), on)
        XCTAssertEqual(EditorSplitScreenSection(json: on.json)?.collapseEmptyPanels, true)
        for bad: JSONValue in [.number(1), .string("true"), .null] {
            var object = try XCTUnwrap(on.json.objectValue)
            object["collapse_empty_panels"] = bad
            XCTAssertNil(EditorSplitScreenSection(json: .object(object)), "\(bad)")
        }
        var document = EditorDocument(clips: [member(0, offset: 0, duration: 4)])
        document.splitScreenSections = [on]
        let reopened = EditorDocument(snapshot: document.encodeSnapshot())
        XCTAssertEqual(reopened.splitScreenSections, [on])
        XCTAssertTrue(reopened.splitScreenIsValid)
    }

    // MARK: Section follows its longest member

    @MainActor
    func testTrailingTrimExtendsTheSectionShiftsLaterContentAndUndoesInOneStep() throws {
        let top = member(0, offset: 0, duration: 4), side = member(1, offset: 0, duration: 2), tail = fullScreen(duration: 2)
        var document = EditorDocument(clips: [top, side, tail], captionCues: [.init(id: "c", startS: 4.2, endS: 4.8, text: "Tail")], capabilities: Self.editable)
        document.splitScreenSections = [section(duration: 4)]
        let session = makeSession(document)
        try trimTrailing(session, top, by: 1.5)

        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 5.5, accuracy: 1e-9, "The tail of the extended clip is not cut off")
        XCTAssertEqual(session.document.clips[0].durationS ?? 0, 5.5, accuracy: 1e-9)
        XCTAssertEqual(session.document.clips[1], side)
        XCTAssertEqual(session.timelineProjection.totalDuration, 7.5, accuracy: 1e-9)
        XCTAssertEqual(session.document.captionCues[0].startS, 5.7, accuracy: 1e-9, "A caption on the later clip follows it")
        XCTAssertEqual(session.document.captionCues[0].endS, 6.3, accuracy: 1e-9)
        XCTAssertTrue(session.document.splitScreenIsValid)

        session.undo()
        XCTAssertEqual(session.document, document, "One undo restores the clip, the section and the caption")
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testNonCollapseSectionKeepsTheClampedTrailingTrim() throws {
        let top = member(0, offset: 0, duration: 4), side = member(1, offset: 0, duration: 2)
        var document = EditorDocument(clips: [top, side], capabilities: Self.editable)
        document.splitScreenSections = [section(duration: 4, collapse: false)]
        let session = makeSession(document)
        try trimTrailing(session, top, by: 1.5)
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testShorteningTheLongestMemberShrinksTheSectionWithoutDroppingAnyone() throws {
        let top = member(0, offset: 0, duration: 4), side = member(1, offset: 0, duration: 2)
        var document = EditorDocument(clips: [top, side], capabilities: Self.editable)
        document.splitScreenSections = [section(duration: 4)]
        let session = makeSession(document)
        try trimTrailing(session, top, by: -3)
        XCTAssertEqual(session.document.clips.count, 2)
        XCTAssertEqual(session.document.clips[0].durationS ?? 0, 1, accuracy: 1e-9)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 2, accuracy: 1e-9)
        XCTAssertTrue(session.document.splitScreenIsValid)
        session.undo()
        XCTAssertEqual(session.document, document)
    }

    @MainActor
    func testRemovingTheLongestMemberShrinksTheSectionInOneUndoStep() throws {
        let top = member(0, offset: 0, duration: 4), side = member(1, offset: 0, duration: 2)
        var document = EditorDocument(clips: [top, side], capabilities: Self.editable)
        document.splitScreenSections = [section(duration: 4)]
        let session = makeSession(document)
        XCTAssertTrue(session.deleteSelection(.init(kind: .clip, id: try clipID(session, top).uuidString)))
        XCTAssertEqual(session.document.clips, [side])
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 2, accuracy: 1e-9)
        session.undo()
        XCTAssertEqual(session.document.clips, [top, side])
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 4)
    }

    // MARK: Playhead and divider

    @MainActor
    func testContextReportsTheDrawnPanelsAndDividerDragMovesOnlyThePairOnScreen() throws {
        let top = member(0, offset: 0, duration: 4), ended = member(1, offset: 0, duration: 2), late = member(2, offset: 1, duration: 3)
        var document = EditorDocument(clips: [top, ended, late], capabilities: Self.editable)
        document.splitScreenSections = [section(panels: 3, duration: 4, weights: [1, 2, 3], gutter: 0.05)]
        let session = makeSession(document)
        session.currentTime = 1.5
        XCTAssertEqual(session.splitContextAtPlayhead?.activePanels, [0, 1, 2])
        session.currentTime = 2
        XCTAssertEqual(session.splitContextAtPlayhead?.activePanels, [0, 2], "The panel whose clip ended at 2 is gone at 2")
        session.currentTime = 4
        XCTAssertEqual(session.splitContextAtPlayhead?.activePanels, [0, 2], "The parked last instant still shows the last frame")

        session.currentTime = 3
        session.beginSplitDividerDrag(sectionID: "s", between: 0, and: 1)
        session.updateSplitDividerDrag(deltaFraction: 0.1)
        session.endSplitDividerDrag()
        XCTAssertEqual(session.document, document, "Panel 1 is not on screen, so this pair is not a divider")
        XCTAssertFalse(session.canUndo)

        session.beginSplitDividerDrag(sectionID: "s", between: 0, and: 2)
        session.updateSplitDividerDrag(deltaFraction: 0.05)
        session.updateSplitDividerDrag(deltaFraction: 0.1)
        XCTAssertFalse(session.canUndo, "Live updates add no undo snapshot")
        session.endSplitDividerDrag()
        let weights = try XCTUnwrap(session.document.splitScreenSections.first).panelWeights
        XCTAssertEqual(weights[1], 2)
        XCTAssertEqual(weights[0] + weights[2], 4, accuracy: 1e-12)
        XCTAssertEqual(weights[0], 1 + 0.1 * 4 / 0.95, accuracy: 1e-9)
        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testNonCollapseContextShowsEveryPanelAndKeepsTheOldDrag() throws {
        var document = EditorDocument(clips: [member(0, offset: 0, duration: 2)], capabilities: Self.editable)
        document.splitScreenSections = [section(panels: 3, duration: 4, collapse: false)]
        let session = makeSession(document)
        session.currentTime = 3
        XCTAssertEqual(session.splitContextAtPlayhead?.activePanels, [0, 1, 2])
        session.beginSplitDividerDrag(sectionID: "s", between: 1, and: 2)
        session.updateSplitDividerDrag(deltaFraction: 0.1)
        session.endSplitDividerDrag()
        let weights = try XCTUnwrap(session.document.splitScreenSections.first).panelWeights
        XCTAssertEqual(weights[0], 1)
        XCTAssertEqual(weights[1] + weights[2], 2, accuracy: 1e-12)
        XCTAssertNotEqual(weights[1], 1)
    }

    // MARK: Creation and the toggle

    @MainActor
    func testNewSectionsCollapseOnlyWhenTheServerOffersIt() throws {
        for (capabilities, expected) in [(Self.editable, false), (Self.collapsing, true)] {
            for scope in [NativeSplitScope.selectedClip, .wholeVideo] {
                let clip = fullScreen(duration: 4)
                let session = makeSession(EditorDocument(clips: [clip], capabilities: capabilities))
                XCTAssertEqual(session.canCollapseSplitPanels, expected)
                session.selectClip(try clipID(session, clip))
                session.createSplitScreen(panelCount: 2, scope: scope)
                let created = try XCTUnwrap(session.document.splitScreenSections.first, "\(scope)")
                XCTAssertEqual(created.collapseEmptyPanels, expected, "\(scope) with \(capabilities.keys.sorted())")
                XCTAssertEqual(created.durationS, 4, accuracy: 1e-9)
            }
        }
    }

    @MainActor
    func testToggleIsOneUndoStepFitsTheSectionAndNeedsTheCapabilityToTurnOn() throws {
        let top = member(0, offset: 0, duration: 4)
        var document = EditorDocument(clips: [top, member(1, offset: 1, duration: 2)], capabilities: Self.editable)
        document.splitScreenSections = [section(duration: 6, collapse: false)]

        let refused = makeSession(document)
        refused.setSplitCollapse(sectionID: "s", true)
        XCTAssertEqual(refused.document, document, "Without the server's capability the flag never turns on")

        document.capabilities = Self.collapsing
        let session = makeSession(document)
        session.setSplitCollapse(sectionID: "s", true)
        XCTAssertEqual(session.document.splitScreenSections.first?.collapseEmptyPanels, true)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 4, accuracy: 1e-9, "The section now ends with its longest member")
        session.undo()
        XCTAssertEqual(session.document, document)
        XCTAssertFalse(session.canUndo)

        session.setSplitCollapse(sectionID: "s", true)
        session.setSplitCollapse(sectionID: "s", false)
        XCTAssertEqual(session.document.splitScreenSections.first?.collapseEmptyPanels, false)
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS ?? 0, 4, accuracy: 1e-9, "Turning it off keeps the length")
        XCTAssertEqual(session.document.splitScreenSections.first?.json.objectValue?["collapse_empty_panels"], nil)
    }

    // MARK: Compile

    @MainActor
    func testCompilerCarriesTheFlagIntoTheRecipeSection() throws {
        let top = member(0, offset: 0, duration: 4)
        var document = EditorDocument(clips: [top], capabilities: Self.collapsing)
        document.splitScreenSections = [section(duration: 4)]
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "source", relativePath: "source.mp4", fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 30),
            url: URL(fileURLWithPath: "/fixture/source.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        func recipe(collapse: Bool) throws -> KriaMediaEngine.EditRecipe {
            var value = document
            value.splitScreenSections[0].collapseEmptyPanels = collapse
            let session = makeSession(value)
            return try compiler.compile(document: value, clips: session.timelineClips, items: [], sources: [0: source]).recipe
        }
        XCTAssertEqual(try recipe(collapse: true).splitScreenSections.map(\.collapseEmptyPanels), [true])
        let off = try recipe(collapse: false)
        XCTAssertEqual(off.splitScreenSections.map(\.collapseEmptyPanels), [false])
    }
}
