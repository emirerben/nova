#if DEBUG
import AVFoundation
import AVKit
import KriaMediaEngine
import SwiftUI
import UniformTypeIdentifiers

@MainActor
private final class MediaDiagnosticModel: ObservableObject {
    @Published var player: AVPlayer?
    @Published var duration: TimeInterval = 0
    @Published var position: TimeInterval = 0
    @Published var exportProgress: Double = 0
    @Published var exportURL: URL?
    @Published var message = "Choose a short clip to exercise the native preview/export path."

    private let metrics = MetricsCollector()
    private var recipe: KriaMediaEngine.EditRecipe?
    private var assetURLs: [String: URL] = [:]

    var metricEvents: [MetricEvent] { metrics.snapshot() }

    func importClip(_ source: URL) async {
        do {
            player?.pause()
            metrics.reset()
            exportURL = nil
            exportProgress = 0

            let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appending(path: "KriaDiagnostics", directoryHint: .isDirectory)
            let project = ProjectDirectory(root: root)
            let imported = try await AssetImportCoordinator(project: project)
                .importAsset(from: source, id: "fixture-a")
            let localURL = root.appending(path: imported.relativePath)
            let asset = AVURLAsset(url: localURL)
            let sourceDuration = try await asset.load(.duration).seconds
            guard sourceDuration.isFinite, sourceDuration > 0 else { throw RecipeError.invalidTimeline }

            duration = min(sourceDuration, 30)
            position = 0
            let recipe = MediaEngineFixtures.recipe(assetIDs: [imported.id], clipDuration: duration)
            let preview = try await AVPlayerPreviewComposer().makePreview(
                recipe: recipe,
                assetURLs: [imported.id: localURL]
            )
            self.recipe = recipe
            self.assetURLs = [imported.id: localURL]
            player = AVPlayer(playerItem: preview.playerItem)
            message = "Preview ready: animated title, vertical crop, original audio, and 1080p export."
        } catch {
            message = error.localizedDescription
        }
    }

    func togglePlayback() {
        guard let player else { return }
        if player.timeControlStatus == .playing { player.pause() } else { player.play() }
    }

    func seek(to seconds: TimeInterval) async {
        guard let player else { return }
        let started = ContinuousClock.now
        let target = CMTime(seconds: min(max(0, seconds), duration), preferredTimescale: 600)
        await withCheckedContinuation { continuation in
            player.seek(
                to: target,
                toleranceBefore: CMTime(seconds: 0.02, preferredTimescale: 600),
                toleranceAfter: CMTime(seconds: 0.02, preferredTimescale: 600)
            ) { _ in continuation.resume() }
        }
        let elapsed = ContinuousClock.now - started
        let value = Double(elapsed.components.seconds)
            + Double(elapsed.components.attoseconds) / 1e18
        metrics.record(MetricEvent(name: .seekLatency, value: value))
        position = seconds
        message = String(format: "Seek visible in %.0f ms.", value * 1_000)
    }

    func export() async {
        guard let recipe else { return }
        do {
            let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appending(path: "KriaDiagnostics", directoryHint: .isDirectory)
            let output = root.appending(path: "exports/diagnostic-\(UUID().uuidString).mp4")
            let checkpoints = FileExportStateStore(directory: root.appending(path: "checkpoints"))
            let exporter = AVFoundationLocalExporter(
                stateStore: checkpoints,
                instrumentation: metrics
            )
            message = "Exporting the native 1080 × 1920 diagnostic cut…"
            let checkpoint = try await exporter.export(
                recipe: recipe,
                assetURLs: assetURLs,
                outputURL: output,
                exportID: UUID().uuidString
            ) { [weak self] progress in
                Task { @MainActor in self?.exportProgress = progress }
            }
            exportURL = checkpoint.outputURL
            exportProgress = checkpoint.progress
            message = "Export complete. Review its timing and memory on a physical device."
        } catch is CancellationError {
            message = "Export cancelled; the recovery checkpoint was preserved."
        } catch {
            message = error.localizedDescription
        }
    }
}

struct MediaDiagnosticView: View {
    @StateObject private var model = MediaDiagnosticModel()
    @State private var isImporting = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Media diagnostics").font(KriaFont.display(34))
                Text("Internal-only AVFoundation preview, seek, instrumentation, and local-export gate.")
                    .foregroundStyle(KriaColor.zinc)

                Group {
                    if let player = model.player {
                        VideoPlayer(player: player)
                    } else {
                        RoundedRectangle(cornerRadius: 20)
                            .fill(KriaColor.ink)
                            .overlay(Image(systemName: "film.stack").font(.largeTitle).foregroundStyle(KriaColor.sky))
                    }
                }
                .aspectRatio(9 / 16, contentMode: .fit)
                .frame(maxHeight: 430)
                .clipShape(RoundedRectangle(cornerRadius: 20))

                if model.player != nil {
                    Slider(
                        value: Binding(
                            get: { model.position },
                            set: { model.position = $0 }
                        ),
                        in: 0...max(model.duration, 0.01),
                        onEditingChanged: { editing in
                            if !editing { Task { await model.seek(to: model.position) } }
                        }
                    )
                    .accessibilityLabel("Preview position")
                    HStack {
                        Button("Play or pause") { model.togglePlayback() }
                            .buttonStyle(KriaSecondaryButtonStyle())
                        Button("Export 1080p") { Task { await model.export() } }
                            .buttonStyle(KriaPrimaryButtonStyle())
                    }
                    if model.exportProgress > 0 {
                        ProgressView(value: model.exportProgress)
                            .accessibilityLabel("Export progress")
                    }
                    if let exportURL = model.exportURL {
                        ShareLink(item: exportURL) { Label("Share diagnostic export", systemImage: "square.and.arrow.up") }
                    }
                }

                Button("Choose test clip") { isImporting = true }
                    .buttonStyle(KriaSecondaryButtonStyle())
                Text(model.message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)

                if !model.metricEvents.isEmpty {
                    KriaSectionLabel(title: "Measurements")
                    ForEach(Array(model.metricEvents.enumerated()), id: \.offset) { _, event in
                        Text("\(event.name.rawValue): \(event.value, specifier: "%.3f") s")
                            .font(.system(.caption, design: .monospaced))
                    }
                }
            }
            .padding(20)
        }
        .navigationTitle("")
        .fileImporter(isPresented: $isImporting, allowedContentTypes: [.movie]) { result in
            if case let .success(url) = result { Task { await model.importClip(url) } }
        }
    }
}
#endif
