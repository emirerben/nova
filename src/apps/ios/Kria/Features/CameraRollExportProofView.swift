#if DEBUG
import SwiftUI
import KriaMediaEngine
import Darwin
import Network

/// Opt-in, offline export proof. Input recipes/synthetic media are copied into the QA
/// app's Documents directory; nothing is fetched or published.
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
    private var ownUploads: Bool { ProcessInfo.processInfo.arguments.contains("-camera-roll-own-upload-export") }
    private var root: URL {
        let name = ownUploads ? "camera-roll-own-upload-export" : "camera-roll-export-proof"
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent(name, isDirectory: true)
    }
    @MainActor private func run() async throws -> String {
        let names = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("cases").path).sorted()
        let allowed: Set<String> = ownUploads ? ["example_1", "example_2"] : ["correct", "wrong_window", "wrong_audio", "wrong_order"]
        guard !names.isEmpty, names.count <= allowed.count, Set(names).isSubset(of: allowed) else { throw APIError.invalidResponse }
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

#if DEBUG
/// Preflight only: compares already-authorized Photos entries with owned original receipts.
/// This is separate from the subsequent live HTTP selection/project/render journey.
struct CameraRollOriginalLinkView: View {
    @State private var status = "Matching selected full-quality originals…"
    var body: some View {
        NavigationStack { Text(status).padding().navigationTitle("Originals").task {
            let root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("camera-roll-live-qa")
            do {
                struct Input: Decodable { let subject: UUID; let sourceCollectionID: UUID; let assets: [SavedMediaAsset]
                    enum CodingKeys: String, CodingKey { case subject, assets; case sourceCollectionID = "source_collection_id" }
                }
                let data = try Data(contentsOf: root.appendingPathComponent("originals.json"))
                guard data.count <= 2 * 1024 * 1024 else { throw APIError.invalidResponse }
                let input = try JSONDecoder().decode(Input.self, from: data)
                let source = MediaLibraryOriginals(owner: "camera-roll-device-collection")
                let collection = try CameraRollCollection.load(id: input.sourceCollectionID, in: source, sharesCaptureContext: false)
                let tokenPayload = Data("{\"sub\":\"\(input.subject.uuidString.lowercased())\"}".utf8).base64EncodedString()
                guard let owner = MediaLibraryOriginals.owner(accessToken: "local.\(tokenPayload).local") else { throw APIError.invalidResponse }
                UIApplication.shared.isIdleTimerDisabled = true
                defer { UIApplication.shared.isIdleTimerDisabled = false }
                if let address = Self.wifiAddress() {
                    try JSONSerialization.data(withJSONObject: ["wifi_ipv4": address]).write(to: root.appendingPathComponent("network.json"), options: .atomic)
                }
                let result = try await CollectionOriginalRelinker.match(assets: input.assets, identifiers: collection.entries.map(\.id), owner: owner) { progress in
                    status = "Linked \(progress.matched.count) of \(input.assets.count) originals…"
                    let progressReport: [String: Any] = ["status": "matching", "matched_originals": progress.matched.count, "expected_originals": input.assets.count, "inspected_videos": progress.inspected]
                    try? JSONSerialization.data(withJSONObject: progressReport).write(to: root.appendingPathComponent("original-link-progress.json"), options: .atomic)
                }
                // Photos identifiers stay on the phone. Only counts are copied out.
                let report: [String: Any] = ["selected_count": collection.entries.count, "expected_originals": input.assets.count, "matched_originals": result.matched.count, "inspected_videos": result.inspected, "source_bytes_read_upper_bound": result.bytesRead, "uploads": 0, "provider_calls": 0, "sha256_and_byte_count_required": true]
                try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: root.appendingPathComponent("original-link-report.json"), options: .atomic)
                status = "Linked \(result.matched.count) of \(input.assets.count) full-quality originals."
            } catch {
                status = "Original matching stopped: \(error.localizedDescription)"
                try? JSONSerialization.data(withJSONObject: ["status": "failed", "error": error.localizedDescription]).write(to: root.appendingPathComponent("original-link-report.json"), options: .atomic)
            }
        } }
    }
    private static func wifiAddress() -> String? {
        var pointer: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&pointer) == 0 else { return nil }
        defer { freeifaddrs(pointer) }
        var cursor = pointer
        while let item = cursor?.pointee {
            defer { cursor = item.ifa_next }
            guard String(cString: item.ifa_name) == "en0", let address = item.ifa_addr,
                  address.pointee.sa_family == UInt8(AF_INET) else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                return String(cString: host)
            }
        }
        return nil
    }
}

/// Real HTTP QA bootstrap. The session is issued by an isolated local API, never a
/// production credential or intercepted URLSession response. No secrets enter source.
enum CameraRollLiveQA {
    struct Configuration: Decodable { let baseURL: URL; let session: MobileSession
        enum CodingKeys: String, CodingKey { case baseURL = "base_url", session }
    }
    static func configuration() -> Configuration? {
        guard ProcessInfo.processInfo.arguments.contains("-camera-roll-live-qa") else { return nil }
        let file = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("camera-roll-live-qa/session.json")
        guard let data = try? Data(contentsOf: file), data.count <= 16 * 1024,
              let result = try? JSONDecoder().decode(Configuration.self, from: data),
              let host = result.baseURL.host, result.baseURL.scheme == "http",
              result.baseURL.user == nil, result.baseURL.password == nil,
              result.baseURL.query == nil, result.baseURL.fragment == nil else { return nil }
        let octets = host.split(separator: ".", omittingEmptySubsequences: false)
        let ipv4 = octets.compactMap { Int($0) }
        let privateIPv4 = octets.count == 4 && ipv4.count == 4 && ipv4.allSatisfy { (0...255).contains($0) } &&
            (ipv4[0] == 10 || (ipv4[0] == 192 && ipv4[1] == 168) || (ipv4[0] == 172 && (16...31).contains(ipv4[1])))
        let rawHost = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        let deviceTunnel = rawHost.hasPrefix("fd") && IPv6Address(rawHost) != nil
        guard host == "127.0.0.1" || host == "localhost" || privateIPv4 || deviceTunnel else { return nil }
        return result
    }
}
#endif
