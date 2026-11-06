import XCTest
@testable import Kria

final class SlidePostMergeTests: XCTestCase {
    private func draft() -> SlidePostDraft {
        SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [
            SlidePostSlide(id: "a", assetID: "asset-a", kind: "image"),
            SlidePostSlide(id: "b", assetID: "asset-b", kind: "image")
        ])
    }

    func testIndependentSlideEditsAndRemoteMetadataSurvive() throws {
        let base = draft()
        var local = base, remote = base
        local.slides[0].alt = "Local description"
        remote.slides[1].alt = "Remote description"
        remote.version = 8
        remote.renderedVersion = 7
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.draft.slides[0].alt, "Local description")
        XCTAssertEqual(result.draft.slides[1].alt, "Remote description")
        XCTAssertEqual(result.draft.version, 8)
        XCTAssertEqual(result.draft.renderedVersion, 7)
    }

    func testCoverFollowsIdentityThroughRemoteReorder() throws {
        let base = draft()
        var local = base, remote = base
        local.caption = "Local caption"
        remote.slides.reverse()
        remote.coverIndex = 1
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.draft.slides[result.draft.coverIndex].id, "a")
        XCTAssertEqual(result.draft.caption, "Local caption")
    }

    func testReplacingSourceConflictsWithEditingTheOldSource() throws {
        let base = draft()
        var local = base, remote = base
        local.slides[0].edits = SlidePostEdits(lookPreset: "olive_film")
        remote.slides[0].assetID = "new-source"
        let unresolved = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertFalse(unresolved.conflicts.isEmpty)
        let resolved = try SlidePostMerge.merge(base: base, local: local, remote: remote, preference: .remote)
        XCTAssertEqual(resolved.draft.slides[0].assetID, "new-source")
        XCTAssertNil(resolved.draft.slides[0].edits)
    }

    func testTextMirrorDoesNotCreateAFalseConflict() throws {
        var base = draft()
        base.slides[0].edits = SlidePostEdits(texts: [SlidePostTextElement(id: "text-a", text: "Original")])
        var local = base, remote = base
        local.slides[0].edits?.setTexts([SlidePostTextElement(id: "text-a", text: "Local")])
        remote.slides[0].edits?.lookPreset = "olive_film"
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.draft.slides[0].edits?.text?.content, "Local")
        XCTAssertEqual(result.draft.slides[0].edits?.lookPreset, "olive_film")
    }

    func testExplicitStickerClearSurvivesMergeAndEncoding() throws {
        var base = draft()
        base.slides[0].edits = SlidePostEdits(stickers: [SlidePostSticker(id: "sticker", assetID: "picture")])
        var local = base, remote = base
        local.slides[0].edits?.stickers = []
        remote.caption = "Remote caption"
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.draft.slides[0].edits?.stickers, [])
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(result.draft.slides[0].edits)) as? [String: Any])
        XCTAssertEqual((wire["stickers"] as? [Any])?.count, 0)
    }

    func testLegacyToRichConflictChoiceDoesNotReapplyTheRejectedText() throws {
        var base = draft()
        base.slides[0].edits = SlidePostEdits(text: SlidePostText(content: "Original", position: "center"))
        var local = base, remote = base
        local.slides[0].edits?.setTexts([SlidePostTextElement(id: "legacy", text: "Local")])
        remote.slides[0].edits?.text?.content = "Remote"
        XCTAssertFalse(try SlidePostMerge.merge(base: base, local: local, remote: remote).conflicts.isEmpty)
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote, preference: .remote)
        XCTAssertNil(result.draft.slides[0].edits?.texts)
        XCTAssertEqual(result.draft.slides[0].edits?.text?.content, "Remote")
    }

    func testDuplicateSlideIdentityFailsWithoutTrapping() {
        var invalid = draft()
        invalid.slides[1].id = invalid.slides[0].id
        XCTAssertThrowsError(try SlidePostMerge.merge(base: invalid, local: invalid, remote: invalid))
    }

    func testFormatChangeAndOtherSlideChangesRequireAnExplicitChoice() throws {
        let base = draft()
        var local = base, remote = base
        local.slides[0].alt = "Local edit in old canvas"
        remote.platformProfile = "instagram_carousel"
        let result = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertFalse(result.conflicts.isEmpty)
        XCTAssertEqual(result.conflicts.first?.path, ["$canvas"])
    }

    func testCrossSlideAssetCollisionBecomesAResolvableCanvasConflict() throws {
        let base = draft()
        var local = base, remote = base
        local.slides.append(SlidePostSlide(id: "local", assetID: "same-new-asset", kind: "image"))
        remote.slides.append(SlidePostSlide(id: "remote", assetID: "same-new-asset", kind: "image"))
        remote.caption = "Independent caption"
        let unresolved = try SlidePostMerge.merge(base: base, local: local, remote: remote)
        XCTAssertEqual(unresolved.conflicts.first?.path, ["$canvas"])
        let resolved = try SlidePostMerge.merge(base: base, local: local, remote: remote, preference: .local)
        XCTAssertTrue(resolved.conflicts.isEmpty)
        XCTAssertEqual(resolved.draft.slides.map(\.id), ["a", "b", "local"])
        XCTAssertEqual(resolved.draft.caption, "Independent caption")
        XCTAssertNil(resolved.draft.validationMessage)
    }
}
