import Foundation

/// Pure rules for putting freshly imported media into a slide post without a second tap (KRI-298 UX).
/// Everything here is deterministic and free of UI so the ordering, dedupe, limit and race rules can
/// be tested directly.
enum SlidePostAutoAppend {
    struct Plan: Equatable {
        /// Ready assets to append, in the order the pool lists them (the order the user chose them).
        var toAppend: [SlidePostAsset] = []
        /// Ready assets that did not fit the platform's slide limit.
        var skippedForLimit: [String] = []
        /// Videos that cannot join a photos-only post (TikTok photo mode).
        var skippedForVideo: [String] = []
        /// The seen-set to store afterwards. Everything the plan decided about is in it, so a removed
        /// slide's asset is never silently re-added and a skipped one is not retried forever.
        var seen: Set<String> = []

        var notice: String? {
            var parts: [String] = []
            if !skippedForLimit.isEmpty { parts.append("\(skippedForLimit.count) over the slide limit") }
            if !skippedForVideo.isEmpty { parts.append("\(skippedForVideo.count) video\(skippedForVideo.count == 1 ? "" : "s") can't go in a TikTok photo post") }
            guard !parts.isEmpty else { return nil }
            return "Not added: " + parts.joined(separator: ", ") + "."
        }
    }

    static func maxSlides(profile: String) -> Int { profile == "instagram_carousel" ? 20 : 35 }

    /// `seen == nil` means the session has never looked at the pool for this draft: everything already
    /// ready is treated as decided (an old draft's removed slides must not reappear), nothing is appended.
    static func plan(draft: SlidePostDraft, ready: [SlidePostAsset], seen: Set<String>?) -> Plan {
        guard let seen else { return Plan(seen: Set(ready.map(\.id))) }
        var plan = Plan(seen: seen)
        var used = Set(draft.slides.map(\.assetID))
        var count = draft.slides.count
        let limit = maxSlides(profile: draft.platformProfile)
        for asset in ready where !seen.contains(asset.id) {
            plan.seen.insert(asset.id)
            // Already a slide (or a duplicate row in the same pool snapshot): one asset, one slide.
            guard !used.contains(asset.id) else { continue }
            if draft.platformProfile == "tiktok_photo", asset.kind == "video" { plan.skippedForVideo.append(asset.id); continue }
            guard count < limit else { plan.skippedForLimit.append(asset.id); continue }
            plan.toAppend.append(asset); used.insert(asset.id); count += 1
        }
        return plan
    }
}

/// A block in the slide strip for media that has been chosen but is not a slide yet.
struct SlidePostPendingTile: Identifiable, Equatable {
    enum Phase: Equatable {
        /// Bytes are going up (`progress` 0...1), or the file is being prepared first (nil).
        case uploading(progress: Double?)
        /// Uploaded; the server is still preparing it.
        case processing
        /// Failed. `recordID` set means Retry can resume the upload; nil means it has to be chosen again.
        case failed(message: String, recordID: UUID?)
    }
    let id: String
    let phase: Phase
}

enum SlidePostPendingMedia {
    private static let serverPending: Set<String> = ["pending", "queued", "uploaded", "processing", "analyzing", "uploading"]

    /// Pending and failed media that are not yet slides, oldest first: server-side assets still being
    /// prepared, local uploads in flight, and failures. Assets already in the draft never show.
    static func tiles(
        assets: [SlidePostAsset],
        draftAssetIDs: Set<String>,
        records: [UploadRecoveryRecord],
        progress: [UUID: Double],
        inFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload],
        failures: [UploadFailure],
        projectID: UUID
    ) -> [SlidePostPendingTile] {
        var tiles: [SlidePostPendingTile] = []
        let assetIDs = Set(assets.map(\.id))
        for asset in assets where !draftAssetIDs.contains(asset.id) {
            if asset.status == "failed" || ["missing", "expired"].contains(asset.mediaStatus ?? "") {
                tiles.append(.init(id: "asset-\(asset.id)", phase: .failed(message: "This file couldn't be prepared.", recordID: nil)))
            } else if serverPending.contains(asset.status) {
                tiles.append(.init(id: "asset-\(asset.id)", phase: .processing))
            }
        }
        let failedRecordIDs = Set(failures.map(\.id))
        for record in records where record.projectID == projectID {
            // The server already lists this file; its own tile above covers it.
            if let mediaID = record.mediaID, assetIDs.contains(mediaID) { continue }
            if record.uploadFailed == true || failedRecordIDs.contains(record.id) {
                let message = failures.first { $0.id == record.id }?.message ?? "The upload didn't finish."
                tiles.append(.init(id: "record-\(record.id.uuidString)", phase: .failed(message: message, recordID: record.id)))
            } else {
                tiles.append(.init(id: "record-\(record.id.uuidString)", phase: .uploading(progress: progress[record.id])))
            }
        }
        let recordIDs = Set(records.map(\.id))
        // Being prepared (no record yet): the coordinator only knows the file name.
        for (id, upload) in inFlight.sorted(by: { $0.value.startedAt < $1.value.startedAt }) where upload.projectID == projectID && !recordIDs.contains(id) {
            tiles.append(.init(id: "flight-\(id.uuidString)", phase: .uploading(progress: nil)))
        }
        // Files that never produced a record (unreadable): nothing to retry, but say so.
        for failure in failures where failure.projectID == projectID && !recordIDs.contains(failure.id) {
            tiles.append(.init(id: "failure-\(failure.id.uuidString)", phase: .failed(message: failure.message, recordID: nil)))
        }
        return tiles
    }
}
