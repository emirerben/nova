#if DEBUG
import AVFoundation
import Foundation
import KriaMediaEngine
import UIKit

/// Item 24 (`upload_place_hint`) UI fixture, selected by `KRIA_CHAT_PLACE_HINT_FIXTURE` with `-ui-testing-chat`:
/// `recording` (take + landscape screen recording), `portrait-recording` (take + `RPReplay…` portrait recording),
/// `single` (the take only), `takes` (two portrait takes). `KRIA_UPLOAD_PLACE_HINT=1` turns the capability on.
enum UploadPlaceHintFixture {
    static let recordingMediaID = "fixture-screen-recording"
    static var mode: String? {
        guard let value = ProcessInfo.processInfo.environment["KRIA_CHAT_PLACE_HINT_FIXTURE"], !value.isEmpty else { return nil }
        return value
    }
    static var capabilityOn: Bool { ProcessInfo.processInfo.environment["KRIA_UPLOAD_PLACE_HINT"] == "1" }

    static func media(mode: String) -> [[String: Any]] {
        let take: [String: Any] = ["media_id": "fixture-clip", "kind": "video", "filename": "Talking take.mov", "duration_s": 20]
        switch mode {
        case "recording":
            return [take, ["media_id": recordingMediaID, "kind": "video", "filename": "Screen Recording 2026-11-04 at 10.12.33.mov", "duration_s": 12]]
        case "portrait-recording":
            return [take, ["media_id": recordingMediaID, "kind": "video", "filename": "RPReplay_Final1730000000.mp4"]]
        case "takes":
            return [take, ["media_id": "fixture-clip-2", "kind": "video", "filename": "Talking take 2.mov"]]
        default:
            return [take]
        }
    }

    /// Same path as `CreationMediaPreview.url(mediaID:)` (that type is main-actor isolated; the transport is not).
    private static func previewURL(mediaID: String) -> URL {
        let safeID = Data(mediaID.utf8).base64EncodedString().replacingOccurrences(of: "/", with: "_")
        return FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appending(path: "KriaMediaPreviews", directoryHint: .isDirectory).appending(path: "\(safeID).jpg")
    }

    private static func writePreview(mediaID: String, width: Int, height: Int, color: UIColor) {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let size = CGSize(width: width, height: height)
        let image = UIGraphicsImageRenderer(size: size, format: format).image { context in
            color.setFill()
            context.fill(CGRect(origin: .zero, size: size))
        }
        guard let data = image.jpegData(compressionQuality: 0.9) else { return }
        let url = previewURL(mediaID: mediaID)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }

    /// Writes the cached preview JPEGs and binds the recording's local original, so the app can re-upload it.
    static func prepare(mode: String, threadID: String) {
        writePreview(mediaID: "fixture-clip", width: 180, height: 320, color: .systemBlue)
        if mode == "takes" { writePreview(mediaID: "fixture-clip-2", width: 180, height: 320, color: .systemTeal) }
        guard mode == "recording" || mode == "portrait-recording" else { return }
        let landscape = mode == "recording"
        writePreview(mediaID: recordingMediaID, width: landscape ? 320 : 180, height: landscape ? 180 : 320, color: .systemOrange)
        guard let projectID = UUID(uuidString: threadID),
              let source = SongTimelineFixture.clipURL(mediaID: "fixture-clip", force: true),
              let filename = media(mode: mode).last?["filename"] as? String else { return }
        do {
            let project = BackgroundUploadCoordinator.projectDirectory(projectID)
            try project.createIfNeeded()
            // Named like the recording, so a re-upload of this file carries the recording's own name.
            let target = project.originals.appendingPathComponent(filename)
            if !FileManager.default.fileExists(atPath: target.path) { try FileManager.default.copyItem(at: source, to: target) }
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: target)
            let asset = MediaAsset(id: recordingMediaID, relativePath: "originals/\(filename)", fingerprint: fingerprint, duration: 8,
                                   naturalSize: MediaSize(width: landscape ? 320 : 180, height: landscape ? 180 : 320), frameRate: 10)
            try SourceAssetStore(project: project).bind(mediaID: recordingMediaID, original: asset)
        } catch {}
    }
}
#endif
