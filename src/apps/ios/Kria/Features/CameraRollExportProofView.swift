#if DEBUG
import SwiftUI
import KriaMediaEngine

/// Opt-in, offline export proof. Input recipes/synthetic media are copied into the QA
/// app's Documents/camera-roll-export-proof directory; nothing is fetched or published.
struct CameraRollExportProofView: View {
    @State private var status = "Preparing export proof…"
    var body: some View {
        NavigationStack {
            ScrollView { Text(status).textSelection(.enabled).padding() }
                .navigationTitle("Camera-roll export proof")
                .task {
                    do { status = try await run() }
                    catch { status = "Export proof failed: \(error.localizedDescription)"; try? save(["status": "failed", "error": error.localizedDescription]) }
                }
        }
    }
    private var root: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("camera-roll-export-proof", isDirectory: true)
    }
    @MainActor private func run() async throws -> String {
        let names = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("cases").path).sorted()
        guard !names.isEmpty, names.count <= 4, Set(names).isSubset(of: ["correct", "wrong_window", "wrong_audio", "wrong_order"]) else { throw APIError.invalidResponse }
        var results: [[String: Any]] = []
        for name in names {
            try Task.checkCancellation()
            status = "Exporting \(name)…"
            let directory = root.appendingPathComponent("cases/\(name)")
            let recipe = try RecipeJSON.decode(Data(contentsOf: directory.appendingPathComponent("recipe.json")))
            let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent("device-assets.json")))
            var assets: [String: URL] = [:]
            for (id, relative) in paths {
                let url = root.appendingPathComponent(relative).resolvingSymlinksInPath()
                guard url.path.hasPrefix(root.resolvingSymlinksInPath().path + "/inputs/") else { throw APIError.invalidResponse }
                assets[id] = url
            }
            let started = ContinuousClock.now
            let output = directory.appendingPathComponent("phone.mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
                .export(recipe: recipe, assetURLs: assets, outputURL: output)
            let elapsed = started.duration(to: .now)
            results.append(["case": name, "status": "exported", "elapsed_ms": Double(elapsed.components.seconds) * 1000 + Double(elapsed.components.attoseconds) / 1e15])
        }
        let result: [String: Any] = ["status": "exported", "cases": results, "provider_calls": 0, "uploads": 0,
                                   "app_version": Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "unknown",
                                   "os_version": ProcessInfo.processInfo.operatingSystemVersionString]
        try save(result)
        return "Exports saved. Run the independent picture/audio comparison.\n\n" + String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self)
    }
    private func save(_ result: [String: Any]) throws {
        try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(to: root.appendingPathComponent("device-export.json"), options: .atomic)
    }
}
#endif
