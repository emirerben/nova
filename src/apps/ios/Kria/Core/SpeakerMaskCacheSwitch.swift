import Foundation
import KriaMediaEngine

/// Launch wiring for the speaker mask cache (a Save of a Talking edit with a background re-uses Vision's masks for
/// footage it has already segmented). Off unless `kria.speakerMaskCache` is true, so a build without the switch runs the
/// exact path it always did. Turn it on for a measurement with the launch argument `-kria.speakerMaskCache YES`. It stays
/// off until a phone has measured the Save time and the cache size per minute.
enum SpeakerMaskCacheSwitch {
    static let defaultsKey = "kria.speakerMaskCache"

    static func isEnabled(_ defaults: UserDefaults = .standard) -> Bool { defaults.bool(forKey: defaultsKey) }

    /// In `Caches`, so the system may reclaim it and it is never backed up.
    static func installIfEnabled(defaults: UserDefaults = .standard, caches: URL? = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first) {
        guard isEnabled(defaults), let caches else { return }
        SpeakerMaskStore.installed = SpeakerMaskStore(root: caches.appending(path: "KriaSpeakerMasks", directoryHint: .isDirectory))
    }
}
