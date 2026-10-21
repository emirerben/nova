#if DEBUG
import Foundation
import Photos
import PhotosUI
import SwiftUI

/// Pre-implementation measurement, deliberately separate from upload/analysis.
/// It only resolves metadata for IDs explicitly returned by the creator's picker.
struct CameraRollBenchmarkView: View {
    @State private var selection: [PhotosPickerItem] = []
    @State private var showsPicker = false
    @State private var report: String?
    @State private var running = false
    @State private var measurement: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Choose one outing to measure metadata indexing. Your photos and videos stay on this iPhone.")
                    Text("No media uploads or AI calls").font(.subheadline).foregroundStyle(.secondary)
                    Button("Choose photos and videos…") {
                        Task {
                            if PHPhotoLibrary.authorizationStatus(for: .readWrite) == .notDetermined {
                                _ = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
                            }
                            showsPicker = true
                        }
                    }
                    .accessibilityIdentifier("camera-roll-benchmark-choose")
                    .disabled(running)
                    if running {
                        ProgressView("Reading selected metadata…")
                        Button("Cancel") { measurement?.cancel() }
                    }
                }
                Section("Measurement") {
                    Text(report ?? "No collection selected").textSelection(.enabled)
                    Text("Resolving metadata does not inspect media or prove an iCloud original is available.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Collection benchmark")
            // 500 is the existing catalogue ceiling, not a newly validated device limit.
            .photosPicker(isPresented: $showsPicker, selection: $selection, maxSelectionCount: 500,
                          matching: .any(of: [.images, .videos]), photoLibrary: .shared())
            .onChange(of: selection) { _, _ in
                measurement?.cancel()
                measurement = Task { await measure() }
            }
            .onDisappear { measurement?.cancel() }
        }
    }

    @MainActor private func measure() async {
        running = true
        defer { running = false }
        let started = ContinuousClock.now
        let ids = Array(Set(selection.compactMap(\.itemIdentifier))).sorted()
        let permission = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        var resolved = 0
        var photos = 0
        var videos = 0
        var missingDates = 0
        var passes: [Double] = []
        let shareContext = ClipCaptureSetting.isEnabled()
        // Probe a small batch first. This is a benchmark parameter, not a shipped limit.
        for offset in stride(from: 0, to: ids.count, by: 32) {
            guard !Task.isCancelled else { report = "Cancelled. No media was uploaded."; return }
            let batchStart = ContinuousClock.now
            let batch = Array(ids[offset..<min(offset + 32, ids.count)])
            if permission == .authorized || permission == .limited {
                let assets = PHAsset.fetchAssets(withLocalIdentifiers: batch, options: nil)
                assets.enumerateObjects { asset, _, _ in
                    resolved += 1
                    if asset.mediaType == .image { photos += 1 }
                    if asset.mediaType == .video { videos += 1 }
                    _ = asset.pixelWidth
                    _ = asset.pixelHeight
                    _ = asset.duration
                    if shareContext, asset.creationDate == nil { missingDates += 1 }
                    if shareContext, let location = asset.location {
                        _ = CoarseCoordinate.round(location.coordinate.latitude)
                        _ = CoarseCoordinate.round(location.coordinate.longitude)
                    }
                }
            }
            passes.append(Self.milliseconds(batchStart.duration(to: .now)))
            await Task.yield()
        }
        var usage = rusage()
        getrusage(RUSAGE_SELF, &usage)
        let result: [String: Any] = [
            "schema_version": 1, "captured_at": ISO8601DateFormatter().string(from: Date()),
            "app_version": Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "unknown",
            "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
            "permission": permission.rawValue, "capture_context_enabled": shareContext,
            "selected_count": selection.count, "resolved_metadata_count": resolved,
            "unresolved_count": selection.count - resolved, "photo_count": photos,
            "video_count": videos, "missing_dates_count": shareContext ? missingDates : NSNull(),
            "elapsed_ms": Self.milliseconds(started.duration(to: .now)),
            "batch_size": 32, "batch_elapsed_ms": passes,
            "process_peak_resident_bytes": usage.ru_maxrss,
            "media_bytes_read": 0, "media_bytes_uploaded": 0, "provider_calls": 0,
            "original_availability": "not_requested",
        ]
        do {
            let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
            let folder = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            try data.write(to: folder.appendingPathComponent("camera-roll-benchmark.json"), options: .atomic)
            report = String(decoding: data, as: UTF8.self)
        } catch { report = "Could not save measurement: \(error.localizedDescription)" }
    }

    private static func milliseconds(_ duration: Duration) -> Double {
        Double(duration.components.seconds) * 1000 + Double(duration.components.attoseconds) / 1e15
    }
}
#endif
