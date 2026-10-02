import UIKit
import XCTest
@testable import Kria

/// KRI-240 (plan 026): caption line editing — word-timing rewrite, line editor
/// configuration, the Variant A edit layout and the session's line lifecycle.
final class CaptionLineEditingTests: XCTestCase {

    // MARK: CaptionWordRewrite (plan 026 D6, R1, R11)

    private func word(_ text: String, _ start: Double, _ end: Double, quality: String? = nil) -> JSONValue {
        var object: [String: JSONValue] = ["text": .string(text), "start_s": .number(start), "end_s": .number(end)]
        if let quality { object["timing_quality"] = .string(quality) }
        return .object(object)
    }

    private let entryWords: [JSONValue] = [
        .object(["text": .string("Simit"), "start_s": .number(0.0), "end_s": .number(0.4), "timing_quality": .string("aligned")]),
        .object(["text": .string("ve"), "start_s": .number(0.4), "end_s": .number(0.6), "timing_quality": .string("aligned")]),
        .object(["text": .string("cay"), "start_s": .number(0.6), "end_s": .number(1.0), "timing_quality": .string("aligned")]),
    ]

    func testSameWordCountKeepsEachWordsTimingByPosition() throws {
        let words = try XCTUnwrap(CaptionWordRewrite.words(entryText: "Simit ve cay", entryWords: entryWords,
                                                           text: "Simit ve çay", startS: 0, endS: 1))
        XCTAssertEqual(words.count, 3)
        let third = try XCTUnwrap(words[2].objectValue)
        XCTAssertEqual(third["text"], .string("çay"))
        XCTAssertEqual(third["start_s"], .number(0.6))
        XCTAssertEqual(third["end_s"], .number(1.0))
        XCTAssertEqual(third["timing_quality"], .string("aligned"), "a positional keep stays real timing")
    }

    func testDifferentWordCountSpreadsByCharacterWeightInsideTheLine() throws {
        let words = try XCTUnwrap(CaptionWordRewrite.words(entryText: "Simit ve cay", entryWords: entryWords,
                                                           text: "Simit ve çay çok güzel", startS: 2, endS: 4))
        XCTAssertEqual(words.compactMap { $0.objectValue?["text"]?.stringValue }, ["Simit", "ve", "çay", "çok", "güzel"])
        let starts = words.compactMap { $0.objectValue?["start_s"] }.compactMap { value -> Double? in
            if case .number(let n) = value { return n } else { return nil }
        }
        XCTAssertEqual(starts.first, 2)
        XCTAssertEqual(starts, starts.sorted(), "words stay in order")
        guard case .number(let lastEnd)? = words.last?.objectValue?["end_s"] else { return XCTFail("missing end") }
        XCTAssertEqual(lastEnd, 4, "the spread fills the line window exactly")
        XCTAssertTrue(words.allSatisfy { $0.objectValue?["timing_quality"] == .string("segment_estimate") })
        // "Simit" (5 chars) gets more time than "ve" (2 chars).
        func span(_ index: Int) -> Double {
            guard case .number(let s)? = words[index].objectValue?["start_s"],
                  case .number(let e)? = words[index].objectValue?["end_s"] else { return 0 }
            return e - s
        }
        XCTAssertGreaterThan(span(0), span(1))
    }

    func testReturningToTheEntryTextRestoresTheOriginalWordsExactly() {
        XCTAssertEqual(CaptionWordRewrite.words(entryText: "Simit ve cay", entryWords: entryWords,
                                                text: "Simit ve cay", startS: 0, endS: 1), entryWords)
    }

    func testCuesWithoutWordsStayWithoutWordsAndEmptyTextHasNone() {
        XCTAssertNil(CaptionWordRewrite.words(entryText: "a", entryWords: nil, text: "b", startS: 0, endS: 1))
        XCTAssertEqual(CaptionWordRewrite.words(entryText: "a b", entryWords: entryWords, text: "   ", startS: 0, endS: 1), [])
    }

