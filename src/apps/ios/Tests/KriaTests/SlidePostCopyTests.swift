import XCTest
@testable import Kria

/// A slide post has one media pool: its photos and videos. The words "overlays" and "visuals" belong
/// to the video editor and must never reach a person adding media to a slideshow.
final class SlidePostCopyTests: XCTestCase {
    func testSlideMediaCopyNeverSaysOverlaysOrVisuals() {
        let all = [SlideMediaCopy.poolTitle, SlideMediaCopy.addHeading, SlideMediaCopy.subtitle,
                   SlideMediaCopy.loading, SlideMediaCopy.retryLoading, SlideMediaCopy.removeFallback, SlideMediaCopy.preparingNoun]
        for text in all {
            XCTAssertFalse(text.lowercased().contains("overlay"), text)
            XCTAssertFalse(text.lowercased().contains("visual"), text)
        }
        XCTAssertEqual(SlideMediaCopy.poolTitle, "Photos & videos")
        XCTAssertEqual(SlideMediaCopy.addHeading, "Add photos & videos")
    }

    func testPreparationSummaryUsesThePlainNounForSlides() throws {
        let assets = [CreationVisual(id: "a", kind: "image", status: "processing", sourceFilename: "a.jpg", displayURL: nil, previewURL: nil, retryable: nil),
                      CreationVisual(id: "b", kind: "video", status: "processing", sourceFilename: "b.mov", displayURL: nil, previewURL: nil, retryable: nil)]
        let video = try XCTUnwrap(VisualPreparationSummary(assets: assets, slowIDs: [], surface: .addMediaSheet))
        XCTAssertTrue(video.title.contains("visuals"), "the video editor keeps its wording")
        let slides = try XCTUnwrap(VisualPreparationSummary(assets: assets, slowIDs: [], surface: .addMediaSheet, nounOverride: SlideMediaCopy.preparingNoun))
        XCTAssertEqual(slides.title, "Getting your photos & videos ready")
    }

    func testVideoWordingIsUnchanged() {
        XCTAssertEqual(AttachmentStep.overlays.title, "Overlays")
        XCTAssertEqual(CreationMediaRole.visual.title, "Visuals")
    }
}
