import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1421 blank block (`media_kind: "blank"` + `color`, protocol 51). Each test is written against a listed failure mode:
/// a blank written with a clip index, read back without its colour or kind, or dropped by an older decode; a blank outside a
/// split, over a clip in its panel, with a bad colour, or alone in a section accepted by validation; Fill taking the wrong
/// free time (the first gap instead of the one under the playhead), offered with no room or without the capability, or
/// making more than one undo step; a colour change that is not one undo step or accepts a name; a trim that lets a blank
/// past its section (fixed sections grow like a photo's) or onto the next member; a cut that shares an identity or loses
/// the colour; a collapsing panel that vanishes while its blank plays; deleting the last clip leaving a blank-only section
/// that refuses the delete; Remove split or Full screen turning a blank into an ordinary clip; the recipe compile
/// refusing what validation accepted (KRI-1423), emitting a track clip for a blank, or drawing the wrong window.
final class NativeSplitBlankTests: XCTestCase {
    private static var capable: [String: EditorCapability] {
        ["timeline": .init(editable: true), "split_screen": .init(editable: true), "split_blank_panel": .init(editable: true)]
    }

    @MainActor
    private func makeSession(_ document: EditorDocument) -> NativeEditorSession {
        let session = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [],
            captions: .init(enabled: false, style: "sentence"), music: nil, revision: 0,
            serverSnapshot: ["render_destination": .string("device")]))
        session.replaceDocumentForTesting(document)
        return session
    }

    private func member(_ panel: Int, offset: Double, duration: Double, section: String = "s") -> EditorTimelineSlot {
        var slot = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 0, durationS: duration, raw: ["source_duration_s": .number(30)])
        slot.setSplitMembership(sectionID: section, panelIndex: panel, offset: offset)
        return slot
    }

    private func blank(_ panel: Int, offset: Double, duration: Double, color: String = "#FF0000", section: String = "s") -> EditorTimelineSlot {
        .blank(color: color, durationS: duration, sectionID: section, panelIndex: panel, offset: offset)
    }

    private func document(_ clips: [EditorTimelineSlot], duration: Double = 6, collapse: Bool = false, background: String = "#112233",
                          capabilities: [String: EditorCapability]? = nil) -> EditorDocument {
        var value = EditorDocument(clips: clips, capabilities: capabilities ?? Self.capable)
        value.splitScreenSections = [EditorSplitScreenSection(id: "s", durationS: duration, panelCount: 2, backgroundColor: background, collapseEmptyPanels: collapse)]
        return value
    }

    @MainActor
    private func clipID(_ session: NativeEditorSession, _ slot: EditorTimelineSlot) throws -> UUID {
        try XCTUnwrap(session.timelineClips.first { $0.slotID == slot.id }?.id)
    }

    @MainActor
    private func blanks(_ session: NativeEditorSession) -> [EditorTimelineSlot] { session.document.clips.filter { !$0.removed && $0.isBlank } }

    /// The row Save writes for slot `id`.
    private func row(_ document: EditorDocument, _ id: String?) throws -> [String: JSONValue] {
        let slots = try XCTUnwrap(document.snapshot(for: .timeline)?.objectValue?["slots"])
        guard case .array(let rows) = slots else { throw XCTSkip("no slots") }
        return try XCTUnwrap(rows.compactMap(\.objectValue).first { $0["slot_id"] == .string(id ?? "") })
    }

    // MARK: Wire

    func testABlankIsWrittenWithoutAClipIndexAndReadBackWithItsColour() throws {
        let top = member(0, offset: 0, duration: 4), fill = blank(1, offset: 1, duration: 2, color: "#00ff00")
        XCTAssertEqual(fill.blankColor, "#00FF00", "A colour is kept in one case")
        let value = document([top, fill], duration: 4)
        let row = try self.row(value, fill.id)
        XCTAssertNil(row["clip_index"], "A blank has no source")
        XCTAssertEqual(row["media_kind"], .string("blank"))
        XCTAssertEqual(row["color"], .string("#00FF00"))
        XCTAssertEqual(row["split_panel_index"], .number(1))
        XCTAssertEqual(row["duration_s"], .number(2))
        XCTAssertEqual(row["in_s"], .number(0))

        let reopened = EditorDocument(snapshot: value.encodeSnapshot())
        XCTAssertEqual(reopened.clips.count, 2, "A decode that wants a clip index would drop the blank")
        XCTAssertTrue(reopened.splitScreenIsValid)
        let read = try XCTUnwrap(reopened.clips.first { $0.isBlank })
        XCTAssertEqual(read.blankColor, "#00FF00")
        XCTAssertEqual(read.splitPanelIndex, 1)
        XCTAssertEqual(read.splitOffsetS, 1)
        XCTAssertEqual(read.durationS, 2)
        XCTAssertEqual(try self.row(reopened, read.id), try self.row(value, fill.id), "Reopening and saving again changes nothing")
        XCTAssertEqual(reopened.snapshot(for: .timeline), value.snapshot(for: .timeline))
    }

    func testValidationAcceptsOnlyABlankThatCanRender() {
        let top = member(0, offset: 0, duration: 4)
        XCTAssertTrue(document([top, blank(1, offset: 1, duration: 2)], duration: 4).splitScreenIsValid)
        XCTAssertTrue(document([top, blank(1, offset: 0, duration: 2), blank(1, offset: 2, duration: 2, color: "#00FF00")], duration: 4).splitScreenIsValid)

        var outside = document([top, blank(1, offset: 1, duration: 2)], duration: 4)
        outside.clips[1].clearSplitMembership()
        XCTAssertFalse(outside.splitScreenIsValid, "A blank belongs in a split panel")

        var withIndex = document([top, blank(1, offset: 1, duration: 2)], duration: 4)
        withIndex.clips[1].clipIndex = 0
        XCTAssertFalse(withIndex.splitScreenIsValid, "A blank has no clip index")

        for color in ["red", "#FFF", "#GG0000", ""] {
            var bad = document([top, blank(1, offset: 1, duration: 2)], duration: 4)
            bad.clips[1].raw["color"] = .string(color)
            XCTAssertFalse(bad.splitScreenIsValid, color)
        }
        var nameless = document([top, blank(1, offset: 1, duration: 2)], duration: 4)
        nameless.clips[1].raw.removeValue(forKey: "color")
        XCTAssertFalse(nameless.splitScreenIsValid)

        XCTAssertFalse(document([top, blank(0, offset: 1, duration: 2)], duration: 4).splitScreenIsValid, "No blank over a clip in its panel")
        XCTAssertFalse(document([top, blank(1, offset: 0, duration: 2), blank(1, offset: 1, duration: 2)], duration: 4).splitScreenIsValid, "No two blanks overlap")
        XCTAssertFalse(document([blank(1, offset: 0, duration: 2)], duration: 4).splitScreenIsValid, "A section keeps one real clip")
    }

    // MARK: Fill

    @MainActor
    func testFillTakesTheFreeTimeUnderThePlayheadInOneUndoStepAndStartsFromTheSectionBackground() throws {
        let top = member(0, offset: 0, duration: 6), early = member(1, offset: 0, duration: 2), late = member(1, offset: 4, duration: 2)
        let original = document([top, early, late])
        let session = makeSession(original)
        session.currentTime = 3
        XCTAssertTrue(session.canFillSplitPanelWithBlank(sectionID: "s", panel: 1))
        session.fillSplitPanelWithBlank(sectionID: "s", panel: 1)

        let fill = try XCTUnwrap(blanks(session).first)
        XCTAssertEqual(blanks(session).count, 1)
        XCTAssertEqual(fill.splitPanelIndex, 1)
        XCTAssertEqual(fill.splitOffsetS, 2, accuracy: 1e-9, "The gap the playhead is in, not the first one")
        XCTAssertEqual(fill.durationS ?? 0, 2, accuracy: 1e-9)
        XCTAssertEqual(fill.blankColor, "#112233", "A new block starts as the section background")
        XCTAssertEqual(session.selection, EditorSelection(kind: .clip, id: try XCTUnwrap(fill.id)))
        XCTAssertEqual(session.document.splitScreenSections.first?.durationS, 6)
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertFalse(session.canFillSplitPanelWithBlank(sectionID: "s", panel: 1), "The gap is now full")

        session.undo()
        XCTAssertEqual(session.document, original)
        XCTAssertFalse(session.canUndo, "Fill is one undo step")
    }

    @MainActor
    func testFillOnAnEmptyPanelCoversTheWholeSectionAndAfterTheLastClipTakesTheRest() throws {
        let empty = makeSession(document([member(0, offset: 0, duration: 6)]))
        empty.fillSplitPanelWithBlank(sectionID: "s", panel: 1)
        XCTAssertEqual(blanks(empty).map { [$0.splitOffsetS, $0.durationS ?? 0] }, [[0, 6]])

        let after = makeSession(document([member(0, offset: 0, duration: 6), member(1, offset: 0, duration: 2)]))
        after.currentTime = 1
        after.fillSplitPanelWithBlank(sectionID: "s", panel: 1)
        XCTAssertEqual(blanks(after).map { [$0.splitOffsetS, $0.durationS ?? 0] }, [[2, 4]])
    }

    @MainActor
    func testFillIsRefusedWithoutRoomOrWithoutTheCapabilityOrAnOldProtocolBuild() throws {
        let full = makeSession(document([member(0, offset: 0, duration: 6), member(1, offset: 0, duration: 6)]))
        XCTAssertFalse(full.canFillSplitPanelWithBlank(sectionID: "s", panel: 1))
        full.fillSplitPanelWithBlank(sectionID: "s", panel: 1)
        XCTAssertFalse(full.canUndo)
        XCTAssertEqual(full.splitScreenActionError, NativeEditorSession.splitPanelFullMessage)

        let locked = makeSession(document([member(0, offset: 0, duration: 6)], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)]))
        XCTAssertFalse(locked.canFillSplitPanelWithBlank(sectionID: "s", panel: 1), "The server has not offered blanks")
        locked.fillSplitPanelWithBlank(sectionID: "s", panel: 1)
        XCTAssertTrue(blanks(locked).isEmpty)
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 51)
    }

    // MARK: Trim, cut, move

    @MainActor
    func testTrimmingABlankGrowsAFixedSectionLikeAPhotoAndStopsAtTheNextMember() throws {
        let top = member(0, offset: 0, duration: 4), fill = blank(1, offset: 0, duration: 2), next = member(1, offset: 3, duration: 1)
        let session = makeSession(document([top, fill, next], duration: 4))
        XCTAssertTrue(session.beginTrim(clipID: try clipID(session, fill), edge: .trailing))
        session.updateTrim(by: 5)
        session.endTrim()
        XCTAssertEqual(blanks(session).first?.durationS ?? 0, 3, accuracy: 1e-9, "Stops at the next member in its panel")

        let tail = makeSession(document([top, blank(1, offset: 2, duration: 2)], duration: 4))
        let tailBlank = try XCTUnwrap(blanks(tail).first)
        XCTAssertTrue(tail.beginTrim(clipID: try clipID(tail, tailBlank), edge: .trailing))
        tail.updateTrim(by: 3)
        tail.endTrim()
        XCTAssertEqual(blanks(tail).first?.durationS ?? 0, 5, accuracy: 1e-9, "No footage to run out of")
        XCTAssertEqual(tail.document.splitScreenSections.first?.durationS ?? 0, 7, accuracy: 1e-9, "A fixed section grows with its member")
        XCTAssertNil(blanks(tail).first?.raw["source_end_s"])
        XCTAssertEqual(blanks(tail).first?.inS, 0)
        XCTAssertTrue(tail.document.splitScreenIsValid)
        tail.undo()
        XCTAssertEqual(tail.document.splitScreenSections.first?.durationS, 4, "One undo restores the block and the section")

        XCTAssertTrue(tail.beginTrim(clipID: try clipID(tail, tailBlank), edge: .leading))
        tail.updateTrim(by: 0.5)
        tail.endTrim()
        let trimmed = try XCTUnwrap(blanks(tail).first)
        XCTAssertEqual(trimmed.splitOffsetS, 2.5, accuracy: 1e-9)
        XCTAssertEqual(trimmed.durationS ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(trimmed.inS, 0)
        XCTAssertEqual(trimmed.blankColor, tailBlank.blankColor)
    }

    @MainActor
    func testMovingABlankChangesOnlyItsOffsetAndRefusesAnOverlap() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 0, duration: 2), other = member(1, offset: 3, duration: 2)
        let session = makeSession(document([top, fill, other]))
        try XCTUnwrap(fill.id).withSelection(in: session)
        session.setSelectedSplitScreenOffset(1)
        XCTAssertEqual(blanks(session).first?.splitOffsetS, 1)
        XCTAssertEqual(blanks(session).first?.durationS, 2)
        let before = session.document
        session.setSelectedSplitScreenOffset(2)
        XCTAssertEqual(session.document, before, "It would overlap the clip at 3 s")
    }

    @MainActor
    func testCutMakesTwoIndependentBlocksThatKeepTheColour() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 1, duration: 4, color: "#00FF00")
        let original = document([top, fill])
        let session = makeSession(original)
        try XCTUnwrap(fill.id).withSelection(in: session)
        session.currentTime = 3
        XCTAssertTrue(session.canSplitSelectedClip)
        session.splitSelectedClip()

        let pieces = blanks(session).sorted { $0.splitOffsetS < $1.splitOffsetS }
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.map(\.splitOffsetS), [1, 3])
        XCTAssertEqual(pieces.map { $0.durationS ?? 0 }, [2, 2])
        XCTAssertEqual(Set(pieces.compactMap(\.id)).count, 2, "Each piece is its own occurrence")
        XCTAssertNil(pieces[1].parentSegmentID, "A blank has no parent source segment")
        XCTAssertTrue(pieces.allSatisfy { $0.blankColor == "#00FF00" && $0.inS == 0 && $0.raw["source_start_locked"] == nil })
        XCTAssertTrue(session.document.splitScreenIsValid)

        try XCTUnwrap(pieces[1].id).withSelection(in: session)
        session.setSelectedBlankColor("#0000FF")
        XCTAssertEqual(blanks(session).map(\.blankColor).sorted { ($0 ?? "") < ($1 ?? "") }, ["#0000FF", "#00FF00"], "A cut block recolours on its own")
        session.undo()
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    // MARK: Collapse

    @MainActor
    func testACollapsingPanelStaysOnScreenWhileItsBlankPlaysAndTheSectionFollowsTheBlank() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 1, duration: 3)
        let session = makeSession(document([top, fill], collapse: true))
        func active(at time: Double) throws -> [Int] {
            session.currentTime = time
            return try XCTUnwrap(session.splitContextAtPlayhead).activePanels
        }
        XCTAssertEqual(try active(at: 0.5), [0])
        XCTAssertEqual(try active(at: 1), [0, 1])
        XCTAssertEqual(try active(at: 3.999), [0, 1])
        XCTAssertEqual(try active(at: 4), [0])
        XCTAssertEqual(try XCTUnwrap(session.splitContextAtPlayhead).emptyPanels, [], "A panel with only a blank is not empty")

        let tail = blank(1, offset: 5, duration: 3)
        let grown = makeSession(document([top, tail], collapse: true))
        let id = try clipID(grown, tail)
        XCTAssertTrue(grown.beginTrim(clipID: id, edge: .trailing))
        grown.updateTrim(by: 1)
        grown.endTrim()
        XCTAssertEqual(grown.document.splitScreenSections.first?.durationS ?? 0, 9, accuracy: 1e-9, "A collapsing section lasts until its longest member, a blank included")
    }

    // MARK: Colour

    @MainActor
    func testColourChangeIsOneUndoStepAndRefusesAnythingButAHexColour() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 0, duration: 6, color: "#112233")
        let original = document([top, fill])
        let session = makeSession(original)
        try XCTUnwrap(fill.id).withSelection(in: session)
        XCTAssertEqual(session.selectedBlankColor, "#112233")

        session.setSelectedBlankColor("#abcdef")
        XCTAssertEqual(session.selectedBlankColor, "#ABCDEF")
        session.setSelectedBlankColor("#ABCDEF")
        for bad in ["red", "#FFF", "", "#12345G"] {
            session.setSelectedBlankColor(bad)
            XCTAssertEqual(session.selectedBlankColor, "#ABCDEF", bad)
        }
        session.undo()
        XCTAssertEqual(session.document, original, "Setting the same colour twice and the refusals leave one step")
        XCTAssertFalse(session.canUndo)
        session.redo()
        XCTAssertEqual(session.selectedBlankColor, "#ABCDEF")

        let plain = makeSession(document([top]))
        try XCTUnwrap(top.id).withSelection(in: plain)
        XCTAssertNil(plain.selectedBlankColor, "Only a blank has a block colour")
        plain.setSelectedBlankColor("#FF0000")
        XCTAssertFalse(plain.canUndo)
    }

    // MARK: Delete, remove, full screen

    @MainActor
    func testDeletingABlankLeavesAValidSectionAndUndoBringsItBack() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 1, duration: 2)
        let original = document([top, fill])
        let session = makeSession(original)
        try XCTUnwrap(fill.id).withSelection(in: session)
        session.deleteSelectedClip()
        XCTAssertTrue(blanks(session).isEmpty)
        XCTAssertEqual(session.document.clips.filter { !$0.removed }.count, 1)
        XCTAssertTrue(session.document.splitScreenIsValid)
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    @MainActor
    func testDeletingTheLastClipOfASectionTakesItsBlanksWithIt() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 1, duration: 2)
        let ordinary = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 0, durationS: 2, raw: ["source_duration_s": .number(30)])
        let original = document([ordinary, top, fill])
        let session = makeSession(original)
        try XCTUnwrap(top.id).withSelection(in: session)
        session.deleteSelectedClip()
        XCTAssertTrue(session.document.splitScreenSections.isEmpty, "A section of only blanks cannot render, so it goes")
        XCTAssertTrue(blanks(session).isEmpty, "No blank is left outside a split")
        XCTAssertTrue(session.document.splitScreenIsValid)
        XCTAssertEqual(session.document.clips.filter { !$0.removed }.map(\.id), [ordinary.id])
        session.undo()
        XCTAssertEqual(session.document, original)
    }

    @MainActor
    func testRemoveSplitAndFullScreenNeverTurnABlankIntoAnOrdinaryClip() throws {
        let top = member(0, offset: 0, duration: 6), fill = blank(1, offset: 0, duration: 6)
        let session = makeSession(document([top, fill]))
        session.removeSplitScreen(sectionID: "s", keepPanel: 1)
        XCTAssertTrue(session.document.clips.contains { $0.isBlank && $0.splitSectionID == "s" }, "A panel that holds only a blank cannot be the one kept")
        XCTAssertFalse(session.canUndo)

        session.removeSplitScreen(sectionID: "s", keepPanel: 0)
        XCTAssertTrue(session.document.splitScreenSections.isEmpty)
        XCTAssertTrue(blanks(session).isEmpty, "The blank goes with the panels that are not kept")
        XCTAssertEqual(session.document.clips.filter { !$0.removed }.map(\.id), [top.id])
        XCTAssertTrue(session.document.splitScreenIsValid)

        let split = makeSession(document([top, fill]))
        split.currentTime = 3
        split.makeSplitFullScreen(sectionID: "s", side: .fromPlayhead)
        XCTAssertTrue(split.document.splitScreenIsValid)
        XCTAssertTrue(split.document.clips.filter { !$0.removed && $0.isBlank }.allSatisfy { $0.splitSectionID != nil }, "No blank is left outside a split")
        XCTAssertFalse(split.document.clips.contains { !$0.removed && $0.splitSectionID == nil && $0.isBlank })
    }

    // MARK: Recipe compile (the path Save and export share)

    @MainActor
    func testCompileTurnsABlankIntoASectionBlankNotATrackClip() throws {
        let top = member(0, offset: 0, duration: 4), fill = blank(1, offset: 1, duration: 2, color: "#00ff00")
        let session = makeSession(document([top, fill], duration: 4, background: "#FFFFFF"))
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "source", relativePath: "source.mp4", fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 30),
            url: URL(fileURLWithPath: "/fixture/source.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: session.document, clips: session.timelineClips, items: [], sources: [0: source]).recipe

        XCTAssertEqual(recipe.tracks.flatMap(\.clips).map(\.id), [top.id!], "A blank adds no track clip, so no audio or source")
        let section = try XCTUnwrap(recipe.splitScreenSections.first)
        XCTAssertEqual(section.blanks, [SplitScreenBlank(id: fill.id!, panelIndex: 1, start: 1, end: 3, color: "#00FF00")])
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.splitScreen, .splitBlankPanel]))
        XCTAssertNoThrow(try recipe.validate())
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), 4, accuracy: 0.001)

        let none = makeSession(document([top], duration: 4))
        let plain = try compiler.compile(document: none.document, clips: none.timelineClips, items: [], sources: [0: source]).recipe
        XCTAssertTrue(plain.splitScreenSections.allSatisfy { $0.blanks.isEmpty })
        XCTAssertFalse(plain.effectiveCapabilities.contains(.splitBlankPanel))
    }

    @MainActor
    func testCompileOfASectionAfterAnOrdinaryClipPlacesTheBlankOnTheSectionClock() throws {
        let lead = EditorTimelineSlot(id: UUID().uuidString, clipIndex: 0, inS: 0, durationS: 2, raw: ["source_duration_s": .number(30)])
        let top = member(0, offset: 0, duration: 4), fill = blank(1, offset: 1, duration: 2)
        let session = makeSession(document([lead, top, fill], duration: 4))
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "source", relativePath: "source.mp4", fingerprint: .init(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 30),
            url: URL(fileURLWithPath: "/fixture/source.mp4"))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let recipe = try compiler.compile(document: session.document, clips: session.timelineClips, items: [], sources: [0: source]).recipe
        let section = try XCTUnwrap(recipe.splitScreenSections.first)
        XCTAssertEqual([section.start, section.end], [2, 6])
        XCTAssertEqual(section.blanks.map { [$0.start, $0.end] }, [[3, 5]], "The section starts after the ordinary clip")
        XCTAssertNoThrow(try recipe.validate())
    }
}

private extension String {
    /// Selects the clip with this slot id without moving the playhead.
    @MainActor func withSelection(in session: NativeEditorSession) {
        session.select(EditorSelection(kind: .clip, id: self), seekToStart: false)
    }
}
