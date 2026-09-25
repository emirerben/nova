import XCTest
@testable import Kria

#if DEBUG
/// KRI-185: what the Text tab lists, and in what order.
final class NativeEditorTextBlocksTests: XCTestCase {
    private var document: EditorDocument {
        EditorDocument(snapshot: NativeEditorUITestFixtures.guidedText.serverSnapshot)
    }

    func testListsTheTitleClipLabelsAndCreatorTextInTimeOrderWithTheTitleFirst() {
        let blocks = document.textBlocks
        // The title and the first label both start at 0:00; the title reads first.
        XCTAssertEqual(blocks.map(\.id), ["guided-title", "clip-label-unified-cut-1", "creator-note", "clip-label-unified-cut-2"])
        XCTAssertEqual(blocks.map(\.kind), [.title, .clipLabel(1), .other, .clipLabel(2)])
        XCTAssertEqual(blocks.map(\.kindLabel), ["Title", "Clip 1", "Text", "Clip 2"])
        XCTAssertEqual(blocks.map(\.text), ["20K Run · Arnavutkoy → Eminonu", "Galata Tower", "My note", "Dolmabahçe Palace"])
    }

    func testCaptionsAndRemovedBlocksAreLeftOut() {
        let ids = document.textBlocks.map(\.id)
        XCTAssertFalse(ids.contains("caption-1"), "captions have their own tab")
        XCTAssertFalse(ids.contains("clip-label-unified-cut-3"), "a removed label is not in the video")
    }

    func testDisabledAndZeroLengthBlocksAreLeftOut() {
        var doc = document
        let noteIndex = doc.textElements.firstIndex { $0.id == "creator-note" }!
        doc.textElements[noteIndex].raw["enabled"] = .bool(false)
        let labelIndex = doc.textElements.firstIndex { $0.id == "clip-label-unified-cut-1" }!
        doc.textElements[labelIndex].endS = doc.textElements[labelIndex].startS
        XCTAssertEqual(doc.textBlocks.map(\.id), ["guided-title", "clip-label-unified-cut-2"])
    }

    func testClipLabelsAreNumberedInTimeOrderWhateverOrderTheyAreStored() {
        // The fixture stores cut-2 before cut-1; the numbers follow the video.
        let labels = document.textBlocks.filter { if case .clipLabel = $0.kind { return true } else { return false } }
        XCTAssertEqual(labels.map(\.id), ["clip-label-unified-cut-1", "clip-label-unified-cut-2"])
        XCTAssertEqual(labels.map(\.kind), [.clipLabel(1), .clipLabel(2)])
    }

    func testAClipLabelKeepsItsOwnClipNumberWhenAnotherClipHasNoLabel() {
        // The server skips a clip without a label; the next label is still that clip's.
        var doc = document
        doc.textElements.removeAll { $0.id == "clip-label-unified-cut-1" }
        XCTAssertEqual(doc.textBlocks.first { $0.id == "clip-label-unified-cut-2" }?.kind, .clipLabel(2))
    }

    func testRemovingALabelNeverRenumbersTheOthers() {
        var doc = document
        let index = doc.textElements.firstIndex { $0.id == "clip-label-unified-cut-1" }!
        doc.textElements[index].raw["removed"] = .bool(true)
        XCTAssertEqual(doc.textBlocks.first { $0.id == "clip-label-unified-cut-2" }?.kind, .clipLabel(2))
    }

    func testAnIdWithoutATrailingNumberFallsBackToTimeOrder() {
        var doc = document
        for index in doc.textElements.indices {
            if doc.textElements[index].id == "clip-label-unified-cut-2" { doc.textElements[index].id = "clip-label-b" }
            if doc.textElements[index].id == "clip-label-unified-cut-1" { doc.textElements[index].id = "clip-label-a" }
        }
        let labels = doc.textBlocks.filter { $0.id.hasPrefix("clip-label-") }
        XCTAssertEqual(labels.map(\.id), ["clip-label-a", "clip-label-b"])
        XCTAssertEqual(labels.map(\.kind), [.clipLabel(1), .clipLabel(2)])
    }

    func testADuplicateIdIsListedOnceAndNeverTrapsTheProjection() {
        var doc = document
        doc.textElements.append(doc.textElements.first { $0.id == "clip-label-unified-cut-1" }!)
        doc.textElements.append(doc.textElements.first { $0.id == "guided-title" }!)
        let ids = doc.textBlocks.map(\.id)
        XCTAssertEqual(ids.count, Set(ids).count)
        XCTAssertEqual(ids.count, 4)
    }

    func testAnEditedTextIsReflectedInTheNextProjection() {
        var doc = document
        let index = doc.textElements.firstIndex { $0.id == "clip-label-unified-cut-2" }!
        doc.textElements[index].text = "Dolmabahçe Palace Gate"
        XCTAssertEqual(doc.textBlocks.last?.text, "Dolmabahçe Palace Gate")
    }

    func testAListWithADraftInProgressIsInertSoTypedWordsAreNeverDiscarded() {
        XCTAssertTrue(EditorTextBlock.listIsInteractive(draft: nil, canEdit: true))
        XCTAssertTrue(EditorTextBlock.listIsInteractive(draft: "", canEdit: true))
        XCTAssertTrue(EditorTextBlock.listIsInteractive(draft: "  \n ", canEdit: true), "whitespace is not a draft")
        XCTAssertFalse(EditorTextBlock.listIsInteractive(draft: "Hello", canEdit: true))
        XCTAssertFalse(EditorTextBlock.listIsInteractive(draft: nil, canEdit: false), "text editing is unavailable")
    }

    func testTimecodeMatchesTheTimelineShape() {
        XCTAssertEqual(EditorTextBlock.clock(0), "0:00.0")
        XCTAssertEqual(EditorTextBlock.clock(2.2), "0:02.2")
        XCTAssertEqual(EditorTextBlock.clock(65.34), "1:05.3")
        XCTAssertEqual(EditorTextBlock.clock(-1), "0:00.0")
        let block = document.textBlocks[0]
        XCTAssertEqual(block.timeRange, "0:00.0 – 0:02.2")
    }
}
#endif