    // MARK: LineEditorConfiguration (plan 026 D20; R4 preserve-assertion 1)

    @MainActor
    func testDefaultConfigurationIsTheTextToolsEditorUnchanged() {
        let view = ExplicitLineTextView()
        LineEditorConfiguration().apply(to: view)
        XCTAssertFalse(view.wrapsLines)
        XCTAssertFalse(view.textContainer.widthTracksTextView, "the Text tool keeps explicit lines that scroll sideways")
        XCTAssertEqual(view.returnKeyType, .default)
        XCTAssertEqual(view.autocorrectionType, .default)
        XCTAssertEqual(view.spellCheckingType, .default)
        XCTAssertEqual(view.autocapitalizationType, .sentences)
        XCTAssertEqual(view.smartQuotesType, .default)
        XCTAssertEqual(view.smartDashesType, .default)
        XCTAssertNil(view.preferredLanguage)
        XCTAssertEqual(view.accessibilityLabel, "Text")
    }

    @MainActor
    func testCaptionLineConfigurationIsTranscriptSafe() {
        let middle = LineEditorConfiguration.captionLine(language: "tr", isLast: false, installedLanguages: ["en-US", "tr-TR"])
        XCTAssertTrue(middle.wrapsLines)
        XCTAssertTrue(middle.interceptsReturn)
        XCTAssertEqual(middle.returnKeyType, .next)
        XCTAssertEqual(middle.autocorrectionType, .no, "never silently rewrite names or Turkish words")
        XCTAssertEqual(middle.spellCheckingType, .default, "a matching keyboard keeps spell-check underlines")
        XCTAssertEqual(middle.preferredLanguage, "tr")
        XCTAssertTrue(middle.flattensNewlines)

        let last = LineEditorConfiguration.captionLine(language: "tr", isLast: true, installedLanguages: ["en-US"])
        XCTAssertEqual(last.returnKeyType, .done)
        XCTAssertEqual(last.spellCheckingType, .no, "no Turkish keyboard: no squiggles under every Turkish word")

        let unknown = LineEditorConfiguration.captionLine(language: nil, isLast: false, installedLanguages: [])
        XCTAssertEqual(unknown.spellCheckingType, .default)
    }

    // Value: protects=Return = next line and newline-free captions; fails_when=the Return intercept or paste flattening regresses and a newline lands in a transcript line; why_new=config tests only check flags; seam=none
    @MainActor
    func testCaptionReturnMovesOnAndPastedNewlinesBecomeSpaces() {
        var returns = 0
        let caption = NativeExplicitLineTextEditor(
            text: .constant("ab"), focused: .constant(false), identifier: "caption",
            configuration: .captionLine(language: nil, isLast: false, installedLanguages: []),
            actions: LineEditorActions(onReturn: { returns += 1 })
        ).makeCoordinator()
        let view = ExplicitLineTextView()
        view.text = "ab"
        XCTAssertFalse(caption.textView(view, shouldChangeTextIn: NSRange(location: 2, length: 0), replacementText: "\n"))
        XCTAssertEqual(returns, 1, "Return moves to the next line")
        XCTAssertEqual(view.text, "ab")
        XCTAssertFalse(caption.textView(view, shouldChangeTextIn: NSRange(location: 2, length: 0), replacementText: "x\ny"))
        XCTAssertEqual(view.text, "abx y", "a pasted newline becomes a space")
        view.allowsNextNewline = true
        XCTAssertTrue(caption.textView(view, shouldChangeTextIn: NSRange(location: 0, length: 0), replacementText: "\n"),
                      "Shift-Return still inserts one newline")
        XCTAssertEqual(returns, 1)

        let text = NativeExplicitLineTextEditor(text: .constant(""), focused: .constant(false), identifier: "text").makeCoordinator()
        XCTAssertTrue(text.textView(view, shouldChangeTextIn: NSRange(location: 0, length: 0), replacementText: "\n"),
                      "the Text tool keeps Return as a newline")
    }

