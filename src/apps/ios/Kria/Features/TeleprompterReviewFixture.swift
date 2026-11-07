#if DEBUG
@preconcurrency import AVFoundation
import Foundation
import SwiftUI
import UIKit

/// Real playable files test review, recovery and native proxy intake. They are
/// imported fixture footage, never evidence of simulator camera capture.
@MainActor enum TeleprompterReviewFixture {
    static func seedIfRequested(store: TeleprompterStore) async throws {
        let process = ProcessInfo.processInfo
        guard process.arguments.contains("-ui-testing-chat"),
              process.environment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] == "1",
              try store.loadManifests().isEmpty,
              let videoURL = Bundle.main.url(forResource: "montage", withExtension: "mp4"),
              let audioURL = VoiceoverReviewFixture.make() else { return }
        let draft = TeleprompterScriptDraft(text: "Here is my saved script. Take your time and enjoy the story.")
        try store.save(draft: draft)
        var reference: SpeakerBackgroundReference?
        if TeleprompterBackgroundFixture.isRequested, let photo = TeleprompterBackgroundFixture.makePhoto() {
            reference = try store.importBackground(from: photo, kind: .image)
            try? FileManager.default.removeItem(at: photo)
        }
        for slot in [TeleprompterTakeSlot.pending, .candidate] {
            var take = TeleprompterTakeManifest(status: .review, script: draft, slot: slot)
            take.speakerBackground = reference
            try store.save(manifest: take)
            let video = AVURLAsset(url: videoURL), audio = AVURLAsset(url: audioURL)
            guard let sourceVideo = try await video.loadTracks(withMediaType: .video).first,
                  let sourceAudio = try await audio.loadTracks(withMediaType: .audio).first else { return }
            let composition = AVMutableComposition()
            let range = CMTimeRange(start: .zero, duration: CMTime(seconds: 2, preferredTimescale: 600))
            let picture = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
            try picture.insertTimeRange(range, of: sourceVideo, at: .zero)
            picture.preferredTransform = try await sourceVideo.load(.preferredTransform)
            let sound = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
            try sound.insertTimeRange(range, of: sourceAudio, at: .zero)
            guard let exporter = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetMediumQuality) else { return }
            try await exporter.export(to: store.fileURL(for: take), as: .mov)
        }
    }
}

/// A tiny generated photo for UI tests, which cannot drive the system photo picker.
@MainActor enum TeleprompterBackgroundFixture {
    static var isRequested: Bool {
        let process = ProcessInfo.processInfo
        return process.arguments.contains("-ui-testing-chat") && process.environment["KRIA_TELEPROMPTER_BACKGROUND_FIXTURE"] == "1"
    }

    static func makePhoto() -> URL? {
        let size = CGSize(width: 90, height: 160)
        let image = UIGraphicsImageRenderer(size: size).image { context in
            UIColor.systemTeal.setFill(); context.fill(CGRect(origin: .zero, size: size))
        }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString)-fixture-background.png")
        guard let data = image.pngData(), (try? data.write(to: url)) != nil else { return nil }
        return url
    }
}

/// A simulator has no camera picture. `KRIA_TELEPROMPTER_SCENE_FIXTURE=light|dark` stands a flat scene behind the
/// controls, so their state can be read over a bright and a dark picture.
enum TeleprompterSceneFixture {
    static var gradient: LinearGradient? {
        let process = ProcessInfo.processInfo
        guard process.arguments.contains("-ui-testing-chat") else { return nil }
        switch process.environment["KRIA_TELEPROMPTER_SCENE_FIXTURE"] {
        case "light": return LinearGradient(colors: [Color(white: 0.98), Color(red: 0.86, green: 0.92, blue: 1)], startPoint: .top, endPoint: .bottom)
        case "dark": return LinearGradient(colors: [Color(white: 0.04), Color(red: 0.05, green: 0.07, blue: 0.12)], startPoint: .top, endPoint: .bottom)
        default: return nil
        }
    }
}
#endif
