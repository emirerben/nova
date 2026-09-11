#if DEBUG
import SwiftUI
import AVKit
import KriaMediaEngine

/// Account-free physical-device exercise of backend-compiled render recipes.
struct DeviceEffectsView: View {
    @State private var session = DeviceEffectsSession()
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    VideoPlayer(player: session.player)
                        .aspectRatio(9 / 16, contentMode: .fit)
                        .frame(maxHeight: 420)
                    Picker("Effect", selection: $session.selection) {
                        ForEach(session.cases) { item in Text(item.id).tag(item.id) }
                    }
                    .disabled(session.busy)
                    HStack {
                        Button("Preview", systemImage: "play.fill") {
                            Task { await session.previewSelected() }
                        }
                        Button("Export", systemImage: "square.and.arrow.up") {
                            Task { await session.exportSelected() }
                        }
                        .disabled(session.player.currentItem == nil)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(session.busy)
                    if let output = session.output { ShareLink("Share exported video", item: output) }
                    Button("Test all \(session.cases.count) effects") { Task { await session.testAll() } }
                        .disabled(session.busy || session.cases.isEmpty)
                    if session.busy { ProgressView() }
                    Text(session.status).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityIdentifier("device-effects-status")
                    Text("1080 × 1920 · 30 fps recipe · 6 seconds. Use the player controls to scrub. Export timings and errors are saved on this device.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding()
            }
            .navigationTitle("Device effects")
            .task { await session.load() }
            .onChange(of: session.selection) { _, _ in
                if !session.busy { Task { await session.previewSelected() } }
            }
            .onDisappear { session.player.pause() }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.willResignActiveNotification)) { _ in session.recordLifecycle("will_resign_active") }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didEnterBackgroundNotification)) { _ in session.recordLifecycle("did_enter_background") }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in session.recordLifecycle("did_become_active") }
        }
    }
}

@MainActor @Observable
private final class DeviceEffectsSession {
    struct Case: Decodable, Identifiable {
        struct Font: Decodable { let id: String; let catalogId: String }
        let id: String
        let layer: PortableTextLayer?
        let font: Font?
        let transition: KriaMediaEngine.Transition.Kind?
    }
    let player = AVPlayer()
    var cases: [Case] = []
    var selection = "fade-in"
    var busy = false
    var status = "Loading compiled effect catalog…"
    var output: URL?
    private var prepared: (KriaMediaEngine.EditRecipe, [String: URL])?
    private var results: [[String: Any]] = []
    private var activeCase: String?
    private var phase = "idle"
    private var requestedCount = 0
    private var exportProgress = 0.0
    private var lifecycle: [[String: Any]] = []
    func recordLifecycle(_ event: String) {
        lifecycle.append(["event": event, "date": ISO8601DateFormatter().string(from: Date())])
        saveReport()
    }
    private let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("DeviceEffects", isDirectory: true)