    // Value: protects=caption lines stay within the server's 600-character cue limit; fails_when=a long paste reaches Save and the whole commit is rejected; why_new=the limit is new; seam=none
    @MainActor
    func testCaptionLinesStopAtTheServersLengthLimit() {
        let coordinator = NativeExplicitLineTextEditor(
            text: .constant(""), focused: .constant(false), identifier: "caption",
            configuration: .captionLine(language: nil, isLast: true, installedLanguages: [])
        ).makeCoordinator()
        let view = ExplicitLineTextView()
        view.text = String(repeating: "a", count: 599)
        XCTAssertTrue(coordinator.textView(view, shouldChangeTextIn: NSRange(location: 599, length: 0), replacementText: "ç"))
        view.text = String(repeating: "a", count: 600)
        XCTAssertFalse(coordinator.textView(view, shouldChangeTextIn: NSRange(location: 600, length: 0), replacementText: "b"))
        XCTAssertTrue(coordinator.textView(view, shouldChangeTextIn: NSRange(location: 599, length: 1), replacementText: ""),
                      "deleting stays allowed")
    }

    func testKeyboardLanguageMatchingComparesTheLanguageSubtag() {
        XCTAssertTrue(LineEditorConfiguration.language("tr-TR", matches: "tr"))
        XCTAssertTrue(LineEditorConfiguration.language("tr_TR", matches: "TR"))
        XCTAssertTrue(LineEditorConfiguration.language("en", matches: "en-GB"))
        XCTAssertFalse(LineEditorConfiguration.language("en-US", matches: "tr"))
        XCTAssertFalse(LineEditorConfiguration.language("tr-TR", matches: ""))
    }

    // MARK: Variant A layout (plan 026 D2 as changed to Variant A)

