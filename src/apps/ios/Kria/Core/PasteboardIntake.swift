import CryptoKit
import Foundation
import UniformTypeIdentifiers

/// KRI-647: what a creator pasted onto an edit. The system `PasteButton` hands over
/// `NSItemProvider`s without the "Allow Paste" prompt; this turns them into one decision.
enum PastedContent: Equatable, Sendable {
    /// A file in the temp directory, still in its original format (a transparent PNG stays a PNG).
    case image(URL)
    case text(String)
}

enum PasteboardIntake {
    /// Strava's "Copy" puts a transparent PNG on the pasteboard; other apps paste text or a JPEG/HEIC photo.
    static let supportedTypes: [UTType] = [.png, .image, .plainText]
    /// Generous ceiling for a paste onto a video; a slide cuts further to its own 120-character text limit.
    static let maxTextLength = 500

    /// An image wins over text when both are present (Strava also puts the run summary on the pasteboard
    /// as text; the picture is what the creator copied).
    @MainActor static func load(from providers: [NSItemProvider]) async -> PastedContent? {
        for provider in providers {
            if let url = await loadImage(from: provider) { return .image(url) }
        }
        for provider in providers {
            if let text = await loadText(from: provider) { return .text(text) }
        }
        return nil
    }

    static func normalizedText(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return String(trimmed.prefix(maxTextLength))
    }

    @MainActor private static func loadImage(from provider: NSItemProvider) async -> URL? {
        // PNG first: asking for the generic image type can hand back a re-encoded copy without alpha.
        for type in [UTType.png, UTType.image] where provider.hasItemConformingToTypeIdentifier(type.identifier) {
            if let url = await copyFile(from: provider, type: type) { return url }
        }
        return nil
    }

    @MainActor private static func copyFile(from provider: NSItemProvider, type: UTType) async -> URL? {
        await withCheckedContinuation { continuation in
            provider.loadFileRepresentation(forTypeIdentifier: type.identifier) { source, _ in
                guard let source else { continuation.resume(returning: nil); return }
                // The system deletes `source` when this handler returns, so copy it out first.
                let ext = source.pathExtension.isEmpty ? (type.preferredFilenameExtension ?? "png") : source.pathExtension
                do {
                    // Named by its own bytes: the pool dedupes identical uploads and hands back the first copy's
                    // filename, so pasting the same picture twice must produce the same name to be found again.
                    let digest = SHA256.hash(data: try Data(contentsOf: source, options: .mappedIfSafe))
                    let name = "pasted-" + digest.prefix(6).map { String(format: "%02x", $0) }.joined()
                    let destination = FileManager.default.temporaryDirectory
                        .appendingPathComponent(name, isDirectory: false)
                        .appendingPathExtension(ext)
                    if FileManager.default.fileExists(atPath: destination.path) { try FileManager.default.removeItem(at: destination) }
                    try FileManager.default.copyItem(at: source, to: destination)
                    continuation.resume(returning: destination)
                } catch {
                    continuation.resume(returning: nil)
                }
            }
        }
    }

    @MainActor private static func loadText(from provider: NSItemProvider) async -> String? {
        guard provider.canLoadObject(ofClass: String.self) else { return nil }
        return await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: String.self) { value, _ in
                continuation.resume(returning: value.flatMap(normalizedText))
            }
        }
    }
}