    func load() async {
        guard cases.isEmpty else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            guard let url = Bundle.main.url(forResource: "device-effects", withExtension: "json") else {
                throw MediaEngineError.missingAsset("device-effects.json")
            }
            cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: url))
            cases += [KriaMediaEngine.Transition.Kind.crossfade, .fadeBlack, .fadeWhite, .wipeLeft, .wipeRight].map {
                Case(id: "transition-\($0.rawValue)", layer: nil, font: nil, transition: $0)
            }
            if ProcessInfo.processInfo.arguments.contains("-device-effects-auto") { await testAll() }
            else { await previewSelected() }
        } catch { status = "Catalog failed: \(error)" }
    }

    private func recipe(for item: Case) throws -> (KriaMediaEngine.EditRecipe, [String: URL]) {
        guard let video = Bundle.main.url(forResource: "montage", withExtension: "mp4") else {
            throw MediaEngineError.missingAsset("montage.mp4")
        }
        var urls = ["footage": video]
        if let font = item.font {
            guard let url = Bundle.main.url(forResource: font.catalogId, withExtension: nil, subdirectory: "fonts") else {
                throw MediaEngineError.missingAsset(font.catalogId)
            }
            urls[font.id] = url
        }
        let assets = try urls.map {
            MediaAsset(id: $0.key, relativePath: $0.key,
                       fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map { asset -> RenderAssetReference in
            guard let fingerprint = asset.fingerprint else { throw MediaEngineError.missingAsset(asset.id) }
            return RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(fingerprint),
                source: asset.id == "footage" ? .original(mediaID: "footage") :
                    .library(catalog: .font, catalogID: item.font!.catalogId, generation: fingerprint.hex))
        }
        let overlap = item.transition == nil ? 0.0 : 0.3
        let clipDuration = 3 + overlap / 2
        let recipe = KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: KriaMediaEngine.Canvas(width: 1080, height: 1920),
            assets: assets, tracks: [TimelineTrack(id: "video", kind: .video, clips: [
                TimelineClip(id: "clip-a", sourceAssetID: "footage", sourceDuration: clipDuration),
                TimelineClip(id: "clip-b", sourceAssetID: "footage", sourceDuration: clipDuration,
                             timelineStart: clipDuration - overlap,
                             transition: item.transition.map { KriaMediaEngine.Transition(kind: $0, duration: overlap) })
            ])], assetManifest: RenderAssetManifest(assets: references), textLayers: item.layer.map { [$0] } ?? [])
        try recipe.validate()
        return (recipe, urls)
    }

    func previewSelected() async {
        guard !busy, let item = cases.first(where: { $0.id == selection }) else { return }
        busy = true
        defer { busy = false }
        output = nil
        prepared = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
        do {
            let value = try recipe(for: item)
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: value.0, assetURLs: value.1)
            prepared = value
            player.replaceCurrentItem(with: preview.playerItem)
            status = "Ready: \(item.id)"
            player.play()
        } catch { status = "Preview failed for \(item.id): \(error)" }
    }

    func exportSelected() async {
        guard !busy, let value = prepared else { return }
        busy = true
        defer { busy = false }
        player.pause()
        do { try await export(value, id: selection) }
        catch {
            status = "Export failed: \(error)"
            results.append(["effect": selection, "status": "failed", "error": String(describing: error)])
        }
        saveReport()
    }

    private func export(_ value: (KriaMediaEngine.EditRecipe, [String: URL]), id: String) async throws {
        let destination = directory.appendingPathComponent("\(id)-\(UUID().uuidString).mp4")
        status = "Exporting \(id)…"
        phase = "export"; activeCase = id; exportProgress = 0; saveReport()
        let start = Date()
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: value.0, assetURLs: value.1, outputURL: destination, progress: { [weak self] value in
                Task { @MainActor in self?.exportProgress = value; self?.saveReport() }
            })
        let seconds = Date().timeIntervalSince(start)
        let asset = AVURLAsset(url: destination)
        let duration = try await asset.load(.duration).seconds
        guard abs(duration - 6) < 0.1 else { throw MediaEngineError.exportFailed }
        output = destination
        status = String(format: "%@: exported 6 seconds in %.2f seconds", id, seconds)
        results.append(["effect": id, "export_seconds": seconds, "duration": duration, "status": "exported"])
    }

    func testAll() async {
        guard !busy else { return }
        busy = true
        UIApplication.shared.isIdleTimerDisabled = true
        defer {
            busy = false
            UIApplication.shared.isIdleTimerDisabled = false
        }
        player.pause()
        player.replaceCurrentItem(with: nil)
        prepared = nil
        results = []
        let arguments = ProcessInfo.processInfo.arguments
        let requested: [Case]
        if let index = arguments.firstIndex(of: "-device-effects-only"), index + 1 < arguments.count {
            let ids = Set(arguments[index + 1].split(separator: ",").map(String.init))
            requested = cases.filter { ids.contains($0.id) }
        } else { requested = cases }
        requestedCount = requested.count
        for item in requested {
            do {
                activeCase = item.id; phase = "preview_prepare"
                status = "Preparing \(item.id)…"; saveReport()
                let prepareStart = Date()
                let value = try recipe(for: item)
                // Request real compositor frames, including a backwards jump, before encoding.
                let preview = try await AVPlayerPreviewComposer().makePreview(recipe: value.0, assetURLs: value.1)
                let prepareSeconds = Date().timeIntervalSince(prepareStart)
                let frameStart = Date()
                let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
                generator.videoComposition = preview.playerItem.videoComposition
                generator.requestedTimeToleranceBefore = .zero
                generator.requestedTimeToleranceAfter = .zero
                for time in [0.1, 1.0, 3.0, 5.3, 1.0, 5.8] {
                    phase = "preview_frame_\(time)"; saveReport()
                    _ = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600))
                }
                let frameSeconds = Date().timeIntervalSince(frameStart)
                try await export(value, id: item.id)
                results[results.count - 1]["preview_prepare_seconds"] = prepareSeconds
                results[results.count - 1]["preview_frame_requests_seconds"] = frameSeconds
            } catch {
                results.append(["effect": item.id, "status": "failed", "error": String(describing: error)])
            }
            activeCase = nil; phase = "between_cases"; saveReport()
        }
        phase = "finished"; saveReport()
        let failed = results.filter { $0["status"] as? String == "failed" }.count
        status = "Finished: \(results.count - failed)/\(requested.count) exported; \(failed) failed. Report saved in DeviceEffects."
        print("DEVICE_EFFECTS_RESULT \(status)")
    }

    private func saveReport() {
        do {
            let report: [String: Any] = ["device": UIDevice.current.model, "os": UIDevice.current.systemVersion,
                                       "date": ISO8601DateFormatter().string(from: Date()), "results": results,
                                       "phase": phase, "active_case": activeCase ?? "", "requested_count": requestedCount,
                                       "export_progress": exportProgress, "lifecycle": lifecycle,
                                       "application_state": UIApplication.shared.applicationState.rawValue,
                                       "scope": "Preview frame requests and six-second exports; not a playback FPS or 60-second performance gate."]
            try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                .write(to: directory.appendingPathComponent("report.json"), options: .atomic)
        } catch { print("DEVICE_EFFECTS_REPORT_ERROR \(error)") }
    }
}
#endif
