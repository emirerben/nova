import XCTest
@testable import Kria

/// `SpeechCleanupOffer.resolve` is the pure decision table behind the v2
/// approval card's (`DirectionStage`) speech-cleanup buttons -- see
/// `ChatWorkspaceTests` for the sibling `WorkspaceStage.resolve` pattern this
/// mirrors. It also backs `ChatWorkspaceView.speechCleanupIsChecking`, which
/// keeps the poll loop's fast cadence while `.checking` -- that glue isn't
/// separately testable without a live view harness, but the predicate it
/// reads is fully covered here.
final class SpeechCleanupOfferTests: XCTestCase {
    private func cleanup(_ json: String) -> [String: JSONValue] {
        let decoded = try! JSONDecoder().decode([String: JSONValue].self, from: Data(json.utf8))
        return decoded
    }

    func testNotApplicableIsPlainRegardlessOfAnalysis() {
        let (offer, analysisID) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": false, "analysis": {"id": "a1", "status": "ready"}}"#))
        XCTAssertEqual(offer, .plain)
        XCTAssertEqual(analysisID, "a1", "analysisID still rides along even when the offer itself is plain")
    }

    func testNilSpeechCleanupIsPlain() {
        let (offer, analysisID) = SpeechCleanupOffer.resolve(nil)
        XCTAssertEqual(offer, .plain)
        XCTAssertNil(analysisID)
    }

    func testNoAnalysisYetIsChecking() {
        let (offer, analysisID) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true}"#))
        XCTAssertEqual(offer, .checking)
        XCTAssertNil(analysisID)
    }

    func testQueuedOrRunningIsChecking() {
        for status in ["queued", "running"] {
            let (offer, analysisID) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true, "analysis": {"id": "a1", "status": "\#(status)"}}"#))
            XCTAssertEqual(offer, .checking, status)
            XCTAssertEqual(analysisID, "a1", status)
        }
    }

    func testAnalysisFailedIsFailed() {
        let (offer, analysisID) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true, "analysis": {"id": "a1", "status": "failed"}}"#))
        XCTAssertEqual(offer, .failed)
        XCTAssertEqual(analysisID, "a1")
    }

    func testReadyWithoutRequiresChoiceIsPlain() {
        for status in ["ready", "no_findings"] {
            let (offer, _) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true, "requires_choice": false, "analysis": {"id": "a1", "status": "\#(status)"}}"#))
            XCTAssertEqual(offer, .plain, status)
        }
    }

    func testReadyWithRequiresChoiceReturnsStats() {
        let (offer, analysisID) = SpeechCleanupOffer.resolve(cleanup(#"""
        {"applicable": true, "requires_choice": true,
         "analysis": {"id": "a1", "status": "ready", "candidate_count": 3, "estimated_removed_ms": 4200,
                      "category_counts": {"filler_sounds": 2, "long_pauses": 1}}}
        """#))
        XCTAssertEqual(analysisID, "a1")
        guard case let .choice(stats) = offer else { return XCTFail("Expected .choice, got \(offer)") }
        XCTAssertEqual(stats.candidateCount, 3)
        XCTAssertEqual(stats.estimatedRemovedMs, 4200)
        XCTAssertEqual(stats.fillerSounds, 2)
        XCTAssertEqual(stats.longPauses, 1)
    }

    func testNoFindingsWithRequiresChoiceStillOffersAChoice() {
        // The server's `public_projection` only ever sets `requires_choice`
        // alongside `status == "ready"` today, but this client follows the
        // flag itself rather than re-deriving it from status -- a defensive
        // choice that keeps working even if that pairing changes server-side.
        let (offer, _) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true, "requires_choice": true, "analysis": {"id": "a1", "status": "no_findings"}}"#))
        guard case .choice = offer else { return XCTFail("Expected .choice, got \(offer)") }
    }

    func testUnrecognizedStatusFallsBackToPlainRatherThanStickSpinning() {
        let (offer, _) = SpeechCleanupOffer.resolve(cleanup(#"{"applicable": true, "analysis": {"id": "a1", "status": "some_future_status"}}"#))
        XCTAssertEqual(offer, .plain)
    }

    // MARK: - SpeechCleanupStats.summarySentence

    func testSummarySentenceDropsEveryFieldTheServerDidNotSend() {
        XCTAssertNil(SpeechCleanupStats().summarySentence)
        XCTAssertEqual(SpeechCleanupStats(candidateCount: 3).summarySentence, "Found 3 pauses and retakes.")
        XCTAssertEqual(SpeechCleanupStats(candidateCount: 1).summarySentence, "Found 1 pause or retake.")
        XCTAssertEqual(SpeechCleanupStats(estimatedRemovedMs: 4200).summarySentence, "About 4.2s shorter.")
        XCTAssertEqual(SpeechCleanupStats(estimatedRemovedMs: 4000).summarySentence, "About 4s shorter.")
    }

    func testSummarySentenceCombinesCandidateCountAndDuration() {
        let stats = SpeechCleanupStats(candidateCount: 3, estimatedRemovedMs: 4200)
        XCTAssertEqual(stats.summarySentence, "Found 3 pauses and retakes — about 4.2s shorter.")
    }

    func testSummarySentenceIncludesCategoryCountsOnlyWhenBothPresentAndNonzero() {
        let withCategories = SpeechCleanupStats(candidateCount: 3, fillerSounds: 2, longPauses: 1)
        XCTAssertEqual(withCategories.summarySentence, "Found 3 pauses and retakes (2 filler sounds, 1 long pause).")

        let missingOneCategory = SpeechCleanupStats(candidateCount: 3, fillerSounds: 2, longPauses: nil)
        XCTAssertEqual(missingOneCategory.summarySentence, "Found 3 pauses and retakes.", "Never fabricate the missing half of the breakdown")

        let zeroCandidates = SpeechCleanupStats(candidateCount: 0, estimatedRemovedMs: 1000)
        XCTAssertEqual(zeroCandidates.summarySentence, "About 1s shorter.", "A zero count is not worth stating as a finding")
    }
}