    private func metrics(bar: CGFloat?, header: CGFloat? = 102, keyboard: Bool = true,
                         height: CGFloat = 457, chrome: CGFloat = 0) -> NativeEditorLayoutMetrics {
        NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: height), safeAreaTop: 59, safeAreaBottom: keyboard ? 336 : 34,
            topChromeHeight: chrome, previewAspectRatio: 9.0 / 16,
            keyboardVisible: keyboard, isAccessibilitySize: false,
            captionEditBarHeight: bar, measuredHeaderHeight: header
        )
    }

    func testEditingACaptionLineGivesThePreviewEverythingAboveTheBar() {
        let bar = CaptionEditBar.height(lineHeight: 22, lines: 3)
        XCTAssertEqual(bar, 154, accuracy: 0.001, "12 + 44 + 6 + (3 × 22 + 16) + 10")
        let editing = metrics(bar: bar, chrome: 24)
        // 457 viewport − 102 header − 24 chrome − 10 preview padding − 6 island pad − 154 bar.
        XCTAssertEqual(editing.defaultPreviewHeight, 161, accuracy: 0.001)
        XCTAssertEqual(editing.previewHeight(resize: 0), 161, accuracy: 0.001, "no grow or shrink while editing a line")
        let area: CGFloat = 457 - 102 - 24 - 161 - 10
        XCTAssertEqual(editing.panelDefaultHeight(areaHeight: area), bar, accuracy: 0.001, "the panel is exactly the bar")
        XCTAssertEqual(editing.panelRange(areaHeight: area, previewHeight: 161), 0, "no panel expansion while editing a line")
        XCTAssertGreaterThan(editing.defaultPreviewHeight, metrics(bar: nil, chrome: 24).defaultPreviewHeight,
                             "Variant A still shows a larger preview than today's keyboard-up split")
    }

    // Value: protects=the edit bar's field line count on short, narrow and AX layouts; fails_when=the SE (667pt tall, 303pt panel) gets 3 lines again; why_new=the reserve and draw sites used different rules; seam=none
    func testShortNarrowAndAccessibilityLayoutsGetATwoLineField() {
        // iPhone SE: short screen, 303pt panel. Reserving 2 lines but drawing 3 put
        // the field's last line under the keyboard.
        XCTAssertEqual(CaptionEditBar.fieldLineCount(isAccessibilitySize: false, screenHeight: 667, contentWidth: 303), 2)
        XCTAssertEqual(CaptionEditBar.fieldLineCount(isAccessibilitySize: false, screenHeight: 852, contentWidth: 321), 3, "iPhone 15")
        XCTAssertEqual(CaptionEditBar.fieldLineCount(isAccessibilitySize: false, screenHeight: 932, contentWidth: 358), 3, "Pro Max")
        XCTAssertEqual(CaptionEditBar.fieldLineCount(isAccessibilitySize: true, screenHeight: 932, contentWidth: 358), 2, "accessibility text size")
        XCTAssertEqual(CaptionEditBar.fieldLineCount(isAccessibilitySize: false, screenHeight: 874, contentWidth: 280), 2, "narrow panel")
    }

    // Value: protects=the Variant A preview height while a line is open; fails_when=a stored timeline-handle resize shrinks the preview under the bar; why_new=the layout test only tried resize 0; seam=none
    func testAStoredPreviewResizeDoesNotShrinkThePreviewWhileEditingALine() {
        let editing = metrics(bar: CaptionEditBar.height(lineHeight: 22, lines: 3), chrome: 24)
        XCTAssertEqual(editing.previewHeight(resize: 40), editing.defaultPreviewHeight, accuracy: 0.001)
        XCTAssertEqual(editing.previewHeight(resize: -40), editing.defaultPreviewHeight, accuracy: 0.001)
        let browse = metrics(bar: nil, chrome: 24)
        XCTAssertLessThan(browse.previewHeight(resize: 40), browse.defaultPreviewHeight, "browse keeps the handle's resize")
    }

    func testEditingACaptionLineWithAHardwareKeyboardHidesTheTransportReservation() {
        let bar = CaptionEditBar.height(lineHeight: 22, lines: 2)
        let editing = metrics(bar: bar, keyboard: false, height: 759)
        XCTAssertEqual(editing.panelBudget(areaHeight: 300), 294, accuracy: 0.001, "no transport row while a line is open")
    }

    func testBrowseLayoutIsUnchangedWhenNoLineIsOpen() {
        // R4 preserve-assertion (4): no bar ⇒ the legacy header constant and split.
        XCTAssertEqual(NativeEditorLayoutMetrics.headerHeight, 94)
        let browse = metrics(bar: nil, header: nil, keyboard: false, height: 759)
        let legacy = NativeEditorLayoutMetrics(
            viewportSize: CGSize(width: 393, height: 759), safeAreaTop: 59, safeAreaBottom: 34,
            topChromeHeight: 0, previewAspectRatio: 9.0 / 16, keyboardVisible: false, isAccessibilitySize: false
        )
        XCTAssertEqual(browse.defaultPreviewHeight, legacy.defaultPreviewHeight, accuracy: 0.001)
        XCTAssertEqual(browse.panelDefaultHeight(areaHeight: 400), legacy.panelDefaultHeight(areaHeight: 400), accuracy: 0.001)
    }

    // MARK: Session line lifecycle

    @MainActor
    private func loadedSession(words: Bool = true, language: String? = "tr", mirrors: Bool = false) async -> NativeEditorSession {
        let threadID = UUID()
        var first: [String: JSONValue] = [
            "id": .string("cue-1"), "text": .string("Simit ve cay"), "start_s": .number(0), "end_s": .number(1),
        ]
        if words { first["words"] = .array(entryWords) }
        var variant: [String: JSONValue] = [
            "render_destination": .string("device"),
            "resolved_archetype": .string("subtitled"),
            "caption_cues": .array([
                .object(first),
                .object(["id": .string("cue-2"), "text": .string("Fiyatlar uygun"), "start_s": .number(1), "end_s": .number(2)]),
            ]),
            "editor_capabilities": .object([
                "caption_cues": .object(["editable": .bool(true)]),
                "caption_meta": .object(["editable": .bool(true)]),
            ]),
        ]
        if let language { variant["caption_language"] = .string(language) }
        if mirrors {
            // The API mirrors every cue into a caption-tagged text element with its own id.
            variant["text_elements"] = .array([("Simit ve cay", 0.0, 1.0), ("Fiyatlar uygun", 1.0, 2.0)].enumerated().map { index, cue in
                .object(["id": .string("mirror-\(index)"), "text": .string(cue.0), "start_s": .number(cue.1), "end_s": .number(cue.2),
                         "source_params": .object(["source": .string("caption_cue"), "key": .string(String(index))])])
            })
        }
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1, snapshotHash: "h",
                                         etag: "e", baseJobID: threadID.uuidString, baseGenerationID: "g1", snapshot: [:],
                                         canUndo: false, createdAt: .now),
            authoritativeVariant: variant
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [],
                                                              captions: CaptionStyle(enabled: false, style: "sentence"),
                                                              music: nil, revision: 0))
        await session.load(api: fake, threadID: threadID)
        return session
    }

    @MainActor
    func testCaptionLanguageComesFromTheLoadedVariant() async {
        let turkish = await loadedSession(language: "tr")
        XCTAssertEqual(turkish.captionLanguage, "tr")
        let none = await loadedSession(language: nil)
        XCTAssertNil(none.captionLanguage)
    }

    @MainActor
    func testTypingRewritesWordsAndRevertingLeavesNoUndoStepOrDot() async throws {
        let session = await loadedSession()
        let undoBefore = session.undoHistoryCount
        session.beginCaptionLineEdit(id: "cue-1")
        session.updateCaptionCue(id: "cue-1", text: "Simit ve çay")
        let cue = try XCTUnwrap(session.document.captionCues.first { $0.id == "cue-1" })
        XCTAssertEqual(cue.raw["words"]?.arrayValue?[2].objectValue?["text"], .string("çay"),
                       "stored words keep spelling the edited text")
        XCTAssertTrue(session.isCaptionUnitEdited(id: "cue-1"))
        // A space typed and deleted, then the original text back: exact restore.
        session.updateCaptionCue(id: "cue-1", text: "Simit ve cay ")
        session.updateCaptionCue(id: "cue-1", text: "Simit ve cay")
        session.endCaptionLineEdit()
        XCTAssertEqual(session.document.captionCues.first { $0.id == "cue-1" }?.raw["words"]?.arrayValue, entryWords)
        XCTAssertEqual(session.undoHistoryCount, undoBefore, "an untouched line adds no undo step")
        XCTAssertFalse(session.isCaptionUnitEdited(id: "cue-1"))
    }

    // Value: protects=one undo step per edited line; fails_when=each keystroke becomes its own undo step or the transaction leaks; why_new=the revert test only covers the no-op case; seam=none
    @MainActor
    func testManyKeystrokesInOneLineAreOneUndoStep() async {
        let session = await loadedSession()
        let before = session.undoHistoryCount
        session.beginCaptionLineEdit(id: "cue-1")
        for text in ["Simit ve ca", "Simit ve çay", "Simit ve çay çok"] { session.updateCaptionCue(id: "cue-1", text: text) }
        session.endCaptionLineEdit()
        XCTAssertEqual(session.undoHistoryCount, before + 1)
        session.beginCaptionLineEdit(id: "cue-2")
        session.updateCaptionCue(id: "cue-2", text: "Fiyatlar çok uygun")
        session.endCaptionLineEdit()
        XCTAssertEqual(session.undoHistoryCount, before + 2, "the next line is its own step")
        session.undo()
        session.undo()
        let cue = session.document.captionCues.first { $0.id == "cue-1" }
        XCTAssertEqual(cue?.text, "Simit ve cay")
        XCTAssertEqual(cue?.raw["words"]?.arrayValue, entryWords, "undo restores the original word timings")
    }

    @MainActor
    func testRemovingAnEmptiedLineIsOneUndoStepThatRestoresTheOriginal() async {
        let session = await loadedSession()
        session.beginCaptionLineEdit(id: "cue-1")
        session.updateCaptionCue(id: "cue-1", text: "")
        session.deleteSelection(EditorSelection(kind: .captionCue, id: "cue-1"))
        session.endCaptionLineEdit()
        XCTAssertNil(session.document.captionCues.first { $0.id == "cue-1" })
        session.undo()
        let restored = session.document.captionCues.first { $0.id == "cue-1" }
        XCTAssertEqual(restored?.text, "Simit ve cay", "one Undo brings back the line, not a blank ghost row")
        XCTAssertEqual(restored?.raw["words"]?.arrayValue, entryWords)
    }

    // Value: protects=deleting every cue leaves no captions; fails_when=the API's cue mirrors turn back into captions once caption_cues is empty; why_new=no test deleted the last cue of a mirrored document; seam=none
    @MainActor
    func testDeletingTheLastCueDoesNotBringBackItsMirrors() async {
        let session = await loadedSession(mirrors: true)
        XCTAssertEqual(session.document.captionUnits.map(\.id), ["cue-1", "cue-2"])
        XCTAssertEqual(session.document.textElements.filter(\.isCaption).count, 2, "fixture carries the mirrors")
        _ = session.deleteSelection(EditorSelection(kind: .captionCue, id: "cue-1"))
        _ = session.deleteSelection(EditorSelection(kind: .captionCue, id: "cue-2"))
        XCTAssertTrue(session.document.captionUnits.isEmpty, "the mirrors must not become captions of their own")
        XCTAssertTrue(session.document.textElements.filter(\.isCaption).allSatisfy { session.document.isCaptionCueMirror($0) },
                      "the timeline and the render compiler keep skipping them")
    }

    // Value: protects=the Undo notice never reverts a newer edit; fails_when=the staleness check uses the undo count, which stops moving at the 100-step limit; why_new=found by the coverage audit; seam=none
    @MainActor
    func testUndoHistoryVersionMovesEvenWhenTheHistoryIsFull() async {
        let session = await loadedSession()
        for index in 0..<105 { session.updateCaptionCue(id: "cue-2", text: "Fiyatlar \(index)") }
        XCTAssertEqual(session.undoHistoryCount, 100, "history is at its limit")
        let version = session.undoHistoryVersion
        session.updateCaptionCue(id: "cue-2", text: "Fiyatlar son")
        XCTAssertEqual(session.undoHistoryCount, 100, "the count no longer moves")
        XCTAssertNotEqual(session.undoHistoryVersion, version, "the version still sees the newer step")
    }

    @MainActor
    func testThePreviewParksAfterThePopInSoTheCaptionIsVisible() async throws {
        let session = await loadedSession()
        let park = try XCTUnwrap(session.captionParkTime(id: "cue-1"))
        XCTAssertEqual(park, 0.15, accuracy: 0.001, "0.15s in: past the 0.12s caption pop fade")
        let range = try XCTUnwrap(session.captionTimelineRange(id: "cue-2"))
        XCTAssertEqual(range.lowerBound, 1, accuracy: 0.001)
        XCTAssertEqual(range.upperBound, 2, accuracy: 0.001)
        // Value: protects=the playing-line lookup's one-pass ranges; fails_when=the batch ranges drift from the per-line range; why_new=captionTimelineRanges is new; seam=none
        let all = session.captionTimelineRanges()
        XCTAssertEqual(all.map(\.id), ["cue-1", "cue-2"])
        XCTAssertEqual(all.last?.range, range)
    }
}
