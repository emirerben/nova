import Foundation
import KriaMediaEngine

enum DeviceRenderIntentPersistence {
    static let store = DeviceRenderIntentStore(root: URL.applicationSupportDirectory.appending(path: "DeviceRenderIntents", directoryHint: .isDirectory))
}
